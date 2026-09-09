#!/usr/bin/env python3
"""Deploy or delete the throwaway OIDC provider used by the federation check.

The provider is a real OIDC identity provider — real discovery document, real
authorization-code flow, real RS256-signed id_tokens validated against a real
JWKS — so Cognito exercises its genuine federation path against it. Its
`/authorize` endpoint approves without authenticating anyone, which is what makes
the flow scriptable; that also makes it unfit for anything but verification.

Signing uses only the Python standard library inside the Lambda (PKCS#1 v1.5
padding plus a modular exponentiation), so the provider needs no dependencies and
no layer. The RSA key is generated here and passed in as template parameters.

    # stand it up, and print the stack parameters to deploy the IDP stack with
    python3 scripts/security/live_checks/oidc_provider/deploy.py up --region us-west-2

    # tear it down
    python3 scripts/security/live_checks/oidc_provider/deploy.py down --region us-west-2

See scripts/security/live_checks/README.md for the full sequence.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import sys

import boto3

STACK_NAME = "idpverify-oidc"
SECRET_NAME = "idpverify-oidc-client-secret"
CLIENT_ID = "idp-verification-client"
IDP_NAME = "VerifyIdP"
GROUP_ATTRIBUTE = "memberOf"
TEMPLATE = pathlib.Path(__file__).resolve().parent / "template.yaml"


def rsa_parameters() -> dict[str, str]:
    """Generate an RSA-2048 keypair as decimal strings for the template."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.private_numbers()
    return {
        "RsaN": str(numbers.public_numbers.n),
        "RsaE": str(numbers.public_numbers.e),
        "RsaD": str(numbers.d),
    }


def up(region: str) -> int:
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    sm = session.client("secretsmanager")

    client_secret = secrets.token_urlsafe(32)
    params = rsa_parameters()
    params["ClientSecret"] = client_secret
    params["ClientId"] = CLIENT_ID

    print(f"Deploying {STACK_NAME}...")
    body = TEMPLATE.read_text()
    parameters = [
        {"ParameterKey": k, "ParameterValue": v} for k, v in params.items()
    ]
    try:
        cfn.create_stack(
            StackName=STACK_NAME,
            TemplateBody=body,
            Parameters=parameters,
            Capabilities=["CAPABILITY_IAM"],
        )
        cfn.get_waiter("stack_create_complete").wait(StackName=STACK_NAME)
    except cfn.exceptions.AlreadyExistsException:
        cfn.update_stack(
            StackName=STACK_NAME,
            TemplateBody=body,
            Parameters=parameters,
            Capabilities=["CAPABILITY_IAM"],
        )
        cfn.get_waiter("stack_update_complete").wait(StackName=STACK_NAME)

    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0]["Outputs"]
    }

    # The IDP stack takes the client secret by Secrets Manager ARN, not by value.
    try:
        secret_arn = sm.create_secret(
            Name=SECRET_NAME, SecretString=client_secret
        )["ARN"]
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=SECRET_NAME, SecretString=client_secret)
        secret_arn = sm.describe_secret(SecretId=SECRET_NAME)["ARN"]

    print("\nProvider is up. Deploy (or update) the IDP stack with:\n")
    for key, value in (
        ("ExternalIdPType", "OIDC"),
        ("ExternalIdPName", IDP_NAME),
        ("ExternalIdPOIDCClientId", CLIENT_ID),
        ("ExternalIdPOIDCClientSecretArn", secret_arn),
        ("ExternalIdPOIDCIssuer", outputs["Issuer"]),
        ("ExternalIdPGroupAttributeName", GROUP_ATTRIBUTE),
        ("ExternalIdPAdminGroupName", "IdP-Admins"),
        ("ExternalIdPAuthorGroupName", "IdP-Authors"),
        ("ExternalIdPReviewerGroupName", "IdP-Reviewers"),
        ("ExternalIdPViewerGroupName", "IdP-Viewers"),
    ):
        print(f"    ParameterKey={key},ParameterValue={value}")
    print(f"\nThen run verify_federated_signin.py with:")
    print(f"    --idp-function {outputs['FunctionName']}")
    print(f"    --idp-name {IDP_NAME}")
    print(json.dumps({"issuer": outputs["Issuer"], "secret_arn": secret_arn}, indent=2))
    return 0


def down(region: str) -> int:
    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    sm = session.client("secretsmanager")
    try:
        cfn.delete_stack(StackName=STACK_NAME)
        cfn.get_waiter("stack_delete_complete").wait(StackName=STACK_NAME)
        print(f"deleted {STACK_NAME}")
    except Exception as e:  # noqa: BLE001
        print(f"WARN could not delete {STACK_NAME}: {e}")
    try:
        sm.delete_secret(SecretId=SECRET_NAME, ForceDeleteWithoutRecovery=True)
        print(f"deleted secret {SECRET_NAME}")
    except Exception as e:  # noqa: BLE001
        print(f"WARN could not delete {SECRET_NAME}: {e}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["up", "down"])
    ap.add_argument("--region", required=True)
    args = ap.parse_args()
    return up(args.region) if args.action == "up" else down(args.region)


if __name__ == "__main__":
    sys.exit(main())
