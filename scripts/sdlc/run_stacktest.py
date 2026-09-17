#!/usr/bin/env python3
"""Run a single deployment-variant stack-test MANUALLY, outside the CI pipeline.

The deploy-variant stack-tests (APIGateway hosting, WAF, PRIVATE/VPC hosting,
Jobs API, ZAP DAST) used to run automatically on every integration pipeline —
concurrently with the primary suite. Standing up 6 stacks at once bursts the
account-wide control planes (CloudWatch Logs create-consistency, CodeBuild
role-trust propagation, IAM CreatePolicy rate limit) and caused flaky failures
unrelated to the code under test. They are infra smoke tests that rarely change,
so they now run ON DEMAND via this script (wrapped by `make stacktest-*`), each
on its own stack with no concurrent burst.

Two modes (choose per invocation):
  * --stack-name EXISTING : run ONLY the test's validator against a stack you
    already deployed (fast; you own the stack lifecycle). Mirrors `make api-test`.
  * (no --stack-name)     : self-deploy a throwaway stack with the test's params,
    validate, and ALWAYS tear it down (like CI did). Slower (~30m deploy).

VPC-requiring tests (jobsapi, apigwpriv) need VPC wiring. Provide it with the
--vpc-* flags; if omitted, this falls back to the IDP_TEST_* env vars, and if
those are absent too, it exits with a clear message (the run-stack-tests skill
can discover a suitable VPC in the account and pass the flags for you).

Usage:
  python3 scripts/sdlc/run_stacktest.py --list
  python3 scripts/sdlc/run_stacktest.py zapdast --stack-name idp-mystack
  python3 scripts/sdlc/run_stacktest.py zapdast --admin-email me@example.com   # self-deploy
  python3 scripts/sdlc/run_stacktest.py jobsapi \
      --vpc-id vpc-abc --subnet-ids subnet-a,subnet-b \
      --lambda-sg-id sg-xyz --apigw-vpce-id vpce-def

Requires AWS creds for the deployment account (AWS_PROFILE=default or idp-ci).
"""

import argparse
import os
import sys

# codebuild_deployment.py lives beside this file; import the shared stack-test
# machinery (its internal probe table) so the manual runner and CI use IDENTICAL
# deploy/validate/cleanup logic (no drift).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codebuild_deployment as cbd  # noqa: E402


def _tests_by_suffix():
    return {p.stack_suffix: p for p in cbd.PROBE_VARIANTS}


def _print_list():
    print("Available deployment-variant stack-tests (by name):\n")
    for p in cbd.PROBE_VARIANTS:
        vpc = " [requires VPC]" if p.requires_vpc else ""
        print(f"  {p.stack_suffix:12s} {p.name}{vpc}")
    print(
        "\nRun one with:  make stacktest-<name> [STACK_NAME=...] "
        "[VPC_ID=... SUBNET_IDS=... LAMBDA_SG_ID=... APIGW_VPCE_ID=...]"
    )


def _resolve_vpc_params(args):
    """Return the VPC deploy-param dict, or None if VPC wiring is unavailable.

    Precedence: explicit --vpc-* flags > IDP_TEST_* env vars. Returns None only
    when neither source supplies the full set (caller decides how to handle).
    """
    vpc_id = args.vpc_id or os.environ.get("IDP_TEST_VPC_ID", "").strip()
    subnets = (
        args.subnet_ids or os.environ.get("IDP_TEST_PRIVATE_SUBNET_IDS", "").strip()
    )
    sg_id = args.lambda_sg_id or os.environ.get("IDP_TEST_LAMBDA_SG_ID", "").strip()
    vpce_id = args.apigw_vpce_id or os.environ.get("IDP_TEST_APIGW_VPCE_ID", "").strip()
    if not (vpc_id and subnets and sg_id and vpce_id):
        return None
    return {
        "DeployInVPC": "true",
        "VpcId": vpc_id,
        "PrivateSubnetIds": subnets,
        "LambdaSubnetIds": subnets,
        "LambdaSecurityGroupId": sg_id,
        "ApiGatewayVpcEndpointId": vpce_id,
    }


def main():
    # Line-buffer stdout so a detached run (`setsid nohup … > log &`) shows
    # progress and the verdict as they happen, not only at exit. Release
    # validation polled CloudFormation instead of these logs for a night
    # because of this.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("suffix", nargs="?", help="stack-test name (see --list)")
    ap.add_argument("--list", action="store_true", help="list available stack-tests")
    ap.add_argument(
        "--stack-name",
        help="run the validator against this ALREADY-DEPLOYED stack "
        "(omit to self-deploy a throwaway stack, validate, and tear down)",
    )
    ap.add_argument(
        "--template-url",
        help="template URL for self-deploy mode (default: build/publish first)",
    )
    ap.add_argument(
        "--admin-email",
        default="citest@suppress.welcome.email",
        help="admin email for self-deploy mode",
    )
    ap.add_argument(
        "--region",
        help="AWS region of the stack (default: AWS_DEFAULT_REGION, else "
        "us-east-1). Needed when your stack is not in us-east-1 — e.g. an "
        "AWS_PROFILE whose configured region differs is NOT picked up by the "
        "validators, which read AWS_DEFAULT_REGION.",
    )
    # VPC wiring (make params, not env) for VPC-requiring stack-tests.
    ap.add_argument("--vpc-id")
    ap.add_argument("--subnet-ids", help="comma-separated private subnet ids")
    ap.add_argument("--lambda-sg-id")
    ap.add_argument("--apigw-vpce-id")
    args = ap.parse_args()

    # The validators (and rbac_common.resolve_stack) resolve region from
    # AWS_DEFAULT_REGION, NOT from the active AWS profile's configured region. So
    # `AWS_PROFILE=default` alone would still look in us-east-1 even if that
    # profile is configured for another region. Make --region authoritative by
    # exporting it into the env the validators read.
    if args.region:
        os.environ["AWS_DEFAULT_REGION"] = args.region

    if args.list or not args.suffix:
        _print_list()
        return 0 if args.list else 2

    tests = _tests_by_suffix()
    test = tests.get(args.suffix)
    if test is None:
        print(f"❌ Unknown stack-test '{args.suffix}'. Use --list to see valid names.")
        return 2

    # VPC tests need wiring in BOTH modes (validate-existing still targets a
    # VPC-deployed stack, but self-deploy is where params are actually consumed).
    vpc_params = {}
    if test.requires_vpc:
        vpc_params = _resolve_vpc_params(args)
        if vpc_params is None:
            print(
                f"❌ Stack-test '{test.name}' requires a VPC. Provide "
                "--vpc-id/--subnet-ids/--lambda-sg-id/--apigw-vpce-id (or set the "
                "IDP_TEST_* env vars). The run-stack-tests skill can discover a "
                "suitable VPC in the account and pass these for you."
            )
            return 2

    if args.stack_name:
        # Validate-only against an existing stack — no deploy, no teardown.
        print(f"🔎 Running stack-test '{test.name}' against {args.stack_name}...")
        result = test.validate_fn(args.stack_name)
    else:
        # Self-deploy + validate + teardown, reusing the CI path so logic can't
        # drift. _run_probe_attempt generates its own stack name, creates/deletes
        # its own IAM, and tears the stack down in a finally.
        template_url = args.template_url
        if not template_url:
            print(
                "❌ Self-deploy mode needs --template-url (publish first with "
                "publish.py, then pass the idp-main.yaml URL). Or use "
                "--stack-name to validate an already-deployed stack."
            )
            return 2
        print(f"🚀 Self-deploying + testing '{test.name}' (own throwaway stack)...")
        result = cbd._run_probe_attempt(
            test, args.admin_email, template_url, vpc_params
        )

    ok = result.get("success")
    if result.get("skipped"):
        print(f"⏭️  SKIPPED: {result.get('detail', '')}")
        return 0
    if ok:
        print(f"✅ Stack-test '{test.name}' PASSED")
        for k in ("zap_alerts", "jobs_url", "web_acl_arn"):
            if k in result:
                print(f"   {k}: {result[k]}")
        # Point at the full report file(s) so they're easy to open (the detailed
        # scan report was already printed above by the validator).
        for name, path in (result.get("report_files") or {}).items():
            print(f"   report: {path}")
        if result.get("report_url"):
            print(f"   report (S3): {result['report_url']}")
        return 0
    print(f"❌ Stack-test '{test.name}' FAILED: {result.get('error', 'unknown')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
