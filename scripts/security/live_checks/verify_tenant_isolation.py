# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live tenant-isolation check against a deployed stack.

Run with ``make verify-tenant-isolation STACK_NAME=<stack>``. It needs a stack
deployed with ``TenantIsolationMode=Strict`` and credentials that can manage the
stack's tables, its Cognito pool, IAM roles and one CloudFormation stack.

What it does, in order, and removes in a ``finally``:

1. Creates two throwaway Configuration Profiles, ``tivA-default`` and
   ``tivB-default``, as copies of the stack's ``default`` profile.
2. Declares two tenants over two throwaway prefixes, ``tenancy-verify/a/`` and
   ``tenancy-verify/b/``, and maps each prefix to its tenant's profile. This goes
   through the same SDK calls ``idp-cli tenancy`` uses, so the declarations pass
   the same boundary checks the API applies.
3. Asserts that a crossing prefix mapping and a crossing tenant declaration are both
   refused.
4. Runs ``client.tenancy.verify``, which is ``idp-cli tenancy verify``: throwaway
   users of each tenant read their own tenant's document and are refused the other
   tenant's, and the strict-mode ingest refusals happen.
5. Creates two throwaway IAM roles, one uploader per tenant, generates the
   per-tenant access point template with ``client.tenancy.generate_policies``,
   deploys it, and asserts that tenant A's uploader can write under A's prefix
   through A's access point and is refused under B's prefix and at B's access
   point. Each role is deliberately OVER-granted — its own identity policy allows
   ``s3:PutObject`` through both access points — because for a same-account role
   S3 honours that grant as readily as the access point's, and the generated
   policies must confine it anyway.
6. Runs ``client.tenancy.audit`` and reports its findings.

Every row is PASS, FAIL or ERROR, and the run exits non-zero unless every row is
PASS.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, List, Tuple

import boto3
from botocore.exceptions import ClientError

PROFILE_A, PROFILE_B = "tivA-default", "tivB-default"
TENANT_A, TENANT_B = "tiv-a", "tiv-b"
PREFIX_A, PREFIX_B = "tenancy-verify/a/", "tenancy-verify/b/"

Row = Tuple[str, str, str]  # (outcome, check, detail)


def _row(rows: List[Row], outcome: str, check: str, detail: str = "") -> None:
    rows.append((outcome, check, detail))
    print(f"{outcome:5}  {check}" + (f" — {detail}" if detail else ""), flush=True)


def _expect(rows: List[Row], check: str, fn: Callable[[], Any]) -> Any:
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - every failure is reported, not raised
        _row(rows, "ERROR", check, f"{type(e).__name__}: {e}")
        return None
    return result


def _profiles(client, stack: str, rows: List[Row]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "default.yaml"
        client.config.download(
            stack_name=stack, output=str(path), config_profile="default"
        )
        for profile in (PROFILE_A, PROFILE_B):
            client.config.upload(
                str(path),
                stack_name=stack,
                config_profile=profile,
                description="tenancy-verify throwaway profile",
                validate=False,
            )
    _row(rows, "PASS", "throwaway profiles created", f"{PROFILE_A}, {PROFILE_B}")


def _declare(client, stack: str, region: str, rows: List[Row]) -> None:
    from idp_common.config.tenants import mapping_rejection_reason

    tenancy = client.tenancy
    tenancy.put(TENANT_A, [PREFIX_A], "tivA-*", stack_name=stack)
    tenancy.put(TENANT_B, [PREFIX_B], "tivB-*", stack_name=stack)
    mappings = _mapping_store(stack, region)
    tenants = [
        {
            "tenantId": t.tenant_id,
            "prefixes": t.prefixes,
            "profilePattern": t.profile_pattern,
        }
        for t in tenancy.list(stack_name=stack).tenants
    ]
    for prefix, profile in ((PREFIX_A, PROFILE_A), (PREFIX_B, PROFILE_B)):
        reason = mapping_rejection_reason(prefix, profile, tenants)
        if reason:
            raise RuntimeError(reason)
        mappings.put(prefix, profile, actor="tenancy-verify")
    _row(rows, "PASS", "tenants declared and mapped", f"{TENANT_A}, {TENANT_B}")

    # A mapping that crosses the boundary is refused by the same check the API runs.
    crossing = mapping_rejection_reason(PREFIX_A + "x/", PROFILE_B, tenants)
    _row(
        rows,
        "PASS" if crossing else "FAIL",
        "a prefix mapping across tenants is refused",
        crossing or "no refusal",
    )
    try:
        tenancy.put("tiv-c", [PREFIX_A + "c/"], "tivC-*", stack_name=stack)
    except Exception as e:  # noqa: BLE001
        _row(rows, "PASS", "a tenant overlapping another is refused", str(e)[:120])
    else:
        tenancy.delete("tiv-c", stack_name=stack)
        _row(rows, "FAIL", "a tenant overlapping another is refused", "it was accepted")


def _mapping_store(stack: str, region: str):
    """The stack's prefix-mapping store, found the way the SDK finds its tables."""
    from idp_common.config.prefix_mappings import PrefixMappingStore

    cfn = boto3.client("cloudformation", region_name=region)
    table = None
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        for r in page["StackResourceSummaries"]:
            if r["LogicalResourceId"] == "ConfigurationTable":
                table = r["PhysicalResourceId"]
    if not table:
        raise RuntimeError("ConfigurationTable not found in the stack")
    return PrefixMappingStore(
        boto3.resource("dynamodb", region_name=region).Table(table)
    )


def _verify(client, stack: str, document: Path, rows: List[Row]) -> None:
    result = client.tenancy.verify(
        document.read_bytes(),
        document.name,
        stack_name=stack,
        tenant_ids=[TENANT_A, TENANT_B],
        timeout_seconds=600,
    )
    for check in result.checks:
        outcome = getattr(check.outcome, "value", str(check.outcome))
        _row(rows, outcome, f"verify: {check.name}", check.detail or "")


def _access_points(
    client, stack: str, region: str, rows: List[Row], cleanup: list
) -> None:
    iam = boto3.client("iam")
    sts = boto3.client("sts", region_name=region)
    cfn = boto3.client("cloudformation", region_name=region)
    caller = sts.get_caller_identity()["Arn"]
    suffix = uuid.uuid4().hex[:8]
    roles = {}
    for tenant in (TENANT_A, TENANT_B):
        name = f"tiv-uploader-{tenant}-{suffix}"
        arn = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": caller},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            MaxSessionDuration=3600,
        )["Role"]["Arn"]
        cleanup.append(lambda n=name: _delete_role(iam, n))
        roles[tenant] = (name, arn)

    principals = {
        TENANT_A: {
            "uploaders": [roles[TENANT_A][1]],
            "consumers": [roles[TENANT_A][1]],
        },
        TENANT_B: {
            "uploaders": [roles[TENANT_B][1]],
            "consumers": [roles[TENANT_B][1]],
        },
    }
    policies = client.tenancy.generate_policies(principals, stack_name=stack)
    template = policies.template
    ap_stack = f"{stack}-tiv-ap-{suffix}"[:128]
    # The template is generated, so nothing in the tree can check what it declares;
    # CloudFormation's own validation is the check, and it declares no Parameters.
    declared = cfn.validate_template(TemplateBody=template).get("Parameters") or []
    if declared:
        raise RuntimeError(f"generated template unexpectedly declares {declared}")
    cfn.create_stack(StackName=ap_stack, TemplateBody=template)
    cleanup.append(lambda: _delete_stack(cfn, ap_stack))
    cfn.get_waiter("stack_create_complete").wait(
        StackName=ap_stack, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
    )
    outputs = cfn.describe_stacks(StackName=ap_stack)["Stacks"][0].get("Outputs", [])
    _row(rows, "PASS", "access point template deployed", ap_stack)

    resources = {
        r["LogicalResourceId"]: r["PhysicalResourceId"]
        for r in cfn.describe_stack_resources(StackName=stack)["StackResources"]
    }
    key_id = resources.get("CustomerManagedEncryptionKey")
    # The resource's physical id is the key id; an IAM policy needs the ARN.
    key_arn = (
        boto3.client("kms", region_name=region).describe_key(KeyId=key_id)[
            "KeyMetadata"
        ]["Arn"]
        if key_id
        else None
    )
    # Each output is described "Tenant <id> upload access point" by the generator.
    ap_for = {
        tenant: next(
            (
                o["OutputValue"]
                for o in outputs
                if o.get("Description") == f"Tenant {tenant} upload access point"
            ),
            None,
        )
        for tenant in (TENANT_A, TENANT_B)
    }
    if not all(ap_for.values()):
        _row(
            rows,
            "ERROR",
            "access point ARNs in the template outputs",
            json.dumps(outputs, default=str),
        )
        return

    for tenant, (name, _) in roles.items():
        iam.put_role_policy(
            RoleName=name,
            PolicyName="tiv-access-point",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:PutObject"],
                            "Resource": [f"{ap}/object/*" for ap in ap_for.values()],
                        },
                        {
                            "Effect": "Allow",
                            "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                            "Resource": [key_arn] if key_arn else ["*"],
                        },
                    ],
                }
            ),
        )
    time.sleep(15)  # IAM propagation

    creds = sts.assume_role(RoleArn=roles[TENANT_A][1], RoleSessionName="tiv")[
        "Credentials"
    ]
    s3a = boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    def put(ap: str, key: str) -> str:
        try:
            s3a.put_object(Bucket=ap, Key=key, Body=b"tenancy-verify")
            return "allowed"
        except ClientError as e:
            return e.response.get("Error", {}).get("Code", "error")

    ok = put(ap_for[TENANT_A], f"{PREFIX_A}ap-probe.txt")
    _row(
        rows,
        "PASS" if ok == "allowed" else "FAIL",
        "tenant A's uploader writes under A's prefix through A's access point",
        ok,
    )
    other_prefix = put(ap_for[TENANT_A], f"{PREFIX_B}ap-probe.txt")
    _row(
        rows,
        "PASS" if other_prefix == "AccessDenied" else "FAIL",
        "tenant A's uploader is refused under B's prefix",
        other_prefix,
    )
    other_ap = put(ap_for[TENANT_B], f"{PREFIX_B}ap-probe.txt")
    _row(
        rows,
        "PASS" if other_ap == "AccessDenied" else "FAIL",
        "tenant A's uploader is refused at B's access point",
        other_ap,
    )


def _delete_role(iam, name: str) -> None:
    for policy in iam.list_role_policies(RoleName=name).get("PolicyNames", []):
        iam.delete_role_policy(RoleName=name, PolicyName=policy)
    iam.delete_role(RoleName=name)


def _delete_stack(cfn, name: str) -> None:
    cfn.delete_stack(StackName=name)
    cfn.get_waiter("stack_delete_complete").wait(
        StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 60}
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--document", default="samples/lending_package.pdf")
    args = parser.parse_args()

    from idp_sdk import IDPClient

    client = IDPClient(stack_name=args.stack_name, region=args.region)
    rows: List[Row] = []
    cleanup: list = []
    mode = client.tenancy.isolation_mode(args.stack_name)
    if mode != "Strict":
        print(f"ERROR  TenantIsolationMode is {mode!r}; this check needs 'Strict'.")
        return 2
    try:
        _expect(
            rows,
            "create throwaway profiles",
            lambda: _profiles(client, args.stack_name, rows),
        )
        if rows and rows[-1][0] == "ERROR":
            return 1
        cleanup.append(
            lambda: [
                client.config.delete(stack_name=args.stack_name, config_profile=p)
                for p in (PROFILE_A, PROFILE_B)
            ]
        )

        def undeclare():
            mappings = _mapping_store(args.stack_name, args.region)
            for prefix in (PREFIX_A, PREFIX_B):
                mappings.delete(prefix)
            for tenant in (TENANT_A, TENANT_B):
                client.tenancy.delete(tenant, stack_name=args.stack_name)

        cleanup.append(undeclare)
        _expect(
            rows,
            "declare tenants",
            lambda: _declare(client, args.stack_name, args.region, rows),
        )
        _expect(
            rows,
            "tenancy verify",
            lambda: _verify(client, args.stack_name, Path(args.document), rows),
        )
        _expect(
            rows,
            "access points",
            lambda: _access_points(client, args.stack_name, args.region, rows, cleanup),
        )
        report = _expect(
            rows, "tenancy audit", lambda: client.tenancy.audit(args.stack_name)
        )
        if report is not None:
            for finding in report.findings:
                print(
                    f"NOTE   audit finding: {finding.kind} {finding.subject} — {finding.detail}"
                )
            _row(
                rows, "PASS", "tenancy audit ran", f"{len(report.findings)} finding(s)"
            )
    finally:
        for step in reversed(cleanup):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                _row(rows, "ERROR", "cleanup", f"{type(e).__name__}: {e}")

    bad = [r for r in rows if r[0] != "PASS"]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} PASS")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
