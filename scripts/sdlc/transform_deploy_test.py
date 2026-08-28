#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deploy a REAL ``--headless`` / ``--govcloud`` stack, process a sample document,
assert the transform's structural promises, and tear the stack down.

WHY THIS EXISTS
---------------
The two CLI template *transforms* have never been deployed by any automated test.
Every deployment-variant probe in ``codebuild_deployment.py`` deploys the
**standard** template with different CloudFormation parameters — the ``jobsapi``
probe was once *named* "headless" but tests the additive ``EnableJobsApi``
parameter, not the transform (see its comment). So the transforms were covered
only by offline unit tests plus (since #684) a ``ValidateTemplate`` call.

That tier cannot prove a transformed template *deploys*. Three defects reported
from a live GovCloud deployment landed in exactly that gap — the templates were
valid, the transforms succeeded, and the failure appeared only once
CloudFormation began creating resources (issues #676, #677, and the
``SuppressAdminInvite`` dangling parameter that broke every headless deploy for
six weeks). This script closes it by doing the thing that actually proves it.

WHAT IT RUNS
------------
The **documented user path**, not a reconstruction:

    idp-cli deploy --headless  --from-code . --wait
    idp-cli deploy --govcloud  --from-code . --wait

That publishes from source, applies the transform, and deploys the transformed
template — so a break anywhere in publish → transform → deploy surfaces here.
It also means the CLI handles the parameter differences itself (notably: the
headless template has no ``AdminEmail`` parameter, and passing one is a
CloudFormation ``ValidationError`` — which is why this does not try to deploy a
pre-transformed URL with the generic probe machinery).

Then, per variant, it asserts the transform's structural promises and processes
a real sample document end to end.

⚠️  SCOPE LIMIT — READ THIS BEFORE TRUSTING A GREEN ``govcloud`` RUN
--------------------------------------------------------------------
Run in a **commercial** account, the ``govcloud`` variant proves the
CloudFront-free / API-Gateway-hosted template is deployable and processes
documents. It does **not** prove GovCloud behaviour: partition-correct ARNs,
GovCloud model availability, and the BDA project rejection are all invisible
outside ``us-gov-*``. Two of the three reported defects above would NOT have been
caught by a commercial run. Point ``--region us-gov-west-1`` at a GovCloud
account for that, and this script is built to run there unchanged.

NOT WIRED INTO CI (yet)
-----------------------
Deliberately on-demand, like ``run_stacktest.py``. Each run is a full publish +
deploy (~1h+). To wire it in later, call :func:`run_variant` from
``codebuild_deployment.main`` or add a row to ``PROBE_VARIANTS`` with a
transform-aware template step — the validators and the result-dict shape here
already match what that framework expects.

USAGE
-----
    python3 scripts/sdlc/transform_deploy_test.py --list
    python3 scripts/sdlc/transform_deploy_test.py headless --admin-email me@example.com
    python3 scripts/sdlc/transform_deploy_test.py govcloud --admin-email me@example.com
    python3 scripts/sdlc/transform_deploy_test.py both --json-out /tmp/result.json

    # Validate an already-deployed stack without deploying (fast, you own the stack)
    python3 scripts/sdlc/transform_deploy_test.py headless --stack-name idp-mystack

Requires AWS credentials for the target account (``AWS_PROFILE=default``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import namedtuple

# codebuild_deployment.py lives beside this file. Import the SHARED deploy /
# IAM / cleanup / inference machinery so this runner and CI cannot drift — the
# same reason run_stacktest.py does it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import codebuild_deployment as cbd  # noqa: E402

# Sample document processed on every variant. lending_package.pdf is what the
# primary suite's Step 3 uses, so a failure here is comparable to that baseline
# rather than a new unknown.
DEFAULT_SAMPLE = "lending_package.pdf"
DEFAULT_SAMPLE_DIR = "samples"
# A string the OCR of page 1 must contain — same assertion the primary suite makes.
SAMPLE_VERIFY_STRING = "ANYTOWN, USA 12345"

Variant = namedtuple(
    "Variant",
    ["key", "name", "cli_flag", "validate_fn", "needs_admin_email", "caveat"],
)


# ---------------------------------------------------------------------------
# Structural helpers (CloudFormation introspection)
# ---------------------------------------------------------------------------


def _stack_resource_types(stack_name):
    """Return {logical_id: resource_type} for every resource in the stack.

    Includes nested-stack resources' logical ids at the top level only; the
    assertions below only need main-stack resources.
    """
    out = cbd.run_command(
        f"aws cloudformation list-stack-resources --stack-name {stack_name} "
        "--query 'StackResourceSummaries[].[LogicalResourceId,ResourceType]' "
        "--output json"
    )
    try:
        pairs = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {lid: rtype for lid, rtype in pairs}


def _stack_outputs(stack_name):
    """Return {OutputKey: OutputValue} for the stack."""
    out = cbd.run_command(
        f"aws cloudformation describe-stacks --stack-name {stack_name} "
        "--query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output json"
    )
    try:
        pairs = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in (pairs or [])}


def _stack_parameters(stack_name):
    out = cbd.run_command(
        f"aws cloudformation describe-stacks --stack-name {stack_name} "
        "--query 'Stacks[0].Parameters[].[ParameterKey,ParameterValue]' --output json"
    )
    try:
        pairs = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {k: v for k, v in (pairs or [])}


# The processing core must survive BOTH transforms — if a transform strips one of
# these, documents cannot be processed at all and the deploy "succeeding" is
# meaningless.
CORE_RESOURCES = ("InputBucket", "OutputBucket", "TrackingTable", "PATTERNSTACK")


def _assert_core_present(resources, failures):
    for logical_id in CORE_RESOURCES:
        if logical_id not in resources:
            failures.append(f"processing core missing: {logical_id}")


def _run_sample_document_test(stack_name, sample_file, sample_dir):
    """Process one real document through the deployed stack.

    Reuses the primary suite's run_inference_test so the assertions (OCR text,
    populated extraction fields, a real classification) are identical to what CI
    already asserts on the standard template — a transform that deploys but
    produces a stack that cannot process a document is still broken.
    """
    batch_id = f"transform-{stack_name[-12:]}"

    def verify_extraction(json_data):
        inference_result = json_data.get("inference_result", {})
        if not inference_result:
            return False, "No inference_result found"
        populated = sum(
            1 for v in inference_result.values() if v not in [None, [], {}]
        )
        if populated == 0:
            return False, "No fields contain extracted data (all null/empty)"
        return True, f"{populated}/{len(inference_result)} fields populated"

    def verify_classification(json_data):
        doc_class = json_data.get("document_class", {}).get("type")
        if not doc_class or doc_class == "none":
            return False, f"No usable document_class.type (got {doc_class!r})"
        return True, f"Classified as {doc_class!r}"

    return cbd.run_inference_test(
        stack_name,
        sample_file,
        batch_id,
        SAMPLE_VERIFY_STRING,
        "pages/1/result.json",
        "text",
        None,
        sample_dir,
        [
            ("Extraction verification", "sections/1/result.json", verify_extraction),
            (
                "Classification verification",
                "sections/1/result.json",
                verify_classification,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Per-variant validators. Signature matches the probe framework's validate_fn
# (stack_name -> result dict) so these can be reused verbatim when wired in.
# ---------------------------------------------------------------------------


def validate_headless_deploy(stack_name, skip_doc_test=False, sample=DEFAULT_SAMPLE,
                             sample_dir=DEFAULT_SAMPLE_DIR):
    """The UI is gone, the processing core is not, and a document still processes."""
    failures = []
    checks = []

    resources = _stack_resource_types(stack_name)
    outputs = _stack_outputs(stack_name)

    # UI / auth must be ABSENT — that is what --headless means.
    for logical_id in ("UserPool", "IdentityPool", "CloudFrontDistribution",
                       "WebUIBucket", "APIRESOLVERSTACK"):
        if logical_id in resources:
            failures.append(f"headless stack still has UI resource: {logical_id}")
    if "ApplicationWebURL" in outputs:
        failures.append("headless stack still exports ApplicationWebURL")
    checks.append("UI/Cognito/CloudFront absent")

    _assert_core_present(resources, failures)
    checks.append("processing core present")

    if not failures and not skip_doc_test:
        if not _run_sample_document_test(stack_name, sample, sample_dir):
            failures.append(f"sample document {sample} did not process successfully")
        checks.append(f"sample document processed ({sample})")

    return {
        "success": not failures,
        "checks": checks,
        "error": "; ".join(failures),
        "resource_count": len(resources),
    }


def validate_govcloud_deploy(stack_name, skip_doc_test=False, sample=DEFAULT_SAMPLE,
                             sample_dir=DEFAULT_SAMPLE_DIR):
    """CloudFront and the LWA chat-stream family are gone; the UI itself remains."""
    failures = []
    checks = []

    resources = _stack_resource_types(stack_name)
    params = _stack_parameters(stack_name)

    # Types that do not exist in GovCloud must not be present at all.
    for logical_id, rtype in resources.items():
        if rtype.startswith("AWS::CloudFront::"):
            failures.append(f"CloudFront resource survived: {logical_id} ({rtype})")
        if rtype == "AWS::Lambda::Url":
            failures.append(f"Lambda Function URL survived: {logical_id}")
    checks.append("no AWS::CloudFront::* / AWS::Lambda::Url")

    # Issue #677: the Function URL's LWA-dependent handler must go with it.
    for logical_id in resources:
        if logical_id.startswith("ChatStreamProcessor"):
            failures.append(f"LWA chat-stream resource survived: {logical_id}")
    checks.append("LWA chat-stream family removed (#677)")

    # The UI is RETAINED in --govcloud (unlike --headless) and served by API GW.
    if "UserPool" not in resources:
        failures.append("govcloud stack lost Cognito UserPool (UI should be retained)")
    if params.get("WebUIHosting") not in (None, "APIGateway"):
        failures.append(f"WebUIHosting={params.get('WebUIHosting')!r}, expected APIGateway")
    checks.append("UI retained, hosted on API Gateway")

    _assert_core_present(resources, failures)
    checks.append("processing core present")

    if not failures and not skip_doc_test:
        if not _run_sample_document_test(stack_name, sample, sample_dir):
            failures.append(f"sample document {sample} did not process successfully")
        checks.append(f"sample document processed ({sample})")

    return {
        "success": not failures,
        "checks": checks,
        "error": "; ".join(failures),
        "resource_count": len(resources),
    }


VARIANTS = (
    Variant(
        key="headless",
        name="--headless transform deploy",
        cli_flag="--headless",
        validate_fn=validate_headless_deploy,
        # The headless template has NO AdminEmail parameter (Cognito is stripped);
        # passing one is a CloudFormation ValidationError.
        needs_admin_email=False,
        caveat=None,
    ),
    Variant(
        key="govcloud",
        name="--govcloud transform deploy",
        cli_flag="--govcloud",
        validate_fn=validate_govcloud_deploy,
        # --govcloud RETAINS the UI, so Cognito needs an admin email.
        needs_admin_email=True,
        caveat=(
            "In a COMMERCIAL account this proves the CloudFront-free template "
            "deploys and processes documents. It does NOT prove GovCloud "
            "behaviour (partition ARNs, model availability, the BDA project "
            "rejection) — run with --region us-gov-west-1 against a GovCloud "
            "account for that."
        ),
    ),
)


def _variants_by_key():
    return {v.key: v for v in VARIANTS}


# ---------------------------------------------------------------------------
# Deploy / validate / teardown
# ---------------------------------------------------------------------------


def _is_govcloud_region(region):
    return bool(region) and region.startswith("us-gov-")


def _extra_deploy_params(variant, region, with_knowledge_base=False):
    """CFN parameter overrides needed to make the variant testable and affordable.

    Two overrides, both only meaningful for --govcloud (the --headless transform
    REMOVES both parameters, so passing either would be a CloudFormation
    ValidationError — the same class of mistake as passing AdminEmail):

    1. ConfigurationPreset. The transform defaults it to
       lending-package-sample-govcloud, which pins GovCloud-invokable model IDs.
       In a COMMERCIAL account some of those do not exist, so the sample-document
       test would fail on model availability rather than on anything the transform
       did. Overridden back to the commercial preset outside us-gov-*; left alone
       when we ARE in GovCloud.

    2. DocumentKnowledgeBase. It defaults to "BEDROCK_KNOWLEDGE_BASE (Create)"
       and the transform forces KnowledgeBaseVectorStore=OPENSEARCH_SERVERLESS, so
       every run would stand up an OpenSearch Serverless collection — the slowest
       and most expensive resource in the stack (minimum-OCU billing, slow to
       create AND to delete) and nothing to do with what a template transform
       does. Disabled by default; --with-knowledge-base opts back in for a
       full-fidelity run.
    """
    if variant.key != "govcloud":
        return {}
    extra = {}
    if not _is_govcloud_region(region):
        extra["ConfigurationPreset"] = "lending-package-sample"
    if not with_knowledge_base:
        extra["DocumentKnowledgeBase"] = "DISABLED"
    return extra


def run_variant(variant, *, admin_email, region, sample=DEFAULT_SAMPLE,
                sample_dir=DEFAULT_SAMPLE_DIR, skip_doc_test=False, keep=False,
                existing_stack=None, with_knowledge_base=False):
    """Deploy (or reuse) a stack for one variant, validate it, always tear down.

    Returns a result dict shaped like the probe framework's:
    {"variant", "stack_name", "success", "checks", ["error", "failure_type"]}.
    """
    result = {"variant": variant.key, "name": variant.name, "success": False}

    # Validate-only mode: caller owns the stack lifecycle (mirrors run_stacktest).
    if existing_stack:
        result["stack_name"] = existing_stack
        print(f"🔎 [{variant.name}] validating EXISTING stack {existing_stack} "
              "(no deploy, no teardown)")
        try:
            result.update(
                variant.validate_fn(
                    existing_stack, skip_doc_test=skip_doc_test,
                    sample=sample, sample_dir=sample_dir,
                )
            )
        except Exception as e:  # noqa: BLE001
            result["error"] = f"validation raised: {e}"
            result["failure_type"] = "test"
        return result

    stack_name = f"{cbd.generate_stack_name()}-{variant.key}"
    result["stack_name"] = stack_name
    role_arn = None
    try:
        role_arn, boundary_arn = cbd.create_iam_resources(
            stack_name, create_boundary=False
        )
        if not role_arn:
            raise RuntimeError(f"Failed to create IAM resources for {stack_name}")

        extra = _extra_deploy_params(variant, region, with_knowledge_base)
        param_pairs = [f"PermissionsBoundaryArn={boundary_arn}"]
        param_pairs += [f"{k}={v}" for k, v in extra.items()]
        params = ",".join(param_pairs)

        # The documented user path. --from-code publishes from source and applies
        # the transform, so publish → transform → deploy is all under test. The
        # CLI itself handles the per-variant parameter differences (notably
        # dropping AdminEmail for --headless, which the template does not define).
        cmd = (
            f"idp-cli deploy --stack-name {stack_name} {variant.cli_flag} "
            f"--from-code . --region {region} --wait --role-arn {role_arn} "
            f'--parameters "{params}"'
        )
        if variant.needs_admin_email:
            cmd += f" --admin-email {admin_email}"

        print(f"🚀 [{variant.name}] deploying {stack_name} in {region}...")
        if extra:
            print(f"   parameter overrides: {extra}")
        cbd.run_command(cmd, timeout=4 * 3600)

        status = cbd.run_command(
            f"aws cloudformation describe-stacks --stack-name {stack_name} "
            "--query 'Stacks[0].StackStatus' --output text"
        )
        # Match the status EXACTLY. A substring test for "COMPLETE" is a trap:
        # ROLLBACK_COMPLETE and UPDATE_ROLLBACK_COMPLETE both contain it, so a
        # rolled-back stack would be treated as deployed and sent on to
        # validation — reported as a confusing "test" failure instead of the
        # deploy failure it is. (The probe framework's own check in
        # codebuild_deployment._run_probe_attempt has the same shape; noted there
        # rather than changed here, since that path is live in CI.)
        status_text = (status.stdout or "").strip()
        if status_text not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            result["error"] = f"Deploy status: {status_text or '<unknown>'}"
            result["failure_type"] = "deploy"
            cbd._capture_cf_events(result, stack_name)
            return result

        print(f"✅ [{variant.name}] stack reached {status_text}")
        # Validation runs in its OWN try. Inside the outer one, a throttled
        # list-stack-resources or an exception from run_inference_test would be
        # reported as failure_type="deploy" and trigger a CF-event capture — i.e.
        # "CloudFormation refused or rolled back", which is exactly the wrong
        # reading (and the one the skill's failure guide gives). The stack
        # demonstrably reached COMPLETE by this point, so nothing here is a deploy
        # failure. Same misattribution class as the "COMPLETE" in
        # "ROLLBACK_COMPLETE" bug fixed above.
        try:
            result.update(
                variant.validate_fn(
                    stack_name, skip_doc_test=skip_doc_test,
                    sample=sample, sample_dir=sample_dir,
                )
            )
        except Exception as e:  # noqa: BLE001
            print(f"❌ [{variant.name}] validation raised: {e}")
            result["success"] = False
            result["error"] = f"validation raised: {e}"
        if not result.get("success"):
            result["failure_type"] = "test"
        return result
    except Exception as e:  # noqa: BLE001
        print(f"❌ [{variant.name}] exception: {e}")
        result["error"] = str(e)
        result["failure_type"] = "deploy"
        cbd._capture_cf_events(result, stack_name)
        return result
    finally:
        if keep:
            print(f"⏸️  [{variant.name}] --keep set; leaving {stack_name} up. "
                  f"Delete it yourself:  idp-cli delete --stack-name {stack_name}")
        else:
            cbd.cleanup_stack({"stack_name": stack_name})


def _print_report(results):
    """Deterministic PASS/FAIL table — printed whatever else happened."""
    print(f"\n{'=' * 78}")
    print("TRANSFORM DEPLOY TEST RESULTS")
    print(f"{'=' * 78}")
    for r in results:
        icon = "✅ PASS" if r.get("success") else "❌ FAIL"
        print(f"{icon}  {r['name']}   stack={r.get('stack_name', '-')}")
        for check in r.get("checks", []):
            print(f"         ✓ {check}")
        if r.get("error"):
            print(f"         ↳ {r.get('failure_type', 'error')}: {r['error']}")
        if r.get("resource_count"):
            print(f"         ({r['resource_count']} main-stack resources)")
    failed = [r for r in results if not r.get("success")]
    print(f"{'-' * 78}")
    print(f"{len(results) - len(failed)}/{len(results)} variant(s) passed")
    print(f"{'=' * 78}\n")
    return not failed


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "variant", nargs="?", choices=[v.key for v in VARIANTS] + ["both"],
        help="which transform to deploy-test",
    )
    parser.add_argument("--list", action="store_true", help="list variants and exit")
    parser.add_argument(
        "--admin-email",
        default=os.environ.get("IDP_ADMIN_EMAIL", cbd.SUPPRESS_INVITE_ADMIN_EMAIL),
        help="admin email for variants that keep Cognito (default: the CI "
             "invite-suppression sentinel)",
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        help="target region; use us-gov-west-1 for a real GovCloud run",
    )
    parser.add_argument("--sample", default=DEFAULT_SAMPLE, help="sample document")
    parser.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR)
    parser.add_argument(
        "--skip-doc-test", action="store_true",
        help="structural assertions only (much faster; does NOT prove processing works)",
    )
    parser.add_argument(
        "--with-knowledge-base", action="store_true",
        help="keep the Bedrock Knowledge Base enabled on --govcloud (adds an "
             "OpenSearch Serverless collection: the slowest and most expensive "
             "resource in the stack; disabled by default)",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="leave the stack up for inspection (you must delete it)",
    )
    parser.add_argument(
        "--stack-name",
        help="validate this EXISTING stack instead of deploying (no teardown)",
    )
    parser.add_argument("--json-out", help="write the result list to this path")
    args = parser.parse_args(argv)

    by_key = _variants_by_key()
    if args.list or not args.variant:
        print("Transform deploy-tests (each does a REAL deploy + sample document):\n")
        for v in VARIANTS:
            print(f"  {v.key:10s} {v.name}")
            if v.caveat:
                print(f"             ⚠️  {v.caveat}")
        print("\nRun:  make transform-deploy-test-<variant> "
              "[REGION=...] [ADMIN_EMAIL=...]")
        return 0 if args.list else 2

    selected = list(VARIANTS) if args.variant == "both" else [by_key[args.variant]]

    if args.stack_name and len(selected) > 1:
        print("❌ --stack-name validates ONE stack; pick a single variant.")
        return 2

    for v in selected:
        if v.caveat and not _is_govcloud_region(args.region):
            print(f"\n⚠️  [{v.key}] {v.caveat}\n")

    results = [
        run_variant(
            v,
            admin_email=args.admin_email,
            region=args.region,
            sample=args.sample,
            sample_dir=args.sample_dir,
            skip_doc_test=args.skip_doc_test,
            keep=args.keep,
            existing_stack=args.stack_name,
            with_knowledge_base=args.with_knowledge_base,
        )
        for v in selected
    ]

    ok = _print_report(results)
    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(results, fh, indent=2)
        print(f"📝 Results written to {args.json_out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
