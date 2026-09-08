#!/usr/bin/env python3
"""Live verification of the shipped ExternalIdPGroupMappingFunction.

Builds a throwaway Cognito pool with a SAML provider, a native user and a
federated user, deploys the trigger's InlineCode *extracted from template.yaml*
into a real Lambda with the same IAM policy the template grants, then invokes it
with real pre-token-generation events and asserts on real Cognito group state.

What this covers that unit tests cannot:
  * the inline handler imports and runs in a real Lambda (python3.12);
  * the AdminGetUser grant added to ExternalIdPGroupMappingCognitoPolicy is
    sufficient for the provenance read;
  * the `identities` attribute Cognito actually stores parses as the handler
    expects, and matches the configured provider name;
  * the group add/remove side effects land in Cognito.

What it does NOT cover: a real SAML assertion round-trip (the metadata here
points at a non-existent SSO endpoint), so it cannot prove Cognito's
AttributeMapping write still succeeds at federated sign-in with WriteAttributes
set. That needs a real IdP.

Everything created is deleted in the finally block. Run:
    AWS_PROFILE=default python3 scripts/security/live_checks/verify_idp_group_mapping.py
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys
import time
import zipfile

import boto3
import yaml

REGION = "us-west-2"
# scripts/security/live_checks/<this file> -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "template.yaml"

IDP_NAME = "TestOkta"
ADMIN_IDP_GROUP = "IdP-Admins"
VIEWER_IDP_GROUP = "IdP-Viewers"
COGNITO_GROUPS = ["Admin", "Author", "Reviewer", "Viewer"]

NATIVE_USER = "native-user@example.invalid"
FED_USER = "fed-user@example.invalid"

FN_NAME = "tmp-idp-group-mapping-verify"
ROLE_NAME = "tmp-idp-group-mapping-verify-role"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    _results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


class _CfnLoader(yaml.SafeLoader):
    pass


def _any_tag(loader, _tag, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_CfnLoader.add_multi_constructor("!", _any_tag)


def shipped_inline_code() -> str:
    """The handler body as template.yaml declares it — what actually deploys."""
    with TEMPLATE.open() as fh:
        loader = _CfnLoader(fh)
        try:
            doc = loader.get_single_data()
        finally:
            loader.dispose()
    fn = doc["Resources"]["ExternalIdPGroupMappingFunction"]["Properties"]
    code = fn["InlineCode"]
    print(f"  extracted InlineCode from template.yaml ({len(code.splitlines())} lines)")
    return code


def saml_metadata() -> str:
    """Self-contained SAML IdP metadata. The SSO endpoint is never contacted."""
    work = pathlib.Path("/tmp/idp-verify-saml")
    work.mkdir(exist_ok=True)
    cert = work / "cert.pem"
    if not cert.exists():
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(work / "key.pem"), "-out", str(cert),
                "-days", "30", "-nodes", "-subj", "/CN=testokta.example",
            ],
            check=True, capture_output=True,
        )
    body = "".join(
        line for line in cert.read_text().splitlines() if "CERTIFICATE" not in line
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<EntityDescriptor xmlns="urn:oasis:names:tc:SAML:2.0:metadata" entityID="https://testokta.example/saml">
  <IDPSSODescriptor WantAuthnRequestsSigned="false" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <KeyDescriptor use="signing">
      <KeyInfo xmlns="http://www.w3.org/2000/09/xmldsig#">
        <X509Data><X509Certificate>{body}</X509Certificate></X509Data>
      </KeyInfo>
    </KeyDescriptor>
    <NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</NameIDFormat>
    <SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="https://testokta.example/sso"/>
  </IDPSSODescriptor>
</EntityDescriptor>"""


def event(username: str, groups_value: str, trigger_source: str, pool_id: str) -> dict:
    return {
        "version": "1",
        "triggerSource": trigger_source,
        "region": REGION,
        "userPoolId": pool_id,
        "userName": username,
        "request": {
            "userAttributes": {
                "email": username,
                "custom:idp_groups": groups_value,
            },
            "groupConfiguration": {},
        },
        "response": {},
    }


def groups_of(cog, pool_id: str, username: str) -> set[str]:
    resp = cog.admin_list_groups_for_user(UserPoolId=pool_id, Username=username)
    return {g["GroupName"] for g in resp.get("Groups", [])}


def override_of(payload: dict) -> list[str] | None:
    details = (payload.get("response") or {}).get("claimsAndScopeOverrideDetails")
    if not details:
        return None
    return (details.get("groupOverrideDetails") or {}).get("groupsToOverride")


def main() -> int:
    session = boto3.Session(region_name=REGION)
    cog = session.client("cognito-idp")
    iam = session.client("iam")
    lam = session.client("lambda")
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    account = identity["Account"]
    # Derive the partition rather than assuming "aws" — a hardcoded arn:aws:
    # is simply invalid in aws-us-gov / aws-cn, and the resulting error reads
    # as a permissions problem rather than a partition one.
    _caller_arn_parts = (identity.get("Arn") or "").split(":")
    partition = (
        _caller_arn_parts[1]
        if len(_caller_arn_parts) > 1 and _caller_arn_parts[1]
        else "aws"
    )
    basic_exec_policy = (
        f"arn:{partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    )
    print(f"Account {account} / {REGION} / partition {partition}\n")

    pool_id = None
    role_arn = None
    created_fn = False

    try:
        # ---------------------------------------------------------------- pool
        print("Setting up throwaway Cognito pool...")
        pool_id = cog.create_user_pool(
            PoolName="tmp-idp-group-mapping-verify",
            Schema=[
                {
                    "Name": "idp_groups",
                    "AttributeDataType": "String",
                    "Mutable": True,
                    "Required": False,
                    "StringAttributeConstraints": {"MaxLength": "2048"},
                }
            ],
        )["UserPool"]["Id"]
        pool_arn = f"arn:{partition}:cognito-idp:{REGION}:{account}:userpool/{pool_id}"
        print(f"  pool {pool_id}")

        for g in COGNITO_GROUPS:
            cog.create_group(UserPoolId=pool_id, GroupName=g)

        cog.create_identity_provider(
            UserPoolId=pool_id,
            ProviderName=IDP_NAME,
            ProviderType="SAML",
            ProviderDetails={"MetadataFile": saml_metadata()},
            AttributeMapping={"custom:idp_groups": "memberOf"},
        )
        print(f"  SAML provider {IDP_NAME}")

        # A native user and a federated user, both claiming the admin IdP group.
        for user in (NATIVE_USER, FED_USER):
            cog.admin_create_user(
                UserPoolId=pool_id,
                Username=user,
                MessageAction="SUPPRESS",
                UserAttributes=[{"Name": "custom:idp_groups", "Value": ADMIN_IDP_GROUP}],
            )
        cog.admin_link_provider_for_user(
            UserPoolId=pool_id,
            DestinationUser={"ProviderName": "Cognito", "ProviderAttributeValue": FED_USER},
            SourceUser={
                "ProviderName": IDP_NAME,
                "ProviderAttributeName": "Cognito_Subject",
                "ProviderAttributeValue": "fed-user@okta.example",
            },
        )
        print(f"  native user + federated user (linked to {IDP_NAME})")

        # ------------------------------------------------------------ IAM role
        print("\nDeploying the shipped handler with the shipped IAM policy...")
        role_arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )["Role"]["Arn"]
        iam.attach_role_policy(
            RoleName=ROLE_NAME,
            PolicyArn=basic_exec_policy,
        )
        # Mirrors ExternalIdPGroupMappingCognitoPolicy in template.yaml exactly.
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName="ExternalIdPGroupMappingCognitoAccess",
            PolicyDocument=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "cognito-idp:AdminGetUser",
                                "cognito-idp:AdminListGroupsForUser",
                                "cognito-idp:AdminAddUserToGroup",
                                "cognito-idp:AdminRemoveUserFromGroup",
                            ],
                            "Resource": pool_arn,
                        }
                    ],
                }
            ),
        )
        print("  role + policy created (AdminGetUser included, scoped to the pool)")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("index.py", shipped_inline_code())
        payload = buf.getvalue()

        for attempt in range(12):
            try:
                lam.create_function(
                    FunctionName=FN_NAME,
                    Runtime="python3.12",
                    Role=role_arn,
                    Handler="index.handler",
                    Code={"ZipFile": payload},
                    Timeout=30,
                    MemorySize=128,
                    Environment={
                        "Variables": {
                            "LOG_LEVEL": "INFO",
                            "ADMIN_GROUP_NAME": ADMIN_IDP_GROUP,
                            "AUTHOR_GROUP_NAME": "IdP-Authors",
                            "REVIEWER_GROUP_NAME": "IdP-Reviewers",
                            "VIEWER_GROUP_NAME": VIEWER_IDP_GROUP,
                            "EXTERNAL_IDP_NAME": IDP_NAME,
                        }
                    },
                )
                created_fn = True
                break
            except lam.exceptions.InvalidParameterValueException:
                # IAM role propagation.
                time.sleep(5)
        if not created_fn:
            print("  ERROR: Lambda creation kept failing on role propagation")
            return 2
        waiter = lam.get_waiter("function_active_v2")
        waiter.wait(FunctionName=FN_NAME)
        print(f"  function {FN_NAME} active")

        def invoke(ev: dict) -> dict:
            r = lam.invoke(FunctionName=FN_NAME, Payload=json.dumps(ev).encode())
            body = json.loads(r["Payload"].read())
            if r.get("FunctionError"):
                raise AssertionError(f"handler raised: {body}")
            return body

        # ------------------------------------------------------------- asserts
        print("\n--- 1. native user sets custom:idp_groups=IdP-Admins on themselves ---")
        out = invoke(event(NATIVE_USER, ADMIN_IDP_GROUP, "TokenGeneration_HostedAuth", pool_id))
        native_groups = groups_of(cog, pool_id, NATIVE_USER)
        check(native_groups == set(), f"no Cognito group granted (groups={native_groups or '{}'})")
        check(override_of(out) is None, "no groupsToOverride injected into the token")

        print("\n--- 2. federated user, fresh sign-in, IdP-Admins ---")
        out = invoke(event(FED_USER, ADMIN_IDP_GROUP, "TokenGeneration_HostedAuth", pool_id))
        fed_groups = groups_of(cog, pool_id, FED_USER)
        check(fed_groups == {"Admin"}, f"Admin granted (groups={fed_groups})")
        check(override_of(out) == ["Admin"], f"token override = {override_of(out)}")

        print("\n--- 3. federated user rewrites the attribute, then REFRESHES ---")
        cog.admin_remove_user_from_group(UserPoolId=pool_id, Username=FED_USER, GroupName="Admin")
        cog.admin_add_user_to_group(UserPoolId=pool_id, Username=FED_USER, GroupName="Viewer")
        out = invoke(event(FED_USER, ADMIN_IDP_GROUP, "TokenGeneration_RefreshTokens", pool_id))
        fed_groups = groups_of(cog, pool_id, FED_USER)
        check(fed_groups == {"Viewer"}, f"still only Viewer (groups={fed_groups})")
        check(override_of(out) is None, "no token override on refresh")

        print("\n--- 4. federated user, fresh sign-in, IdP demotes to Viewers ---")
        cog.admin_remove_user_from_group(UserPoolId=pool_id, Username=FED_USER, GroupName="Viewer")
        cog.admin_add_user_to_group(UserPoolId=pool_id, Username=FED_USER, GroupName="Admin")
        out = invoke(event(FED_USER, VIEWER_IDP_GROUP, "TokenGeneration_HostedAuth", pool_id))
        fed_groups = groups_of(cog, pool_id, FED_USER)
        check(fed_groups == {"Viewer"}, f"Admin removed, Viewer granted (groups={fed_groups})")
        check(override_of(out) == ["Viewer"], f"token override = {override_of(out)}")

        print("\n--- 5. provenance comes from AdminGetUser, not the event ---")
        ev = event(NATIVE_USER, ADMIN_IDP_GROUP, "TokenGeneration_HostedAuth", pool_id)
        ev["request"]["userAttributes"]["identities"] = json.dumps(
            [{"providerName": IDP_NAME, "providerType": "SAML"}]
        )
        out = invoke(ev)
        native_groups = groups_of(cog, pool_id, NATIVE_USER)
        check(
            native_groups == set(),
            f"forged identities in the event grants nothing (groups={native_groups or '{}'})",
        )
        check(override_of(out) is None, "no token override for a forged event identity")

        print("\n--- 6. unknown provider name is not trusted ---")
        lam.update_function_configuration(
            FunctionName=FN_NAME,
            Environment={
                "Variables": {
                    "LOG_LEVEL": "INFO",
                    "ADMIN_GROUP_NAME": ADMIN_IDP_GROUP,
                    "AUTHOR_GROUP_NAME": "IdP-Authors",
                    "REVIEWER_GROUP_NAME": "IdP-Reviewers",
                    "VIEWER_GROUP_NAME": VIEWER_IDP_GROUP,
                    "EXTERNAL_IDP_NAME": "SomeOtherIdP",
                }
            },
        )
        lam.get_waiter("function_updated_v2").wait(FunctionName=FN_NAME)
        before = groups_of(cog, pool_id, FED_USER)
        out = invoke(event(FED_USER, ADMIN_IDP_GROUP, "TokenGeneration_HostedAuth", pool_id))
        after = groups_of(cog, pool_id, FED_USER)
        check(after == before, f"groups unchanged ({before} -> {after})")
        check(override_of(out) is None, "no token override for a non-matching provider")

        # ------------------------------------------------------------- summary
        failed = [label for ok, label in _results if not ok]
        print(f"\n{'=' * 68}")
        print(f"{len(_results) - len(failed)}/{len(_results)} checks passed")
        if failed:
            print("FAILED:")
            for label in failed:
                print(f"  - {label}")
        print("=" * 68)
        return 1 if failed else 0

    finally:
        print("\nTearing down...")
        if created_fn:
            try:
                lam.delete_function(FunctionName=FN_NAME)
                print(f"  deleted function {FN_NAME}")
            except Exception as e:
                print(f"  WARN could not delete function: {e}")
        if role_arn:
            try:
                iam.delete_role_policy(
                    RoleName=ROLE_NAME, PolicyName="ExternalIdPGroupMappingCognitoAccess"
                )
                iam.detach_role_policy(
                    RoleName=ROLE_NAME,
                    PolicyArn=basic_exec_policy,
                )
                iam.delete_role(RoleName=ROLE_NAME)
                print(f"  deleted role {ROLE_NAME}")
            except Exception as e:
                print(f"  WARN could not delete role: {e}")
        if pool_id:
            try:
                cog.delete_user_pool(UserPoolId=pool_id)
                print(f"  deleted pool {pool_id}")
            except Exception as e:
                print(f"  WARN could not delete pool: {e}")


if __name__ == "__main__":
    sys.exit(main())
