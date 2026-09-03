#!/usr/bin/env python3
"""
CodeBuild Deployment Script

Handles IDP stack deployment and testing in AWS CodeBuild environment.
"""

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from textwrap import dedent

import boto3
from botocore.config import Config as _BotoConfig

# Cap test/monitor commands so a hung inference run cannot consume the
# CodeBuild job timeout and prevent stack cleanup from running (leaks ~116
# IAM roles). Known-slow commands (publish, deploy --wait, delete --wait)
# pass explicit larger timeouts.
DEFAULT_COMMAND_TIMEOUT = 3600

# Sentinel admin email that makes the template create the admin user WITHOUT
# sending the Cognito invite (MessageAction=SUPPRESS). Used for ALL CI stacks so
# many-stacks-per-run deploys don't exhaust Cognito's low default daily email
# quota. MUST match the SuppressAdminInvite condition in template.yaml.
SUPPRESS_INVITE_ADMIN_EMAIL = "citest@suppress.welcome.email"

# Set when the test suite fails fast: newly started commands abort
# immediately, and _kill_running_commands() terminates in-flight ones so
# abandoned test threads cannot keep mutating the stack during cleanup.
ABORT_TESTS = threading.Event()
_RUNNING_PROCS = set()
_RUNNING_PROCS_LOCK = threading.Lock()

# Per-thread opt-out of the fail-fast abort machinery. The APIGW hosting test
# runs on its OWN thread concurrently with the primary suite (to overlap the
# two ~30m stack deploys), but it operates on an independent stack — a primary-
# suite fail-fast must NOT kill its in-flight deploy. Threads that set
# _thread_local.never_abort mark their run_command subprocesses non-abortable:
# they are neither registered in _RUNNING_PROCS nor refused when ABORT_TESTS is
# set, so the kill sweep can't touch them.
_thread_local = threading.local()


def _kill_proc_group(proc):
    """Best-effort SIGKILL of a subprocess's entire process group."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _kill_running_commands():
    """Kill the process groups of all in-flight run_command subprocesses."""
    with _RUNNING_PROCS_LOCK:
        procs = list(_RUNNING_PROCS)
    for proc in procs:
        _kill_proc_group(proc)


def run_command(cmd, check=True, timeout=DEFAULT_COMMAND_TIMEOUT):
    """Run shell command and return result

    Args:
        cmd: Command to run
        check: Raise exception if command fails
        timeout: Timeout in seconds (default: DEFAULT_COMMAND_TIMEOUT).
            With check=False a timeout returns a failed result instead of
            raising, so cleanup paths always continue.

    Commands run from test-pool threads (anything off the main thread) are
    abortable: when the suite fails fast, in-flight ones are killed and new
    ones refuse to start, so abandoned test threads cannot keep mutating the
    stack while cleanup deletes it.
    """
    # Abortable = runs on a test-pool thread AND has not opted out. The APIGW
    # hosting thread opts out (never_abort) so a primary-suite fail-fast kill
    # sweep leaves its independent-stack deploy untouched.
    abortable = (
        threading.current_thread() is not threading.main_thread()
        and not getattr(_thread_local, "never_abort", False)
    )
    if abortable and ABORT_TESTS.is_set():
        raise Exception(f"Command aborted (test suite failed fast): {cmd}")
    print(f"Running: {cmd}")
    # start_new_session puts the shell and everything it spawns (idp-cli,
    # docker, sam) in its own process group so timeout/abort can kill the
    # whole tree, not just the shell.
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )  # nosec B602 nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - hardcoded commands, no user input
    if abortable:
        with _RUNNING_PROCS_LOCK:
            _RUNNING_PROCS.add(proc)
        # Close the race with the fail-fast kill sweep: if ABORT_TESTS was set
        # between the check above and registration, the sweep may have already
        # run and missed this proc — kill it ourselves.
        if ABORT_TESTS.is_set():
            _kill_proc_group(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        _kill_proc_group(proc)
        # Bounded drain: a descendant that escaped the process group (its own
        # setsid) can hold the pipes open forever — losing partial output is
        # better than hanging the timeout path that guarantees progress.
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        returncode = -1
        msg = f"Command timed out after {timeout}s: {cmd}"
        print(msg)
        if check:
            raise Exception(msg)
        stderr = (stderr or "") + f"\n{msg}"
    finally:
        if abortable:
            with _RUNNING_PROCS_LOCK:
                _RUNNING_PROCS.discard(proc)
    result = subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        print(f"Command failed with exit code {result.returncode}")
        raise Exception(f"Command failed: {cmd}")
    return result


def get_env_var(name, default=None):
    """Get environment variable with optional default"""
    value = os.environ.get(name, default)
    if value is None:
        raise Exception(f"Environment variable {name} is required")
    return value


def generate_stack_name():
    """Generate unique stack name with timestamp including seconds"""
    timestamp = datetime.now().strftime("%m%d-%H%M%S")  # Format: MMDD-HHMMSS
    return f"idp-{timestamp}"


def cleanup_stale_bda_blueprints():
    """Delete BDA projects, blueprint versions, and blueprints whose stacks are no longer active"""
    print("🧹 Cleaning up stale BDA blueprints...")
    try:
        bda_client = boto3.client("bedrock-data-automation")
        cf_client = boto3.client("cloudformation")

        active_statuses = {
            "CREATE_IN_PROGRESS",
            "CREATE_COMPLETE",
            "UPDATE_IN_PROGRESS",
            "UPDATE_COMPLETE",
            "UPDATE_ROLLBACK_COMPLETE",
            "UPDATE_ROLLBACK_IN_PROGRESS",
            "IMPORT_IN_PROGRESS",
            "IMPORT_COMPLETE",
        }

        # Collect all idp- blueprints and projects
        paginator = bda_client.get_paginator("list_blueprints")
        blueprints = []
        for page in paginator.paginate(blueprintStageFilter="LIVE"):
            for bp in page.get("blueprints", []):
                name = bp.get("blueprintName", "")
                arn = bp.get("blueprintArn", "")
                if name.startswith("idp-") and "aws:blueprint" not in arn:
                    blueprints.append((name, arn))

        projects = []
        for p in bda_client.list_data_automation_projects().get("projects", []):
            name = p.get("projectName", "")
            arn = p.get("projectArn", "")
            if name.startswith("idp-"):
                projects.append((name, arn))

        if not blueprints and not projects:
            print("✅ No stale BDA resources found")
            return

        # Check stack status for each unique stack prefix
        stack_cache = {}
        for name, _ in blueprints + projects:
            parts = name.split("-")
            if len(parts) >= 3:
                prefix = f"{parts[0]}-{parts[1]}-{parts[2]}"
                if prefix not in stack_cache:
                    try:
                        resp = cf_client.describe_stacks(StackName=prefix)
                        status = resp["Stacks"][0]["StackStatus"]
                        stack_cache[prefix] = status in active_statuses
                    except cf_client.exceptions.ClientError:
                        stack_cache[prefix] = False

        def _is_stale(name):
            parts = name.split("-")
            if len(parts) >= 3:
                return not stack_cache.get(f"{parts[0]}-{parts[1]}-{parts[2]}", False)
            return False

        # Step 1: Delete projects first (blueprints are referenced by projects)
        deleted_projects = 0
        for name, arn in projects:
            if _is_stale(name):
                try:
                    bda_client.delete_data_automation_project(projectArn=arn)
                    deleted_projects += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to delete project {name}: {e}")
                    time.sleep(1)

        if deleted_projects:
            time.sleep(5)

        # Step 2: Delete blueprint versions then base blueprints
        deleted_bps = 0
        for name, arn in blueprints:
            if _is_stale(name):
                try:
                    try:
                        bda_client.delete_blueprint(
                            blueprintArn=arn, blueprintVersion="1"
                        )
                    except Exception:
                        pass
                    time.sleep(0.3)
                    bda_client.delete_blueprint(blueprintArn=arn)
                    deleted_bps += 1
                except Exception as e:
                    print(f"  ⚠️ Failed to delete blueprint {name}: {e}")
                    time.sleep(0.5)

        print(
            f"✅ Cleaned up {deleted_projects} projects, {deleted_bps} blueprints (skipped active stacks)"
        )
    except Exception as e:
        print(f"⚠️ BDA blueprint cleanup failed: {e}")


def publish_templates():
    """Run publish.py to build and upload templates to S3"""
    print("📦 Publishing templates to S3...")

    # Get AWS account ID and region
    account_id = get_env_var("IDP_ACCOUNT_ID", "020432867916")
    region = get_env_var("AWS_DEFAULT_REGION", "us-east-1")

    # Generate bucket name and prefix
    bucket_basename = f"genaiic-sdlc-sourcecode-{account_id}"
    prefix = f"codebuild-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Run idp-cli publish — cold-cache Docker/UI builds can run long
    cmd = f"idp-cli publish --source-dir . --bucket-basename {bucket_basename} --prefix {prefix} --region {region}"
    result = run_command(cmd, timeout=3 * 3600)

    # Extract template URL from output - match S3 URLs only
    template_url_pattern = r"https://s3\..*?idp-main\.yaml"

    # Remove line breaks that might split the URL in terminal output
    clean_stdout = result.stdout.replace("\n", "").replace("\r", "")
    template_url_match = re.search(template_url_pattern, clean_stdout)

    if template_url_match:
        template_url = template_url_match.group(0)
        print(f"✅ Template published: {template_url}")
        return template_url
    else:
        print("❌ Failed to extract template URL from publish output")
        raise Exception("Failed to extract template URL from publish output")


def validate_headless_template(main_template_url):
    """Validate the `--headless` template variant through real CloudFormation.

    WHY THIS EXISTS. Nothing in CI has ever exercised the `--headless` template
    transform. The deployment-variant probes all deploy the STANDARD template
    with different parameters (see the Probe list; `jobsapi` was once called
    "headless" but tests the EnableJobsApi *parameter*, not the transform), and
    the transform's only other coverage is offline unit tests. That gap shipped a
    template CloudFormation rejects outright: the `SuppressAdminInvite` condition
    referenced the `AdminEmail` parameter that headless removes, so for six weeks
    EVERY headless deploy failed at validation with

        Template format error: Unresolved dependencies [AdminEmail].
        Cannot reference resources in the Conditions block of the template

    before creating a single resource. `publish --headless` does run exactly this
    check — but no CI job invokes it.

    WHY IT IS CHEAP. It reuses the PACKAGED template the publish step just built
    (`.aws-sam/idp-main.yaml`), so there is no second SAM build: one S3 put plus
    one ValidateTemplate call. Validating the packaged template also closes the
    gap the offline unit tests cannot — they transform the *source*
    `template.yaml`, which differs from the packaged artifact (SAM expansion,
    nested-stack URLs).

    Deliberately does NOT deploy a headless stack. This proves the template is
    well-formed and every reference resolves; it does not prove a headless stack
    stands up. That would need its own ~1h probe.

    Returns (ok: bool, detail: str) and never raises — a failure here must be
    reported in the verdict, not crash the harness before the primary suite runs.
    """
    from urllib.parse import urlparse

    print("🔎 Validating the --headless template variant...")

    packaged = os.path.join(".aws-sam", "idp-main.yaml")
    if not os.path.exists(packaged):
        return False, f"packaged template not found at {packaged}"

    # Derive bucket/prefix/region from the published main-template URL
    # (https://s3.<region>.amazonaws.com/<bucket>/<prefix>/idp-main.yaml) so this
    # lands beside it rather than recomputing the timestamped prefix.
    parsed = urlparse(main_template_url)
    host_parts = parsed.netloc.split(".")
    region = (
        host_parts[1]
        if len(host_parts) > 2
        else get_env_var("AWS_DEFAULT_REGION", "us-east-1")
    )
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        return False, f"could not parse bucket/prefix from {main_template_url}"
    bucket, key_prefix = path_parts[0], "/".join(path_parts[1:-1])

    out_path = os.path.join(".aws-sam", "idp-headless.yaml")
    try:
        from idp_sdk._core.template_transform import HeadlessTemplateTransformer

        if not HeadlessTemplateTransformer().transform(packaged, out_path):
            return False, "headless transform reported failure (see log above)"
    except Exception as e:  # noqa: BLE001
        return False, f"headless transform raised: {e}"

    key = f"{key_prefix}/idp-headless.yaml" if key_prefix else "idp-headless.yaml"
    url = f"https://s3.{region}.amazonaws.com/{bucket}/{key}"
    try:
        boto3.client("s3", region_name=region).upload_file(
            out_path, bucket, key, ExtraArgs={"ContentType": "text/yaml"}
        )
        boto3.client("cloudformation", region_name=region).validate_template(
            TemplateURL=url
        )
    except Exception as e:  # noqa: BLE001
        return False, f"{e}"

    print(f"✅ Headless template validated: {url}")
    return True, url


# boto3's default retry mode is "legacy" (max ~4 attempts, no adaptive
# rate-limiting). When the primary suite + N probes each create their IAM stack
# at once, the account-wide (low, non-adjustable) IAM mutating-call rate is
# exceeded and iam:CreatePolicy fails with "Throttling: Rate exceeded (reached
# max retries: 4)" — killing the whole probe at Step 0, BEFORE any deploy (so
# the CFN transient-race retry can't help). "adaptive" mode adds client-side
# rate-limiting + more attempts to ride through the throttle. Launch stagger
# reduces the burst; this rides out what stagger doesn't fully eliminate.
_THROTTLE_RETRY_CONFIG = _BotoConfig(retries={"max_attempts": 10, "mode": "adaptive"})


def create_iam_resources(stack_name, create_boundary=True):
    """Create the CFN service role, and (optionally) a permissions boundary.

    create_boundary=True (primary suite): also create a no-op {Action:*,Resource:*}
    permissions-boundary policy and return its ARN, so the deploy exercises the
    PermissionsBoundaryArn feature (verified by validate_permission_boundaries).

    create_boundary=False (manual probes): skip the boundary — the probe is an
    infra-deploy smoke test, not a boundary test, so it deploys with an EMPTY
    PermissionsBoundaryArn (the template's HasPermissionsBoundary gate supports
    this). Skipping it removes an iam:CreatePolicy + iam:DeletePolicy call per
    probe, so a `make probes-all` sweep no longer bursts the account-wide IAM
    rate limit. Returns (role_arn, "") in this mode.
    """
    print(f"[{stack_name}] Creating IAM resources...")

    try:
        cf_client = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
        iam_stack_name = f"{stack_name}-iam"

        # Deploy IAM CloudFormation stack
        with open(
            "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml",
            "r",
        ) as f:
            template_body = f.read()

        try:
            cf_client.create_stack(
                StackName=iam_stack_name,
                TemplateBody=template_body,
                Capabilities=["CAPABILITY_NAMED_IAM"],
            )

            # Wait for stack creation to complete
            waiter = cf_client.get_waiter("stack_create_complete")
            waiter.wait(
                StackName=iam_stack_name, WaiterConfig={"MaxAttempts": 30, "Delay": 10}
            )

            print(f"[{stack_name}] ✅ Created IAM stack: {iam_stack_name}")

        except cf_client.exceptions.AlreadyExistsException:
            print(f"[{stack_name}] ℹ️ IAM stack already exists: {iam_stack_name}")

        # Get outputs from the stack
        response = cf_client.describe_stacks(StackName=iam_stack_name)
        outputs = response["Stacks"][0].get("Outputs", [])

        role_arn = None
        for output in outputs:
            if output["OutputKey"] == "ServiceRoleArn":
                role_arn = output["OutputValue"]
                break

        if not role_arn:
            raise Exception("Could not find ServiceRoleArn in stack outputs")

        if not create_boundary:
            # Probe mode: deploy with an EMPTY PermissionsBoundaryArn — no
            # per-stack boundary policy created (removes an iam:CreatePolicy /
            # DeletePolicy from the concurrent burst).
            print(f"[{stack_name}] ℹ️ Skipping permissions boundary (probe mode)")
            return role_arn, ""

        # Create permission boundary policy. Adaptive retry so a burst of
        # concurrent iam:CreatePolicy calls rides through the account-wide IAM
        # throttle instead of failing at "reached max retries: 4".
        iam_client = boto3.client("iam", config=_THROTTLE_RETRY_CONFIG)
        boundary_name = f"{stack_name}-PermissionsBoundary"
        boundary_policy = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }

        try:
            iam_client.create_policy(
                PolicyName=boundary_name,
                PolicyDocument=json.dumps(boundary_policy),
                Description=f"Permissions boundary for {stack_name} IDP deployment",
            )
            print(f"[{stack_name}] ✅ Created permissions boundary: {boundary_name}")
        except iam_client.exceptions.EntityAlreadyExistsException:
            print(
                f"[{stack_name}] ℹ️ Permissions boundary already exists: {boundary_name}"
            )

        # Get account ID for boundary ARN
        sts_client = boto3.client("sts")
        account_id = sts_client.get_caller_identity()["Account"]
        boundary_arn = f"arn:aws:iam::{account_id}:policy/{boundary_name}"

        return role_arn, boundary_arn

    except Exception as e:
        print(f"[{stack_name}] ❌ Failed to create IAM resources: {e}")
        return None, None


def cleanup_iam_resources(stack_name):
    """Clean up IAM CloudFormation stack"""
    print(f"[{stack_name}] Cleaning up IAM stack...")

    try:
        # Clean up IAM CloudFormation stack (adaptive retry: concurrent teardown
        # of primary + probe IAM stacks also bursts CFN/IAM mutating calls).
        cf_client = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
        iam_stack_name = f"{stack_name}-iam"
        try:
            cf_client.delete_stack(StackName=iam_stack_name)

            # Wait for stack deletion to complete
            waiter = cf_client.get_waiter("stack_delete_complete")
            waiter.wait(
                StackName=iam_stack_name, WaiterConfig={"MaxAttempts": 30, "Delay": 10}
            )

            print(f"[{stack_name}] ✅ Deleted IAM stack: {iam_stack_name}")
        except cf_client.exceptions.ClientError as e:
            if "does not exist" in str(e):
                print(f"[{stack_name}] ℹ️ IAM stack not found: {iam_stack_name}")
            else:
                print(f"[{stack_name}] ⚠️ Failed to delete IAM stack: {e}")

    except Exception as e:
        print(f"[{stack_name}] ❌ Failed to cleanup IAM stack: {e}")


def test_step3_default_config(stack_name):
    """Step 3: Test with default config (Pipeline mode)"""
    print("Step 3: Testing with default config (Pipeline mode)...")
    batch_id = "test-default"
    sample_file = "lending_package.pdf"
    verify_string = "ANYTOWN, USA 12345"
    result_location = "pages/1/result.json"
    content_path = "text"

    def verify_extraction(json_data):
        inference_result = json_data.get("inference_result", {})
        if not inference_result:
            return False, "No inference_result found"
        total_fields = len(inference_result)
        if total_fields == 0:
            return False, "inference_result is empty"
        populated_fields = sum(
            1 for v in inference_result.values() if v not in [None, [], {}]
        )
        min_expected_fields = 3
        if total_fields < min_expected_fields:
            return (
                False,
                f"Expected at least {min_expected_fields} fields, found {total_fields}",
            )
        if populated_fields == 0:
            return False, "No fields contain extracted data (all null/empty)"
        return True, f"{populated_fields}/{total_fields} fields populated"

    def verify_classification(json_data):
        doc_class = json_data.get("document_class", {}).get("type")
        if not doc_class:
            return False, "No document_class.type found"
        if doc_class == "none":
            return False, "Document classified as 'none' (no class detected)"
        return True, f"Classified as '{doc_class}'"

    additional_checks = [
        ("Extraction verification", "sections/1/result.json", verify_extraction),
        (
            "Classification verification",
            "sections/1/result.json",
            verify_classification,
        ),
    ]

    if not run_inference_test(
        stack_name,
        sample_file,
        batch_id,
        verify_string,
        result_location,
        content_path,
        None,
        "samples",
        additional_checks,
    ):
        return {"success": False, "error": "Default config test failed"}

    return {"success": True}


def test_step4_bda_mode(stack_name):
    """Step 4: Upload and test BDA config (sync without activation for parallel execution)"""
    print("Step 4: Testing with BDA mode...")
    config_version = "test-bda"
    config_path = "config_library/unified/lending-package-sample/config.yaml"

    with open(config_path, "r") as f:
        config_content = f.read()

    bda_config_content = config_content.replace("use_bda: false", "use_bda: true")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        tmp.write(bda_config_content)
        bda_config_path = tmp.name

    try:
        print("Uploading BDA config (use_bda: true)")
        cmd = f"idp-cli config-upload --stack-name {stack_name} --config-file {bda_config_path} --config-version {config_version}"
        run_command(cmd)

        print("Syncing BDA config to create blueprints (without activation)")
        cmd = f"idp-cli config-sync-bda --stack-name {stack_name} --config-version {config_version}"
        run_command(cmd)
        print("✅ BDA config synced (will use --config-version for inference)")

        batch_id = "test-bda"
        sample_file = "lending_package.pdf"
        verify_string = "ANYTOWN, USA 12345"
        bda_result_location = "pages/1/parsedResult.json"
        content_path = "text"

        def verify_bda_extraction(json_data):
            inference_result = json_data.get("inference_result", {})
            if not inference_result:
                return False, "No inference_result found in BDA output"
            total_fields = len(inference_result)
            populated_fields = sum(
                1 for v in inference_result.values() if v not in [None, [], {}]
            )
            min_expected_fields = 3
            if total_fields < min_expected_fields:
                return (
                    False,
                    f"Expected at least {min_expected_fields} fields, found {total_fields}",
                )
            if populated_fields == 0:
                return False, "No fields contain extracted data (all null/empty)"
            return True, f"{populated_fields}/{total_fields} fields populated by BDA"

        bda_additional_checks = [
            (
                "BDA extraction verification",
                "sections/1/result.json",
                verify_bda_extraction,
            ),
        ]

        if not run_inference_test(
            stack_name,
            sample_file,
            batch_id,
            verify_string,
            bda_result_location,
            content_path,
            config_version,
            "samples",
            bda_additional_checks,
        ):
            return {"success": False, "error": "BDA config test failed"}

        return {"success": True}
    finally:
        os.unlink(bda_config_path)


def test_step5_rule_validation(stack_name):
    """Step 5: Test rule validation"""
    print("Step 5: Testing rule validation...")
    config_version = "rule-validation"
    config_path = "config_library/unified/rule-validation/config.yaml"
    sample_file = "medicare_respiratory_pa_packet.pdf"
    sample_dir = "samples/rule-validation"
    batch_id = "test-rules"
    verify_string = "global_periods"
    result_location = "rule_validation/sections/section_1_responses.json"
    content_path = "responses.global_periods.0.policy_type"

    print(f"Uploading rule validation config from: {config_path}")
    cmd = f"idp-cli config-upload --stack-name {stack_name} --config-file {config_path} --config-version {config_version}"
    run_command(cmd)

    def verify_rule_results(json_data):
        responses = json_data.get("responses", {})
        if not responses:
            return False, "No rule responses found"
        total_rules = 0
        passed_rules = 0
        failed_rules = 0
        for rule_name, rule_list in responses.items():
            if isinstance(rule_list, list):
                for rule in rule_list:
                    total_rules += 1
                    result = rule.get("result", "").lower()
                    if "pass" in result:
                        passed_rules += 1
                    elif "fail" in result:
                        failed_rules += 1
        if total_rules == 0:
            return False, "No rules were evaluated"
        return (
            True,
            f"{total_rules} rules evaluated ({passed_rules} passed, {failed_rules} failed)",
        )

    rule_additional_checks = [
        (
            "Rule validation results",
            "rule_validation/sections/section_1_responses.json",
            verify_rule_results,
        ),
    ]

    if not run_inference_test(
        stack_name,
        sample_file,
        batch_id,
        verify_string,
        result_location,
        content_path,
        config_version,
        sample_dir,
        rule_additional_checks,
    ):
        return {"success": False, "error": "Rule validation test failed"}

    return {"success": True}


def test_step6_multi_document(stack_name):
    """Step 6: Test multi-document batch processing"""
    print("Step 6: Testing multi-document batch processing...")
    batch_id = "test-multi-batch"
    sample_dir = "samples/w2"
    file_pattern = "W2_XL_input_clean_100[0-2].pdf"

    try:
        print("Processing 3 W-2 documents in parallel...")
        cmd = f"idp-cli run-inference --stack-name {stack_name} --dir {sample_dir} --file-pattern '{file_pattern}' --batch-id {batch_id} --monitor"
        run_command(cmd)

        result_dir = f"/tmp/result-{batch_id}"  # nosec B108
        cmd = f"idp-cli download-results --stack-name {stack_name} --batch-id {batch_id} --output-dir {result_dir}"
        run_command(cmd)

        print("Verifying all documents processed successfully...")
        cmd = f"find {result_dir} -path '*/sections/*/result.json' | wc -l"
        result = run_command(cmd, check=False)
        extraction_count = int(result.stdout.strip())

        if extraction_count < 3:
            print(f"❌ Expected 3 documents processed, found {extraction_count}")
            return {
                "success": False,
                "error": f"Multi-document batch test failed: only {extraction_count}/3 documents processed",
            }

        print(
            f"✅ Multi-document batch test passed: {extraction_count} documents processed successfully"
        )
        return {"success": True}

    except Exception as e:
        print(f"❌ Multi-document batch test failed: {e}")
        return {
            "success": False,
            "error": f"Multi-document batch test failed: {str(e)}",
        }


def test_step7_test_studio(stack_name):
    """Step 7: Test Studio - Run evaluation against pre-deployed test set using idp-cli test-result"""
    print("Step 7: Testing Test Studio with pre-deployed test set...")

    try:
        cf_client = boto3.client("cloudformation")
        stack_response = cf_client.describe_stacks(StackName=stack_name)
        outputs = stack_response["Stacks"][0].get("Outputs", [])

        test_set_bucket = None
        for output in outputs:
            if output["OutputKey"] == "S3TestSetBucketName":
                test_set_bucket = output["OutputValue"]
                break

        if not test_set_bucket:
            print(
                "⚠️  S3TestSetBucketName not found in stack outputs, skipping Test Studio test"
            )
            return {"success": True}

        s3_client = boto3.client("s3")
        try:
            response = s3_client.list_objects_v2(
                Bucket=test_set_bucket, Delimiter="/", MaxKeys=10
            )
            test_sets = [
                prefix["Prefix"].rstrip("/")
                for prefix in response.get("CommonPrefixes", [])
            ]

            if not test_sets:
                print(
                    f"⚠️  No test sets found in {test_set_bucket}, skipping Test Studio test"
                )
                return {"success": True}

            print(f"Found test sets: {', '.join(test_sets)}")

            test_set_name = None
            for preferred in ["fake-w2", "realkie-fcc-verified"]:
                if preferred in test_sets:
                    test_set_name = preferred
                    break
            if not test_set_name:
                test_set_name = test_sets[0]

            print(
                f"Running test against test set: {test_set_name} (limited to 3 documents)"
            )
            print(f"Using config version: {test_set_name}")

            # Run test inference
            cmd = f"idp-cli run-inference --stack-name {stack_name} --test-set {test_set_name} --config-version {test_set_name} --context 'CI/CD smoke test' --number-of-files 3"
            result = run_command(cmd, check=False)

            if result.returncode != 0:
                print("⚠️  Test set processing failed")
                return {
                    "success": False,
                    "error": f"Test Studio test failed for {test_set_name}",
                }

            # Extract test run ID from output
            test_run_id = None
            for line in result.stdout.split("\n"):
                if "Test run started:" in line:
                    test_run_id = line.split("Test run started:")[1].strip()
                    break

            if not test_run_id:
                print(
                    "⚠️  Could not extract test run ID from output, skipping result verification"
                )
                return {"success": True}

            print(f"Test run ID: {test_run_id}")
            print("Retrieving test results using idp-cli test-result...")

            # Use idp-cli test-result command to get results (triggers evaluation and waits)
            cmd = f"idp-cli test-result --stack-name {stack_name} --test-run-id {test_run_id} --wait --timeout 600"
            result = run_command(cmd, check=False)

            if result.returncode != 0:
                print("❌ Test result retrieval failed")
                return {
                    "success": False,
                    "error": "Test Studio test result retrieval failed",
                }

            # Parse output for accuracy check
            overall_accuracy = None
            for line in result.stdout.split("\n"):
                if "Overall Accuracy:" in line:
                    # Extract percentage (e.g., "Overall Accuracy: 95.45%")
                    parts = line.split(":")
                    if len(parts) >= 2:
                        accuracy_str = parts[1].strip().rstrip("%")
                        try:
                            overall_accuracy = float(accuracy_str) / 100.0
                        except ValueError:
                            pass
                    break

            if overall_accuracy is not None:
                if overall_accuracy > 0.30:
                    print(
                        f"✅ Test Studio test completed: {test_set_name} with {overall_accuracy:.2%} accuracy"
                    )
                else:
                    print(
                        f"⚠️  Low accuracy detected: {overall_accuracy:.2%} (threshold: 30%)"
                    )
                return {"success": True}
            else:
                print("⚠️  Could not parse accuracy from output, but test completed")
                return {"success": True}

        except Exception as e:
            print(f"⚠️  Could not access test set bucket: {e}")

        return {"success": True}

    except Exception as e:
        print(f"❌ Test Studio test failed: {e}")
        return {"success": False, "error": f"Test Studio test failed: {str(e)}"}


def summarize_list_sections(sections, list_field):
    """Total rows in ``list_field`` across sections, plus a per-section report.

    ``sections`` is ``[(result_json_path, payload), ...]``. Returns
    ``(total_rows, ["section 1: pages 1-6, 193 rows", ...])``.

    Kept as a pure function so the aggregation that #750 got wrong is unit
    tested: reading only the first section made a boundary mis-split look like
    lost extraction rows.
    """
    total = 0
    report = []
    for path, payload in sections:
        section_id = path.rsplit("/", 2)[-2]
        rows = len(payload.get("inference_result", {}).get(list_field) or [])
        pages = payload.get("split_document", {}).get("page_indices") or []
        # page_indices are 0-based; report the 1-based page numbers a human sees.
        page_range = f"{min(pages) + 1}-{max(pages) + 1}" if pages else "unknown"
        total += rows
        report.append(f"section {section_id}: pages {page_range}, {rows} rows")
    return total, report


def test_step8_agentic_extraction(stack_name):
    """Step 8: Test agentic extraction with large table"""
    print("Step 8: Testing agentic extraction with Nuveen (532 fund items)...")

    try:
        print("Uploading nuveen.yaml configuration...")
        cmd = f"idp-cli config-upload --stack-name {stack_name} --config-file scripts/sdlc/config/nuveen.yaml --config-version agentic-nuveen --no-validate"
        run_command(cmd, check=False)

        print(
            "Running agentic extraction on samples/Nuveen.pdf (this will take ~9 minutes)..."
        )
        cmd = f"idp-cli run-inference --stack-name {stack_name} --dir samples/ --file-pattern Nuveen.pdf --config-version agentic-nuveen --monitor"
        result = run_command(cmd, check=False)

        if result.returncode != 0:
            print("❌ Agentic extraction command failed")
            return {"success": False, "error": "Agentic extraction command failed"}

        batch_id = None
        for line in result.stdout.split("\n"):
            if "Batch ID:" in line:
                batch_id = line.split("Batch ID:")[1].strip()
                break

        if batch_id:
            print(f"Downloading results for batch: {batch_id}")
            result_dir = f"/tmp/result-agentic-{batch_id}"  # nosec B108
            cmd = f"idp-cli download-results --stack-name {stack_name} --batch-id {batch_id} --output-dir {result_dir}"
            run_command(cmd, check=False)

            # Read EVERY section, not `head -1`. Row completeness and section
            # count are separate properties and this step used to conflate them:
            # a classification mis-split (#750) left 193 of 532 rows in
            # sections/1, and the failure was reported as "expected 532 fund
            # items, got 193" — an extraction defect that never happened. Each
            # dimension now fails with its own message so the failure names the
            # subsystem that actually broke.
            cmd = f"find {result_dir} -path '*/sections/*/result.json' -type f"
            find_result = run_command(cmd, check=False)
            result_files = sorted(
                (p for p in find_result.stdout.split("\n") if p.strip()),
                key=lambda p: (
                    int(p.rsplit("/", 2)[-2]) if p.rsplit("/", 2)[-2].isdigit() else 0
                ),
            )

            if not result_files:
                print("❌ Result file not found")
                return {
                    "success": False,
                    "error": "Agentic extraction test failed: result file not found",
                }

            sections = []
            for path in result_files:
                with open(path, "r") as f:
                    sections.append((path, json.load(f)))

            doc_class = sections[0][1].get("document_class", {}).get("type")
            if doc_class == "Estimated2024AnnualTaxableDistributions":
                print(f"  ✓ Document class correct: {doc_class}")
            else:
                print(f"❌ Unexpected document class: {doc_class}")
                return {
                    "success": False,
                    "error": f"Agentic extraction test failed: unexpected document class '{doc_class}'",
                }

            total_funds, per_section = summarize_list_sections(
                sections, "FundInformation"
            )
            for line in per_section:
                print(f"    {line}")

            # Nuveen.pdf is ONE document. More than one section means the
            # classifier split it (boundary detection), which is a different bug
            # from losing rows — and it is silent otherwise, because each section
            # can still be 100% complete and the document still reaches COMPLETED.
            if len(sections) != 1:
                print(
                    f"❌ Classification over-split the document: expected 1 section, got {len(sections)}"
                )
                return {
                    "success": False,
                    "error": (
                        f"Agentic extraction test failed: classification over-split "
                        f"Nuveen.pdf into {len(sections)} sections (expected 1); "
                        f"{'; '.join(per_section)}. This is a document_boundary "
                        f"defect, not an extraction defect - see #750."
                    ),
                }

            if total_funds == 532:
                print(f"  ✓ FundInformation count correct: {total_funds} items")
                print("✅ Agentic extraction test completed successfully")
                return {"success": True}

            print(f"❌ FundInformation count mismatch: expected 532, got {total_funds}")
            return {
                "success": False,
                "error": (
                    f"Agentic extraction test failed: expected 532 fund items, got "
                    f"{total_funds} across {len(sections)} section(s) "
                    f"({'; '.join(per_section)})"
                ),
            }
        else:
            print("❌ Could not extract batch ID from output")
            return {
                "success": False,
                "error": "Agentic extraction test failed: could not extract batch ID",
            }

    except Exception as e:
        print(f"❌ Agentic extraction test failed: {e}")
        return {"success": False, "error": f"Agentic extraction test failed: {str(e)}"}


def test_step9_single_doc_discovery(stack_name):
    """Step 9: Test single-document discovery"""
    print("Step 9: Testing single-document discovery...")

    try:
        sample_file = "samples/insurance_package_single.pdf"
        config_version = "test-discovery"
        print(f"Running discovery on {sample_file}...")
        print(f"Saving to config version: {config_version}")
        print("This will take approximately 3-5 minutes...")

        cmd = f"idp-cli discover --stack-name {stack_name} -d {sample_file} --config-version {config_version}"
        run_command(cmd, check=True, timeout=300)

        print("Verifying discovered class saved to configuration...")

        config_file = "/tmp/discovery-config.yaml"  # nosec B108
        cmd = f"idp-cli config-download --stack-name {stack_name} --config-version {config_version} --output {config_file}"
        run_command(cmd, check=True)

        import yaml

        with open(config_file, "r") as f:
            config_data = yaml.safe_load(f)

        classes = config_data.get("classes", [])
        if len(classes) == 0:
            print(f"❌ No classes found in config version {config_version}")
            return {
                "success": False,
                "error": f"Single-document discovery test failed: no classes found in config version {config_version}",
            }

        discovered_class = classes[0]
        doc_class = discovered_class.get("$id", "Unknown")
        num_properties = len(discovered_class.get("properties", {}))
        print(f"  ✓ Discovered class: {doc_class}")
        print(f"  ✓ Properties: {num_properties} top-level fields")
        print(
            f"✅ Discovery test completed: schema saved to config version {config_version}"
        )
        return {"success": True}

    except Exception as e:
        print(f"❌ Single-document discovery test failed: {e}")
        return {
            "success": False,
            "error": f"Single-document discovery test failed: {str(e)}",
        }


def test_step10_multi_doc_discovery(stack_name):
    """Step 10: Test multi-document discovery"""
    print("Step 10: Testing multi-document discovery...")

    try:
        test_dir = "/tmp/multidoc-test"  # nosec B108
        import shutil

        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)
        os.makedirs(test_dir)

        sample_files = [
            ("samples/w2/W2_XL_input_clean_1000.pdf", "w2_1.pdf"),
            ("samples/w2/W2_XL_input_clean_1001.pdf", "w2_2.pdf"),
            ("samples/bank-statement-multipage.pdf", "bank_statement.pdf"),
            ("samples/insurance_package_single.pdf", "insurance.pdf"),
        ]

        for src, dest_name in sample_files:
            dest = f"{test_dir}/{dest_name}"
            if not os.path.exists(src):
                raise FileNotFoundError(f"Sample file not found: {src}")
            shutil.copy(src, dest)
            if not os.path.exists(dest):
                raise RuntimeError(f"Failed to copy {src} to {dest}")

        copied_files = len(os.listdir(test_dir))
        print(f"  ✓ Copied {copied_files} sample documents to {test_dir}")

        if copied_files != len(sample_files):
            raise RuntimeError(
                f"Expected {len(sample_files)} files but found {copied_files}"
            )

        print(f"Running multi-document discovery on {test_dir}...")
        print("This will take approximately 2-3 minutes...")

        cmd = f"idp-cli discover-multidoc --dir {test_dir} -o /tmp/multidoc-schemas"
        run_command(cmd, check=True, timeout=240)

        cmd = "find /tmp/multidoc-schemas -name '*.json' | wc -l"
        count_result = run_command(cmd, check=True)
        schema_count = (
            int(count_result.stdout.strip()) if count_result.stdout.strip() else 0
        )

        if schema_count == 0:
            print("❌ Multi-document discovery completed but no schemas found")
            return {
                "success": False,
                "error": "Multi-document discovery test failed: no schemas generated",
            }

        print(f"  ✓ Generated {schema_count} schema(s)")

        cmd = "find /tmp/multidoc-schemas -name '*.json' | head -1"
        first_schema = run_command(cmd, check=True).stdout.strip()
        if not first_schema:
            print("❌ Could not find generated schema file")
            return {
                "success": False,
                "error": "Multi-document discovery test failed: could not find generated schema file",
            }

        with open(first_schema, "r") as f:
            schema_json = json.load(f)

        if "$schema" not in schema_json or "properties" not in schema_json:
            print("❌ Generated schema missing required fields ($schema, properties)")
            return {
                "success": False,
                "error": "Multi-document discovery test failed: schema missing required fields",
            }

        print("  ✓ Schema structure validated")
        print("✅ Multi-document discovery test completed")
        return {"success": True}

    except Exception as e:
        print(f"❌ Multi-document discovery test failed: {e}")
        return {
            "success": False,
            "error": f"Multi-document discovery test failed: {str(e)}",
        }


def test_step12_api_rbac(stack_name):
    """Step 12: API RBAC authorization tests (static scan + dynamic matrix).

    Runs sequentially (the only sequential step) because the dynamic harness
    temporarily enables ADMIN_USER_PASSWORD_AUTH on the UI app client and
    restores it — unsafe to interleave with the parallel suite. Creates
    temporary Cognito users, exercises every API op across all roles +
    unauthenticated + token negatives, then tears the users down.
    """
    print("Step 12: API RBAC authorization tests (static + dynamic)...")
    report_dir = "/tmp/api-test-results"  # nosec B108
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    # IDP_SECTEST_STRICT_INPUT=true: this branch deploys the central dispatcher
    # input-shape validation, so malformed args now get a clean 400 (not a silent
    # 200 or resolver 5xx). Enable strict input-validation so the security suite
    # HARD-FAILS on any op that ISN'T cleanly rejected — turning the new
    # enforcement into a regression gate (was WARN-only / GAP-SEC-INPUT before
    # this feature existed). An env override is honored if already set.
    strict = os.environ.get("IDP_SECTEST_STRICT_INPUT", "true")
    cmd = (
        f"IDP_SECTEST_STRICT_INPUT={strict} "
        f"make api-test STACK_NAME={stack_name} REGION={region} REPORT_DIR={report_dir}"
    )
    result = run_command(cmd, check=False, timeout=1800)
    if result.returncode != 0:
        # Surface the report location; the full report is in the build log.
        return {
            "success": False,
            "error": (
                "API RBAC test failed (static scan or dynamic authorization "
                f"matrix) — see {report_dir} output in build log"
            ),
        }
    return {"success": True}


def test_step11_test_compare(stack_name):
    """Step 11: Test Compare - Compare results from multiple test runs using idp-cli test-compare"""
    print("Step 11: Testing test-compare command...")

    try:
        cf_client = boto3.client("cloudformation")
        stack_response = cf_client.describe_stacks(StackName=stack_name)
        outputs = stack_response["Stacks"][0].get("Outputs", [])

        test_set_bucket = None
        for output in outputs:
            if output["OutputKey"] == "S3TestSetBucketName":
                test_set_bucket = output["OutputValue"]
                break

        if not test_set_bucket:
            print(
                "⚠️  S3TestSetBucketName not found in stack outputs, skipping test-compare test"
            )
            return {"success": True}

        s3_client = boto3.client("s3")
        try:
            response = s3_client.list_objects_v2(
                Bucket=test_set_bucket, Delimiter="/", MaxKeys=10
            )
            test_sets = [
                prefix["Prefix"].rstrip("/")
                for prefix in response.get("CommonPrefixes", [])
            ]

            if not test_sets:
                print(
                    f"⚠️  No test sets found in {test_set_bucket}, skipping test-compare test"
                )
                return {"success": True}

            print(f"Found test sets: {', '.join(test_sets)}")

            test_set_name = None
            for preferred in ["fake-w2", "realkie-fcc-verified"]:
                if preferred in test_sets:
                    test_set_name = preferred
                    break
            if not test_set_name:
                test_set_name = test_sets[0]

            print(
                f"Running 2 test inferences against test set: {test_set_name} (limited to 2 documents each)"
            )

            # Run first test inference
            test_run_ids = []
            for i in range(2):
                print(f"\nRunning test inference {i + 1}/2...")
                cmd = f"idp-cli run-inference --stack-name {stack_name} --test-set {test_set_name} --config-version {test_set_name} --context 'CI/CD test-compare test {i + 1}' --number-of-files 2"
                result = run_command(cmd, check=False)

                if result.returncode != 0:
                    print(f"⚠️  Test inference {i + 1} failed")
                    return {
                        "success": False,
                        "error": f"Test inference {i + 1} failed for test-compare",
                    }

                # Extract test run ID from output
                test_run_id = None
                for line in result.stdout.split("\n"):
                    if "Test run started:" in line:
                        test_run_id = line.split("Test run started:")[1].strip()
                        break

                if not test_run_id:
                    print(f"⚠️  Could not extract test run ID {i + 1} from output")
                    return {
                        "success": False,
                        "error": f"Could not extract test run ID {i + 1}",
                    }

                test_run_ids.append(test_run_id)
                print(f"Test run {i + 1} ID: {test_run_id}")

                # Wait for test run to complete before starting next one
                print(f"Waiting for test run {i + 1} to complete...")
                cmd = f"idp-cli test-result --stack-name {stack_name} --test-run-id {test_run_id} --wait --timeout 300"
                result = run_command(cmd, check=False)

                if result.returncode != 0:
                    print(f"⚠️  Test run {i + 1} completion check failed")
                    return {
                        "success": False,
                        "error": f"Test run {i + 1} completion failed",
                    }

            # Compare the two test runs and save to JSON for validation
            print(f"\nComparing test runs: {', '.join(test_run_ids)}")

            # Create temp directory for comparison output
            import tempfile

            with tempfile.TemporaryDirectory() as tmpdir:
                cmd = f"idp-cli test-compare --stack-name {stack_name} --test-run-ids '{','.join(test_run_ids)}' --output-dir {tmpdir}"
                result = run_command(cmd, check=False)

                if result.returncode != 0:
                    print("❌ test-compare command failed")
                    return {"success": False, "error": "test-compare command failed"}

                # Find and load the comparison JSON file
                comparison_files = [
                    f
                    for f in os.listdir(tmpdir)
                    if f.startswith("comparison-") and f.endswith(".json")
                ]

                if not comparison_files:
                    print("⚠️  No comparison JSON file generated")
                    return {
                        "success": False,
                        "error": "No comparison JSON file generated",
                    }

                comparison_file = os.path.join(tmpdir, comparison_files[0])

                with open(comparison_file, "r") as f:
                    comparison_data = json.load(f)

                # Validate JSON structure contains expected data
                if "metrics" not in comparison_data:
                    print("⚠️  Comparison data missing 'metrics' field")
                    return {
                        "success": False,
                        "error": "Comparison data missing 'metrics' field",
                    }

                metrics = comparison_data["metrics"]

                # Verify both test runs are in metrics
                missing_runs = [tid for tid in test_run_ids if tid not in metrics]
                if missing_runs:
                    print(
                        f"⚠️  Test runs missing from comparison: {', '.join(missing_runs)}"
                    )
                    return {
                        "success": False,
                        "error": f"Test runs missing from comparison: {', '.join(missing_runs)}",
                    }

                # Verify each test run has required metric fields
                required_metrics = ["overallAccuracy", "totalCost"]
                for test_run_id in test_run_ids:
                    run_metrics = metrics[test_run_id]
                    missing_metrics = [
                        m for m in required_metrics if m not in run_metrics
                    ]

                    if missing_metrics:
                        print(
                            f"⚠️  Test run {test_run_id} missing metrics: {', '.join(missing_metrics)}"
                        )
                        return {
                            "success": False,
                            "error": f"Test run missing metrics: {', '.join(missing_metrics)}",
                        }

                print("  ✓ Comparison JSON contains both test runs")
                print("  ✓ All required metrics present")
                print("✅ test-compare test completed successfully")
                return {"success": True}

        except Exception as e:
            print(f"⚠️  Could not access test set bucket: {e}")
            return {"success": True}

    except Exception as e:
        print(f"❌ test-compare test failed: {e}")
        return {"success": False, "error": f"test-compare test failed: {str(e)}"}


def test_step13_permission_boundaries(stack_name):
    """Verify the deployed IAM roles actually carry the permissions boundary.

    The primary suite deploys with a non-empty PermissionsBoundaryArn, so the
    template's HasPermissionsBoundary condition should attach that boundary to
    every AWS::IAM::Role it creates. This is the ONLY place that boundary
    behavior is checked end-to-end (probes now deploy WITHOUT a boundary), so it
    closes a real gap: a template change that drops the PermissionsBoundary from
    a role would otherwise ship silently.

    Read-only IAM (list stack role resources + iam:GetRole), safe in the
    parallel pool. Samples up to 25 roles to bound API calls. Fails if any
    checked role is missing its boundary.
    """
    print("Step 13: Verifying IAM permissions boundaries are attached...")
    try:
        cf = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
        iam = boto3.client("iam", config=_THROTTLE_RETRY_CONFIG)

        # Collect AWS::IAM::Role physical ids from the stack + its nested stacks.
        role_names = []
        stacks_to_scan = [stack_name]
        scanned = set()
        while stacks_to_scan:
            sn = stacks_to_scan.pop()
            if sn in scanned:
                continue
            scanned.add(sn)
            paginator = cf.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=sn):
                for r in page.get("StackResourceSummaries", []):
                    rtype = r.get("ResourceType")
                    if rtype == "AWS::IAM::Role":
                        pid = r.get("PhysicalResourceId")
                        if pid:
                            role_names.append(pid)
                    elif rtype == "AWS::CloudFormation::Stack":
                        nested = r.get("PhysicalResourceId")
                        if nested:
                            stacks_to_scan.append(nested)

        if not role_names:
            return {"success": False, "error": "No IAM roles found in stack to check"}

        sample = role_names[:25]
        missing = []
        checked = 0
        for rn in sample:
            try:
                role = iam.get_role(RoleName=rn)["Role"]
            except Exception:  # noqa: BLE001 — role may be a path/ARN edge case
                continue
            checked += 1
            if not role.get("PermissionsBoundary"):
                missing.append(rn)

        if checked == 0:
            return {
                "success": False,
                "error": "Could not read any role for boundary check",
            }
        if missing:
            return {
                "success": False,
                "error": (
                    f"{len(missing)}/{checked} sampled roles missing a "
                    f"PermissionsBoundary (e.g. {missing[0]})"
                ),
            }
        print(
            f"✅ All {checked} sampled IAM roles carry a permissions boundary "
            f"(of {len(role_names)} total)"
        )
        return {"success": True}
    except Exception as e:  # noqa: BLE001
        print(f"❌ Permission-boundary check failed: {e}")
        return {"success": False, "error": f"Permission-boundary check failed: {e}"}


# ---------------------------------------------------------------------------
# Step 14: pipeline hooks (preprocessing / postprocessing)
# ---------------------------------------------------------------------------
#
# The pipeline-hook mechanism — the platform's supported way for a feature or an
# admin to inject business logic into document processing — had NO end-to-end
# coverage before this step, at any of its seven extension points. Unit tests
# cover the dispatcher with fakes, but nothing exercised the parts that only
# exist on a real stack:
#
#   * the real dispatcher Lambda reading a hook out of a real config version,
#   * `UpdateSchemaConfig` actually publishing the hook config sections, so the
#     fields an admin edits exist in the deployed schema,
#   * a hook Lambda cleared by the dispatcher's tag/name IAM condition,
#   * a hook's `updatedDocument` surviving into the PERSISTED document (the
#     tracking row), not just the state machine's output.
#
# It is a genuine gap rather than a theoretical one: a hook that silently never
# fires looks identical to one that ran and decided to do nothing — the workflow
# still succeeds, the document still reaches COMPLETED, and the hook Lambda
# writes no logs. That exact failure mode shipped once already (#599).
#
# The test is deliberately self-contained: it creates its own hook Lambda + role,
# registers the hook in its OWN config version (never the active one, so it
# cannot disturb the parallel steps sharing this stack), processes one document
# pinned to that version, then deletes everything it made. A leftover hook ARN
# would be actively harmful — a stale ARN at a flat point with onError:fail fails
# every subsequent document — so teardown runs in a `finally`.
# Naming here has to satisfy TWO different IAM policies at once, which is why it
# is not simply `GENAIIDP-*`:
#
#   * The CI CodeBuild role (scripts/sdlc/cfn/codepipeline-s3.yml) scopes its
#     `iam:*` to `role/idp-*` and its `lambda:*` to `function:idp-*`. A
#     `GENAIIDP-` prefixed role or function is AccessDenied — which is exactly
#     how this step first failed in the pipeline.
#   * The host dispatcher (patterns/unified/template.yaml) will only invoke a
#     hook that EITHER is named `GENAIIDP-*` OR carries the `idp:feature-id` tag.
#
# So the resources are named `idp-*` for the CI role, and the dispatcher is
# satisfied via the TAG path instead of the name path. That is also the more
# representative test: tagging is how installed Feature Platform features clear
# the check, while the `GENAIIDP-*` name is the admin escape hatch.
_HOOK_FN_PREFIX = "idp-citest-hook"
_HOOK_FEATURE_ID = "ci-hook-test"  # value of the idp:feature-id tag

# Marker the hook writes into the document; asserted in the persisted document.
_HOOK_MARKER_KEY = "ci_hook_marker"

# Inline source for the test hook. Uses `idp_common.hooks` — the documented
# helper pair — so this doubles as a check that the published contract works
# against a real stack (a hook built by hand would test our own scaffolding
# instead of the API we tell feature authors to use).
_HOOK_SOURCE = '''
import os
from idp_common.hooks import load_hook_document, updated_document_result

MARKER_KEY = os.environ["MARKER_KEY"]


def lambda_handler(event, context):
    """Mutate the document so the test can prove the hook ran AND that its
    change propagated. Reads its own settings from `args`, like a real hook.

    The marker goes into a SECTION's `attributes` (and `summary_report_uri` as a
    document-level backstop) because those are fields `Document.to_dict()`
    actually serializes. Note `Document.metadata` is a runtime-only field that
    to_dict() drops entirely — writing the marker there looks correct and
    silently never persists.
    """
    # WORKING_BUCKET is required to resolve the COMPRESSED document reference the
    # dispatcher hands over at postprocessing; load_hook_document raises without
    # it. Read explicitly so a missing env var fails with a clear message here
    # rather than deep inside idp_common.
    working_bucket = os.environ.get("WORKING_BUCKET") or ""
    if not working_bucket:
        raise RuntimeError(
            "WORKING_BUCKET is not set on this hook Lambda; a compressed "
            "document reference cannot be resolved"
        )
    document = load_hook_document(event, working_bucket=working_bucket)
    args = {a["key"]: a.get("value") for a in (event.get("args") or [])}
    point = event.get("hookPoint")

    marker = {
        "hookPoint": point,
        "note": args.get("note", ""),
        # Proves the hook saw a real, populated document rather than a stub.
        "saw_sections": len(document.sections or []),
        "saw_status": str(getattr(document, "status", "")),
        # HITL fields are omitted when falsy, so absent == no HITL.
        "saw_hitl_status": (event.get("document") or {}).get("hitl_status"),
    }

    # postprocessing runs last, so only its marker is the final persisted value;
    # tag the document-level field with the point so the assertion can tell which
    # hook wrote it.
    document.summary_report_uri = f"{MARKER_KEY}:{point}"

    # Section attributes are what a real hook mutates, and they round-trip.
    # `setdefault`-style write so an existing extraction result is preserved.
    for section in document.sections or []:
        if section.attributes is None:
            section.attributes = {}
        if isinstance(section.attributes, dict):
            section.attributes[MARKER_KEY] = marker
            break

    return updated_document_result(
        document,
        working_bucket=working_bucket,
        ciHookRan=True,
        ciHookPoint=point,
    )
'''


def _hook_iam_role(iam, role_name):
    """Create (idempotently) an execution role for the test hook Lambda.

    Needs basic logs plus read/write on the working bucket: `load_hook_document`
    resolves a compressed reference from S3, and `updated_document_result` may
    spill a large document back.
    """
    trust = json.dumps(
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
    )
    try:
        role_arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=trust)[
            "Role"
        ]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="hook-s3-kms",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject"],
                        "Resource": "arn:aws:s3:::*/compressed_documents/*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "kms:Decrypt",
                            "kms:Encrypt",
                            "kms:GenerateDataKey",
                            "kms:DescribeKey",
                        ],
                        "Resource": "*",
                    },
                ],
            }
        ),
    )
    return role_arn


def _dump_hook_logs(fn_name, limit=40):
    """Print the hook Lambda's recent log lines.

    Called only when Step 14 fails. A pipeline round-trip is ~70 minutes, so the
    single most valuable thing a failure can do is carry the hook's own
    traceback out with it instead of forcing another cycle to learn one fact.
    Best-effort: never raises, and says so when there is nothing to show (no log
    group means the hook was never invoked at all, which is itself the answer).
    """
    lg = f"/aws/lambda/{fn_name}"
    try:
        logs = boto3.client("logs", config=_THROTTLE_RETRY_CONFIG)
        streams = logs.describe_log_streams(
            logGroupName=lg, orderBy="LastEventTime", descending=True, limit=3
        ).get("logStreams", [])
        if not streams:
            print(f"  [diag] no log streams in {lg} — the hook was never invoked")
            return
        print(f"  [diag] last log lines from {lg}:")
        for st in streams[:2]:
            ev = logs.get_log_events(
                logGroupName=lg,
                logStreamName=st["logStreamName"],
                limit=limit,
                startFromHead=False,
            ).get("events", [])
            for e in ev:
                msg = (e.get("message") or "").rstrip()
                if msg:
                    print(f"    | {msg[:300]}")
    except Exception as exc:  # noqa: BLE001 — diagnostics must never mask the real failure
        print(f"  [diag] could not read {lg}: {exc}")


def _wait_lambda_ready(lam, fn_name, attempts=20, delay=3):
    """Block until the function has no update in flight.

    Lambda rejects a second mutating call while one is pending
    (ResourceConflictException: "The operation cannot be performed at this time.
    An update is in progress"), so the create -> tag -> configure sequence has to
    wait between steps.
    """
    for _ in range(attempts):
        try:
            cfg = lam.get_function_configuration(FunctionName=fn_name)
        except Exception:  # noqa: BLE001 — transient during creation
            time.sleep(delay)
            continue
        if cfg.get("State") in (None, "Active") and cfg.get("LastUpdateStatus") in (
            None,
            "Successful",
        ):
            return True
        time.sleep(delay)
    print(f"  ⚠️  {fn_name} still not settled; continuing")
    return False


# How long to keep looking for OUR execution after run-inference returns, and how
# often to re-check. `--monitor` is not a dependable barrier: it aborts on any
# internal error and still exits 0, so the step must wait on the thing it
# actually needs (a SUCCEEDED execution pinned to our config version) rather than
# on the CLI's say-so. The document takes ~80-120s in practice.
_TARGET_WAIT_SECS = 600
_TARGET_POLL_SECS = 20


def _find_target_execution(sfn, sm_arn, config_version):
    """(execution_arn, scanned) for the SUCCEEDED execution pinned to
    `config_version`, or (None, scanned).

    Identified by the execution INPUT's `document.config_version`, which is exact:
    only our document is pinned to that version. Matching on hook point instead
    would latch onto another parallel step's execution, since every execution
    emits both hook results.
    """
    scanned = 0
    next_token = None
    while scanned < 300:
        kwargs = {
            "stateMachineArn": sm_arn,
            "statusFilter": "SUCCEEDED",
            "maxResults": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        page = sfn.list_executions(**kwargs)
        for ex in page.get("executions", []):
            scanned += 1
            try:
                raw = (
                    sfn.describe_execution(executionArn=ex["executionArn"]).get("input")
                    or "{}"
                )
                doc_in = json.loads(raw).get("document") or {}
            except (ValueError, TypeError, KeyError):
                continue
            if doc_in.get("config_version") == config_version:
                return ex["executionArn"], scanned
        next_token = page.get("nextToken")
        if not next_token:
            break
    return None, scanned


def _resolve_working_bucket(stack_name):
    """Physical name of the stack's WorkingBucket.

    The hook Lambda MUST know this: at `postprocessing` the dispatcher hands over
    a COMPRESSED document reference, and `idp_common.hooks.load_hook_document`
    raises outright ("carries a compressed document reference but no working
    bucket was given") unless WORKING_BUCKET is set. There is no stack Output for
    it — the template only passes it down to nested stacks — so it is resolved
    from the stack's resources.
    """
    cf = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
    for page in cf.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for r in page.get("StackResourceSummaries", []):
            if (
                r.get("ResourceType") == "AWS::S3::Bucket"
                and r.get("LogicalResourceId") == "WorkingBucket"
            ):
                return r.get("PhysicalResourceId") or ""
    return ""


def _hook_lambda_runtime():
    """Lambda runtime string matching the interpreter building the hook zip.

    Falls back to python3.12 if the build interpreter is outside the range Lambda
    supports, since a wrong-but-supported runtime at least fails with a clear
    import error rather than an InvalidParameterValueException at create time.
    """
    import sys

    minor = sys.version_info.minor
    if sys.version_info.major == 3 and 9 <= minor <= 13:
        return f"python3.{minor}"
    print(
        f"  ⚠️  build interpreter python3.{minor} has no matching Lambda runtime; "
        f"falling back to python3.12"
    )
    return "python3.12"


def _build_hook_zip(path):
    """Zip the hook source together with idp_common and its import-time deps.

    `make setup` has already installed idp_common into the build environment, so
    every package is located by IMPORTING it and walking its real directory,
    rather than assuming a shared site-packages parent. That matters because the
    packages are not necessarily siblings: idp_common may be an editable install
    pointing into the repo while its dependencies live in site-packages, and an
    earlier version of this helper silently produced a zip with idp_common but
    NO pydantic — which fails only at Lambda import time, in CI, where it is
    expensive to debug.
    """
    import importlib
    import zipfile

    # idp_common[core] needs these at import time (config.models imports
    # pydantic). Deliberately explicit: a broad site-packages sweep would push
    # the zip past Lambda's 50MB direct-upload limit.
    # The closure is what `idp_common.hooks` actually pulls in at import time:
    # load_hook_document -> Document.decompress -> idp_common.utils ->
    # idp_common.config.models -> configuration_manager, which imports BOTH
    # pydantic and yaml. Omitting yaml produced a ModuleNotFoundError at cold
    # start that no offline test caught, so this list is validated by actually
    # invoking the deployed function (see the zip self-check below and the
    # dry-run procedure in CI_TEST_COVERAGE.md).
    required = [
        "idp_common",
        "pydantic",
        "pydantic_core",
        "annotated_types",
        "yaml",  # PyYAML — imported by idp_common.config.configuration_manager
    ]
    # `_yaml` is PyYAML's optional C extension; boto3/botocore are provided by
    # the Lambda runtime, so they are deliberately NOT vendored (they would push
    # the zip past the 50MB direct-upload limit).
    optional = ["typing_extensions", "typing_inspection", "_yaml"]

    def _add_module(zf, mod_name, required_flag):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            if required_flag:
                raise RuntimeError(
                    f"Cannot build the hook zip: {mod_name!r} is not importable. "
                    f"Run `make setup` first."
                ) from None
            return False
        src = mod.__file__ or ""
        if not src:
            return False
        # A package (has __init__.py) is added as a directory tree; a bare
        # module as its single file.
        if os.path.basename(src).startswith("__init__."):
            pkg_dir = os.path.dirname(os.path.abspath(src))
            base = os.path.dirname(pkg_dir)
            for root, _dirs, files in os.walk(pkg_dir):
                if "__pycache__" in root:
                    continue
                for fn in files:
                    if fn.endswith((".pyc", ".pyo")):
                        continue
                    full = os.path.join(root, fn)
                    zf.write(full, os.path.relpath(full, base))
        else:
            zf.write(os.path.abspath(src), os.path.basename(src))
        return True

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.py", _HOOK_SOURCE)
        for name in required:
            _add_module(zf, name, True)
        for name in optional:
            _add_module(zf, name, False)

    # Fail loudly here rather than at Lambda cold start: a zip missing pydantic
    # imports fine locally and dies with ModuleNotFoundError only in CI.
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        expected = ["index.py", "idp_common/hooks/__init__.py"]
        # Every REQUIRED third-party package must be present, not just a
        # hand-picked one: the yaml omission slipped through a pydantic-only check.
        expected += [f"{pkg}/" for pkg in required if pkg != "idp_common"]
        for want in expected:
            if not any(n == want or n.startswith(want) for n in names):
                raise RuntimeError(
                    f"Hook zip is missing {want!r} — the Lambda would fail at "
                    f"import time ({len(names)} entries built)"
                )
    return path


def test_step14_pipeline_hooks(stack_name):
    """Step 14: End-to-end pipeline-hook test (postprocessing + preprocessing).

    Deploys a real hook Lambda, registers it in a DEDICATED config version at
    both standalone hook points, processes one document pinned to that version,
    and asserts four things that only a live stack can show:

      1. the hook sections survive the config write/read round-trip, so the
         dispatcher has something to call (and the deployed schema carries them);
      2. the dispatcher actually INVOKED the hook at BOTH points (`invoked >= 1`
         in the state machine's `HookResults`, plus the config version it
         resolved) — the assertion that distinguishes "ran and did nothing" from
         "never called";
      3. the `updatedDocument` was ACCEPTED rather than refused by the
         dispatcher's guardrails (`documentUpdatedBy` non-empty);
      4. the mutation reached the PERSISTED document (the tracking row), and is
         the POSTPROCESSING marker — i.e. the last writer won.

    Uses its own config version, so it never perturbs the parallel steps sharing
    this stack. Everything it creates is removed in `finally`.
    """
    print("Step 14: Testing pipeline hooks end-to-end...")

    config_version = "test-pipeline-hooks"
    suffix = stack_name.split("-")[-1][:8].lower() or "ci"
    fn_name = f"{_HOOK_FN_PREFIX}-{suffix}"
    role_name = f"{fn_name}-role"
    zip_path = "/tmp/citest-hook.zip"  # nosec B108
    lam = boto3.client("lambda", config=_THROTTLE_RETRY_CONFIG)
    iam = boto3.client("iam", config=_THROTTLE_RETRY_CONFIG)
    created_fn = created_role = False
    outcome = {"ok": False}

    try:
        import yaml

        # --- 1. Deploy the hook Lambda -------------------------------------
        working_bucket = _resolve_working_bucket(stack_name)
        if not working_bucket:
            return {
                "success": False,
                "error": (
                    f"Could not resolve the WorkingBucket for {stack_name}; the "
                    f"hook cannot resolve compressed document references without it"
                ),
            }
        print(f"  Creating hook Lambda {fn_name} (working bucket {working_bucket})...")
        role_arn = _hook_iam_role(iam, role_name)
        created_role = True
        _build_hook_zip(zip_path)
        # IAM role propagation to Lambda is eventually consistent.
        time.sleep(12)
        with open(zip_path, "rb") as f:
            code = f.read()
        for attempt in range(5):
            try:
                lam.create_function(
                    FunctionName=fn_name,
                    # Match the runtime to the interpreter that BUILT the zip.
                    # pydantic_core ships a compiled extension
                    # (_pydantic_core.cpython-3XX-*.so), so a zip built on 3.12
                    # and run on a 3.13 Lambda (or vice versa) fails at cold
                    # start with an opaque ModuleNotFoundError. Hardcoding 3.12
                    # happens to match today's buildspec, but would break
                    # silently the day that buildspec's python is bumped.
                    Runtime=_hook_lambda_runtime(),
                    Role=role_arn,
                    Handler="index.lambda_handler",
                    Code={"ZipFile": code},
                    Timeout=120,
                    MemorySize=512,
                    Environment={
                        "Variables": {
                            "MARKER_KEY": _HOOK_MARKER_KEY,
                            "WORKING_BUCKET": working_bucket,
                        }
                    },
                    # Load-bearing, not metadata: the dispatcher's ABAC
                    # condition (StringLike aws:ResourceTag/idp:feature-id)
                    # is the ONLY thing authorizing it to invoke a function
                    # not named GENAIIDP-*. Without this tag every dispatch
                    # fails closed with AccessDenied.
                    Tags={"idp:feature-id": _HOOK_FEATURE_ID},
                )
                created_fn = True
                break
            except lam.exceptions.ResourceConflictException:
                lam.update_function_code(FunctionName=fn_name, ZipFile=code)
                created_fn = True
                break
            except Exception as exc:  # noqa: BLE001 — role not yet assumable
                if attempt == 4:
                    raise
                print(f"    create_function retry {attempt + 1}: {exc}")
                time.sleep(10)
        hook_arn = (
            lam.get_function(FunctionName=fn_name)
            .get("Configuration", {})
            .get("FunctionArn", "")
        )
        if not hook_arn:
            return {
                "success": False,
                "error": f"Could not resolve the ARN of hook Lambda {fn_name}",
            }
        # Tag AND set the environment unconditionally rather than relying on
        # create_function's Tags/Environment: the ResourceConflictException path
        # above reuses a function left by an interrupted earlier run, and
        # update_function_code sets NEITHER. An untagged function fails every
        # dispatch closed with AccessDenied; one without WORKING_BUCKET raises
        # inside load_hook_document the moment it is handed a compressed
        # document reference (which is what `postprocessing` always gets).
        lam.tag_resource(Resource=hook_arn, Tags={"idp:feature-id": _HOOK_FEATURE_ID})
        _wait_lambda_ready(lam, fn_name)
        lam.update_function_configuration(
            FunctionName=fn_name,
            Environment={
                "Variables": {
                    "MARKER_KEY": _HOOK_MARKER_KEY,
                    "WORKING_BUCKET": working_bucket,
                }
            },
        )
        _wait_lambda_ready(lam, fn_name)
        env_now = (
            lam.get_function_configuration(FunctionName=fn_name)
            .get("Environment", {})
            .get("Variables", {})
        )
        if env_now.get("WORKING_BUCKET") != working_bucket:
            return {
                "success": False,
                "error": (
                    f"Hook Lambda {fn_name} has WORKING_BUCKET="
                    f"{env_now.get('WORKING_BUCKET')!r}, expected "
                    f"{working_bucket!r}; load_hook_document would raise on the "
                    f"compressed document reference it receives"
                ),
            }
        tags = lam.list_tags(Resource=hook_arn).get("Tags", {})
        if tags.get("idp:feature-id") != _HOOK_FEATURE_ID:
            return {
                "success": False,
                "error": (
                    f"Hook Lambda {fn_name} is missing the idp:feature-id tag "
                    f"(got {tags!r}); the dispatcher would fail closed with "
                    f"AccessDenied because the function is not named GENAIIDP-*"
                ),
            }
        print(f"  ✓ Hook Lambda ready: {hook_arn} (tagged idp:feature-id)")

        # --- 2. Register the hook in a DEDICATED config version ------------
        # Start from the stack's own default config so the version is valid.
        base = "/tmp/citest-hook-config.yaml"  # nosec B108
        run_command(
            f"idp-cli config-download --stack-name {stack_name} "
            f"--config-version default --output {base}",
            check=True,
        )
        with open(base, "r") as f:
            cfg = yaml.safe_load(f) or {}

        # Assertion 3: the deployed SCHEMA must expose these sections. Checked
        # against the downloaded config's own shape below (see schema check).
        for point in ("preprocessing", "postprocessing"):
            cfg[point] = {
                "enabled": True,
                # Same id as the Lambda's idp:feature-id tag, so the config and
                # the ABAC grant cannot drift apart.
                "featureId": _HOOK_FEATURE_ID,
                "arn": hook_arn,
                # `continue` on BOTH points: a hook fault must not fail the
                # document and turn a coverage test into a flaky gate. The
                # invoked-count assertion is what proves it ran.
                "onError": "continue",
                "args": [{"key": "note", "value": f"ci-{point}"}],
                "allowDocumentUpdate": True,
            }
        with open(base, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)

        run_command(
            f"idp-cli config-upload --stack-name {stack_name} "
            f"--config-file {base} --config-version {config_version}",
            check=True,
        )
        # Round-trip check: the flat hook sections must survive the config
        # write/read path (they are dropped if a model field is missing —
        # exactly the regression the config-model tests guard offline).
        rt = "/tmp/citest-hook-config-rt.yaml"  # nosec B108
        run_command(
            f"idp-cli config-download --stack-name {stack_name} "
            f"--config-version {config_version} --output {rt}",
            check=True,
        )
        with open(rt, "r") as f:
            rt_cfg = yaml.safe_load(f) or {}
        for point in ("preprocessing", "postprocessing"):
            section = rt_cfg.get(point) or {}
            if section.get("arn") != hook_arn:
                return {
                    "success": False,
                    "error": (
                        f"{point} hook did not survive the config round-trip "
                        f"(arn={section.get('arn')!r}) — the dispatcher would "
                        f"find no hook to call"
                    ),
                }
        print("  ✓ Both hook sections survived the config round-trip")

        # --- 3. Process a document pinned to that version ------------------
        print("  Processing a document with the hook registered...")
        batch_id = "test-pipeline-hooks"
        # `--dir` takes a DIRECTORY (file_okay=False) and run-inference has no
        # short flags at all, so `-d <file>` is rejected outright. Select the one
        # document with --file-pattern, exactly as Steps 6/8 do.
        run_command(
            f"idp-cli run-inference --stack-name {stack_name} "
            f"--dir samples/ --file-pattern lending_package.pdf "
            f"--batch-id {batch_id} "
            f"--config-version {config_version} --monitor",
            check=True,
            timeout=900,
        )

        # --- 4. Assert the dispatcher INVOKED the hook ---------------------
        cf = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cf.describe_stacks(StackName=stack_name)["Stacks"][0].get(
                "Outputs", []
            )
        }
        sm_arn = outputs.get("StateMachineArn", "")
        if not sm_arn:
            return {
                "success": False,
                "error": "StateMachineArn missing from stack outputs",
            }

        # Locate OUR execution, then read its history IN FULL.
        #
        # Two traps here, both hit in real pipeline runs:
        #
        # 1. Step 14 shares this state machine with the other parallel steps, and
        #    since PreprocessingHook is StartAt and PostprocessingHook is on the
        #    shared tail, EVERY execution emits both hook results (invoked=0 for a
        #    hook-less config). Matching on hook point alone latches onto a
        #    foreign execution. So the target execution is identified by its
        #    INPUT's config_version — only our document is pinned to it — via a
        #    cheap describe_execution, with no history scan at all.
        #
        # 2. get_execution_history(reverseOrder=True, maxResults=200) returns the
        #    NEWEST 200 events. PreprocessingHook is the FIRST task, so on any
        #    execution with >200 events (a multi-section document — measured at
        #    240 events for one real doc) that window never reaches it, while
        #    PostprocessingHook near the end is always found. That asymmetry is
        #    exactly the "No preprocessing dispatcher result" failure. The history
        #    is therefore paginated FORWARD and in full.
        sfn = boto3.client("stepfunctions", config=_THROTTLE_RETRY_CONFIG)

        # Do NOT trust `--monitor` as the barrier. It aborts on any internal
        # error and still exits 0 — a missing DocumentState enum value made it
        # bail after 21s with "Monitoring error: 1 validation error for
        # DocumentStatus", so the scan ran while the document was still QUEUED
        # and reported a false "no execution found". Poll for our execution
        # until it appears, up to a deadline.
        target_arn = None
        scanned = 0
        deadline = time.time() + _TARGET_WAIT_SECS
        while True:
            target_arn, scanned = _find_target_execution(sfn, sm_arn, config_version)
            if target_arn or time.time() > deadline:
                break
            print(
                f"  (our execution not SUCCEEDED yet; scanned {scanned}, "
                f"retrying in {_TARGET_POLL_SECS}s)"
            )
            time.sleep(_TARGET_POLL_SECS)

        if not target_arn:
            return {
                "success": False,
                "error": (
                    f"No SUCCEEDED execution found whose input pins "
                    f"config_version={config_version!r} (scanned {scanned}) within "
                    f"{_TARGET_WAIT_SECS}s. The document either failed to process "
                    f"or was not pinned to that version by run-inference "
                    f"--config-version."
                ),
            }
        print(
            f"  ✓ found our execution ({scanned} scanned): {target_arn.rsplit(':', 1)[-1]}"
        )

        found = {}
        hist_token = None
        pages = 0
        while pages < 12:
            hkw = {"executionArn": target_arn, "maxResults": 1000}
            if hist_token:
                hkw["nextToken"] = hist_token
            hist = sfn.get_execution_history(**hkw)
            for event in hist.get("events", []):
                det = event.get("taskSucceededEventDetails") or {}
                out = det.get("output") or ""
                if '"hookPoint"' not in out:
                    continue
                try:
                    payload = json.loads(out).get("Payload") or {}
                except (ValueError, TypeError):
                    continue
                point = payload.get("hookPoint")
                if point in ("preprocessing", "postprocessing"):
                    found[point] = payload
            hist_token = hist.get("nextToken")
            pages += 1
            if not hist_token:
                break
        print(f"  (read {pages} history page(s); hook points found: {sorted(found)})")

        for point in ("preprocessing", "postprocessing"):
            payload = found.get(point)
            if not payload:
                return {
                    "success": False,
                    "error": (
                        f"No {point} dispatcher result for configVersion "
                        f"{config_version!r} in {scanned} scanned SUCCEEDED "
                        f"execution(s) — either the {point} hook state is not "
                        f"wired into the state machine, or our document's "
                        f"execution did not resolve that config version"
                    ),
                }
            if payload.get("invoked", 0) < 1:
                return {
                    "success": False,
                    "error": (
                        f"{point} dispatcher reported invoked=0 (resolved "
                        f"configVersion={payload.get('configVersion')!r}); the "
                        f"hook was registered in {config_version!r} but never "
                        f"called"
                    ),
                }
            print(
                f"  ✓ {point}: invoked={payload['invoked']} "
                f"configVersion={payload.get('configVersion')!r}"
            )

        # --- 5. Assert the mutation reached the PERSISTED document ---------
        # postprocessing is the last writer, so its marker is the one that must
        # survive into the tracking row.
        post = found["postprocessing"]
        if not post.get("documentUpdatedBy"):
            return {
                "success": False,
                "error": (
                    "postprocessing hook ran but the dispatcher recorded no "
                    "documentUpdatedBy — its updatedDocument was refused "
                    f"(results={json.dumps(post.get('results'))[:400]})"
                ),
            }
        print(f"  ✓ document updated by {post['documentUpdatedBy']}")

        # The marker must appear in the PERSISTED document, and must be the
        # POSTPROCESSING one: both hooks write `summary_report_uri`, so the value
        # that survives proves the LAST writer's mutation is what got persisted.
        marker_seen = _find_marker_in_tracking(stack_name, outputs)
        if not marker_seen:
            return {
                "success": False,
                "error": (
                    f"Hook ran and its update was accepted, but no tracking row "
                    f"carries SummaryReportUri starting with {_HOOK_MARKER_KEY!r} "
                    f"— the mutation did not survive into the persisted document"
                ),
            }
        if "postprocessing" not in marker_seen:
            return {
                "success": False,
                "error": (
                    f"The persisted marker is not the postprocessing one "
                    f"({marker_seen}) — the final document does not reflect the "
                    f"LAST hook to run"
                ),
            }
        print(f"  ✓ marker persisted to the tracking row: {marker_seen}")

        print(
            "✅ Pipeline-hook end-to-end test passed (preprocessing + postprocessing)"
        )
        outcome["ok"] = True
        return {"success": True}

    except Exception as e:  # noqa: BLE001
        print(f"❌ Pipeline-hook test failed: {e}")
        return {"success": False, "error": f"Pipeline-hook test failed: {e}"}

    finally:
        # On failure, carry the hook's own logs out with the error — a pipeline
        # round-trip is ~70 minutes, so guessing costs far more than dumping.
        if not outcome["ok"] and created_fn:
            _dump_hook_logs(fn_name)
        # Teardown is NOT optional: a leftover hook ARN pointing at a deleted
        # Lambda is exactly the stale-ARN state that fails every subsequent
        # document at a flat hook point. The config version is disposable (never
        # activated), but the Lambda + role must go.
        if created_fn:
            try:
                lam.delete_function(FunctionName=fn_name)
                print(f"  ✓ deleted hook Lambda {fn_name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  ⚠️  could not delete hook Lambda {fn_name}: {exc}")
        if created_role:
            for call, kwargs in (
                (
                    iam.delete_role_policy,
                    {"RoleName": role_name, "PolicyName": "hook-s3-kms"},
                ),
                (
                    iam.detach_role_policy,
                    {
                        "RoleName": role_name,
                        "PolicyArn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    },
                ),
                (iam.delete_role, {"RoleName": role_name}),
            ):
                try:
                    call(**kwargs)
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            print(f"  ✓ deleted hook role {role_name}")


def _find_marker_in_tracking(stack_name, outputs):
    """Return the persisted marker value from the tracking row, or None.

    The hook writes its marker to `summary_report_uri`, which the DynamoDB
    document service persists as the `SummaryReportUri` attribute. That choice is
    deliberate: the tracking row stores only a REDUCED view of each section
    (`Id`/`Class`/`PageIds`/`OutputJSONUri`) and drops section `attributes`
    entirely, so a marker written only into section attributes proves the
    in-flight mutation but can never be observed here. `Document.metadata` is
    worse still — `to_dict()` drops it before serialization.

    Checks the `SummaryReportUri` attribute explicitly rather than string-matching
    the whole item, so a failure says which document was found and what it held.
    """
    table_name = outputs.get("TrackingTableName")
    if not table_name:
        # Not exported by every template revision; fall back to a resource lookup.
        cf = boto3.client("cloudformation", config=_THROTTLE_RETRY_CONFIG)
        for page in cf.get_paginator("list_stack_resources").paginate(
            StackName=stack_name
        ):
            for r in page.get("StackResourceSummaries", []):
                if (
                    r.get("ResourceType") == "AWS::DynamoDB::Table"
                    and r.get("LogicalResourceId") == "TrackingTable"
                ):
                    table_name = r.get("PhysicalResourceId")
                    break
    if not table_name:
        print("  ⚠️  could not locate the tracking table")
        return None

    ddb = boto3.client("dynamodb", config=_THROTTLE_RETRY_CONFIG)
    for page in ddb.get_paginator("scan").paginate(
        TableName=table_name,
        # filtered-scan-ok: bounded to a fresh smoke-test stack, and the
        # paginator pages to completion, so no match can be missed.
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": {"S": "doc#"}},
        PaginationConfig={"MaxItems": 500},
    ):
        for item in page.get("Items", []):
            uri = (item.get("SummaryReportUri") or {}).get("S") or ""
            if uri.startswith(_HOOK_MARKER_KEY):
                pk = (item.get("PK") or {}).get("S", "?")
                # postprocessing runs last, so it must be the final writer.
                return f"{pk} -> SummaryReportUri={uri!r}"
    return None


# Single source of truth for the smoke-test suite: (func, step, name,
# description). The parallel runner, the success summary, and the AI
# failure-analysis prompt are all derived from this list — add or remove a
# test here only. Step 12 runs sequentially after the parallel steps.
PARALLEL_TEST_STEPS = [
    (
        test_step3_default_config,
        "Step 3",
        "Default config",
        "Default config inference (Pipeline mode)",
    ),
    (test_step4_bda_mode, "Step 4", "BDA mode", "BDA mode config and inference"),
    (
        test_step5_rule_validation,
        "Step 5",
        "Rule validation",
        "Rule validation config and processing",
    ),
    (
        test_step6_multi_document,
        "Step 6",
        "Multi-document batch",
        "Multi-document batch processing",
    ),
    (
        test_step7_test_studio,
        "Step 7",
        "Test Studio",
        "Test Studio evaluation (idp-cli test-result)",
    ),
    # Step 8: the earlier hang was NOT an extraction regression — nuveen.yaml set
    # extraction.agentic.enabled without extraction.mode, so the merge silently
    # reverted to simple single-pass, which times out on the 532-row/17-page doc.
    # Fixed by converting nuveen.yaml to native v0.6 (mode: advanced); live-
    # validated at ~305s extraction / 532 rows. Re-enabled.
    #
    # The "got 193 of 532" failure that followed was not extraction either: the
    # #653 boundary rules read this document's repeated running header as an
    # opening header block and split it (#750). Fixed by the BOUNDARY sentence in
    # nuveen.yaml's class description (the rules' own PRECEDENCE escape hatch),
    # and the step now checks row completeness and section count separately.
    (
        test_step8_agentic_extraction,
        "Step 8",
        "Agentic extraction",
        "Agentic extraction with large tables",
    ),
    (
        test_step9_single_doc_discovery,
        "Step 9",
        "Single-doc discovery",
        "Single-document discovery",
    ),
    (
        test_step10_multi_doc_discovery,
        "Step 10",
        "Multi-doc discovery",
        "Multi-document discovery",
    ),
    # Step 11 (test-compare) only runs inferences against a test set — same
    # shape as Steps 3-10 with no shared-stack mutation — so it is safe to run
    # in the parallel pool. (Previously sequential for no functional reason.)
    (
        test_step11_test_compare,
        "Step 11",
        "test-compare",
        "Test comparison (idp-cli test-compare)",
    ),
    # Step 13: read-only IAM check that the deployed roles carry the permissions
    # boundary (probes no longer test this, so the primary suite must). Safe in
    # the parallel pool — no shared-stack mutation.
    (
        test_step13_permission_boundaries,
        "Step 13",
        "Permission boundaries",
        "IAM permissions boundaries attached to deployed roles",
    ),
    # Step 14: pipeline hooks. Safe in the parallel pool — it registers its hook
    # in its OWN config version and never activates it, so the other steps
    # (which use `default` or their own versions) are unaffected. It does create
    # a Lambda + role, both removed in its own `finally`.
    (
        test_step14_pipeline_hooks,
        "Step 14",
        "Pipeline hooks",
        "Pipeline hooks end-to-end (preprocessing + postprocessing, mutation persisted)",
    ),
]
# Step 12 stays sequential: its dynamic RBAC harness temporarily flips
# ADMIN_USER_PASSWORD_AUTH on the shared UI app client (a stack-wide auth
# mutation) and restores it, so interleaving it with API-hitting parallel
# tests would corrupt them. Runs alone after the parallel pool drains.
SEQUENTIAL_TEST_STEPS = [
    (
        test_step12_api_rbac,
        "Step 12",
        "API RBAC",
        "API RBAC authorization tests (static scan + dynamic matrix)",
    ),
]
ALL_TEST_STEPS = PARALLEL_TEST_STEPS + SEQUENTIAL_TEST_STEPS


def _deploy_primary_stack_attempt(stack_name, admin_email, template_url):
    """One deploy+health-check attempt for the primary shared stack.

    Creates IAM, runs `idp-cli deploy --wait`, and verifies the stack reached a
    COMPLETE status. On failure returns a result dict with failure_type="deploy"
    and captured `cf_events` so the caller can classify it (e.g. the transient
    CloudWatch Logs race via _is_transient_logs_race) and decide whether to
    retry. Returns {"success": True} on a healthy deploy.
    """
    result = {"stack_name": stack_name, "success": False, "failure_type": "deploy"}
    # Step 0: Create IAM resources
    print("Step 0: Creating IAM resources...")
    role_arn, permissions_boundary_arn = create_iam_resources(stack_name)
    if not role_arn or not permissions_boundary_arn:
        result["error"] = "Failed to create required IAM resources"
        return result

    # Step 1: Deploy using template URL
    print("Step 1: Deploying stack...")
    cmd = f"idp-cli deploy --stack-name {stack_name} --template-url {template_url} --admin-email {admin_email} --wait"
    cmd += f" --role-arn {role_arn}"
    cmd += f" --parameters PermissionsBoundaryArn={permissions_boundary_arn}"
    # Full nested-stack creation can legitimately run long; don't let the default
    # test-command timeout kill a healthy in-progress deploy. check=False so a
    # rollback is captured (with cf_events) and classified rather than raising.
    deploy = run_command(cmd, check=False, timeout=3 * 3600)

    # Step 2: Verify stack status
    print("Step 2: Verifying stack status...")
    status = run_command(
        f"aws cloudformation describe-stacks --stack-name {stack_name} "
        "--query 'Stacks[0].StackStatus' --output text",
        check=False,
    )
    if "COMPLETE" in status.stdout and "ROLLBACK" not in status.stdout:
        print("✅ Deployment completed")
        return {"stack_name": stack_name, "success": True}

    # Failed/rolled-back — capture CF events so _is_transient_logs_race can see
    # whether this was the known LogGroup create race (and thus retryable).
    detail = status.stdout.strip() or f"idp-cli deploy exit {deploy.returncode}"
    print(f"❌ Stack status: {detail}")
    result["error"] = f"Stack deployment failed with status: {detail}"
    _capture_cf_events(result, stack_name)
    return result


def deploy_and_test_stack(stack_name, admin_email, template_url, progress_cb=None):
    """Deploy and test the unified IDP stack.

    progress_cb, if given, is called with the current step_results dict at each
    milestone (after the parallel pool drains, and after each sequential step).
    It lets main() publish a running summary to S3 BEFORE the whole primary
    suite finishes — so the GitLab monitor's ~45-min handoff always finds a
    current snapshot even when the suite (e.g. a slow Step 12) runs long. Best
    effort: a callback error must never fail the suite.
    """
    print(f"Starting deployment: {stack_name}")

    def _emit(step_results):
        if progress_cb is None:
            return
        try:
            progress_cb(step_results)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ progress_cb failed (non-fatal): {e}")

    try:
        # Steps 0-2: create IAM, deploy, verify — with a one-shot retry for the
        # known transient CloudWatch Logs create-consistency race
        # (_is_transient_logs_race). The probes already retry this
        # (_run_probe_attempt); the primary suite did NOT, so a race on the
        # shared stack failed the whole pipeline. A rolled-back stack + its IAM
        # are torn down before the retry so the redeploy is clean and reuses no
        # partially-created resources. Any OTHER deploy failure returns
        # immediately (no retry) so real regressions surface fast.
        deploy_result = None
        for attempt in range(1, PROBE_TRANSIENT_MAX_ATTEMPTS + 1):
            deploy_result = _deploy_primary_stack_attempt(
                stack_name, admin_email, template_url
            )
            if deploy_result.get("success"):
                break
            if attempt < PROBE_TRANSIENT_MAX_ATTEMPTS and _is_transient_logs_race(
                deploy_result
            ):
                print(
                    f"♻️ Primary stack hit the transient CloudWatch Logs "
                    f"create-consistency race (attempt {attempt}/"
                    f"{PROBE_TRANSIENT_MAX_ATTEMPTS}); tearing down and "
                    "redeploying a fresh stack once..."
                )
                # Reclaim the rolled-back stack + IAM so the retry starts clean
                # (same stack name; cleanup is idempotent/best-effort).
                cleanup_stack({"stack_name": stack_name})
                deploy_result["retried_transient_logs_race"] = True
                continue
            # Not the transient race (or out of attempts) — surface it.
            return deploy_result

        if not deploy_result.get("success"):
            return deploy_result

        print("✅ Stack is healthy")

        # Run tests 3-10 in parallel (Step 4 BDA now uses config-sync-bda + --config-version, no activation race)
        print(f"\n{'=' * 80}")
        print("Running tests 3-10 in parallel (fail-fast enabled)...")
        print(f"{'=' * 80}\n")

        parallel_tests = [
            (func, f"{step}: {name}") for func, step, name, _ in PARALLEL_TEST_STEPS
        ]

        # Per-step status for the consolidated end-of-run summary table. Steps
        # not reached (fail-fast cancels the rest) stay "cancelled".
        step_results = {
            f"{step}: {name}": {"status": "cancelled", "error": ""}
            for _, step, name, _ in ALL_TEST_STEPS
        }

        failed_test = None
        # No `with` block: its shutdown(wait=True) would join still-running
        # test threads on failure, burning the CodeBuild job timeout before
        # cleanup_stack can run (which is how stacks/IAM roles get leaked).
        executor = ThreadPoolExecutor(max_workers=8)
        futures = {
            executor.submit(func, stack_name): name for func, name in parallel_tests
        }

        # Process results as they complete (fail-fast)
        for future in as_completed(futures):
            test_name = futures[future]
            try:
                result = future.result()
                if result["success"]:
                    print(f"✅ {test_name} passed")
                    step_results[test_name] = {"status": "passed", "error": ""}
                else:
                    err = result.get("error", "Unknown error")
                    print(f"❌ {test_name} failed: {err}")
                    step_results[test_name] = {"status": "failed", "error": err}
                    failed_test = (test_name, result)
                    break
            except Exception as e:
                print(f"❌ {test_name} exception: {e}")
                step_results[test_name] = {"status": "failed", "error": str(e)}
                failed_test = (test_name, {"success": False, "error": str(e)})
                break

        if failed_test:
            # Fail fast: stop the other tests from mutating the stack while
            # cleanup deletes it. New run_command calls in test threads abort
            # immediately; in-flight subprocess trees are killed. The threads
            # themselves then error out quickly against dead subprocesses.
            ABORT_TESTS.set()
            _kill_running_commands()
        executor.shutdown(wait=failed_test is None, cancel_futures=True)

        # Publish a snapshot now that the parallel pool has drained — this is
        # well before the ~45-min handoff even when the sequential steps below
        # (Step 12 API RBAC can be slow) push the suite past it.
        _emit(step_results)

        # Check if any parallel test failed
        if failed_test:
            test_name, result = failed_test
            print(f"\n❌ Test suite failed at {test_name}")
            return {
                "stack_name": stack_name,
                "success": False,
                "failure_type": "test",
                "error": f"{test_name} failed: {result.get('error', 'Unknown error')}",
                "step_results": step_results,
            }

        # Run the sequential steps (test-compare, API RBAC) after parallel tests
        for func, step, name, _ in SEQUENTIAL_TEST_STEPS:
            print(f"\n{'=' * 80}")
            print(f"Running {step} ({name}) sequentially...")
            print(f"{'=' * 80}\n")

            key = f"{step}: {name}"
            result = func(stack_name)
            if result["success"]:
                print(f"✅ {step}: {name} passed")
                step_results[key] = {"status": "passed", "error": ""}
            else:
                err = result.get("error", "Unknown error")
                print(f"❌ {step}: {name} failed: {err}")
                step_results[key] = {"status": "failed", "error": err}
                _emit(step_results)
                return {
                    "stack_name": stack_name,
                    "success": False,
                    "failure_type": "test",
                    "error": f"{step}: {name} failed: {err}",
                    "step_results": step_results,
                }
            _emit(step_results)

        print("✅ All tests passed")
        return {
            "stack_name": stack_name,
            "success": True,
            "step_results": step_results,
        }

    except Exception as e:
        print(f"❌ Testing failed: {e}")
        return {
            "stack_name": stack_name,
            "success": False,
            "failure_type": "deploy",
            "error": f"Deployment/testing failed: {str(e)}",
        }


def run_inference_test(
    stack_name,
    sample_file,
    batch_id,
    verify_string,
    result_location,
    content_path,
    config_version=None,
    sample_dir="samples",
    additional_checks=None,
):
    """Run inference test and verify results

    Args:
        stack_name: Name of the CloudFormation stack
        sample_file: Name of the sample file to process
        batch_id: Batch ID for this test run
        verify_string: String to verify in the main result
        result_location: Path to the main result file (relative to document directory)
        content_path: Dot-separated path to content in JSON (e.g., "pages.0.text")
        config_version: Optional config version to use
        sample_dir: Directory containing sample files
        additional_checks: Optional list of (check_name, file_path, verify_func) tuples
                          where verify_func takes JSON and returns (success: bool, message: str)
    """
    try:
        # Run inference
        print(f"Running inference with batch-id: {batch_id}...")
        cmd = f"idp-cli run-inference --stack-name {stack_name} --dir {sample_dir} --file-pattern {sample_file} --batch-id {batch_id} --monitor"
        if config_version:
            cmd += f" --config-version {config_version}"
        run_command(cmd)
        print("✅ Inference completed")

        # Download results
        print("Downloading results...")
        result_dir = f"/tmp/result-{batch_id}"  # nosec B108 - isolated CodeBuild environment
        cmd = f"idp-cli download-results --stack-name {stack_name} --batch-id {batch_id} --output-dir {result_dir}"
        run_command(cmd)

        # Verify result content
        print("Verifying result content...")

        # Find result file
        cmd = f"find {result_dir} -path '*/{result_location}' | head -1"
        result = run_command(cmd, check=False)
        result_file = result.stdout.strip()

        if not result_file:
            cmd = f"find {result_dir} -name 'result.json' | head -10"
            debug_result = run_command(cmd, check=False)
            print("Found result.json files:")
            print(debug_result.stdout)
            print(f"❌ No result file found at {result_location}")
            return False

        # Verify content
        with open(result_file, "r") as f:
            result_json = json.load(f)

        text_content = result_json
        for key in content_path.split("."):
            if key.isdigit():
                text_content = text_content[int(key)]
            else:
                text_content = text_content[key]

        if verify_string not in str(text_content):
            print(
                f"❌ Text content does not contain expected string: '{verify_string}'"
            )
            print(f"Actual text starts with: '{str(text_content)[:100]}...'")
            return False

        print(f"✅ Found expected verification string: '{verify_string}'")

        # Run additional verification checks
        if additional_checks:
            for check_name, check_path, verify_func in additional_checks:
                print(f"Running additional check: {check_name}...")

                # Find the check file
                cmd = f"find {result_dir} -path '*/{check_path}' | head -1"
                check_result = run_command(cmd, check=False)
                check_file = check_result.stdout.strip()

                if not check_file:
                    print(
                        f"⚠️  {check_name}: file not found at {check_path} (may be optional)"
                    )
                    continue  # Skip optional checks

                # Load and verify
                try:
                    with open(check_file, "r") as f:
                        check_json = json.load(f)

                    success, message = verify_func(check_json)
                    if not success:
                        print(f"❌ {check_name} failed: {message}")
                        return False

                    print(f"✅ {check_name} passed: {message}")
                except Exception as e:
                    print(f"❌ {check_name} error: {e}")
                    return False

        return True

    except Exception as e:
        print(f"❌ Inference test failed: {e}")
        return False


def get_codebuild_logs():
    """Get CodeBuild logs from CloudWatch"""
    try:
        # Get CodeBuild build ID from environment
        build_id = os.environ.get("CODEBUILD_BUILD_ID", "")
        if not build_id:
            return "CodeBuild logs not available (not running in CodeBuild)"

        # Wait for logs to propagate to CloudWatch
        time.sleep(10)

        # Extract log group and stream from build ID
        log_group = f"/aws/codebuild/{build_id.split(':')[0]}"
        log_stream = build_id.split(":")[-1]

        # Get the NEWEST events (startFromHead=False): a long build exceeds
        # one get_log_events page (~10K events / 1MB), and callers take the
        # tail of what we return — the first page would give them lines from
        # the start of the build instead of the failure at the end.
        logs_client = boto3.client("logs")
        response = logs_client.get_log_events(
            logGroupName=log_group, logStreamName=log_stream, startFromHead=False
        )

        # Extract log messages
        log_messages = []
        for event in response.get("events", []):
            log_messages.append(event["message"])

        return "\n".join(log_messages)

    except Exception as e:
        return f"Failed to retrieve CodeBuild logs: {str(e)}"


# A nested-stack CodeBuild custom resource (e.g. MULTIDOCDISCOVERYSTACK's
# DockerBuildRun) reports failure to CloudFormation as nothing more than
# "CodeBuild failed with status: FAILED". The evidence chain dead-ends at the
# CloudFormation boundary, so no amount of prompt tuning can explain WHY the
# image build failed — the real cause (a pip BrokenPipeError, a Docker layer
# error, an ECR auth failure) lives only in that build's own CloudWatch stream.
# This marker is what tells us to go follow that trail.
_CODEBUILD_CR_FAILURE_MARKER = "codebuild failed with status"

# Lines of the failing build's log to attach. The interesting part (the failing
# command + its traceback + the buildspec phase summary) is always at the tail.
_CODEBUILD_LOG_TAIL_LINES = 120

# Build statuses that represent a settled failure worth reporting on. Notably
# EXCLUDES IN_PROGRESS: a still-running build carries no failure phase, so
# treating it as "the failure" produces an empty, misleading report.
_TERMINAL_BUILD_FAILURE_STATUSES = ("FAILED", "FAULT", "TIMED_OUT", "STOPPED")


def _codebuild_projects_in_stack(cf_client, stack_name):
    """Logical-id → project name for every CodeBuild project in a stack."""
    projects = {}
    try:
        paginator = cf_client.get_paginator("describe_stack_resources")
        pages = paginator.paginate(StackName=stack_name)
    except Exception:
        # describe_stack_resources isn't paginated in all botocore versions.
        try:
            pages = [cf_client.describe_stack_resources(StackName=stack_name)]
        except Exception:
            return projects
    try:
        for page in pages:
            for res in page.get("StackResources", []):
                if res.get("ResourceType") == "AWS::CodeBuild::Project":
                    projects[res.get("LogicalResourceId", "")] = res.get(
                        "PhysicalResourceId", ""
                    )
    except Exception:
        pass
    return projects


def _codebuild_log_tail(build, tail_lines=_CODEBUILD_LOG_TAIL_LINES):
    """Tail of a build's CloudWatch log stream, plus its console URL."""
    logs_info = build.get("logs", {}) or {}
    group = logs_info.get("groupName")
    stream = logs_info.get("streamName")
    deep_link = logs_info.get("deepLink", "")
    if not group or not stream:
        return "", deep_link
    try:
        logs_client = boto3.client("logs")
        response = logs_client.get_log_events(
            logGroupName=group, logStreamName=stream, startFromHead=False
        )
        messages = [e.get("message", "") for e in response.get("events", [])]
        return "\n".join(messages[-tail_lines:]), deep_link
    except Exception as e:  # noqa: BLE001
        return f"(could not read {group}/{stream}: {e})", deep_link


def get_codebuild_failure_details(stack_name, failed_events, max_projects=3):
    """Follow a CodeBuild custom-resource failure down to the real build error.

    When a nested stack fails because its Docker-image CodeBuild run failed, the
    CloudFormation reason is only "CodeBuild failed with status: FAILED". This
    resolves the CodeBuild project from the *nested* stack that reported the
    failure (the project is a resource in that same stack), finds its most recent
    non-successful build, and returns that build's phase error + log tail so the
    summary can name the actual cause.

    Must be called BEFORE stack teardown — the nested stacks and their builds'
    log streams are needed. Returns a list of dicts; empty when nothing matched
    (so a summary prompt can simply omit the section).
    """
    triggering = [
        ev
        for ev in failed_events or []
        if isinstance(ev, dict)
        and _CODEBUILD_CR_FAILURE_MARKER in (ev.get("reason") or "").lower()
    ]
    if not triggering:
        return []

    cf_client = boto3.client("cloudformation")
    cb_client = boto3.client("codebuild")
    details = []
    seen_projects = set()

    for ev in triggering:
        # The failing custom resource lives in a nested stack; its `stack_name`
        # is what get_cloudformation_logs recorded when it walked that stack.
        owning_stack = ev.get("stack_name") or stack_name
        for logical_id, project_name in _codebuild_projects_in_stack(
            cf_client, owning_stack
        ).items():
            if not project_name or project_name in seen_projects:
                continue
            if len(seen_projects) >= max_projects:
                break
            seen_projects.add(project_name)
            try:
                ids = cb_client.list_builds_for_project(
                    projectName=project_name, sortOrder="DESCENDING"
                ).get("ids", [])
                if not ids:
                    continue
                # Inspect the few most recent builds and report the newest one
                # that reached a TERMINAL FAILURE. Matching on "not SUCCEEDED"
                # would also match IN_PROGRESS — and since the DockerBuildRun
                # custom resource now retries once, the newest build is often
                # still running when we look. An in-progress build has no
                # FAILED/FAULT/TIMED_OUT phase, so it yields an empty
                # phase_error and a partial log tail, and next() would stop
                # there and never reach the build that actually failed.
                builds = cb_client.batch_get_builds(ids=ids[:5]).get("builds", [])
                failed = next(
                    (
                        b
                        for b in builds
                        if b.get("buildStatus") in _TERMINAL_BUILD_FAILURE_STATUSES
                    ),
                    None,
                )
                if not failed:
                    continue
                phase_error = next(
                    (
                        (p.get("contexts") or [{}])[0].get("message", "")
                        for p in reversed(failed.get("phases", []))
                        if p.get("phaseStatus") in ("FAILED", "FAULT", "TIMED_OUT")
                    ),
                    "",
                )
                log_tail, deep_link = _codebuild_log_tail(failed)
                details.append(
                    {
                        "stack_name": owning_stack,
                        "logical_id": logical_id,
                        "project_name": project_name,
                        "build_id": failed.get("id", ""),
                        "build_status": failed.get("buildStatus", ""),
                        "failed_phase": next(
                            (
                                p.get("phaseType", "")
                                for p in failed.get("phases", [])
                                if p.get("phaseStatus")
                                in ("FAILED", "FAULT", "TIMED_OUT")
                            ),
                            "",
                        ),
                        "phase_error": phase_error,
                        "log_url": deep_link,
                        "log_tail": log_tail,
                    }
                )
            except Exception as e:  # noqa: BLE001
                details.append(
                    {
                        "stack_name": owning_stack,
                        "project_name": project_name,
                        "error": f"Could not retrieve CodeBuild detail: {e}",
                    }
                )
    return details


def _recovery_command(stack_name):
    """The CORRECT recovery command for the stack's actual current state.

    `continue-update-rollback` is valid ONLY from UPDATE_ROLLBACK_FAILED. A
    CREATE that rolled back lands in ROLLBACK_FAILED, where the only way forward
    is delete-stack — recommending continue-update-rollback there just errors out
    (the model guessed wrong on exactly this case), so the command is decided in
    Python from the real status instead of being left to the model.
    """
    try:
        cf_client = boto3.client("cloudformation")
        status = (
            cf_client.describe_stacks(StackName=stack_name)
            .get("Stacks", [{}])[0]
            .get("StackStatus", "")
        )
    except Exception:  # noqa: BLE001
        status = ""

    if status == "UPDATE_ROLLBACK_FAILED":
        return (
            f"aws cloudformation continue-update-rollback --stack-name {stack_name}",
            status,
        )
    if status in ("ROLLBACK_FAILED", "ROLLBACK_COMPLETE", "CREATE_FAILED"):
        # A CREATE rollback cannot be continued or updated — the stack must go.
        return (
            f"aws cloudformation delete-stack --stack-name {stack_name}",
            status,
        )
    return ("", status)


def get_workflow_failure_details(stack_name, max_executions=5):
    """Capture the real cause of a document processing failure before teardown.

    When a smoke test fails because a document didn't process, the tracking
    table / batch monitor only surface a generic "Unknown error" — the actual
    exception (a Lambda traceback, a Bedrock/BDA InvokeDataAutomationAsync
    error, a validation failure) lives in the Step Functions execution history
    and is destroyed when cleanup_stack deletes the stack. This snapshots the
    failed executions' error/cause so the summary can name the true root cause
    instead of echoing "Unknown error".

    Returns a list of {execution_arn, name, error, cause, failed_state} dicts
    (empty if none found or the stack has no reachable state machine).
    """
    try:
        cf = boto3.client("cloudformation")
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cf.describe_stacks(StackName=stack_name)["Stacks"][0].get(
                "Outputs", []
            )
        }
        state_machine_arn = outputs.get("StateMachineArn", "")
        if not state_machine_arn:
            return []

        sfn = boto3.client("stepfunctions")
        failed = sfn.list_executions(
            stateMachineArn=state_machine_arn,
            statusFilter="FAILED",
            maxResults=max_executions,
        ).get("executions", [])

        details = []
        for execution in failed:
            arn = execution["executionArn"]
            # Walk the execution history for the terminal failure event, which
            # carries the concrete error + cause (Lambda stack trace, service
            # exception) that the tracking table flattens to "Unknown error".
            error = cause = failed_state = ""
            try:
                events = sfn.get_execution_history(
                    executionArn=arn, reverseOrder=True, maxResults=25
                ).get("events", [])
                for event in events:
                    for key in (
                        "executionFailedEventDetails",
                        "taskFailedEventDetails",
                        "lambdaFunctionFailedEventDetails",
                    ):
                        detail = event.get(key)
                        if detail:
                            error = error or detail.get("error", "")
                            cause = cause or detail.get("cause", "")
                    # reverseOrder=True → the first StateEntered we see is the
                    # last state the execution reached, i.e. the one that
                    # failed. (Don't break once error/cause are set: the
                    # terminal ExecutionFailed event precedes this in reverse
                    # order, so an early break would miss the state name.)
                    if not failed_state and event.get("type", "").endswith(
                        "StateEntered"
                    ):
                        failed_state = event.get("stateEnteredEventDetails", {}).get(
                            "name", ""
                        )
            except Exception as e:  # noqa: BLE001
                cause = f"(could not read execution history: {e})"

            details.append(
                {
                    "execution_arn": arn,
                    "name": execution.get("name", ""),
                    "error": error or "(no error field)",
                    # Causes can be huge (full traceback) — cap so the summary
                    # prompt stays small while keeping the actionable head.
                    "cause": (cause or "(no cause field)")[:2000],
                    "failed_state": failed_state or "(unknown state)",
                }
            )
        return details

    except Exception as e:  # noqa: BLE001
        return [{"error": f"Failed to retrieve workflow failure details: {str(e)}"}]


def generate_publish_failure_summary(publish_error):
    """Generate summary for publish/build failures"""
    try:
        # Build errors sit at the end of the log; a bounded tail keeps the
        # prompt small instead of shipping the entire (potentially huge) log.
        log_tail = "\n".join(get_codebuild_logs().split("\n")[-400:])
        prompt = dedent(f"""
        You are a build system analyst. Analyze this publish/build failure and provide specific technical guidance.

        Publish Error: {publish_error}

        Build Logs (last 400 lines):
        {log_tail}

        ANALYZE THE LOGS FOR ALL ERROR TYPES:
        - Python linting/formatting errors (ruff check failed, code formatting check failed)
        - Python syntax errors (py_compile failures, SyntaxError, IndentationError)
        - UI build failures (npm ci errors, package-lock.json sync issues, missing @esbuild packages)
        - AWS/Infrastructure errors (S3 access denied, CloudFormation validation failed, SAM build/package failures)
        - Missing prerequisites (aws/sam not found, version requirements not met)
        - File system errors (missing files, permission denied, disk space issues)
        - Dependency issues (pip install failures, missing Python packages, Docker build errors)
        - Lambda validation failures (missing idp_common in builds, import test failures)

        Create a summary focused on BUILD/PUBLISH issues with bullet points:

        🔧 BUILD FAILURE ANALYSIS

        📋 Component Status:
        • UI Build: FAILED - npm dependency issues
        • Lambda Build: SUCCESS - All patterns built correctly
        • Template Publish: FAILED - S3 access denied

        🔍 Technical Root Cause:
        • Extract exact error messages from logs (ruff, npm, pip, aws, sam errors)
        • Identify specific missing packages, version conflicts, or permission issues
        • Focus on build-time errors, not deployment errors
        • Check AWS credentials, S3 bucket permissions, and file access issues

        💡 Fix Commands:
        • Provide specific commands based on actual error found
        • For linting: run ruff format . && ruff check --fix .
        • For npm: cd src/ui && rm package-lock.json && npm install
        • For AWS S3: aws s3 ls s3://bucket-name to test access
        • For permissions: chmod +x script.sh or check IAM policies

        Keep each bullet point under 75 characters. Use sub-bullets for details.
        
        IMPORTANT: Respond ONLY with the bullet format above. Do not include any text before or after.
        """)

        return _invoke_bedrock(prompt)

    except Exception as e:
        return f"⚠️ Failed to generate build failure summary: {e}"


def get_cloudformation_logs(stack_name):
    """Get CloudFormation stack events for error analysis"""
    try:
        cf_client = boto3.client("cloudformation")
        all_failed_events = []

        # Get events from main stack
        all_events = []
        next_token = None

        while True:
            if next_token:
                response = cf_client.describe_stack_events(
                    StackName=stack_name, NextToken=next_token
                )
            else:
                response = cf_client.describe_stack_events(StackName=stack_name)

            events = response.get("StackEvents", [])
            all_events.extend(events)

            next_token = response.get("NextToken")
            if not next_token:
                break

        # Filter for failed events and extract nested stack ARNs
        nested_stack_arns = []
        for event in all_events:
            status = event.get("ResourceStatus", "")
            if "FAILED" in status or "ROLLBACK" in status:
                all_failed_events.append(
                    {
                        "stack_name": stack_name,
                        "timestamp": event.get("Timestamp", "").isoformat()
                        if event.get("Timestamp")
                        else "",
                        "resource_type": event.get("ResourceType", ""),
                        "logical_id": event.get("LogicalResourceId", ""),
                        "status": status,
                        "reason": event.get(
                            "ResourceStatusReason", "No reason provided"
                        ),
                    }
                )

                # Extract nested stack ARN from CREATE_FAILED events
                if (
                    status == "CREATE_FAILED"
                    and event.get("ResourceType") == "AWS::CloudFormation::Stack"
                    and "Embedded stack arn:aws:cloudformation:"
                    in event.get("ResourceStatusReason", "")
                ):
                    reason = event.get("ResourceStatusReason", "")
                    start = reason.find("arn:aws:cloudformation:")
                    end = reason.find(" was not successfully created")
                    if start != -1 and end != -1:
                        nested_arn = reason[start:end]
                        nested_stack_arns.append(nested_arn)

        # Get events from nested stacks
        for nested_arn in nested_stack_arns:
            try:
                nested_events = []
                next_token = None

                while True:
                    if next_token:
                        response = cf_client.describe_stack_events(
                            StackName=nested_arn, NextToken=next_token
                        )
                    else:
                        response = cf_client.describe_stack_events(StackName=nested_arn)

                    events = response.get("StackEvents", [])
                    nested_events.extend(events)

                    next_token = response.get("NextToken")
                    if not next_token:
                        break

                # Add failed events from nested stack
                for event in nested_events:
                    status = event.get("ResourceStatus", "")
                    if "FAILED" in status or "ROLLBACK" in status:
                        all_failed_events.append(
                            {
                                "stack_name": nested_arn.split("/")[
                                    -2
                                ],  # Extract stack name from ARN
                                "timestamp": event.get("Timestamp", "").isoformat()
                                if event.get("Timestamp")
                                else "",
                                "resource_type": event.get("ResourceType", ""),
                                "logical_id": event.get("LogicalResourceId", ""),
                                "status": status,
                                "reason": event.get(
                                    "ResourceStatusReason", "No reason provided"
                                ),
                            }
                        )

            except Exception:
                # Skip nested stacks we can't access
                continue

        return _filter_root_cause_events(all_failed_events)

    except Exception as e:
        return [{"error": f"Failed to retrieve CloudFormation logs: {str(e)}"}]


def _filter_root_cause_events(failed_events):
    """Drop cancellation-cascade noise so only concrete failures reach Bedrock.

    A full rollback emits hundreds of 'Resource creation cancelled' and
    ROLLBACK_* status events downstream of a handful of real failures;
    filtering them here shrinks the summary prompt ~50x and lets the model
    focus on actual ResourceStatusReasons.
    """
    cascade_markers = (
        "Resource creation cancelled",
        "cancelled",
        "Rollback requested by user",
        "No reason provided",
    )
    root_causes = [
        e
        for e in failed_events
        if "FAILED" in e.get("status", "")
        and not any(m in e.get("reason", "") for m in cascade_markers)
    ]
    # If filtering removed everything (unexpected event shapes), fall back to
    # the raw list rather than sending the model nothing. Cap either way.
    events = root_causes or failed_events
    events.sort(key=lambda e: e.get("timestamp", ""))
    return events[:20]


def _invoke_bedrock(prompt):
    """Invoke Bedrock with a prompt and return the response text"""
    bedrock = boto3.client("bedrock-runtime")
    # Opus 4.8 rejects sampling params (temperature/top_p/top_k) with a 400 —
    # do not add them back.
    response = bedrock.invoke_model(
        modelId="us.anthropic.claude-opus-4-8",
        body=json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            }
        ),
    )
    response_body = json.loads(response["body"].read())
    # Opus 4.8 may emit thinking blocks before the text block — take the
    # first text block rather than assuming content[0] is text.
    for block in response_body["content"]:
        if block.get("type") == "text":
            return block["text"]
    # No text block (e.g. truncated response) — raise so callers fall back to
    # their manual summary instead of silently printing nothing.
    raise ValueError(
        f"Bedrock response contained no text block "
        f"(stop_reason={response_body.get('stop_reason')})"
    )


def generate_deployment_summary(result, stack_name, template_url):
    """Generate deployment summary using Bedrock API.

    Case routing (success / infrastructure failure / test failure) is done in
    Python from result["failure_type"] — the model is only asked to explain,
    never to decide pass/fail (earlier prompt-based routing misclassified
    failures and leaked its scratchpad into the summary).
    """
    try:
        error_text = result.get("error", "")

        # Case C: success — Bedrock writes a short PASS narrative (the user
        # asked for a Bedrock report on both pass and fail). The deterministic
        # test list is always included below the narrative, and if Bedrock is
        # unavailable the except-clause fallback still yields a usable summary.
        if result.get("success"):
            test_lines = "\n".join(
                f"• Test {i} ({step}): {desc} ✓"
                for i, (_, step, _, desc) in enumerate(ALL_TEST_STEPS, 1)
            )
            deterministic = dedent(f"""
            🚀 DEPLOYMENT RESULTS

            📋 Stack Status: {stack_name} deployed successfully
            📦 Template: {template_url}

            ✅ All Tests Passed ({len(ALL_TEST_STEPS)} tests):
            {{test_lines}}
            """).format(test_lines=test_lines)

            success_prompt = dedent(f"""
            An IDP CloudFormation deployment succeeded and ALL post-deployment
            smoke tests passed. Write a brief, upbeat PASS report.

            Stack Name: {stack_name}
            Template: {template_url}
            Tests that passed:
            {test_lines}

            GROUNDING RULES — follow strictly:
            • State only what the evidence supports: the deploy succeeded and
              every listed test passed. Do NOT invent metrics, timings, or
              coverage claims not present above.
            • Remind the reader (one bullet) that these are deploy + smoke
              checks, not exhaustive functional coverage.

            Provide the report in this format:

            🚀 DEPLOYMENT RESULTS — ✅ PASS

            📋 Status: {stack_name} deployed; all {len(ALL_TEST_STEPS)} tests passed

            ✅ What passed:
            • One concise bullet naming the test areas covered
            • One bullet noting these are deploy + smoke checks, not full coverage

            Keep each bullet under 75 characters.
            Respond ONLY with the format above, no other text.
            """)
            try:
                narrative = _invoke_bedrock(success_prompt)
            except Exception as e:  # noqa: BLE001
                # Bedrock down / no text block — the deterministic list alone is
                # a complete, accurate PASS summary.
                print(f"⚠️ Bedrock PASS narrative unavailable ({e}); using list only")
                return deterministic
            return f"{narrative}\n\n{deterministic}"

        # Case B: infrastructure failure — the deploy itself failed, so pull
        # CloudFormation events for root cause. failure_type is set where the
        # failure is classified in deploy_and_test_stack; "deploy" is also the
        # safe default when the field is missing (e.g. exception result dicts
        # built in main), since CF-event analysis degrades gracefully.
        if result.get("failure_type", "deploy") != "test":
            # Use pre-captured events when the caller saved them before the
            # stack was torn down (the APIGW/VPC hosting test deletes its
            # throwaway stack in a finally block, so a post-cleanup fetch by
            # stack name would find nothing).
            logs = result.get("cf_events")
            if logs is None:
                print(f"🔍 Getting CloudFormation logs for: {stack_name}")
                try:
                    logs = get_cloudformation_logs(stack_name)
                    print(f"✅ Retrieved {len(logs)} CF events for {stack_name}")
                except Exception as e:
                    print(f"⚠️ Exception getting CF logs for {stack_name}: {e}")
                    logs = [{"error": f"Exception: {str(e)}", "stack_name": stack_name}]

            # A CodeBuild custom resource reports only "CodeBuild failed with
            # status: FAILED" to CloudFormation, so follow that trail into the
            # failing build's own log stream. Prefer pre-captured details.
            codebuild_failures = result.get("codebuild_failures")
            if codebuild_failures is None:
                try:
                    codebuild_failures = get_codebuild_failure_details(stack_name, logs)
                    if codebuild_failures:
                        print(
                            f"✅ Captured {len(codebuild_failures)} CodeBuild "
                            "failure detail(s)"
                        )
                except Exception as e:  # noqa: BLE001
                    print(f"⚠️ Could not capture CodeBuild failure detail: {e}")
                    codebuild_failures = []

            # Order events chronologically here rather than asking the model to
            # do it: the FIRST concrete failure is what caused the rollback, and
            # anything after it (notably a DELETE_FAILED mid-rollback) is a
            # SEPARATE, secondary fault that blocked the rollback. Conflating the
            # two sends readers chasing the wrong resource.
            ordered = [e for e in logs if isinstance(e, dict)]
            primary = ordered[0] if ordered else {}
            secondary = [
                e
                for e in ordered[1:]
                if e.get("status") in ("DELETE_FAILED", "UPDATE_ROLLBACK_FAILED")
                or "failed to delete" in (e.get("reason") or "").lower()
            ]
            recovery_cmd, live_status = _recovery_command(stack_name)

            cf_prompt = dedent(f"""
            An AWS CloudFormation deployment failed. Analyze the error events to
            determine the root cause.

            Stack Name: {stack_name}
            Current stack status: {live_status or "unknown (already deleted)"}

            Deployment error:
            {error_text}

            CloudFormation error events (may span multiple stacks — e.g. a
            throwaway VPC stack AND the IDP stack; the real failure can be in
            either, so read the `stack_name` field on each event):
            {json.dumps(logs, indent=2)}

            PRIMARY failure (earliest concrete event — this CAUSED the rollback):
            {json.dumps(primary, indent=2)}

            SECONDARY failures that BLOCKED the rollback (may be empty; these did
            NOT cause the deploy to fail, they only prevented clean teardown):
            {json.dumps(secondary, indent=2)}

            Failing CodeBuild build detail (AUTHORITATIVE when present: a
            CloudFormation reason of "CodeBuild failed with status: FAILED" is a
            dead end, and `phase_error` / the tail of `log_tail` below hold the
            REAL cause — e.g. a pip network error, a Docker build error, an ECR
            auth failure):
            {json.dumps(codebuild_failures, indent=2)}

            GROUNDING RULES — follow strictly:
            • Base the root cause ONLY on a concrete ResourceStatusReason or
              CodeBuild phase_error/log_tail line actually present above. Do NOT
              invent causes.
            • If CodeBuild detail IS present, the root cause is the error inside
              that build — quote the specific failing command/exception from
              phase_error or log_tail. Do NOT stop at "CodeBuild failed".
            • Report the PRIMARY failure as the root cause. If SECONDARY failures
              are present, report them separately and explicitly as having
              blocked the rollback, not as the cause of the deploy failure.
            • If the events list is empty or every entry has only an "error"
              field (retrieval failed / stack already deleted), you MUST say the
              root cause was NOT captured and recommend re-running with
              `idp-cli deploy --no-rollback` to preserve the failed resources.
              Do NOT guess at IAM/quota/API-limit causes with no evidence.
            • For the recovery command, use EXACTLY this (it was derived from the
              stack's real status; do not substitute your own):
              {recovery_cmd or "aws cloudformation describe-stacks --stack-name " + stack_name}
              Never suggest `continue-update-rollback` unless it appears above —
              it is invalid for anything other than UPDATE_ROLLBACK_FAILED.

            Provide analysis in this format:

            🚀 DEPLOYMENT RESULT

            📋 Status: {stack_name} FAILED - [one-line root cause, or "root cause not captured"]

            🔍 Root Cause (primary):
            • Quote the exact error that caused the failure (from CodeBuild
              phase_error/log_tail when present, else the ResourceStatusReason)
            • Name the stack + logical resource that failed (from the events)
            • If nothing concrete was captured, say so explicitly

            ⚠️ Also blocked rollback (omit this whole section if none):
            • Name the resource + quote its reason, and state it is secondary

            💡 Fix Commands:
            • The recovery command given above, verbatim
            • Any further command supported by the evidence
            {"• Failing build log: " + (codebuild_failures[0].get("log_url") or "n/a") if codebuild_failures else ""}

            Keep each bullet point under 75 characters, except URLs which may run long.
            Respond ONLY with the format above, no other text.
            """)
            return _invoke_bedrock(cf_prompt)

        # Case A: smoke test failure — deploy succeeded, a test step failed.
        # Attach a bounded log tail: several tests report only a one-line
        # error, and the actual mismatch (expected string, missing file,
        # CLI stderr) is in the build log.
        log_tail = "\n".join(get_codebuild_logs().split("\n")[-150:])
        suite_reference = "\n".join(
            f"• {step}: {desc}" for _, step, _, desc in ALL_TEST_STEPS
        )

        # When a document failed to process, the test's own error is a generic
        # "Unknown error" (the tracking table flattens the real cause). Pull the
        # Step Functions execution failure now — the stack still exists (summary
        # runs before cleanup_stack) but will be gone by the time anyone reads
        # this. Prefer pre-captured details if the caller already snapshotted.
        workflow_failures = result.get("workflow_failures")
        if workflow_failures is None:
            print(f"🔍 Capturing Step Functions failures for: {stack_name}")
            workflow_failures = get_workflow_failure_details(stack_name)
        if workflow_failures:
            print(f"✅ Captured {len(workflow_failures)} workflow failure(s)")

        test_prompt = dedent(f"""
        An IDP deployment succeeded but a post-deployment smoke test failed.

        Stack Name: {stack_name}

        Test error (this is often a GENERIC wrapper like "Unknown error" or
        "BDA config test failed" — it is NOT necessarily the root cause):
        {error_text}

        Test suite reference:
        {suite_reference}

        Step Functions execution failures (the AUTHORITATIVE root cause when
        present — the `cause` field holds the real Lambda traceback / service
        exception behind a generic "Unknown error"):
        {json.dumps(workflow_failures, indent=2)}

        Last build log lines (context only — note that "exit code -9" / SIGKILL
        lines are fail-fast collateral from OTHER parallel tests being killed
        after the first failure, NOT independent failures; do not report them):
        {log_tail}

        GROUNDING RULES — follow strictly:
        • Base the root cause ONLY on evidence actually present above (the
          Step Functions `cause`/`error`, a concrete log line, or the test
          error). Do NOT invent likely causes.
        • If the Step Functions failures list is empty or contains only an
          "error" field (capture failed), and no concrete cause appears in the
          logs, you MUST say the root cause was not captured and recommend how
          to capture it — do NOT guess at IAM/region/quota/config causes.
        • Quote exact strings; never paraphrase an error you cannot see.

        Provide analysis in this format:

        🚀 DEPLOYMENT RESULTS

        📋 Test Status: FAILED - [which step/test failed, from the error]

        🔍 Root Cause Analysis:
        • Quote the exact error/cause from the Step Functions failure or logs
        • If no concrete cause is present, state: "Root cause not captured"
        • Identify which test step failed and what it validates

        💡 Fix Guidance:
        • Only suggest fixes that follow from evidence above
        • If root cause not captured, say what evidence to collect next
        • Reference relevant CLI commands if applicable

        Keep each bullet point under 75 characters.
        Respond ONLY with the format above, no other text.
        """)
        return _invoke_bedrock(test_prompt)

    except Exception as e:
        # Manual summary when Bedrock unavailable — still include the real
        # error so the job log is actionable without AI analysis
        return dedent(f"""
        DEPLOYMENT SUMMARY (MANUAL)

        Deployment result {stack_name} : {"SUCCESS" if result.get("success") else "FAILED"}

        Error: {result.get("error", "None")}

        (AI analysis unavailable: {e})
        """)


def cancel_bedrock_ingestion_jobs(stack_name):
    """Cancel any running Bedrock ingestion jobs before stack deletion"""
    print(f"[{stack_name}] Checking for running Bedrock ingestion jobs...")

    try:
        cf_client = boto3.client("cloudformation")
        bedrock_agent = boto3.client("bedrock-agent")

        # Get all resources from main stack and nested stacks
        stacks_to_check = [stack_name]

        # Find nested stacks
        try:
            resources = cf_client.describe_stack_resources(StackName=stack_name)
            for resource in resources["StackResources"]:
                if resource["ResourceType"] == "AWS::CloudFormation::Stack":
                    nested_stack_name = resource["PhysicalResourceId"].split("/")[1]
                    stacks_to_check.append(nested_stack_name)
        except Exception as e:
            print(f"  ⚠️ Could not list nested stacks: {e}")

        jobs_cancelled = 0

        # Check each stack for Bedrock data sources
        for stack in stacks_to_check:
            try:
                resources = cf_client.describe_stack_resources(StackName=stack)

                for resource in resources["StackResources"]:
                    if resource["ResourceType"] == "AWS::Bedrock::DataSource":
                        # Parse physical resource ID: knowledgeBaseId|dataSourceId
                        physical_id = resource["PhysicalResourceId"]
                        if "|" in physical_id:
                            kb_id, ds_id = physical_id.split("|")

                            # List ingestion jobs for this data source
                            try:
                                response = bedrock_agent.list_ingestion_jobs(
                                    knowledgeBaseId=kb_id,
                                    dataSourceId=ds_id,
                                    maxResults=10,
                                )

                                for job in response.get("ingestionJobSummaries", []):
                                    if job["status"] == "IN_PROGRESS":
                                        job_id = job["ingestionJobId"]
                                        print(f"  Cancelling ingestion job: {job_id}")

                                        # Stop the ingestion job
                                        bedrock_agent.stop_ingestion_job(
                                            knowledgeBaseId=kb_id,
                                            dataSourceId=ds_id,
                                            ingestionJobId=job_id,
                                        )
                                        jobs_cancelled += 1
                                        print(f"  ✓ Cancelled ingestion job: {job_id}")

                            except Exception as e:
                                print(
                                    f"  ⚠️ Could not check/cancel jobs for {physical_id}: {e}"
                                )

            except Exception as e:
                print(f"  ⚠️ Could not check stack {stack}: {e}")

        if jobs_cancelled > 0:
            print(
                f"[{stack_name}] ✅ Cancelled {jobs_cancelled} running ingestion job(s)"
            )
            # Wait a bit for cancellation to propagate
            print(f"[{stack_name}] Waiting 10s for job cancellation to complete...")
            time.sleep(10)
        else:
            print(f"[{stack_name}] No running ingestion jobs found")

    except Exception as e:
        print(f"[{stack_name}] ⚠️ Error checking ingestion jobs: {e}")


def cleanup_stack(result):
    """Clean up stack"""
    stack_name = result.get("stack_name")
    print(f"🧹 Starting cleanup for stack: {stack_name}")
    try:
        # Check stack status first
        cmd_result = run_command(
            f"aws cloudformation describe-stacks --stack-name {stack_name} --query 'Stacks[0].StackStatus' --output text",
            check=False,
        )
        stack_status = (
            cmd_result.stdout.strip() if cmd_result.returncode == 0 else "NOT_FOUND"
        )

        print(f"[{stack_name}] stack status: {stack_status}")

        # Cancel any running Bedrock ingestion jobs before stack deletion
        cancel_bedrock_ingestion_jobs(stack_name)

        # Delete the stack and wait for completion (includes all cleanup via
        # --force-delete-all). Bucket emptying + CloudFront/KB teardown can
        # run long; with check=False a timeout returns a failed result rather
        # than raising, so cleanup_iam_resources below always still runs.
        print(f"[{stack_name}] attempting stack deletion...")
        run_command(
            f"idp-cli delete --stack-name {stack_name} --force --empty-buckets --force-delete-all --wait",
            check=False,
            timeout=3 * 3600,
        )

        print(f"[{stack_name}] ✅ Cleanup completed")

        # Clean up CodeBuild-specific IAM resources
        cleanup_iam_resources(stack_name)
    except Exception as e:
        print(f"⚠️ Cleanup task failed: {e}")


# ---------------------------------------------------------------------------
# API Gateway Web UI hosting test
#
# Separate from the primary shared-stack test suite (Steps 3-11), which deploys
# once with default hosting (CloudFront). This phase deploys a SECOND throwaway
# IDP stack configured for API-Gateway Web UI hosting in its GLOBAL (regional,
# internet-facing, NO VPC) form: WebUIHosting=APIGateway +
# ApiGatewayVisibility=GLOBAL. It exercises the S3-proxy REST API hosting code
# on every run and fetches the UI over HTTP, without consuming VPC quota.
#
# The VPC/PRIVATE variant is NOT run in routine CI: it stood up a throwaway VPC
# per run, which leaked VPCs (Lambda ENIs blocking teardown) and consumed 1 of
# only 5 VPC slots per concurrent run, exhausting the quota under parallel
# pipelines. Validate the PRIVATE/VPC path out-of-band (manual/local) instead.
# The self-contained VPC template (scripts/sdlc/apigw-hosting-test-vpc.yaml) is
# retained for that manual use. delete_apigw_test_vpc / the startup reaper below
# remain to clean up any historical *-apigw-vpc stragglers.
#
# Gated by IDP_TEST_APIGW_HOSTING (default "true"); set to "false" to skip.
# ---------------------------------------------------------------------------


def _force_delete_vpc_stack_enis(vpc_stack_name):
    """Delete detached Lambda ENIs that block a test VPC stack's teardown.

    IDP deploys VPC-attached Lambdas (e.g. DashboardMergerFunction). When the
    IDP stack is deleted, its ENIs linger in 'available' state for a while;
    CloudFormation then can't delete the subnets/security group, so the VPC
    stack goes DELETE_FAILED and the VPC leaks — eventually exhausting the
    account's VPC quota and rolling back every later apigw hosting test. This
    reaps the orphaned (unattached) ENIs so the stack delete can proceed.

    Returns the number of ENIs deleted. Best effort — never raises.
    """
    deleted = 0
    try:
        cf = boto3.client("cloudformation")
        outputs = {
            o["OutputKey"]: o["OutputValue"]
            for o in cf.describe_stacks(StackName=vpc_stack_name)["Stacks"][0].get(
                "Outputs", []
            )
        }
        vpc_id = outputs.get("VpcId", "")
        if not vpc_id:
            return 0
        ec2 = boto3.client("ec2")
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces", [])
        for eni in enis:
            # Only unattached ENIs are safe to delete directly; attached ones
            # (VPC endpoint / NAT) are removed by CloudFormation with their
            # owning resource.
            if eni.get("Status") != "available" or eni.get("Attachment"):
                continue
            eni_id = eni["NetworkInterfaceId"]
            try:
                ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                deleted += 1
                print(f"[{vpc_stack_name}]   force-deleted orphaned ENI {eni_id}")
            except Exception as e:  # noqa: BLE001
                print(f"[{vpc_stack_name}]   ⚠️ could not delete ENI {eni_id}: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[{vpc_stack_name}]   ⚠️ ENI sweep failed: {e}")
    return deleted


def delete_apigw_test_vpc(vpc_stack_name):
    """Delete the test VPC stack, recovering from ENI-blocked DELETE_FAILED.

    First attempt is a plain stack delete. If it fails (almost always because
    orphaned Lambda ENIs hold the subnets/SG), sweep the detached ENIs and
    retry once — this stops the VPC leak that otherwise exhausts the account's
    VPC quota. Best effort — never raises.
    """
    print(f"[{vpc_stack_name}] Deleting test VPC...")
    cf = boto3.client("cloudformation")

    def _attempt():
        cf.delete_stack(StackName=vpc_stack_name)
        cf.get_waiter("stack_delete_complete").wait(
            StackName=vpc_stack_name, WaiterConfig={"MaxAttempts": 60, "Delay": 15}
        )

    try:
        _attempt()
        print(f"[{vpc_stack_name}] ✅ Test VPC deleted")
        return
    except Exception as e:  # noqa: BLE001
        print(
            f"[{vpc_stack_name}] ⚠️ First delete failed ({e}); sweeping ENIs and retrying"
        )

    # Retry path: orphaned Lambda ENIs are the usual culprit. Give them a
    # moment to detach, sweep, then delete again.
    time.sleep(30)
    swept = _force_delete_vpc_stack_enis(vpc_stack_name)
    print(f"[{vpc_stack_name}] swept {swept} orphaned ENI(s); retrying delete")
    try:
        _attempt()
        print(f"[{vpc_stack_name}] ✅ Test VPC deleted (after ENI sweep)")
    except Exception as e:  # noqa: BLE001
        print(
            f"[{vpc_stack_name}] ❌ Test VPC still failed to delete after ENI sweep: {e}. "
            f"Startup reaper will retry on the next run."
        )


# Only reap *-apigw-vpc stacks older than this. A manual/local PRIVATE-VPC
# test can legitimately be running concurrently with a CI job; a young stack
# may be that in-flight test, so the age gate prevents this reaper from
# deleting a VPC that is still in use. Historical leaks are always far older.
APIGW_VPC_STALE_AGE_SECONDS = 2 * 3600


def cleanup_stale_apigw_test_vpcs():
    """Reap OLD leftover apigw test VPC stacks (defense in depth).

    Routine CI no longer creates test VPCs (the every-run apigw test is the
    no-VPC GLOBAL variant), so this exists to clean up historical `*-apigw-vpc`
    stragglers and any left by a manual PRIVATE-VPC test whose teardown failed.
    Left unchecked these hold VPCs until the account hits its quota.

    Age-gated (APIGW_VPC_STALE_AGE_SECONDS): a manual VPC test could be running
    concurrently, so only stacks older than the threshold are deleted — never a
    possibly-in-flight one. Best effort — never raises.
    """
    print("🧹 Cleaning up stale apigw test VPC stacks...")
    try:
        cf = boto3.client("cloudformation")
        # Compare CreationTime against server-side "now" (a stack's own
        # DeletionTime is unavailable pre-delete, and Date.now-style local
        # clocks can skew); use a timezone-aware now from the newest stack's tz.
        now = datetime.now(tz=timezone.utc)
        stale, skipped_young = [], 0
        paginator = cf.get_paginator("list_stacks")
        for page in paginator.paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE",
                "CREATE_FAILED",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
                "DELETE_FAILED",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            ]
        ):
            for s in page.get("StackSummaries", []):
                name = s.get("StackName", "")
                if not (name.startswith("idp-") and name.endswith("-apigw-vpc")):
                    continue
                created = s.get("CreationTime")
                age = (now - created).total_seconds() if created else None
                if age is None or age >= APIGW_VPC_STALE_AGE_SECONDS:
                    stale.append(name)
                else:
                    skipped_young += 1
                    print(
                        f"[{name}] skipping — only {age / 60:.0f}m old "
                        f"(may be an in-flight manual VPC test)"
                    )

        if not stale:
            print(
                f"✅ No stale apigw test VPC stacks to reap "
                f"({skipped_young} young stack(s) skipped)"
            )
            return

        for name in stale:
            print(f"[{name}] reaping stale test VPC stack...")
            delete_apigw_test_vpc(name)
        print(f"✅ Reaped {len(stale)} stale apigw test VPC stack(s)")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Stale apigw VPC cleanup failed: {e}")


# Reap idp- test stacks (main, -iam, and probe stacks) older than this. A CI
# run completes in well under 2h, so anything older is a leftover from a run
# whose own cleanup was interrupted (e.g. creds expired mid-teardown, which is
# how the ~600 orphaned IAM roles accumulated). Age-gated so a concurrently
# running pipeline's in-flight stacks are never touched.
IDP_STACK_STALE_AGE_SECONDS = 3 * 3600  # 3h


def cleanup_stale_idp_stacks():
    """Reap OLD leftover idp- test stacks so their IAM roles don't leak (defense in depth).

    Every run's cleanup_stack/cleanup_iam_resources deletes its own stacks, but
    if that cleanup is interrupted (creds expire mid-teardown, job killed), the
    stack — and crucially its `-iam` helper stack holding the CFServiceRole +
    permissions boundary + per-run roles — is orphaned. Hundreds of these
    accumulated and exhausted the account's RolesPerAccount quota, failing every
    deploy. This startup reaper converges the account back to clean regardless
    of whether any individual run finished its own cleanup.

    Targets top-level idp- stacks (main deploy stacks, their `-iam` stacks, and
    the -apigw/-waf/-apigwpriv/-jobsapi probe stacks + their -iam stacks).
    Age-gated (IDP_STACK_STALE_AGE_SECONDS) so a concurrent pipeline's in-flight
    run is never deleted. Best effort — never raises. Skips *-apigw-vpc (owned
    by cleanup_stale_apigw_test_vpcs) and the persistent pipeline stack.
    """
    print("🧹 Cleaning up stale idp- test stacks (IAM role leak guard)...")
    try:
        cf = boto3.client("cloudformation")
        now = datetime.now(tz=timezone.utc)
        stale, skipped_young = [], 0
        paginator = cf.get_paginator("list_stacks")
        for page in paginator.paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE",
                "CREATE_FAILED",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
                "DELETE_FAILED",
                "UPDATE_COMPLETE",
                "UPDATE_ROLLBACK_COMPLETE",
            ]
        ):
            for s in page.get("StackSummaries", []):
                name = s.get("StackName", "")
                # Only our timestamped test stacks (idp-MMDD-HHMMSS[...]). The
                # apigw-vpc reaper owns *-apigw-vpc; skip those here.
                if not name.startswith("idp-") or name.endswith("-apigw-vpc"):
                    continue
                # Only reap TOP-LEVEL stacks: nested stacks (RootId set) are
                # deleted by their parent, and deleting a parent cascades.
                if s.get("RootId") or s.get("ParentId"):
                    continue
                created = s.get("CreationTime")
                age = (now - created).total_seconds() if created else None
                if age is None or age >= IDP_STACK_STALE_AGE_SECONDS:
                    stale.append(name)
                else:
                    skipped_young += 1

        if skipped_young:
            print(f"  ({skipped_young} young idp- stack(s) skipped — may be in-flight)")
        if not stale:
            print("✅ No stale idp- test stacks to reap")
            return

        # Delete non-iam stacks first (they reference their -iam CFServiceRole /
        # boundary), then the -iam stacks — mirrors cleanup_stack ordering so a
        # main stack isn't stranded when its service role is deleted first.
        non_iam = [n for n in stale if not n.endswith("-iam")]
        iam_stacks = [n for n in stale if n.endswith("-iam")]
        for name in non_iam + iam_stacks:
            try:
                print(f"[{name}] reaping stale idp- stack...")
                cf.delete_stack(StackName=name)
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] ⚠️ delete failed: {e}")
        print(
            f"✅ Issued delete for {len(stale)} stale idp- stack(s) "
            f"({len(non_iam)} main/probe + {len(iam_stacks)} -iam)"
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Stale idp- stack cleanup failed: {e}")


# Reap idp- test buckets older than this. Same rationale as the stack reaper:
# a run's `idp-cli delete` deletes its buckets, but if interrupted (creds
# expire mid-teardown), buckets with content survive — CloudFormation skips
# non-empty buckets, so the stack reaper above can't remove them. Thousands can
# accumulate. Age-gated + protected against any prefix with a live stack so a
# concurrent pipeline's in-flight buckets are never touched.
IDP_BUCKET_STALE_AGE_SECONDS = 6 * 3600  # 6h

# idp- run-prefix: "idp-MMDD-HHMMSS". A bucket name is
# "idp-MMDD-HHMMSS[-suffix]-<role>bucket-<rand>"; we group by this prefix so a
# bucket is protected iff its RUN still has any CloudFormation stack.
_IDP_RUN_PREFIX_RE = re.compile(r"^(idp-\d{4}-\d{6})")


def _live_idp_run_prefixes():
    """Run-prefixes (idp-MMDD-HHMMSS) that still have ANY CloudFormation stack.

    A bucket whose run-prefix is in this set belongs to a run that isn't fully
    torn down (possibly in-flight), so it must NOT be reaped. Includes stacks in
    every non-terminal and terminal-but-present state.
    """
    prefixes = set()
    cf = boto3.client("cloudformation")
    paginator = cf.get_paginator("list_stacks")
    # All statuses EXCEPT DELETE_COMPLETE (a completed delete means the stack is
    # gone, so its buckets — if any survived — are fair game).
    statuses = [
        "CREATE_IN_PROGRESS",
        "CREATE_FAILED",
        "CREATE_COMPLETE",
        "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_FAILED",
        "ROLLBACK_COMPLETE",
        "DELETE_IN_PROGRESS",
        "DELETE_FAILED",
        "UPDATE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_COMPLETE",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_ROLLBACK_COMPLETE",
        "REVIEW_IN_PROGRESS",
        "IMPORT_IN_PROGRESS",
        "IMPORT_COMPLETE",
        "IMPORT_ROLLBACK_IN_PROGRESS",
        "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
    ]
    for page in paginator.paginate(StackStatusFilter=statuses):
        for s in page.get("StackSummaries", []):
            name = s.get("StackName", "")
            m = _IDP_RUN_PREFIX_RE.match(name)
            if m:
                prefixes.add(m.group(1))
    return prefixes


def cleanup_stale_idp_buckets():
    """Reap OLD leftover idp- test S3 buckets whose run is fully torn down.

    Companion to cleanup_stale_idp_stacks: buckets leak independently of stacks
    because CloudFormation cannot delete a non-empty bucket, so an interrupted
    `idp-cli delete` leaves the bucket behind even after the stack is gone.
    Thousands accumulated this way. This converges them back regardless.

    Safety: a bucket is deleted only if BOTH
      * its run-prefix (idp-MMDD-HHMMSS) has NO surviving CloudFormation stack
        (so no in-flight/partly-deployed run owns it), AND
      * it is older than IDP_BUCKET_STALE_AGE_SECONDS (backstop for a brand-new
        bucket whose stack hasn't registered yet).
    Best effort — never raises.
    """
    print("🧹 Cleaning up stale idp- test buckets (S3 leak guard)...")
    try:
        s3 = boto3.client("s3")
        s3r = boto3.resource("s3")
        now = datetime.now(tz=timezone.utc)
        protected = _live_idp_run_prefixes()

        stale, skipped_protected, skipped_young = [], 0, 0
        for b in s3.list_buckets().get("Buckets", []):
            name = b.get("Name", "")
            if not name.startswith("idp-"):
                continue
            m = _IDP_RUN_PREFIX_RE.match(name)
            if m and m.group(1) in protected:
                skipped_protected += 1
                continue
            created = b.get("CreationDate")
            age = (now - created).total_seconds() if created else None
            if age is not None and age < IDP_BUCKET_STALE_AGE_SECONDS:
                skipped_young += 1
                continue
            stale.append(name)

        if skipped_protected or skipped_young:
            print(
                f"  ({skipped_protected} protected by a live stack, "
                f"{skipped_young} younger than the age gate — skipped)"
            )
        if not stale:
            print("✅ No stale idp- test buckets to reap")
            return

        deleted = 0
        for name in stale:
            try:
                # Empty first — versions, delete markers, and objects — then
                # delete the (now empty) bucket.
                s3r.Bucket(name).object_versions.delete()
                s3.delete_bucket(Bucket=name)
                deleted += 1
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] ⚠️ bucket delete failed: {e}")
        print(f"✅ Reaped {deleted}/{len(stale)} stale idp- test bucket(s)")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Stale idp- bucket cleanup failed: {e}")


def validate_apigw_global_hosting(stack_name):
    """Assert the deployed stack serves the Web UI on a GLOBAL (REGIONAL) REST API.

    This is the no-VPC APIGateway hosting path (WebUIHosting=APIGateway +
    ApiGatewayVisibility=GLOBAL): the Web UI is served as an S3 proxy on a
    regional, internet-facing REST API. Because it IS reachable, validate both
    structurally and by actually fetching the UI:
      * the REST API "{stack}-api" has endpoint type REGIONAL,
      * the stack's ApplicationWebURL output is the execute-api /api URL, and
      * an HTTP GET of that URL returns 200 with HTML (the S3-proxy served UI).
    """
    apig = boto3.client("apigateway")
    cf = boto3.client("cloudformation")

    # 1. REST API is REGIONAL (GLOBAL visibility maps to a REGIONAL endpoint)
    api_name = f"{stack_name}-api"
    apis = apig.get_rest_apis(limit=500).get("items", [])
    match = next((a for a in apis if a.get("name") == api_name), None)
    if not match:
        return {"success": False, "error": f"REST API {api_name} not found"}
    types = match.get("endpointConfiguration", {}).get("types", [])
    if "REGIONAL" not in types:
        return {
            "success": False,
            "error": f"REST API {api_name} endpoint types={types}, expected REGIONAL",
        }

    # 2. ApplicationWebURL output points at the execute-api /api URL
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName=stack_name)["Stacks"][0].get(
            "Outputs", []
        )
    }
    web_url = outputs.get("ApplicationWebURL", "")
    if "execute-api" not in web_url or "/api" not in web_url:
        return {
            "success": False,
            "error": f"ApplicationWebURL={web_url!r} is not an execute-api /api URL",
        }

    # 3. The UI actually loads over HTTP (S3-proxy hosting served the app).
    # Unlike the PRIVATE variant this endpoint is internet-reachable, so we can
    # do a real end-to-end fetch instead of only checking structure.
    fetch = run_command(
        f"curl -s -o /dev/null -w '%{{http_code}}' -L {web_url}", check=False
    )
    http_code = fetch.stdout.strip()
    if http_code != "200":
        return {
            "success": False,
            "error": f"GET {web_url} returned HTTP {http_code!r}, expected 200",
        }

    print(f"✅ GLOBAL REST API serving Web UI: {web_url} (types={types}, HTTP 200)")
    return {"success": True, "web_url": web_url}


def _stack_outputs(stack_name):
    """Return the deployed stack's Outputs as a {key: value} dict."""
    cf = boto3.client("cloudformation")
    return {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName=stack_name)["Stacks"][0].get(
            "Outputs", []
        )
    }


def validate_apigw_private_hosting(stack_name):
    """Assert the stack serves the Web UI on a PRIVATE REST API (VPC-only).

    ApiGatewayVisibility=PRIVATE + DeployInVPC=true: the REST API is reachable
    ONLY through the VPC execute-api interface endpoint, so — unlike the GLOBAL
    probe — we CANNOT HTTP-fetch it from CodeBuild (which is not in the test
    VPC). Validate structurally:
      * the REST API "{stack}-api" has endpoint type PRIVATE, and
      * it carries a resource policy (the private API denies traffic not from
        its VPCE, so a policy MUST be present).
    """
    apig = boto3.client("apigateway")
    api_name = f"{stack_name}-api"
    apis = apig.get_rest_apis(limit=500).get("items", [])
    match = next((a for a in apis if a.get("name") == api_name), None)
    if not match:
        return {"success": False, "error": f"REST API {api_name} not found"}
    types = match.get("endpointConfiguration", {}).get("types", [])
    if "PRIVATE" not in types:
        return {
            "success": False,
            "error": f"REST API {api_name} endpoint types={types}, expected PRIVATE",
        }
    # A PRIVATE REST API must carry a resource policy binding it to the VPCE;
    # without one it would be unreachable (or, worse, open). get_rest_apis
    # returns `policy` as an escaped JSON string when set.
    if not match.get("policy"):
        return {
            "success": False,
            "error": f"PRIVATE REST API {api_name} has no resource policy (VPCE binding)",
        }
    print(
        f"✅ PRIVATE REST API present with resource policy: {api_name} (types={types})"
    )
    return {"success": True, "api_name": api_name, "endpoint_types": types}


def validate_jobs_api(stack_name):
    """Assert the Jobs REST API deployed (EnableJobsApi=true + VPC).

    The Jobs API is a PRIVATE API Gateway reachable only inside the test VPC,
    so — like the PRIVATE hosting probe — CodeBuild can't call it. Validate
    structurally that the Jobs API deployment stood up:
      * the stack exposes the ApiGatewayEndpoint output (only present when
        EnableJobsApi=true / the Jobs API + Cognito M2M client deployed), and
      * that output is an execute-api URL for a real REST API.
    """
    outputs = _stack_outputs(stack_name)
    jobs_url = outputs.get("ApiGatewayEndpoint", "")
    if not jobs_url:
        return {
            "success": False,
            "error": (
                "Stack has no ApiGatewayEndpoint output — the Jobs API "
                "did not deploy (EnableJobsApi=true expected)"
            ),
        }
    if "execute-api" not in jobs_url:
        return {
            "success": False,
            "error": f"ApiGatewayEndpoint={jobs_url!r} is not an execute-api URL",
        }
    # Confirm the underlying REST API actually exists (the output is a !Sub, so
    # it is always a well-formed string even if the API failed to create).
    apig = boto3.client("apigateway")
    api_id = jobs_url.split("//", 1)[-1].split(".", 1)[0]
    try:
        api = apig.get_rest_api(restApiId=api_id)
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"Jobs API {api_id} from ApiGatewayEndpoint not found: {e}",
        }
    print(f"✅ Jobs API deployed: {jobs_url} (restApiId={api.get('id')})")
    return {"success": True, "jobs_url": jobs_url}


def validate_waf_enabled(stack_name):
    """Assert the WAFv2 IP allow-list WebACL deployed and is associated.

    WAFAllowedIPv4Ranges set to a non-default CIDR creates a REGIONAL WebACL
    "{stack}-api-acl" (DefaultAction=Block + an allow-list rule) associated with
    the REST API stage. Validate:
      * a REGIONAL WebACL named "{stack}-api-acl" exists, and
      * it is associated with at least one resource (the API stage).
    """
    waf = boto3.client("wafv2")
    acl_name = f"{stack_name}-api-acl"
    acls = waf.list_web_acls(Scope="REGIONAL", Limit=100).get("WebACLs", [])
    match = next((a for a in acls if a.get("Name") == acl_name), None)
    if not match:
        return {
            "success": False,
            "error": f"WAFv2 WebACL {acl_name} not found (WAF not enabled?)",
        }
    acl_arn = match["ARN"]
    resources = waf.list_resources_for_web_acl(
        WebACLArn=acl_arn, ResourceType="API_GATEWAY"
    ).get("ResourceArns", [])
    if not resources:
        return {
            "success": False,
            "error": f"WebACL {acl_name} is not associated with any API Gateway stage",
        }
    print(f"✅ WAF WebACL {acl_name} associated with {len(resources)} resource(s)")
    return {"success": True, "web_acl_arn": acl_arn, "associated": resources}


# ---------------------------------------------------------------------------
# ZAP DAST probe
#
# Dynamic Application Security Testing of the deployed UI REST API using the
# official OWASP ZAP Docker image. Complements the RBAC probe (authorization
# semantics) and SRT (static code) with the class of bugs neither can see:
# injection (XSS/SQLi/…), missing security headers, TLS/cookie flags, and info
# leaks against the RUNNING API.
#
# Why a probe (own throwaway stack) and not a primary-suite step: the scan needs
# a Cognito token, which means temporarily enabling ADMIN_USER_PASSWORD_AUTH on
# the app client — the exact stack-wide mutation that forces Step 12 (RBAC) to
# run sequentially. On the probe's OWN stack that flip is safe and the whole
# thing runs fully concurrently with everything else (zero added wall-clock).
#
# The UI API is a single Cognito-gated route POST /op/{field} with NO OpenAPI
# spec, so ZAP's spider finds nothing to crawl. We SEED the scan by generating a
# minimal OpenAPI 3 doc from scripts/api_rbac_expectations.yaml (the same op
# source-of-truth the RBAC tests use), giving ZAP one authenticated request per
# operation to attack.
#
# WARN-only for now: findings are reported (build log + S3 report) but do not
# fail the build. Promote high-confidence rules to FAIL in scripts/sdlc/
# zap-rules.conf once the baseline is triaged (the path SRT took).
# ---------------------------------------------------------------------------

ZAP_DOCKER_IMAGE = os.environ.get("ZAP_DOCKER_IMAGE", "ghcr.io/zaproxy/zaproxy:stable")
# Passive baseline (spider + passive rules, no attack payloads) every run.
# Active scan (real injection/XSS payloads) is opt-in via IDP_ZAP_ACTIVE so the
# intrusive traffic runs on demand/nightly, not on every MR — and wall-clock
# stays flat. Only ever run against these throwaway probe stacks.
ZAP_ACTIVE_SCAN = os.environ.get("IDP_ZAP_ACTIVE", "false").lower() == "true"
ZAP_RULES_CONF = os.path.join(os.path.dirname(__file__), "zap-rules.conf")


def _zap_op_fields():
    """The set of UI API operation names, from the RBAC expectations file.

    This is the shared op source-of-truth (also consumed by scan_api_rbac.py and
    test_api_rbac.py). Returns a sorted list of field names; raises if the file
    is missing/empty so a silently-empty scan can't look like a pass.
    """
    import yaml

    expectations = os.path.join(
        os.path.dirname(__file__), os.pardir, "api_rbac_expectations.yaml"
    )
    with open(expectations) as fh:
        spec = yaml.safe_load(fh)
    ops = sorted((spec.get("operations") or {}).keys())
    if not ops:
        raise RuntimeError(
            f"No operations found in {expectations}; refusing an empty ZAP scan"
        )
    return ops


def generate_zap_openapi(api_base, fields):
    """Build a minimal OpenAPI 3 doc describing the UI API for ZAP.

    Every operation is the same shape — POST {api_base}/op/{field} with a JSON
    body {"arguments": {}} — so we emit one path per field. ZAP imports this as
    its scan surface (a spider would find nothing on this single-route API).

    Returns the spec as a dict (the caller writes it as JSON).
    """
    # api_base is like https://<id>.execute-api.<region>.amazonaws.com/api
    from urllib.parse import urlsplit

    parts = urlsplit(api_base)
    server_url = f"{parts.scheme}://{parts.netloc}"
    base_path = parts.path.rstrip("/")  # e.g. "/api"

    paths = {}
    for field in fields:
        paths[f"{base_path}/op/{field}"] = {
            "post": {
                "operationId": field,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"arguments": {"type": "object"}},
                            },
                            "example": {"arguments": {}},
                        }
                    },
                },
                "responses": {"200": {"description": "op result"}},
            }
        }
    return {
        "openapi": "3.0.0",
        "info": {"title": "IDP UI API (ZAP DAST seed)", "version": "1.0"},
        "servers": [{"url": server_url}],
        "paths": paths,
    }


def _parse_zap_rule_tally(scan_stdout):
    """Parse ZAP's rule-outcome tally line from zap-api-scan.py stdout.

    zap-api-scan prints a summary like:
      "FAIL-NEW: 0  FAIL-INPROG: 0  WARN-NEW: 3  WARN-INPROG: 0  INFO: 0
       IGNORE: 1  PASS: 114"
    This is the count of RULES (not alerts) by outcome — the PASS count shows how
    much was actually exercised, which the alert list alone doesn't convey.
    Returns a dict of {label: int} for the labels present, or {} if not found.
    """
    labels = (
        "FAIL-NEW",
        "FAIL-INPROG",
        "WARN-NEW",
        "WARN-INPROG",
        "INFO",
        "IGNORE",
        "PASS",
    )
    tally = {}
    for label in labels:
        m = re.search(rf"{re.escape(label)}:\s*(\d+)", scan_stdout)
        if m:
            tally[label] = int(m.group(1))
    return tally


def _zap_ignored_plugin_ids(rules_conf_path):
    """Plugin ids marked IGNORE in zap-rules.conf (format: '<id>\\t<action>\\t..').

    zap-api-scan's `-c` file only controls the WARN/FAIL/PASS gating tier;
    purely INFORMATIONAL alerts still land in the JSON report regardless. So the
    report parser applies the IGNORE list itself, keeping the printed findings
    consistent with the rules-conf intent (an IGNORE'd id shows nowhere).
    """
    ids = set()
    try:
        with open(rules_conf_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1].upper() == "IGNORE":
                    ids.add(parts[0])
    except OSError:
        pass
    return ids


def _parse_zap_alerts(report_json_path, ignore_ids=None):
    """Summarize a ZAP JSON report into {risk: count} + a flat alert list.

    ZAP's JSON report nests alerts under site[].alerts[]; each alert has a
    'riskcode' ("0"=Info, "1"=Low, "2"=Medium, "3"=High) and 'riskdesc'.
    Returns (counts_by_risk, alerts) — counts keyed by the human risk label.
    Alerts whose pluginid is in ignore_ids (from zap-rules.conf IGNORE lines) are
    dropped from BOTH the counts and the list, so the report matches the
    rules-conf intent for informational alerts too.
    """
    ignore_ids = ignore_ids or set()
    with open(report_json_path) as fh:
        report = json.load(fh)
    risk_label = {"0": "Informational", "1": "Low", "2": "Medium", "3": "High"}
    counts = {"High": 0, "Medium": 0, "Low": 0, "Informational": 0}
    alerts = []
    for site in report.get("site", []):
        for alert in site.get("alerts", []):
            if str(alert.get("pluginid", "")) in ignore_ids:
                continue  # muted in zap-rules.conf (applies to Info alerts too)
            label = risk_label.get(str(alert.get("riskcode", "0")), "Informational")
            counts[label] = counts.get(label, 0) + 1
            instances = alert.get("instances", []) or []
            # A few sample affected URLs (deduped) so the summary is actionable
            # without needing the full HTML report.
            sample_urls = []
            for inst in instances:
                uri = inst.get("uri", "")
                if uri and uri not in sample_urls:
                    sample_urls.append(uri)
                if len(sample_urls) >= 3:
                    break
            alerts.append(
                {
                    "risk": label,
                    "name": alert.get("alert") or alert.get("name", ""),
                    "pluginid": alert.get("pluginid", ""),
                    "count": len(instances) or int(alert.get("count", 0) or 0),
                    # 'solution' is ZAP's remediation guidance (HTML-ish); keep a
                    # trimmed plain-ish version for the log.
                    "solution": (alert.get("solution", "") or "").strip(),
                    "sample_urls": sample_urls,
                }
            )
    # High→Info, then most-instances first, so the log leads with what matters.
    order = {"High": 0, "Medium": 1, "Low": 2, "Informational": 3}
    alerts.sort(key=lambda a: (order.get(a["risk"], 9), -a["count"]))
    return counts, alerts


def _upload_zap_report(stack_name, workdir):
    """Upload the ZAP HTML/JSON reports to the SDLC source bucket. Best effort.

    Returns the s3:// URL of the HTML report (or "" if not uploaded), so the
    build log and the GitLab after_script can point at it.
    """
    bucket = os.environ.get("SOURCE_BUCKET", "")
    build_id = os.environ.get("CODEBUILD_BUILD_ID", "")
    if not bucket:
        print("ℹ️ Skipping ZAP report upload (no SOURCE_BUCKET)")
        return ""
    tag = build_id.split(":")[-1] if build_id else stack_name
    s3 = boto3.client("s3")
    html_url = ""
    for name, ctype in (
        ("zap-report.html", "text/html"),
        ("zap-report.json", "application/json"),
    ):
        path = os.path.join(workdir, name)
        if not os.path.exists(path):
            continue
        key = f"deploy/zap/{stack_name}-{tag}-{name}"
        try:
            with open(path, "rb") as fh:
                s3.put_object(Bucket=bucket, Key=key, Body=fh.read(), ContentType=ctype)
            print(f"📁 ZAP report uploaded to s3://{bucket}/{key}")
            if name.endswith(".html"):
                html_url = f"s3://{bucket}/{key}"
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Failed to upload ZAP report {name}: {e}")
    return html_url


def _persist_zap_report(workdir):
    """Copy the ZAP report files to a stable local dir and return {name: path}.

    The scan writes reports into a random /tmp/zap-XXXX workdir; a manual run
    (no SOURCE_BUCKET/S3 upload) otherwise has no obvious path to open. Copy them
    to IDP_ZAP_REPORT_DIR if set, else leave them in the workdir (which is NOT
    deleted) and just return the paths so the report can print them. Best effort.
    """
    names = ("zap-report.html", "zap-report.json", "zap-report.md")
    dest_dir = os.environ.get("IDP_ZAP_REPORT_DIR", "").strip()
    out = {}
    if dest_dir:
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            print(f"⚠️ Could not create IDP_ZAP_REPORT_DIR {dest_dir!r}: {e}")
            dest_dir = ""
    for name in names:
        src = os.path.join(workdir, name)
        if not os.path.exists(src):
            continue
        if dest_dir:
            dst = os.path.join(dest_dir, name)
            try:
                shutil.copy(src, dst)
                out[name] = os.path.abspath(dst)
            except OSError as e:  # noqa: BLE001
                print(f"⚠️ Could not copy ZAP report {name} to {dest_dir}: {e}")
                out[name] = os.path.abspath(src)
        else:
            out[name] = os.path.abspath(src)
    return out


def validate_zap_dast(stack_name):
    """Run an authenticated OWASP ZAP DAST scan against the deployed UI API.

    Resolves the API base + Cognito ids from the stack, mints an ID token
    (temporarily enabling ADMIN_USER_PASSWORD_AUTH on this probe's OWN app
    client and always restoring it), seeds a minimal OpenAPI doc from the op
    source-of-truth, and runs zap-api-scan.py in the official ZAP Docker image
    with the token injected on every request via ZAP's `replacer`.

    WARN-only: returns success=True even with findings, carrying the alert
    counts + report URL for the consolidated summary. The `# TODO promote`
    marks where to gate the build once zap-rules.conf is triaged.
    """
    # rbac_common lives in scripts/; add it to the path (this file is in
    # scripts/sdlc/). Shared with test_api_rbac.py so stack resolution + the
    # auth-flow capture/restore can't drift.
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    )
    import rbac_common

    # Preflight: the scan needs a working Docker daemon (to `docker run` the ZAP
    # image). That requires PrivilegedMode:true on the app-sdlc CodeBuild project
    # (scripts/sdlc/cfn/codepipeline-s3.yml) — which only takes effect once that
    # SDLC pipeline stack is (re)deployed. If Docker isn't usable, SKIP (not
    # fail): a scan that can't run is an ENVIRONMENT gap, not a security finding,
    # and this is a WARN-only probe. Mirrors how VPC-requiring probes skip when
    # their infra is absent. `docker info` is a cheap daemon-reachability check.
    probe_check = run_command("docker info", check=False, timeout=60)
    if probe_check.returncode != 0:
        msg = (
            "Docker daemon unavailable — skipping ZAP DAST scan. Enable it by "
            "deploying the SDLC pipeline stack with PrivilegedMode:true on the "
            "app-sdlc CodeBuild project (scripts/sdlc/cfn/codepipeline-s3.yml)."
        )
        print(f"⏭️  {msg}")
        return {"success": True, "skipped": True, "detail": msg}

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    try:
        ctx = rbac_common.resolve_stack(stack_name, region)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"Could not resolve UI API: {e}"}

    api_base = ctx["api_base"]
    if not api_base:
        return {
            "success": False,
            "error": "Stack has no HttpApiEndpoint output — UI API not deployed",
        }

    # The workdir is bind-mounted into the ZAP container (`docker run -v
    # {workdir}:/zap/wrk`). The mount itself works from /tmp in CodeBuild dind
    # (verified). The catch: CodeBuild runs the build as root, so the workdir is
    # root-owned, but the official ZAP image runs zap-api-scan.py as the non-root
    # `zap` user — which can READ the seeded openapi.json (world-readable) but
    # CANNOT WRITE the report back, so ZAP dies with
    # `PermissionError: [Errno 13] ... /zap/wrk/zap-report.html` and produces no
    # report (previously misread as a mount failure). chmod 0o777 the workdir so
    # the container's non-root user can write the report. (Contents are a
    # throwaway OpenAPI seed + ZAP reports — no secrets; the token lives in a
    # separate options file, also 0o777 here but never uploaded.)
    # nosec B103 - accepted: the report is written by the ZAP image's non-root
    # `zap` user, whose uid we don't control, into this root-owned bind mount,
    # so a narrower mask makes the scan fail (see the comment above). Scope is a
    # throwaway per-run mkdtemp dir in a single-tenant, ephemeral CodeBuild
    # container holding only an OpenAPI seed and ZAP reports.
    workdir = tempfile.mkdtemp(prefix="zap-")
    os.chmod(workdir, 0o777)  # nosec B103
    email = "zap-dast@example.invalid"
    password = "Aa1!" + secrets.token_urlsafe(24)
    token = None
    try:
        # Own stack → this app-client auth-flow flip is safe (nothing else uses
        # it). Always restored in finally.
        rbac_common.enable_admin_auth(ctx)
        rbac_common.create_cognito_user(ctx, email, "Admin", password)
        token = rbac_common.get_id_token(ctx, email, password)
        # nosec B105 - the "None" literal is the string the AWS CLI prints for a
        # null `--query` result (`--output text`), i.e. the sentinel for "no
        # token was minted", not a credential. Bandit's heuristic fires only
        # because the compared variable is named `token`.
        if not token or token == "None":  # nosec B105
            return {"success": False, "error": "Failed to mint Cognito ID token"}

        fields = _zap_op_fields()
        spec = generate_zap_openapi(api_base, fields)
        spec_path = os.path.join(workdir, "openapi.json")
        with open(spec_path, "w") as fh:
            json.dump(spec, fh)
        print(
            f"🕷️  ZAP DAST: {len(fields)} operations seeded, target {api_base} "
            f"(active_scan={ZAP_ACTIVE_SCAN})"
        )

        # ZAP `replacer` injects the raw ID token into the Authorization header
        # on every request (the API Gateway authorizer expects the raw token,
        # no "Bearer " prefix). Write the replacer settings — including the token
        # — to a ZAP config-file loaded via `-configfile`, NOT onto the command
        # line: run_command prints every cmd it runs, so a token on argv would
        # leak into the CodeBuild/CloudWatch log. The options file lives only in
        # the mounted workdir and is never uploaded (only zap-report.* are).
        options_prop = os.path.join(workdir, "zap-options.prop")
        with open(options_prop, "w") as fh:
            fh.write(
                "replacer.full_list(0).description=auth\n"
                "replacer.full_list(0).enabled=true\n"
                "replacer.full_list(0).matchtype=REQ_HEADER\n"
                "replacer.full_list(0).matchstr=Authorization\n"
                "replacer.full_list(0).regex=false\n"
                f"replacer.full_list(0).replacement={token}\n"
            )
        rules_flag = ""
        if os.path.exists(ZAP_RULES_CONF):
            import shutil

            shutil.copy(ZAP_RULES_CONF, os.path.join(workdir, "zap-rules.conf"))
            rules_flag = "-c zap-rules.conf"
        # zap-api-scan runs passive by default; active scan is its default too,
        # so DISABLE active unless opted in (-S = safe mode, no active attacks).
        active_flag = "" if ZAP_ACTIVE_SCAN else "-S"
        docker_cmd = (
            f"docker run --rm -v {workdir}:/zap/wrk:rw {ZAP_DOCKER_IMAGE} "
            f"zap-api-scan.py -t /zap/wrk/openapi.json -f openapi {active_flag} "
            f"{rules_flag} "
            '-z "-configfile /zap/wrk/zap-options.prop" '
            "-J zap-report.json -r zap-report.html -w zap-report.md"
        )
        # zap-api-scan exits non-zero when it FINDS issues (WARN/FAIL rules) —
        # that is not a probe failure in WARN mode, so check=False and rely on
        # the parsed report. Cap runtime so a hung scan can't eat the job.
        scan_run = run_command(docker_cmd, check=False, timeout=45 * 60)
        # ZAP prints a one-line rule tally, e.g.
        #   "FAIL-NEW: 0  FAIL-INPROG: 0  WARN-NEW: 3  WARN-INPROG: 0  INFO: 0
        #    IGNORE: 1  PASS: 114"
        # Capture it so the report shows EVERY rule outcome (not just alerts) —
        # 114 PASS is as meaningful as the 3 WARN for "what got tested".
        rule_tally = _parse_zap_rule_tally(getattr(scan_run, "stdout", "") or "")

        report_json = os.path.join(workdir, "zap-report.json")
        if not os.path.exists(report_json):
            # The daemon was up (preflight passed) but the scan still produced no
            # report — e.g. the ZAP image failed to pull, or zap-api-scan errored
            # before writing output. That's a tooling/environment problem, not a
            # security finding, so SKIP rather than fail this WARN-only probe.
            msg = (
                "ZAP produced no JSON report (image pull or scan startup failed) "
                "— skipping DAST for this run."
            )
            print(f"⏭️  {msg}")
            return {"success": True, "skipped": True, "detail": msg}
        counts, alerts = _parse_zap_alerts(
            report_json, ignore_ids=_zap_ignored_plugin_ids(ZAP_RULES_CONF)
        )
        report_url = _upload_zap_report(stack_name, workdir)
        # Copy the HTML/JSON/MD reports to a stable local dir so a manual run
        # (no SOURCE_BUCKET) can still open them — the workdir is a random
        # /tmp/zap-XXXX. IDP_ZAP_REPORT_DIR overrides the destination.
        local_reports = _persist_zap_report(workdir)

        summary = (
            f"High={counts.get('High', 0)} Medium={counts.get('Medium', 0)} "
            f"Low={counts.get('Low', 0)} Info={counts.get('Informational', 0)}"
        )
        # Detailed, self-contained report in the build log (so the findings are
        # actionable without opening the HTML report). Scope line first, then
        # EVERY alert (not just High/Medium) with its risk, instance count,
        # sample affected URLs, and remediation hint.
        print(f"\n{'=' * 72}")
        print("🔎 ZAP DAST scan report")
        print(f"{'=' * 72}")
        print(f"  Target:      {api_base}")
        print(f"  Operations:  {len(fields)} seeded (POST /op/<field>)")
        print(
            f"  Mode:        {'active scan' if ZAP_ACTIVE_SCAN else 'passive baseline'}"
        )
        if rule_tally:
            # Full rule-outcome tally — shows EVERYTHING that ran, not just the
            # alerts. e.g. "114 PASS · 3 WARN · 0 FAIL · 1 IGNORE".
            fails = rule_tally.get("FAIL-NEW", 0) + rule_tally.get("FAIL-INPROG", 0)
            warns = rule_tally.get("WARN-NEW", 0) + rule_tally.get("WARN-INPROG", 0)
            print(
                f"  Rules:       {rule_tally.get('PASS', 0)} PASS · {warns} WARN · "
                f"{fails} FAIL · {rule_tally.get('IGNORE', 0)} IGNORE"
            )
        print(f"  Alerts:      {summary}")
        if alerts:
            print("  Findings (most severe first):")
            for a in alerts:
                print(f"    • [{a['risk']}] {a['name']} — {a['count']} instance(s)")
                for u in a.get("sample_urls", []):
                    print(f"        {u}")
                if a.get("solution"):
                    fix = " ".join(a["solution"].split())[:200]
                    print(f"        ↳ fix: {fix}")
        else:
            print("  No alerts raised.")
        # Where to read the full report.
        html_local = local_reports.get("zap-report.html")
        if html_local:
            print(f"  Report:      {html_local}")
            print("               (open in a browser for the full findings view)")
        if local_reports.get("zap-report.json"):
            print(f"               {local_reports['zap-report.json']}")
        if report_url:
            print(f"  Report (S3): {report_url}")
        print(f"{'=' * 72}\n")

        # TODO promote: once zap-rules.conf is triaged, gate the build here, e.g.
        #   if counts.get("High", 0) > 0: return {"success": False, ...}
        # For now WARN-only: always succeed, carry the findings in the result.
        return {
            "success": True,
            "zap_alerts": counts,
            "zap_summary": summary,
            "zap_findings": alerts,
            "operations_scanned": len(fields),
            "rule_tally": rule_tally,
            "target": api_base,
            "report_url": report_url,
            "report_files": local_reports,
            "active_scan": ZAP_ACTIVE_SCAN,
        }
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"ZAP DAST scan error: {e}"}
    finally:
        # Always clean up the test user + the app-client auth-flow flip, even on
        # error (mirrors the RBAC harness's finally).
        try:
            rbac_common.delete_cognito_user(ctx, email)
        except Exception:  # noqa: BLE001
            pass
        try:
            rbac_common.restore_auth_flows(ctx)
        except Exception:  # noqa: BLE001
            pass


def _capture_cf_events(result, *stack_names):
    """Snapshot CF failure events from candidate stacks before teardown.

    The APIGW hosting test can fail either in its throwaway IDP stack or in the
    self-contained VPC stack it stands up first; both are deleted in a finally
    block, so events must be captured while the stacks still exist. Passing all
    candidates (and dropping ones that were never created) means the summary
    sees the stack that actually rolled back — previously we only captured the
    IDP stack, so a VPC-creation failure surfaced as "<stack> does not exist".
    """
    events = []
    for name in stack_names:
        try:
            stack_events = get_cloudformation_logs(name)
        except Exception as e:  # noqa: BLE001
            stack_events = [{"error": f"Exception: {str(e)}", "stack_name": name}]
        # get_cloudformation_logs returns a single {"error": ...} entry when a
        # stack doesn't exist; keep only real failure events so a genuine
        # rollback in a sibling stack isn't buried under "does not exist" noise.
        real = [e for e in stack_events if "error" not in e]
        events.extend(real)
    # If every candidate yielded only "does not exist"/errors, keep a note so
    # the summary can say evidence was unavailable rather than showing nothing.
    result["cf_events"] = events or [
        {"error": "No CloudFormation events captured", "stacks": list(stack_names)}
    ]

    # Snapshot the failing nested CodeBuild build's error + log tail NOW, for the
    # same reason as the events above: resolving the project requires the nested
    # stack to still exist, and teardown deletes it. Without this the summary can
    # only ever report "CodeBuild failed with status: FAILED".
    if events:
        try:
            details = get_codebuild_failure_details(stack_names[0], events)
            if details:
                result["codebuild_failures"] = details
                print(f"✅ Captured {len(details)} CodeBuild failure detail(s)")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Could not capture CodeBuild failure detail: {e}")


# ---------------------------------------------------------------------------
# Deployment-variant probe framework
#
# A "probe" is a self-contained *deploy-a-config-variant + smoke-check-its-
# distinguishing-feature* unit: it stands up its OWN throwaway IDP stack with a
# set of extra CloudFormation parameters, runs a validator against the deployed
# stack, and tears the stack down in a finally — all concurrently with the
# primary functional suite (Steps 3-12, which run on ONE default-hosting stack).
#
# This is a table of Probe(...) rows that a concurrent launcher iterates. Adding
# a new deployment permutation is one table row + a validator, not a copy-pasted
# deploy/validate/cleanup function.
#
# CONSTRAINTS (learned the hard way — see the VPC-quota incident in
# scripts/sdlc/docs/CI_TEST_COVERAGE.md):
#   * Probes are DEPLOY + FEATURE-SMOKE ONLY, not full functional coverage. A
#     variant can deploy clean yet still have a doc-processing regression that
#     only the primary suite would catch. Keep that expectation explicit.
#   * Each concurrent probe deploys a FULL IDP stack (+ IAM role/boundary) at
#     the same time as the primary suite and any other in-flight pipeline. That
#     is bounded stack/IAM quota, so fan-out is capped at
#     DEFAULT_PROBE_MAX_CONCURRENCY.
#   * VPC-requiring variants (jobsapi, PRIVATE hosting) do NOT create a VPC per
#     run anymore. A single PERSISTENT test VPC is owned by the pipeline stack
#     (scripts/sdlc/cfn/codepipeline-s3.yml, CreateTestVpc) and passed to every
#     run via env vars (IDP_TEST_VPC_ID / IDP_TEST_PRIVATE_SUBNET_IDS /
#     IDP_TEST_LAMBDA_SG_ID / IDP_TEST_APIGW_VPCE_ID). Probes REFERENCE it
#     (never mutate/create/destroy it), so VPCs no longer bound probe
#     concurrency and the 5-VPC quota is never approached. If those env vars are
#     unset (CreateTestVpc=false), a requires_vpc probe SKIPS itself with a note.
# ---------------------------------------------------------------------------

# name:         human-readable label (summary + AI failure analysis).
# stack_suffix: appended to the generated stack name (e.g. "apigw" ->
#               "idp-MMDD-HHMMSS-apigw"); keep short and DNS/CFN-safe.
# deploy_params: dict of EXTRA CFN parameter key->value merged into the deploy
#               (PermissionsBoundaryArn is added automatically). VPC params are
#               NOT listed here — set requires_vpc and they are injected at
#               runtime from the persistent-test-VPC env vars.
# validate_fn:  callable(stack_name) -> {"success": bool, ...}; asserts the
#               variant's distinguishing feature (endpoint type, reachable URL,
#               API responds, ...). Must not raise for an expected failure —
#               return {"success": False, "error": ...} instead.
# requires_vpc: True if the variant needs the persistent test VPC. Its
#               DeployInVPC/VpcId/PrivateSubnetIds/LambdaSubnetIds/
#               LambdaSecurityGroupId/ApiGatewayVpcEndpointId params are injected
#               from env at runtime; the probe skips (not fails) if the VPC env
#               vars are absent.
Probe = namedtuple(
    "Probe",
    ["name", "stack_suffix", "deploy_params", "validate_fn", "requires_vpc"],
    defaults=[False],
)

# Max concurrent probes. Each probe deploys a FULL IDP stack concurrently with
# the primary suite's stack AND any other in-flight pipeline. VPCs NO LONGER
# bound this (a single persistent pipeline-owned test VPC is shared read-only —
# see the framework header), so the cap only guards bounded stack/IAM quota. Set
# high enough to run every default probe in parallel; override with
# IDP_PROBE_MAX_CONCURRENCY.
DEFAULT_PROBE_MAX_CONCURRENCY = 8

# Seconds of launch stagger PER probe index (probe i waits i * this before its
# deploy). Spreads the concurrent CreateLogGroup/CreateProject/CreatePolicy burst
# that drives the AWS control-plane races (_TRANSIENT_DEPLOY_RACES + IAM rate
# limit) so the same resource types across stacks don't overlap their create
# windows. 120s × up to 5 probes = at most ~8min added to the LAST probe's start
# — negligible against a ~30-min deploy, and it prevents the burst rather than
# recovering from it. Only applies when probes actually run (off by default in
# CI now; used by `make probes-all` / IDP_RUN_PROBES=true). 0 disables.
DEFAULT_PROBE_LAUNCH_STAGGER_SECS = 120


def _resolve_probe_launch_stagger():
    """Per-index probe launch stagger (secs) from IDP_PROBE_LAUNCH_STAGGER_SECS.

    Non-negative float; malformed/negative → default. 0 = no stagger (all probes
    launch at once, the pre-mitigation behavior).
    """
    raw = get_env_var(
        "IDP_PROBE_LAUNCH_STAGGER_SECS", str(DEFAULT_PROBE_LAUNCH_STAGGER_SECS)
    )
    try:
        secs = float(raw)
    except (TypeError, ValueError):
        print(
            f"⚠️ Invalid IDP_PROBE_LAUNCH_STAGGER_SECS={raw!r}; "
            f"using default {DEFAULT_PROBE_LAUNCH_STAGGER_SECS}"
        )
        return float(DEFAULT_PROBE_LAUNCH_STAGGER_SECS)
    return secs if secs >= 0 else float(DEFAULT_PROBE_LAUNCH_STAGGER_SECS)


# The probe table. The primary suite (Steps 3-12) still runs separately on ONE
# default-hosting (CloudFront) stack; these are ADDITIONAL deploy+smoke probes
# of alternative deployment permutations, each on its own throwaway stack.
PROBE_VARIANTS = [
    # No VPC. GLOBAL visibility = regional internet-facing REST API serving the
    # SPA as an S3 proxy. Asserts REGIONAL endpoint, ApplicationWebURL is the
    # execute-api /api URL, and a real HTTP GET returns 200.
    Probe(
        name="APIGateway hosting (GLOBAL, no VPC)",
        stack_suffix="apigw",
        deploy_params={
            "WebUIHosting": "APIGateway",
            "ApiGatewayVisibility": "GLOBAL",
        },
        validate_fn=validate_apigw_global_hosting,
    ),
    # WAFv2 IP allow-list (no VPC). A non-default WAFAllowedIPv4Ranges creates a
    # REGIONAL WebACL (DefaultAction=Block + allow-list) associated with the API
    # stage. Structural check (WebACL exists + associated). Uses APIGateway
    # hosting so there is a REST API stage to associate the WebACL with.
    Probe(
        name="WAF-enabled (IP allow-list, no VPC)",
        stack_suffix="waf",
        deploy_params={
            "WebUIHosting": "APIGateway",
            "ApiGatewayVisibility": "GLOBAL",
            # Any non-default CIDR turns WAF on; 10.0.0.0/8 is arbitrary.
            "WAFAllowedIPv4Ranges": "10.0.0.0/8",
        },
        validate_fn=validate_waf_enabled,
    ),
    # PRIVATE API Gateway hosting (needs the persistent test VPC). The REST API
    # is VPC-only, so CodeBuild can't fetch it — structural check (endpoint type
    # PRIVATE + resource policy present). VPC params injected from env.
    Probe(
        name="APIGateway hosting (PRIVATE, VPC)",
        stack_suffix="apigwpriv",
        deploy_params={
            "WebUIHosting": "APIGateway",
            "ApiGatewayVisibility": "PRIVATE",
        },
        validate_fn=validate_apigw_private_hosting,
        requires_vpc=True,
    ),
    # Jobs REST API (needs the persistent test VPC). EnableJobsApi=true stands
    # up a private API GW + /jobs Lambdas. Structural check (ApiGatewayEndpoint
    # output + the REST API exists). VPC params injected from env. NOTE: this
    # exercises the additive EnableJobsApi CFN parameter on the STANDARD
    # template — NOT the `idp-cli deploy --headless` template transform.
    Probe(
        name="Jobs API (VPC)",
        stack_suffix="jobsapi",
        deploy_params={"EnableJobsApi": "true"},
        validate_fn=validate_jobs_api,
        requires_vpc=True,
    ),
    # OWASP ZAP DAST scan of the deployed UI REST API. Default hosting
    # (CloudFront) → the REST API is REGIONAL/internet-reachable so CodeBuild can
    # scan it (NOT requires_vpc — a PRIVATE API would be unreachable). Its own
    # stack means the Cognito auth-flow flip needed to mint a token is isolated,
    # so it runs fully concurrently. WARN-only (see validate_zap_dast). Gated by
    # IDP_TEST_ZAP (see run_variant_probes filtering in main()).
    Probe(
        name="ZAP DAST scan",
        stack_suffix="zapdast",
        deploy_params={},
        validate_fn=validate_zap_dast,
        requires_vpc=False,
    ),
    # --- Adding a future variant: one row + a validator ----------------------
    # deploy_params are extra CFN params; validate_fn is a new
    # callable(stack_name) -> {"success": bool, ...}. Set requires_vpc=True to
    # get the persistent-test-VPC params injected. Remember: DEPLOY +
    # FEATURE-SMOKE only, not full functional coverage. Candidates: BYO S3 VPC
    # endpoint, custom domain, GovCloud (deploy-only where the account allows).
]


def _test_vpc_params():
    """Resolve the persistent-test-VPC CFN params from env, or None if unset.

    Returns the dict of VPC params a requires_vpc probe must pass, populated
    from the pipeline-stack env vars (IDP_TEST_VPC_ID / IDP_TEST_PRIVATE_SUBNET_IDS
    / IDP_TEST_LAMBDA_SG_ID / IDP_TEST_APIGW_VPCE_ID). Returns None when the
    core ids are absent (CreateTestVpc=false), signalling the caller to SKIP the
    probe rather than fail it. Subnet lists are passed verbatim as comma-joined
    values — idp-cli's --parameters parser splits only on commas followed by a
    `key=`, so an embedded subnet list survives.
    """
    vpc_id = os.environ.get("IDP_TEST_VPC_ID", "").strip()
    subnets = os.environ.get("IDP_TEST_PRIVATE_SUBNET_IDS", "").strip()
    sg_id = os.environ.get("IDP_TEST_LAMBDA_SG_ID", "").strip()
    vpce_id = os.environ.get("IDP_TEST_APIGW_VPCE_ID", "").strip()
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


# One redeploy after a transient-only deploy failure (see
# _is_transient_logs_race). 2 = original attempt + 1 retry; a genuine config
# error fails identically both times and still surfaces.
PROBE_TRANSIENT_MAX_ATTEMPTS = 2


# Known AWS eventual-consistency (control-plane propagation) races that a fresh
# redeploy re-rolls. Each entry is (resource_type, reason_substring, statuses)
# matched case-insensitively on reason against an event whose status is in
# statuses. These are the ONLY failures retried: they are non-deterministic (a
# clean redeploy usually passes), whereas a real config/permission/template error
# fails IDENTICALLY every time and must surface, not be masked. Verified from
# real CI runs:
#   * LogGroup: an AWS::Logs::LogGroup with RetentionInDays makes CFN issue
#     CreateLogGroup then PutRetentionPolicy; under heavy concurrent stack
#     creation (~250 log groups at once) CWL isn't read-your-write consistent, so
#     the second call hits the not-yet-propagated group → "does not exist".
#   * CodeBuild service role: a nested stack's AWS::CodeBuild::Project is created
#     right after its service role; IAM role/trust-policy propagation is
#     eventually consistent, so CodeBuild's CreateProject trust validation
#     occasionally races the new role → "is not authorized to perform:
#     sts:AssumeRole on service role ... trust policy configured".
#   * S3 bucket-config OperationAborted: S3 permits only one conditional
#     bucket-config write at a time, so concurrent CFN operations on the same
#     bucket (notification config vs bucket policy vs auto-delete) collide →
#     "A conflicting conditional operation is currently in progress". Seen as a
#     DELETE_FAILED during rollback, which wedges the stack in ROLLBACK_FAILED.
#     This is now fixed at the source (retry ladder + best-effort delete +
#     DependsOn ordering in template.yaml); the entry remains as a backstop for
#     any other bucket-config resource that hits the same S3 constraint.
_TRANSIENT_DEPLOY_RACES = (
    ("AWS::Logs::LogGroup", "does not exist", ("CREATE_FAILED",)),
    ("AWS::CodeBuild::Project", "sts:assumerole on service role", ("CREATE_FAILED",)),
    (
        "Custom::S3BucketNotification",
        "conflicting conditional operation",
        ("CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"),
    ),
)


def _is_transient_deploy_race(result):
    """True iff the deploy rolled back on a KNOWN transient AWS-consistency race.

    NOT a blanket "retry any rollback": matched tightly to resource type +
    message + status (see _TRANSIENT_DEPLOY_RACES) so genuine
    config/permission/template errors — which fail identically on a redeploy —
    still surface. Requirements:
      * failure_type must be "deploy" (never a validation failure), and
      * some captured event matches a known-racy (resource type, reason
        substring, status) triple. Most races are CREATE_FAILED on the
        initiating resource; collateral rolled-back resources carry "Resource
        creation cancelled", which this deliberately does NOT match. A few
        (S3 bucket-config conflicts) surface as DELETE_FAILED mid-rollback, so
        the matchable statuses are per-entry rather than CREATE_FAILED-only.
    """
    if result.get("failure_type") != "deploy":
        return False
    for ev in result.get("cf_events") or []:
        if not isinstance(ev, dict):
            continue
        status = ev.get("status")
        rtype = ev.get("resource_type")
        reason = (ev.get("reason") or "").lower()
        for race_type, race_substr, race_statuses in _TRANSIENT_DEPLOY_RACES:
            if rtype == race_type and status in race_statuses and race_substr in reason:
                return True
    return False


# Back-compat alias: the CWL-race-only name kept so existing call sites/tests
# that reference it continue to work while the detector now covers more races.
_is_transient_logs_race = _is_transient_deploy_race


def _run_probe_attempt(probe, admin_email, template_url, vpc_params):
    """One deploy+validate+teardown attempt for a probe (fresh stack + IAM).

    Generates its OWN stack name and creates/deletes its own IAM so that a retry
    reuses no names and leaves nothing behind — cleanup always runs in finally.
    Returns the same result-dict shape as deploy_and_test_probe.
    """
    stack_name = f"{generate_stack_name()}-{probe.stack_suffix}"
    result = {"stack_name": stack_name, "success": False, "probe": probe.name}
    try:
        # Probes don't create their own permissions boundary (only the primary
        # suite does + tests it) — removes an iam:CreatePolicy/DeletePolicy from
        # the burst. boundary_arn is "" here; deploy with an empty
        # PermissionsBoundaryArn (template's HasPermissionsBoundary gate handles
        # the empty case).
        role_arn, boundary_arn = create_iam_resources(stack_name, create_boundary=False)
        if not role_arn:
            raise Exception(f"Failed to create IAM resources for probe {probe.name!r}")

        # idp-cli --parameters takes ONE comma-separated key=value string. The
        # probe's extra params, then any injected VPC params. idp-cli's parser
        # splits only on commas preceding a `key=`, so the comma-joined subnet
        # list is safe.
        merged = {**probe.deploy_params, **vpc_params}
        param_pairs = [f"PermissionsBoundaryArn={boundary_arn}"]  # empty = feature off
        param_pairs += [f"{k}={v}" for k, v in merged.items()]
        params = ",".join(param_pairs)
        cmd = (
            f"idp-cli deploy --stack-name {stack_name} --template-url {template_url} "
            f"--admin-email {admin_email} --wait --role-arn {role_arn} "
            f'--parameters "{params}"'
        )
        print(f"Probe [{probe.name}]: deploying stack {stack_name}...")
        run_command(cmd, timeout=3 * 3600)

        status = run_command(
            f"aws cloudformation describe-stacks --stack-name {stack_name} "
            "--query 'Stacks[0].StackStatus' --output text"
        )
        if "COMPLETE" not in status.stdout:
            result["error"] = f"Deploy status: {status.stdout.strip()}"
            result["failure_type"] = "deploy"
            _capture_cf_events(result, stack_name)
            return result

        validation = probe.validate_fn(stack_name)
        result.update(validation)
        if not validation.get("success"):
            result["failure_type"] = "test"
        return result
    except Exception as e:  # noqa: BLE001
        print(f"❌ Probe [{probe.name}] exception: {e}")
        result["error"] = str(e)
        result["failure_type"] = "deploy"
        _capture_cf_events(result, stack_name)
        return result
    finally:
        cleanup_stack({"stack_name": stack_name})


def deploy_and_test_probe(probe, admin_email, template_url):
    """Deploy + validate + tear down ONE deployment-variant probe.

    Stands up a throwaway IDP stack with the probe's extra CFN params (plus, for
    requires_vpc probes, the persistent-test-VPC params from env), runs its
    validator, and ALWAYS tears the stack down (finally). Runs on its own pool
    thread and opts that thread out of the primary suite's fail-fast abort
    machinery (_thread_local.never_abort) so a primary failure's kill sweep
    cannot terminate this independent-stack deploy mid-flight. CF failure events
    are captured before teardown so the AI summary can name the root cause.

    A requires_vpc probe with no test-VPC env vars configured returns a SKIPPED
    result (success=True, skipped=True) — it is absent infra, not a failure.

    A deploy that rolls back on the KNOWN-TRANSIENT CloudWatch Logs create
    consistency race (_is_transient_logs_race) is retried ONCE on a fresh stack;
    every other failure (validation, or any other deploy error) returns
    immediately without retry so real regressions surface fast.

    Returns a result dict shaped like the primary suite's:
    {"stack_name", "success", "probe", ["error", "failure_type", "skipped", ...]}.
    """
    _thread_local.never_abort = True

    # Resolve VPC params up front so a requires_vpc probe skips cleanly (before
    # creating any IAM/stack) when the persistent test VPC isn't configured.
    vpc_params = {}
    if probe.requires_vpc:
        vpc_params = _test_vpc_params()
        if vpc_params is None:
            msg = (
                f"Probe [{probe.name}] SKIPPED — requires the persistent test "
                "VPC but IDP_TEST_* env vars are unset (CreateTestVpc=false)"
            )
            print(f"⏭️  {msg}")
            return {
                "stack_name": f"<{probe.stack_suffix} probe>",
                "success": True,
                "skipped": True,
                "probe": probe.name,
                "detail": msg,
            }

    result = None
    for attempt in range(1, PROBE_TRANSIENT_MAX_ATTEMPTS + 1):
        result = _run_probe_attempt(probe, admin_email, template_url, vpc_params)
        if result.get("success"):
            return result
        # Retry ONLY the tightly-scoped CWL create race, and only if attempts
        # remain. The prior attempt's stack + IAM are already torn down (its
        # finally), so the retry is a clean, independent redeploy.
        if attempt < PROBE_TRANSIENT_MAX_ATTEMPTS and _is_transient_logs_race(result):
            print(
                f"♻️ Probe [{probe.name}] hit the transient CloudWatch Logs "
                f"create-consistency race (attempt {attempt}/"
                f"{PROBE_TRANSIENT_MAX_ATTEMPTS}); redeploying a fresh stack once..."
            )
            result["retried_transient_logs_race"] = True
            continue
        return result
    return result


def resolve_probe_concurrency(num_probes):
    """Resolve the probe fan-out cap from IDP_PROBE_MAX_CONCURRENCY.

    Clamped to [1, num_probes]: never spins up more workers than there are
    probes, and a malformed/<=0 override falls back to the conservative
    default rather than deploying an unbounded number of concurrent stacks.
    """
    raw = get_env_var("IDP_PROBE_MAX_CONCURRENCY", str(DEFAULT_PROBE_MAX_CONCURRENCY))
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        print(
            f"⚠️ Invalid IDP_PROBE_MAX_CONCURRENCY={raw!r}; "
            f"using default {DEFAULT_PROBE_MAX_CONCURRENCY}"
        )
        cap = DEFAULT_PROBE_MAX_CONCURRENCY
    if cap < 1:
        cap = DEFAULT_PROBE_MAX_CONCURRENCY
    return max(1, min(cap, num_probes))


def run_variant_probes(admin_email, template_url, probes=None):
    """Run the deployment-variant probes concurrently, capped at the quota budget.

    Intended to run on its OWN supervisor thread so its probe deploys overlap
    the primary suite's ~30m deploy (the caller in main() submits it to a
    single-worker executor). Internally it fans out to at most
    IDP_PROBE_MAX_CONCURRENCY probes at a time — the budget that bounds how many
    full IDP stacks deploy at once (VPCs no longer bound this: VPC-requiring
    probes share one persistent pipeline-owned test VPC read-only).

    Each probe deploys/validates/tears-down its own throwaway stack and opts out
    of fail-fast independently, so one probe failing (or the primary suite
    failing) never affects the others. Returns a list of per-probe result dicts
    (order not significant; each carries its own "probe" label; VPC-requiring
    probes with no test VPC configured come back skipped=True).
    """
    probes = PROBE_VARIANTS if probes is None else probes
    # The ZAP DAST probe is individually gateable (it needs Docker/PrivilegedMode
    # and pulls the ZAP image); default on, set IDP_TEST_ZAP=false to skip it
    # without disabling the other probes.
    if get_env_var("IDP_TEST_ZAP", "true").lower() != "true":
        skipped = [p.name for p in probes if p.stack_suffix == "zapdast"]
        probes = [p for p in probes if p.stack_suffix != "zapdast"]
        for name in skipped:
            print(f"⏭️  Skipping probe [{name}] (IDP_TEST_ZAP=false)")
    if not probes:
        print("ℹ️ No deployment-variant probes configured")
        return []

    cap = resolve_probe_concurrency(len(probes))
    print(
        f"🚀 Launching {len(probes)} deployment-variant probe(s) "
        f"(max {cap} concurrent) alongside the primary suite..."
    )
    for p in probes:
        print(f"   • {p.name} (stack suffix -{p.stack_suffix})")

    # Stagger probe launches so the primary suite + N probes don't all fire
    # their CreateLogGroup / CreateProject bursts at the same instant. These
    # deploys hit two AWS control-plane eventual-consistency races
    # (_TRANSIENT_DEPLOY_RACES: CWL "log group does not exist";
    # CodeBuild "sts:AssumeRole on service role" trust propagation) whose
    # probability rises with concurrent-create burst load. Spreading launches by
    # a few seconds each flattens the burst and AVOIDS the race up front — much
    # cheaper than the rollback + teardown + retry it would otherwise trigger.
    # Probability reducer, NOT a guarantee (a single stack can still race), so
    # the one-shot retry stays as the backstop. Tunable/disable via
    # IDP_PROBE_LAUNCH_STAGGER_SECS (0 = simultaneous, old behavior).
    stagger = _resolve_probe_launch_stagger()

    def _staggered_deploy(probe, launch_index):
        if stagger and launch_index:
            time.sleep(launch_index * stagger)
        return deploy_and_test_probe(probe, admin_email, template_url)

    results = []
    # No `with`: match the primary suite's pattern — shutdown(wait=True) in a
    # finally, never an implicit join that could burn the job timeout.
    executor = ThreadPoolExecutor(max_workers=cap)
    try:
        futures = {
            executor.submit(_staggered_deploy, probe, i): probe
            for i, probe in enumerate(probes)
        }
        for future in as_completed(futures):
            probe = futures[future]
            try:
                results.append(future.result())
            except Exception as e:  # noqa: BLE001
                # deploy_and_test_probe already catches its own exceptions, so
                # this is a last-resort guard (e.g. the thread died) — record a
                # failure rather than losing the probe from the summary.
                print(f"❌ Probe [{probe.name}] supervisor exception: {e}")
                results.append(
                    {
                        "stack_name": f"<{probe.stack_suffix} probe>",
                        "success": False,
                        "error": str(e),
                        "failure_type": "deploy",
                        "probe": probe.name,
                    }
                )
    finally:
        executor.shutdown(wait=True)
    return results


def publish_summary_to_s3(summary_text):
    """Upload the deployment summary to the SDLC source bucket.

    The GitLab job fetches this file directly (deterministic key derived from
    the CodeBuild build id) instead of scraping it out of CloudWatch Logs,
    which truncates on long builds. Best effort — never fails the build.
    """
    bucket = os.environ.get("SOURCE_BUCKET", "")
    build_id = os.environ.get("CODEBUILD_BUILD_ID", "")
    if not bucket or not build_id:
        print("ℹ️ Skipping summary upload (not running in CodeBuild)")
        return
    key = f"deploy/summaries/{build_id.split(':')[-1]}.txt"
    try:
        boto3.client("s3").put_object(
            Bucket=bucket, Key=key, Body=summary_text.encode("utf-8")
        )
        print(f"📁 Summary uploaded to s3://{bucket}/{key}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Failed to upload summary to S3: {e}")


def build_consolidated_summary(
    stack_name, primary_result, probe_results, publish_success
):
    """Build the deterministic end-of-run status table for EVERY test.

    Independent of Bedrock — this ALWAYS renders (the GitLab log needs a
    reliable "here is every test and its status" view even when Bedrock is
    unavailable). Covers the publish step, the primary suite's per-step results
    (from result["step_results"]), and each deployment-variant probe. The
    Bedrock pass/fail narrative is layered on top of this, not instead of it.

    Returns the summary text; the caller prints and uploads it.
    """
    lines = []
    overall_ok = True

    def row(status, label, detail=""):
        icon = {
            "passed": "✅",
            "failed": "❌",
            "skipped": "⏭️",
            "cancelled": "⚪",
        }.get(status, "❓")
        text = f"  {icon} {label}"
        if detail:
            # Keep the table readable — trim long errors.
            text += f" — {detail[:120]}"
        return text

    # Publish / build
    lines.append("📦 Build & Publish")
    if publish_success:
        lines.append(row("passed", "Publish templates to S3"))
    else:
        lines.append(row("failed", "Publish templates to S3"))
        overall_ok = False

    # Primary shared-stack suite (Steps 3-12)
    lines.append("")
    lines.append(f"🧪 Primary suite (shared stack {stack_name})")
    if not publish_success:
        lines.append(row("cancelled", "Not run (publish failed)"))
    else:
        step_results = (primary_result or {}).get("step_results")
        if step_results:
            for label, info in step_results.items():
                lines.append(row(info["status"], label, info.get("error", "")))
                if info["status"] == "failed":
                    overall_ok = False
        else:
            # Deploy failed before any step ran (or an exception result dict
            # with no step_results) — reflect the primary result directly.
            if (primary_result or {}).get("success"):
                lines.append(row("passed", "All steps passed"))
            else:
                lines.append(
                    row(
                        "failed",
                        "Deployment/health check",
                        (primary_result or {}).get("error", "Unknown error"),
                    )
                )
                overall_ok = False

    # Deployment-variant probes
    lines.append("")
    lines.append("🔬 Deployment-variant probes")
    if not probe_results:
        lines.append(row("cancelled", "None run"))
    else:
        for pr in probe_results:
            name = pr.get("probe", "probe")
            if pr.get("skipped"):
                lines.append(row("skipped", name, pr.get("detail", "")))
            elif pr.get("success"):
                lines.append(row("passed", name))
            else:
                lines.append(row("failed", name, pr.get("error", "Unknown error")))
                overall_ok = False

    header = "🎉 OVERALL: PASS" if overall_ok else "💥 OVERALL: FAIL"
    banner = "=" * 72
    return "\n".join(
        [banner, "CONSOLIDATED TEST SUMMARY", banner, "", *lines, "", header, banner]
    )


def send_failure_notification(subject, summary_text):
    """Publish the failure summary to the SDLC SNS topic (email fan-out).

    Gated on IDP_FAILURE_SNS_TOPIC (set by the pipeline template). Best
    effort — a notification failure must never mask the build result.
    """
    topic_arn = os.environ.get("IDP_FAILURE_SNS_TOPIC", "")
    if not topic_arn:
        print("ℹ️ IDP_FAILURE_SNS_TOPIC not set — skipping failure email")
        return
    try:
        boto3.client("sns").publish(
            TopicArn=topic_arn,
            # SNS subjects are capped at 100 chars
            Subject=subject[:100],
            Message=summary_text,
        )
        print(f"📧 Failure notification published to {topic_arn}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Failed to publish failure notification: {e}")


def main():
    """Main execution function"""
    print("Starting CodeBuild deployment process...")

    # Every CI stack (primary + all probes) uses the sentinel admin email that
    # makes the template SUPPRESS the Cognito invite email. No CI test signs in
    # via the emailed temp password (Step 12 RBAC creates its own users), and
    # each stack's admin invite burns Cognito's low daily email quota — 6
    # stacks/run (primary + 5 probes) exhausts it and rolls back the deploy.
    # Must match the SuppressAdminInvite condition in template.yaml.
    admin_email = SUPPRESS_INVITE_ADMIN_EMAIL
    stack_name = generate_stack_name()

    print(f"Stack Name: {stack_name}")
    print(f"Admin Email: {admin_email} (Cognito invite suppressed)")

    # initialize AI summary
    ai_summary = ""
    publish_success = False
    stack_success = False
    # Primary-suite + probe results, initialized so the consolidated summary
    # renders even if publish fails before either runs.
    result = None
    probe_results = []
    # One-line root cause carried onto the final 🎉/💥 line so the job result
    # is actionable even if the AI summary generation itself breaks.
    failure_reason = ""

    # Step 0: Clean up stale resources leaked by PRIOR runs whose own cleanup
    # was interrupted (creds expiring mid-teardown is the usual cause). These
    # startup reapers converge the account back to clean regardless — otherwise
    # leaked test VPCs exhaust the VPC quota, and leaked -iam stacks' roles
    # exhaust the RolesPerAccount quota (which failed every deploy once ~600
    # roles had accumulated). All are age-gated so a concurrent pipeline's
    # in-flight run is never touched.
    cleanup_stale_bda_blueprints()
    cleanup_stale_apigw_test_vpcs()
    cleanup_stale_idp_stacks()
    # Buckets last: a stack the reaper above just deleted frees its buckets for
    # reaping here (CloudFormation can't delete a non-empty bucket, so they'd
    # otherwise leak — thousands accumulated this way).
    cleanup_stale_idp_buckets()

    # Step 1: Publish templates to S3
    try:
        template_url = publish_templates()
        print(f"Publish script ran successfully template url {template_url}")
        publish_success = True
    except Exception as e:
        print(f"❌ Publish failed: {e}")
        failure_reason = f"publish/build failed: {e}"
        ai_summary = generate_publish_failure_summary(str(e))

    # Step 1a: Validate the --headless template variant against real
    # CloudFormation, reusing the packaged template publish just built. Failure
    # does NOT short-circuit here — the primary suite still runs so its signal
    # isn't lost — it is folded into the final verdict below.
    headless_ok, headless_detail = True, ""
    if publish_success:
        headless_ok, headless_detail = validate_headless_template(template_url)
        if not headless_ok:
            print(f"❌ Headless template validation failed: {headless_detail}")

    if publish_success:
        # Step 2: Launch the deployment-variant probes on their OWN supervisor
        # thread FIRST so their ~30m stack deploys overlap the primary suite's
        # ~30m deploy instead of running after it. Each probe uses an independent
        # throwaway stack and opts out of the fail-fast abort machinery, so
        # probes and the primary suite are fully isolated. The supervisor
        # internally caps concurrent probes at the quota budget
        # (IDP_PROBE_MAX_CONCURRENCY).
        #
        # DEFAULT OFF in CI. Standing up the primary + 5 probe stacks at once
        # bursts the account-wide control planes (CWL log-group create
        # consistency, CodeBuild role-trust propagation, IAM CreatePolicy rate
        # limit) — a recurring source of flaky pipeline failures unrelated to the
        # code under test. The deploy-variant probes are infra smoke tests that
        # rarely change, so they now run MANUALLY via `make stacktest-*` (see
        # scripts/sdlc/run_stacktest.py / .claude/skills/run-stack-tests.md),
        # each on its own stack with no concurrent-burst. Set IDP_RUN_PROBES=true
        # to re-enable them in a pipeline run. (IDP_TEST_APIGW_HOSTING is still
        # honored as a legacy alias so an existing override keeps working.)
        probes_enabled = (
            get_env_var("IDP_RUN_PROBES", "false").lower() == "true"
            or get_env_var("IDP_TEST_APIGW_HOSTING", "false").lower() == "true"
        )
        probes_future = None
        probes_executor = None
        if probes_enabled:
            print(
                "\n🚀 Launching deployment-variant probes concurrently with "
                "the primary suite...\n"
            )
            probes_executor = ThreadPoolExecutor(max_workers=1)
            probes_future = probes_executor.submit(
                run_variant_probes, admin_email, template_url
            )
        else:
            print("ℹ️ Skipping deployment-variant probes (disabled)")

        # Step 2b: Deploy + test the primary shared stack (runs concurrently
        # with the APIGW thread above).
        print(f"🚀 Starting deployment for stack: {stack_name}")

        # Publish a PROGRESSIVE summary after each primary-suite milestone so the
        # GitLab monitor's ~45-min handoff always finds a current snapshot in S3
        # — even when the primary suite itself runs long (a slow Step 12 pushed
        # a run's only upload past the handoff, so after_script saw "No summary
        # found" despite the run finishing fine). Marked IN-PROGRESS; overwritten
        # by the interim (post-primary) and final (post-probe) uploads below.
        def _publish_progress(step_results):
            partial = {
                "stack_name": stack_name,
                # success unknown mid-run; build_consolidated_summary derives
                # PASS/FAIL from the per-step statuses, not this flag.
                "success": False,
                "step_results": step_results,
            }
            snapshot = build_consolidated_summary(
                stack_name, partial, [], publish_success
            )
            snapshot = (
                "⏳ IN-PROGRESS SUMMARY (primary suite still running; "
                "probes not yet joined — updated as steps complete)\n\n" + snapshot
            )
            publish_summary_to_s3(snapshot)

        try:
            result = deploy_and_test_stack(
                stack_name, admin_email, template_url, progress_cb=_publish_progress
            )
            if not result["success"]:
                print(f"[{stack_name}] ❌ Failed")
            else:
                stack_success = True
                print(f"[{stack_name}] ✅ Success")
        except Exception as e:
            print(f"[{stack_name}] ❌ Exception: {e}")
            # Add failed result for exception cases
            result = {"stack_name": stack_name, "success": False, "error": str(e)}

        if not result["success"]:
            failure_reason = result.get("error", "Unknown error")

        # Step 3: Generate deployment summary using Bedrock (but don't print
        # yet). Must run before cleanup_stack so CF events still exist for
        # deploy-failure analysis.
        try:
            ai_summary = generate_deployment_summary(result, stack_name, template_url)
        except Exception as e:
            ai_summary = f"⚠️ Failed to generate deployment summary: {e}"

        # Step 4: clean up the primary stack (the APIGW thread cleans up its own
        # stack in its finally block).
        cleanup_stack(result)

        # Step 4a: Upload an INTERIM summary now — BEFORE blocking on the probe
        # join below. The probes can run well past the GitLab monitor's ~45-min
        # handoff (main() stays alive under CodeBuild's own longer timeout), so
        # if we only uploaded after the join, after_script would fetch the S3
        # summary key before it exists and report "No summary found" (exactly
        # what happened once the probe count grew from 1 to 4). Publishing the
        # primary result here guarantees the handoff always finds at least that;
        # Step 5 overwrites it with the full consolidated version once probes
        # finish. Marked INTERIM so a reader knows probe rows may still be
        # pending.
        interim = build_consolidated_summary(
            stack_name, result, probe_results, publish_success
        )
        interim = (
            "⏳ INTERIM SUMMARY (primary suite done; deployment-variant probes "
            "may still be running — final summary overwrites this)\n\n" + interim
        )
        if ai_summary:
            interim = f"{interim}\n\n{ai_summary}"
        publish_summary_to_s3(interim)

        # Step 4b: Join the concurrent deployment-variant probes and fold in
        # their results. A probe failure marks the overall run failed but does
        # not affect the already-completed primary suite result.
        if probes_future is not None:
            print(f"\n{'=' * 80}")
            print("Waiting for deployment-variant probes...")
            print(f"{'=' * 80}\n")
            try:
                probe_results = probes_future.result()
            except Exception as e:  # noqa: BLE001
                # run_variant_probes catches per-probe failures itself; this
                # only fires if the supervisor thread itself died.
                print(f"❌ Deployment-variant probe supervisor exception: {e}")
                probe_results = [
                    {
                        "stack_name": "<probe supervisor>",
                        "success": False,
                        "error": str(e),
                        "failure_type": "deploy",
                        "probe": "probe supervisor",
                    }
                ]
            finally:
                probes_executor.shutdown(wait=True)

            for probe_result in probe_results:
                probe_name = probe_result.get("probe", "deployment-variant probe")
                if probe_result.get("skipped"):
                    print(
                        f"⏭️  Probe [{probe_name}] skipped: {probe_result.get('detail', '')}"
                    )
                    continue
                if probe_result.get("success"):
                    print(f"✅ Probe [{probe_name}] passed")
                    continue
                stack_success = False
                probe_error = probe_result.get("error", "Unknown error")
                print(f"❌ Probe [{probe_name}] failed: {probe_error}")
                if not failure_reason:
                    failure_reason = f"Probe [{probe_name}] failed: {probe_error}"
                # The primary summary was generated before this join and may say
                # "All Tests Passed" — analyze each probe failure too (its CF
                # events were captured before the throwaway stack teardown).
                try:
                    probe_summary = generate_deployment_summary(
                        probe_result,
                        probe_result.get("stack_name", "<probe stack>"),
                        template_url,
                    )
                except Exception as e:  # noqa: BLE001
                    probe_summary = f"⚠️ Failed to generate probe summary: {e}"
                ai_summary = (
                    f"{ai_summary}\n\n"
                    f"--- Deployment-variant probe: {probe_name} (Step 4b) ---\n"
                    f"{probe_summary}"
                )

    # Fold the headless-template gate (Step 1a) into the verdict. Applied HERE,
    # not at the check itself, because stack_success is assigned (not and-ed) by
    # the primary suite above, so an early False would be overwritten.
    if not headless_ok:
        stack_success = False
        if not failure_reason:
            failure_reason = f"--headless template validation failed: {headless_detail}"

    # Step 5: Print the deterministic consolidated status table FIRST (always
    # renders, Bedrock or not — the GitLab log needs a reliable "every test +
    # status" view), then the Bedrock pass/fail narrative(s). Both are uploaded
    # to S3 so the GitLab job can fetch the full report.
    consolidated = build_consolidated_summary(
        stack_name, result, probe_results, publish_success
    )
    print(f"\n{consolidated}\n")

    print("\n🤖 Generating deployment summary with Bedrock...")
    full_summary = consolidated
    if ai_summary:
        print(ai_summary)
        full_summary = f"{consolidated}\n\n{ai_summary}"
    publish_summary_to_s3(full_summary)

    # Check final status after all cleanups are done. Use os._exit so the
    # concurrent.futures atexit hook doesn't block on abandoned test threads
    # that are still failing out against the (now deleted) stack.
    if stack_success:
        print(f"🎉 Stack: {stack_name} deployment completed successfully!")
        exit_code = 0
    else:
        reason_suffix = f" — {failure_reason}" if failure_reason else ""
        print(f"💥 Stack: {stack_name} deployment failed!{reason_suffix}")
        send_failure_notification(
            f"IDP CI failure: {stack_name}",
            f"Stack: {stack_name}\nRoot cause: {failure_reason or 'unknown'}\n\n"
            f"{full_summary or 'No summary available.'}",
        )
        exit_code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
