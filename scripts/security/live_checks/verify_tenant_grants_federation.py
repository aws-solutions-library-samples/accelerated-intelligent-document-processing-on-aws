# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Live check of tenant grants from the identity provider's claim (route 2).

Run with ``make verify-tenant-grants-federation`` against a stack deployed with
``TenantGrantSource=IdpClaim`` and federated to the throwaway OIDC provider in
``oidc_provider/`` (see ``.claude/skills/live-auth-checks.md`` for standing both up).

The provider is driven to issue tokens **shaped like Microsoft Entra ID's** for an
app registration with app roles: a ``roles`` array of app-role values (a functional
role such as ``DMS.Viewer`` and a tenant role such as ``tenant.tga``), plus Entra's
``oid``, ``tid`` and ``preferred_username``. The stack reads the claim named by
``ExternalIdPGroupAttributeName`` (``roles`` here). Nothing in the stack knows the
provider is "Entra"; that is the property this exercises.

Each scenario is a real hosted-UI sign-in through Cognito, followed by reading the
user's UsersTable row and, where it matters, calling the API with the federated
user's own ID token:

1. roles ``DMS.Viewer`` + ``tenant.tga`` -> Viewer, scoped to tenant A; reads A's
   document, refused B's.
2. the role changed to ``tenant.tgb`` -> the next sign-in moves the user to B.
3. a token refresh with the provider now asserting A does NOT move them back, because
   only a fresh sign-in is trusted.
4. two tenant roles -> refused: the deny marker, and A's document is refused.
5. no tenant role -> the deny marker.
6. an Entra-shaped token with no ``email`` claim -> reported as a NOTE: whether the
   sign-in completes depends on the pool's attribute requirements, and it is the
   setting an Entra administrator most often has to add (the ``email`` optional claim).

Every row is PASS, FAIL, ERROR or NOTE; the run exits non-zero unless every check
passes. Throwaway profiles, tenants, mappings, documents and the federated user are
removed in a ``finally``.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_federated_signin import (  # noqa: E402
    delete_federated_user,
    federated_signin,
    jwt_claims,
)

NO_TENANT_GRANTED = "!no-tenant-granted"
VIEWER_ROLE = "DMS.Viewer"
TENANTS = {
    "tg-a": {
        "prefix": "tenant-grants/a/",
        "pattern": "tgA-*",
        "profile": "tgA-default",
        "grant": "tenant.tga",
    },
    "tg-b": {
        "prefix": "tenant-grants/b/",
        "pattern": "tgB-*",
        "profile": "tgB-default",
        "grant": "tenant.tgb",
    },
}
FED_EMAIL = "tenant-grants-fed@example.com"
RESULTS: list[tuple[str, str, str]] = []


def record(outcome: str, label: str, detail: str = "") -> bool:
    RESULTS.append((outcome, label, detail))
    print(f"{outcome:5}  {label}" + (f" — {detail}" if detail else ""), flush=True)
    return outcome == "PASS"


def check(ok: bool, label: str, detail: str = "") -> bool:
    return record("PASS" if ok else "FAIL", label, detail)


def entra_user(roles: list[str], *, email: str | None = FED_EMAIL) -> dict:
    """A mock-user record the provider turns into an Entra-shaped id_token."""
    return {
        "sub": "entra-oid-1",
        "email": email,
        "given_name": "Fed",
        "family_name": "Viewer",
        "extraClaims": {
            "roles": roles,
            "oid": "00000000-0000-0000-0000-000000000001",
            "tid": "11111111-1111-1111-1111-111111111111",
            "preferred_username": FED_EMAIL,
        },
    }


def set_provider_user(lam, function_name: str, user: dict) -> None:
    cfg = lam.get_function_configuration(FunctionName=function_name)
    env = cfg["Environment"]["Variables"]
    env["MOCK_USER"] = json.dumps(user)
    lam.update_function_configuration(
        FunctionName=function_name, Environment={"Variables": env}
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=function_name)


def api_call(api_base: str, field: str, args: dict, token: str) -> int:
    req = urllib.request.Request(
        f"{api_base}/op/{field}",
        data=json.dumps({"arguments": args}).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 - the stack's own API URL from CloudFormation
            body = json.loads(r.read() or b"null")
            # The dispatcher answers some refusals as 200 with an errors array.
            if isinstance(body, dict) and body.get("errors"):
                return 403
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def row_for(table, email: str) -> dict | None:
    from boto3.dynamodb.conditions import Key

    items = table.query(
        IndexName="EmailIndex", KeyConditionExpression=Key("email").eq(email)
    ).get("Items", [])
    rows = [i for i in items if str(i.get("PK", "")).startswith("USER#")]
    return rows[0] if rows else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stack-name", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--idp-function", required=True)
    ap.add_argument("--idp-name", required=True)
    ap.add_argument("--pool-id", required=True)
    ap.add_argument("--client-id", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--document", default="samples/lending_package.pdf")
    args = ap.parse_args()

    from idp_common.config.prefix_mappings import PrefixMappingStore
    from idp_sdk import IDPClient

    s = boto3.Session(region_name=args.region)
    cfn, cog, lam, s3 = (
        s.client("cloudformation"),
        s.client("cognito-idp"),
        s.client("lambda"),
        s.client("s3"),
    )
    ddb = s.resource("dynamodb")
    client = IDPClient(stack_name=args.stack_name, region=args.region)

    stack = cfn.describe_stacks(StackName=args.stack_name)["Stacks"][0]
    params = {
        p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])
    }
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    if params.get("TenantGrantSource") != "IdpClaim":
        print(
            f"ERROR  TenantGrantSource is {params.get('TenantGrantSource')!r}; this check needs 'IdpClaim'."
        )
        return 2
    redirect_uri = (outputs.get("ApplicationWebURL") or "").rstrip("/")
    resources = {}
    for page in cfn.get_paginator("list_stack_resources").paginate(
        StackName=args.stack_name
    ):
        for r in page["StackResourceSummaries"]:
            resources[r["LogicalResourceId"]] = r["PhysicalResourceId"]
    users = ddb.Table(resources["UsersTable"])
    mappings = PrefixMappingStore(ddb.Table(resources["ConfigurationTable"]))
    input_bucket = resources["InputBucket"]
    api_stack = resources["APIRESOLVERSTACK"]
    api_base = next(
        o["OutputValue"]
        for o in cfn.describe_stacks(StackName=api_stack)["Stacks"][0].get(
            "Outputs", []
        )
        if o["OutputKey"] == "HttpApiEndpoint"
    ).rstrip("/")
    domain = args.domain.rstrip("/")
    docs = {}
    run = str(int(time.time()))
    cleanup: list = []

    def signin(user: dict):
        delete_federated_user(cog, args.pool_id)
        set_provider_user(lam, args.idp_function, user)
        tokens, hops = federated_signin(
            domain, args.client_id, redirect_uri, args.idp_name
        )
        if tokens is None:
            print("       " + " | ".join(hops))
        return tokens

    def scope_of() -> tuple[list, str | None]:
        row = row_for(users, FED_EMAIL) or {}
        return list(row.get("allowedConfigVersions") or []), row.get("scopeTenant")

    try:
        # ---- setup: profiles, tenants with grants, mappings, one document each
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "default.yaml"
            client.config.download(
                stack_name=args.stack_name, output=str(path), config_profile="default"
            )
            for t in TENANTS.values():
                client.config.upload(
                    str(path),
                    stack_name=args.stack_name,
                    config_profile=t["profile"],
                    validate=False,
                )
        cleanup.append(
            lambda: [
                client.config.delete(
                    stack_name=args.stack_name, config_profile=t["profile"]
                )
                for t in TENANTS.values()
            ]
        )
        for tid, t in TENANTS.items():
            client.tenancy.put(
                tid,
                [t["prefix"]],
                t["pattern"],
                granted_by=[t["grant"]],
                stack_name=args.stack_name,
            )
            mappings.put(t["prefix"], t["profile"], actor="tenant-grants-check")

        def undeclare():
            for tid, t in TENANTS.items():
                mappings.delete(t["prefix"])
                client.tenancy.delete(tid, stack_name=args.stack_name)

        cleanup.append(undeclare)
        body = Path(args.document).read_bytes()
        for tid, t in TENANTS.items():
            key = f"{t['prefix']}tg-{run}/{Path(args.document).name}"
            s3.put_object(Bucket=input_bucket, Key=key, Body=body)
            docs[tid] = key
            cleanup.append(
                lambda k=key: client.document.delete(k, stack_name=args.stack_name)
            )
        record("PASS", "setup: two tenants with grantedBy, mapped, one document each")
        time.sleep(20)  # ingest records the tracking rows

        # ---- 1. Viewer + tenant A
        tokens = signin(entra_user([VIEWER_ROLE, TENANTS["tg-a"]["grant"]]))
        if check(
            tokens is not None,
            "1. Entra-shaped sign-in (roles claim) completes through Cognito",
        ):
            groups = jwt_claims(tokens["id_token"]).get("cognito:groups") or []
            check(
                "Viewer" in groups,
                "1. the functional app role maps to the Viewer group",
                f"groups={groups}",
            )
            scope, tenant = scope_of()
            check(
                scope == [TENANTS["tg-a"]["pattern"]] and tenant == "tg-a",
                "1. the tenant app role scopes the user to tenant A",
                f"scope={scope} tenant={tenant}",
            )
            check(
                api_call(
                    api_base,
                    "getDocument",
                    {"ObjectKey": docs["tg-a"]},
                    tokens["id_token"],
                )
                == 200,
                "1. reads tenant A's document",
            )
            status_b = api_call(
                api_base, "getDocument", {"ObjectKey": docs["tg-b"]}, tokens["id_token"]
            )
            check(
                status_b == 403, "1. is refused tenant B's document", f"HTTP {status_b}"
            )

        # ---- 2. role moved to tenant B
        tokens = signin(entra_user([VIEWER_ROLE, TENANTS["tg-b"]["grant"]]))
        if check(tokens is not None, "2. sign-in after the role moved to tenant B"):
            scope, tenant = scope_of()
            check(
                scope == [TENANTS["tg-b"]["pattern"]],
                "2. the next fresh sign-in moves the user to tenant B",
                f"scope={scope}",
            )
            refresh = tokens.get("refresh_token")

            # ---- 3. refresh does not re-read the claim
            set_provider_user(
                lam,
                args.idp_function,
                entra_user([VIEWER_ROLE, TENANTS["tg-a"]["grant"]]),
            )
            try:
                cog.initiate_auth(
                    ClientId=args.client_id,
                    AuthFlow="REFRESH_TOKEN_AUTH",
                    AuthParameters={"REFRESH_TOKEN": refresh},
                )
                scope, _ = scope_of()
                check(
                    scope == [TENANTS["tg-b"]["pattern"]],
                    "3. a token refresh leaves the scope as the last fresh sign-in set it",
                    f"scope={scope}",
                )
            except Exception as e:  # noqa: BLE001
                record("ERROR", "3. token refresh", f"{type(e).__name__}: {e}")

        # ---- 4. two tenant roles
        tokens = signin(
            entra_user(
                [VIEWER_ROLE, TENANTS["tg-a"]["grant"], TENANTS["tg-b"]["grant"]]
            )
        )
        if check(tokens is not None, "4. sign-in with two tenant roles completes"):
            scope, tenant = scope_of()
            check(
                scope == [NO_TENANT_GRANTED] and not tenant,
                "4. two tenants are refused: the deny marker",
                f"scope={scope}",
            )
            status_a = api_call(
                api_base, "getDocument", {"ObjectKey": docs["tg-a"]}, tokens["id_token"]
            )
            check(
                status_a == 403,
                "4. and the user is refused tenant A's document",
                f"HTTP {status_a}",
            )

        # ---- 5. no tenant role
        tokens = signin(entra_user([VIEWER_ROLE]))
        if check(tokens is not None, "5. sign-in with no tenant role completes"):
            scope, _ = scope_of()
            check(
                scope == [NO_TENANT_GRANTED],
                "5. no tenant role: the deny marker",
                f"scope={scope}",
            )

        # ---- 6. no email claim (Entra without the optional claim)
        tokens = signin(entra_user([VIEWER_ROLE, TENANTS["tg-a"]["grant"]], email=None))
        record(
            "NOTE",
            "6. Entra-shaped token with no email claim",
            "sign-in completed"
            if tokens
            else "sign-in refused by Cognito: configure Entra's email optional claim",
        )
    finally:
        try:
            delete_federated_user(cog, args.pool_id)
        except Exception as e:  # noqa: BLE001
            record("ERROR", "cleanup: federated user", str(e))
        row = row_for(users, FED_EMAIL)
        if row:
            # The row and every SUB# pointer at it: each scenario's fresh federated
            # user has a new Cognito sub, so each left a pointer behind.
            from boto3.dynamodb.conditions import Attr

            pointers = users.scan(
                FilterExpression=Attr("PK").begins_with("SUB#")
                & Attr("userId").eq(row.get("userId")),
                ProjectionExpression="PK, SK",
            ).get(
                "Items", []
            )  # filtered-scan-ok: a throwaway stack's UsersTable, one page
            for item in [*pointers, row]:
                users.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        for step in reversed(cleanup):
            try:
                step()
            except Exception as e:  # noqa: BLE001
                record("ERROR", "cleanup", f"{type(e).__name__}: {e}")

    bad = [r for r in RESULTS if r[0] in ("FAIL", "ERROR")]
    print(
        f"\n{sum(r[0] == 'PASS' for r in RESULTS)} PASS, {len(bad)} FAIL/ERROR, {sum(r[0] == 'NOTE' for r in RESULTS)} NOTE"
    )
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
