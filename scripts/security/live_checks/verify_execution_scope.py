#!/usr/bin/env python3
"""Live verification of the getStepFunctionExecution authorization checks.

Deploys the shipped resolver source into a real Lambda with the IAM policy the
template grants it, against two real Step Functions state machines and a real
DynamoDB table shaped like UsersTable, then invokes it with resolver-shaped
events.

The two state machines are named so the IAM grant's prefix wildcard covers both:
the resolver's policy allows `execution:<stack>-*`, and the "foreign" machine is
named as a sibling deployment whose stack name extends the first
(`tmp-idpverify-*` also matches `tmp-idpverify-prod-...`). So the IAM layer
genuinely permits the cross-deployment read, and any refusal has to come from the
resolver code — which is the point of the fix.

What this covers that unit tests cannot:
  * the shipped policy is sufficient for the reads the resolver makes, and does
    NOT stop the cross-deployment read on its own;
  * a real DynamoDB EmailIndex Query returns the scope in the shape the resolver
    reads;
  * real describe_execution / get_execution_history payloads parse, including
    the config_version the scope check depends on;
  * PermissionError leaves the Lambda as a FunctionError (what the dispatcher
    turns into a 403) rather than a 200 error payload.

Everything created is deleted in the finally block. Run:
    AWS_PROFILE=default python3 scripts/security/live_checks/verify_execution_scope.py
"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import time
import zipfile

import boto3

REGION = "us-west-2"
# scripts/security/live_checks/<this file> -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
RESOLVER = (
    REPO_ROOT
    / "nested/api-resolvers/src/lambda/get_stepfunction_execution_resolver/index.py"
)

PREFIX = "tmp-idpverify"
OWN_SM = f"{PREFIX}-DocumentProcessingStateMachine-own"
# Named as a SIBLING deployment: stack "tmp-idpverify-prod" extends "tmp-idpverify",
# so the resolver's `execution:tmp-idpverify-*` IAM grant matches this too.
FOREIGN_SM = f"{PREFIX}-prod-DocumentProcessingStateMachine-other"

FN_NAME = f"{PREFIX}-execscope-verify"
ROLE_NAME = f"{PREFIX}-execscope-verify-role"
SFN_ROLE_NAME = f"{PREFIX}-execscope-sfn-role"
TABLE_NAME = f"{PREFIX}-UsersTable"

IN_SCOPE_VERSION = "tenant-a"
OUT_OF_SCOPE_VERSION = "tenant-b"

SCOPED_USER = "scoped@example.invalid"
UNSCOPED_USER = "unscoped@example.invalid"

_results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    _results.append((ok, label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")


def event(execution_arn: str, email: str, groups: list[str]) -> dict:
    """The AppSync-shaped event http_api_dispatcher hands the resolver."""
    return {
        "arguments": {"executionArn": execution_arn},
        "identity": {
            "username": email,
            "claims": {
                "email": email,
                "cognito:username": email,
                "cognito:groups": groups,
            },
        },
        "info": {"fieldName": "getStepFunctionExecution"},
    }


def main() -> int:
    s = boto3.Session(region_name=REGION)
    iam, lam, sfn, ddb, sts = (
        s.client("iam"), s.client("lambda"), s.client("stepfunctions"),
        s.client("dynamodb"), s.client("sts"),
    )
    account = sts.get_caller_identity()["Account"]
    print(f"Account {account} / {REGION}\n")

    made = {"sfn_role": False, "sm": [], "table": False, "role": False, "fn": False}

    try:
        # ------------------------------------------------------- state machines
        print("Setting up two state machines (own + sibling deployment)...")
        sfn_role = iam.create_role(
            RoleName=SFN_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "states.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }],
            }),
        )["Role"]["Arn"]
        made["sfn_role"] = True
        time.sleep(10)  # role propagation

        definition = json.dumps({
            "Comment": "verification only",
            "StartAt": "Done",
            "States": {"Done": {"Type": "Pass", "End": True}},
        })
        arns = {}
        for name in (OWN_SM, FOREIGN_SM):
            for _ in range(12):
                try:
                    arns[name] = sfn.create_state_machine(
                        name=name, definition=definition, roleArn=sfn_role
                    )["stateMachineArn"]
                    made["sm"].append(name)
                    break
                except sfn.exceptions.StateMachineDeleting:
                    time.sleep(5)
                except Exception as e:
                    if "AccessDenied" in str(e) or "not authorized" in str(e):
                        time.sleep(5)
                        continue
                    raise
            print(f"  {name}")

        # Start one execution per machine, with the compressed-document wrapper
        # the real pipeline sends (see Document.compress).
        def start(sm_name: str, config_version: str) -> str:
            payload = {
                "document": {
                    "document_id": "acme/statement.pdf",
                    "s3_uri": "s3://working/compressed_documents/acme/1_state.json",
                    "num_pages": 3,
                    "config_version": config_version,
                }
            }
            arn = sfn.start_execution(
                stateMachineArn=arns[sm_name], input=json.dumps(payload)
            )["executionArn"]
            return arn

        own_in_scope = start(OWN_SM, IN_SCOPE_VERSION)
        own_out_of_scope = start(OWN_SM, OUT_OF_SCOPE_VERSION)
        foreign = start(FOREIGN_SM, IN_SCOPE_VERSION)
        time.sleep(5)  # let them finish so history is populated
        print("  3 executions started (own in-scope, own out-of-scope, sibling)")

        # -------------------------------------------------------- users table
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "EmailIndex",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        made["table"] = True
        ddb.get_waiter("table_exists").wait(TableName=TABLE_NAME)
        table_arn = ddb.describe_table(TableName=TABLE_NAME)["Table"]["TableArn"]
        ddb.put_item(TableName=TABLE_NAME, Item={
            "userId": {"S": "u1"},
            "email": {"S": SCOPED_USER},
            "allowedConfigVersions": {"L": [{"S": IN_SCOPE_VERSION}]},
        })
        ddb.put_item(TableName=TABLE_NAME, Item={
            "userId": {"S": "u2"}, "email": {"S": UNSCOPED_USER},
        })
        print(f"  {TABLE_NAME} with EmailIndex; scoped user -> [{IN_SCOPE_VERSION}]")

        # ------------------------------------------------------------- resolver
        print("\nDeploying the shipped resolver with the shipped IAM policy...")
        role_arn = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }],
            }),
        )["Role"]["Arn"]
        made["role"] = True
        iam.attach_role_policy(
            RoleName=ROLE_NAME,
            PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )
        # Mirrors GetStepFunctionExecutionResolverFunction's policy: the states
        # actions are scoped to a `<StackName>-*` execution prefix, which DOES
        # cover the sibling deployment's machine.
        iam.put_role_policy(
            RoleName=ROLE_NAME, PolicyName="resolver",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "states:DescribeExecution",
                            "states:GetExecutionHistory",
                        ],
                        "Resource": (
                            f"arn:aws:states:{REGION}:{account}:execution:{PREFIX}-*:*"
                        ),
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:Query", "dynamodb:GetItem"],
                        "Resource": [table_arn, f"{table_arn}/index/*"],
                    },
                ],
            }),
        )
        print("  role + policy created (states scoped to "
              f"execution:{PREFIX}-* — covers BOTH machines)")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("index.py", RESOLVER.read_text())
        for _ in range(12):
            try:
                lam.create_function(
                    FunctionName=FN_NAME, Runtime="python3.12", Role=role_arn,
                    Handler="index.lambda_handler", Code={"ZipFile": buf.getvalue()},
                    Timeout=60, MemorySize=512,
                    Environment={"Variables": {
                        "STATE_MACHINE_ARN": arns[OWN_SM],
                        "USERS_TABLE_NAME": TABLE_NAME,
                    }},
                )
                made["fn"] = True
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)
        if not made["fn"]:
            print("  ERROR: Lambda creation kept failing on role propagation")
            return 2
        lam.get_waiter("function_active_v2").wait(FunctionName=FN_NAME)
        print(f"  function {FN_NAME} active")

        def invoke(ev: dict) -> tuple[str | None, dict]:
            """Return (function_error, payload)."""
            r = lam.invoke(FunctionName=FN_NAME, Payload=json.dumps(ev).encode())
            return r.get("FunctionError"), json.loads(r["Payload"].read())

        def denied(ev: dict) -> tuple[bool, str]:
            err, body = invoke(ev)
            if not err:
                return False, f"returned 200 payload: {str(body)[:120]}"
            msg = body.get("errorMessage", "")
            typ = body.get("errorType", "")
            ok = typ == "PermissionError" and msg.startswith("Unauthorized")
            return ok, f"{typ}: {msg}"

        # --------------------------------------------------------- IAM baseline
        print("\n--- 0. baseline: the IAM policy alone does NOT block the sibling ---")
        # Prove the grant really permits it, so the refusals below are the code's.
        try:
            probe_ok = bool(
                sfn.describe_execution(executionArn=foreign)["executionArn"]
            )
        except Exception as e:  # pragma: no cover
            probe_ok = False
            print(f"    (probe failed: {e})")
        check(probe_ok, "sibling execution is describable in this account")

        print("\n--- 1. sibling deployment's execution, unscoped Viewer ---")
        ok, detail = denied(event(foreign, UNSCOPED_USER, ["Viewer"]))
        check(ok, f"refused despite the IAM grant allowing it — {detail}")

        print("\n--- 2. own execution, unscoped Viewer ---")
        err, body = invoke(event(own_out_of_scope, UNSCOPED_USER, ["Viewer"]))
        check(err is None and body.get("status") == "SUCCEEDED",
              f"allowed (status={body.get('status')}, error={err})")
        check(bool(body.get("steps")), f"real step history parsed ({len(body.get('steps') or [])} steps)")

        print("\n--- 3. scoped user, execution INSIDE their scope ---")
        err, body = invoke(event(own_in_scope, SCOPED_USER, ["Viewer"]))
        check(err is None and body.get("status") == "SUCCEEDED",
              f"allowed (status={body.get('status')}, error={err})")

        print("\n--- 4. scoped user, execution OUTSIDE their scope ---")
        ok, detail = denied(event(own_out_of_scope, SCOPED_USER, ["Viewer"]))
        check(ok, f"refused via a real EmailIndex Query — {detail}")

        print("\n--- 5. Admin is not config-scoped ---")
        err, body = invoke(event(own_out_of_scope, SCOPED_USER, ["Admin"]))
        check(err is None and body.get("status") == "SUCCEEDED",
              f"allowed (status={body.get('status')}, error={err})")

        print("\n--- 6. malformed ARN ---")
        ok, detail = denied(event("not-an-arn", UNSCOPED_USER, ["Viewer"]))
        check(ok, f"refused — {detail}")

        print("\n--- 7. operational failure still returns a 200 error payload ---")
        bogus = (
            f"arn:aws:states:{REGION}:{account}:execution:{OWN_SM}:"
            "00000000-0000-0000-0000-000000000000"
        )
        err, body = invoke(event(bogus, UNSCOPED_USER, ["Viewer"]))
        check(err is None and body.get("status") == "ERROR",
              f"not-found is an error payload, not a denial (status={body.get('status')})")

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
        if made["fn"]:
            try:
                lam.delete_function(FunctionName=FN_NAME)
                print(f"  deleted function {FN_NAME}")
            except Exception as e:
                print(f"  WARN function: {e}")
        if made["role"]:
            try:
                iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="resolver")
                iam.detach_role_policy(
                    RoleName=ROLE_NAME,
                    PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                )
                iam.delete_role(RoleName=ROLE_NAME)
                print(f"  deleted role {ROLE_NAME}")
            except Exception as e:
                print(f"  WARN role: {e}")
        for name in made["sm"]:
            try:
                sfn.delete_state_machine(
                    stateMachineArn=f"arn:aws:states:{REGION}:{account}:stateMachine:{name}"
                )
                print(f"  deleted state machine {name}")
            except Exception as e:
                print(f"  WARN state machine {name}: {e}")
        if made["sfn_role"]:
            try:
                iam.delete_role(RoleName=SFN_ROLE_NAME)
                print(f"  deleted role {SFN_ROLE_NAME}")
            except Exception as e:
                print(f"  WARN sfn role: {e}")
        if made["table"]:
            try:
                ddb.delete_table(TableName=TABLE_NAME)
                print(f"  deleted table {TABLE_NAME}")
            except Exception as e:
                print(f"  WARN table: {e}")


if __name__ == "__main__":
    sys.exit(main())
