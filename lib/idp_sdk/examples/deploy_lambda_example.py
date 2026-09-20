#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Deploy and Test IDP SDK Lambda Example

This script packages the IDP SDK as a Lambda layer, deploys the example
Lambda function, and tests it with sample events.

Usage:
    # Deploy and test (creates Lambda layer and function)
    python deploy_lambda_example.py --stack-name IDP-Nova-1 --deploy

    # Test only (assumes function already deployed)
    python deploy_lambda_example.py --stack-name IDP-Nova-1 --test

    # Clean up (delete Lambda function and layer)
    python deploy_lambda_example.py --stack-name IDP-Nova-1 --cleanup

Requirements:
    - AWS CLI configured with appropriate permissions
    - Deployed IDP stack
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Constants
LAMBDA_FUNCTION_NAME = "idp-sdk-example"
LAYER_NAME = "idp-sdk"
RUNTIME = "python3.12"


def run_command(cmd: list, capture: bool = False, cwd: str = None) -> tuple:
    """Run a command and return success status and output."""
    print(f"  Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            cwd=cwd,
        )
        if capture:
            return result.returncode == 0, result.stdout, result.stderr
        return result.returncode == 0, None, None
    except Exception as e:
        return False, None, str(e)


def aws_partition() -> str:
    """The partition this account is in, from the caller identity's own ARN.

    Hardcoding ``aws`` makes the managed-policy ARNs below wrong in GovCloud, where
    the same policy is ``arn:aws-us-gov:iam::aws:policy/...``, and the resulting
    failure reads as a permissions problem rather than a partition one.
    """
    ok, stdout, _ = run_command(
        ["aws", "sts", "get-caller-identity", "--query", "Arn", "--output", "text"],
        capture=True,
    )
    if ok and stdout.strip().startswith("arn:"):
        return stdout.strip().split(":")[1]
    return "aws"


def create_layer(sdk_dir: Path) -> str:
    """Create Lambda layer with IDP SDK and return layer ARN."""
    print("\n📦 Creating Lambda layer with IDP SDK...")

    with tempfile.TemporaryDirectory() as tmpdir:
        layer_dir = Path(tmpdir) / "python"
        layer_dir.mkdir()

        # First install idp_common WITH dependencies (includes pydantic, boto3, etc.)
        idp_common_dir = sdk_dir.parent / "idp_common_pkg"
        if idp_common_dir.exists():
            print("  Installing idp_common and dependencies...")
            success, _, stderr = run_command(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    str(idp_common_dir),
                    "-t",
                    str(layer_dir),
                    "--quiet",
                    "--platform",
                    "manylinux2014_x86_64",
                    "--implementation",
                    "cp",
                    "--python-version",
                    "3.12",
                    "--only-binary=:all:",
                ],
                capture=True,
            )
            if not success:
                # Fallback without platform constraints
                print("  Retrying without platform constraints...")
                success, _, stderr = run_command(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        str(idp_common_dir),
                        "-t",
                        str(layer_dir),
                        "--quiet",
                    ],
                    capture=True,
                )
                if not success:
                    print(f"  ❌ Failed to install idp_common: {stderr}")
                    return None

        # Install idp_cli (local package, deps already installed)
        idp_cli_dir = sdk_dir.parent / "idp_cli_pkg"
        if idp_cli_dir.exists():
            print("  Installing idp-cli dependency...")
            success, _, stderr = run_command(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    str(idp_cli_dir),
                    "-t",
                    str(layer_dir),
                    "--no-deps",  # idp_common already installed
                    "--quiet",
                ],
                capture=True,
            )
            if not success:
                print(f"  ❌ Failed to install idp_cli: {stderr}")
                return None

        # Install IDP SDK (uses already-installed local deps)
        print("  Installing IDP SDK into layer...")
        success, _, stderr = run_command(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                str(sdk_dir),
                "-t",
                str(layer_dir),
                "--no-deps",  # Dependencies already installed
                "--quiet",
            ],
            capture=True,
        )
        if not success:
            print(f"  ❌ Failed to install SDK: {stderr}")
            return None

        # Create zip file
        zip_path = Path(tmpdir) / "layer.zip"
        print("  Creating layer zip...")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(layer_dir.parent):
                for file in files:
                    file_path = Path(root) / file
                    arc_name = file_path.relative_to(layer_dir.parent)
                    zf.write(file_path, arc_name)

        zip_size = zip_path.stat().st_size / (1024 * 1024)
        print(f"  Layer zip size: {zip_size:.1f} MB")

        # Publish layer
        print("  Publishing Lambda layer...")
        success, stdout, stderr = run_command(
            [
                "aws",
                "lambda",
                "publish-layer-version",
                "--layer-name",
                LAYER_NAME,
                "--zip-file",
                f"fileb://{zip_path}",
                "--compatible-runtimes",
                RUNTIME,
                "python3.11",
                "--description",
                "IDP SDK for document processing",
            ],
            capture=True,
        )

        if not success:
            print(f"  ❌ Failed to publish layer: {stderr}")
            return None

        result = json.loads(stdout)
        layer_arn = result["LayerVersionArn"]
        print(f"  ✅ Layer published: {layer_arn}")
        return layer_arn


def get_latest_layer_arn() -> str:
    """Get the latest layer ARN."""
    success, stdout, stderr = run_command(
        [
            "aws",
            "lambda",
            "list-layer-versions",
            "--layer-name",
            LAYER_NAME,
            "--query",
            "LayerVersions[0].LayerVersionArn",
            "--output",
            "text",
        ],
        capture=True,
    )
    if success and stdout and stdout.strip() != "None":
        return stdout.strip()
    return None


def create_iam_role() -> str:
    """Create or get IAM role for Lambda function."""
    role_name = f"{LAMBDA_FUNCTION_NAME}-role"

    # Check if role exists
    success, stdout, _ = run_command(
        [
            "aws",
            "iam",
            "get-role",
            "--role-name",
            role_name,
            "--query",
            "Role.Arn",
            "--output",
            "text",
        ],
        capture=True,
    )

    if success and stdout:
        print(f"  Using existing role: {role_name}")
        return stdout.strip()

    print(f"  Creating IAM role: {role_name}")

    # Trust policy
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    success, stdout, stderr = run_command(
        [
            "aws",
            "iam",
            "create-role",
            "--role-name",
            role_name,
            "--assume-role-policy-document",
            json.dumps(trust_policy),
            "--query",
            "Role.Arn",
            "--output",
            "text",
        ],
        capture=True,
    )

    if not success:
        print(f"  ❌ Failed to create role: {stderr}")
        return None

    role_arn = stdout.strip()

    # Attach basic Lambda execution policy
    run_command(
        [
            "aws",
            "iam",
            "attach-role-policy",
            "--role-name",
            role_name,
            "--policy-arn",
            f"arn:{aws_partition()}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        ],
        capture=True,
    )

    # Create and attach IDP access policy
    policy_name = f"{LAMBDA_FUNCTION_NAME}-idp-policy"
    idp_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "cloudformation:DescribeStacks",
                    "cloudformation:ListStackResources",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["sqs:SendMessage", "sqs:GetQueueUrl"],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                "Resource": "*",
            },
            {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": "*"},
            {
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
                "Resource": "*",
            },
        ],
    }

    # Create inline policy
    run_command(
        [
            "aws",
            "iam",
            "put-role-policy",
            "--role-name",
            role_name,
            "--policy-name",
            policy_name,
            "--policy-document",
            json.dumps(idp_policy),
        ],
        capture=True,
    )

    # Wait for role to propagate
    print("  Waiting for role propagation...")
    import time

    time.sleep(10)

    return role_arn


def deploy_function(layer_arn: str, idp_stack_name: str) -> bool:
    """Deploy the Lambda function."""
    print("\n🚀 Deploying Lambda function...")

    # Get the lambda function code
    examples_dir = Path(__file__).parent
    lambda_code = examples_dir / "lambda_function.py"

    # Create role
    role_arn = create_iam_role()
    if not role_arn:
        return False

    # Create deployment package
    with tempfile.TemporaryDirectory() as tmpdir:
        zip_path = Path(tmpdir) / "function.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(lambda_code, "lambda_function.py")

        # Check if function exists
        success, _, _ = run_command(
            [
                "aws",
                "lambda",
                "get-function",
                "--function-name",
                LAMBDA_FUNCTION_NAME,
            ],
            capture=True,
        )

        if success:
            # Update existing function
            print("  Updating existing function...")
            success, _, stderr = run_command(
                [
                    "aws",
                    "lambda",
                    "update-function-code",
                    "--function-name",
                    LAMBDA_FUNCTION_NAME,
                    "--zip-file",
                    f"fileb://{zip_path}",
                ],
                capture=True,
            )
            if not success:
                print(f"  ❌ Failed to update function code: {stderr}")
                return False

            # Wait for code update to complete
            wait_for_function_active()

            # Update configuration
            success, _, stderr = run_command(
                [
                    "aws",
                    "lambda",
                    "update-function-configuration",
                    "--function-name",
                    LAMBDA_FUNCTION_NAME,
                    "--layers",
                    layer_arn,
                    "--environment",
                    f"Variables={{IDP_STACK_NAME={idp_stack_name}}}",
                ],
                capture=True,
            )
        else:
            # Create new function
            print("  Creating new function...")
            success, _, stderr = run_command(
                [
                    "aws",
                    "lambda",
                    "create-function",
                    "--function-name",
                    LAMBDA_FUNCTION_NAME,
                    "--runtime",
                    RUNTIME,
                    "--role",
                    role_arn,
                    "--handler",
                    "lambda_function.handler",
                    "--zip-file",
                    f"fileb://{zip_path}",
                    "--layers",
                    layer_arn,
                    "--timeout",
                    "300",
                    "--memory-size",
                    "512",
                    "--environment",
                    f"Variables={{IDP_STACK_NAME={idp_stack_name}}}",
                ],
                capture=True,
            )

        if not success:
            print(f"  ❌ Failed to deploy function: {stderr}")
            return False

    print(f"  ✅ Function deployed: {LAMBDA_FUNCTION_NAME}")
    return True


def wait_for_function_active() -> bool:
    """Wait for Lambda function to become Active and not updating."""
    import time

    print("  Waiting for function to become active...")
    for _ in range(60):  # Wait up to 60 seconds
        success, stdout, _ = run_command(
            [
                "aws",
                "lambda",
                "get-function",
                "--function-name",
                LAMBDA_FUNCTION_NAME,
                "--query",
                "Configuration.[State, LastUpdateStatus]",
                "--output",
                "text",
            ],
            capture=True,
        )
        if success and stdout:
            parts = stdout.strip().split()
            state = parts[0] if len(parts) > 0 else ""
            update_status = parts[1] if len(parts) > 1 else ""

            # Function is ready when State=Active and LastUpdateStatus=Successful (or not present)
            if state == "Active" and update_status in ("Successful", "None", ""):
                print("  ✅ Function is active")
                return True
            # If state says InProgress, keep waiting
            elif update_status == "InProgress":
                pass  # Keep waiting

        time.sleep(2)

    print("  ⚠️ Function still not ready after 60 seconds")
    return False


def test_function(idp_stack_name: str, s3_uri: str = None) -> bool:
    """Test the Lambda function with sample events."""
    print("\n🧪 Testing Lambda function...")

    # Wait for function to become active
    wait_for_function_active()

    test_cases = [
        {
            "name": "Config Create",
            "event": {
                "action": "config",
                "operation": "create",
                "features": "min",
                "pattern": "pattern-2",
            },
            "expect_success": True,
        },
        {
            "name": "Config Download",
            "event": {
                "action": "config",
                "operation": "download",
                "stack_name": idp_stack_name,
            },
            "expect_success": True,
        },
    ]

    # Add processing test if S3 URI provided
    if s3_uri:
        test_cases.append(
            {
                "name": "Process Documents",
                "event": {
                    "action": "process",
                    "stack_name": idp_stack_name,
                    "s3_uri": s3_uri,
                    "batch_prefix": "lambda-test",
                },
                "expect_success": True,
            }
        )

    results = []
    for test in test_cases:
        print(f"\n  Testing: {test['name']}")
        print(f"  Event: {json.dumps(test['event'], indent=2)}")

        # Invoke function
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(test["event"], f)
            payload_file = f.name

        try:
            success, stdout, stderr = run_command(
                [
                    "aws",
                    "lambda",
                    "invoke",
                    "--function-name",
                    LAMBDA_FUNCTION_NAME,
                    "--payload",
                    f"fileb://{payload_file}",
                    "--cli-read-timeout",
                    "300",
                    "/dev/stdout",
                ],
                capture=True,
            )

            if success and stdout:
                # Parse response - Lambda invoke appends metadata, extract JSON response
                try:
                    # Find the first JSON object (Lambda response) before AWS metadata
                    import re

                    match = re.search(
                        r'\{.*?"statusCode".*?\}(?=\s*\{)', stdout, re.DOTALL
                    )
                    if match:
                        response_str = match.group(0)
                    else:
                        # Try to find any JSON with statusCode
                        response_str = (
                            stdout.split("{")[0]
                            + "{"
                            + stdout.split("{", 1)[1].split("}")[0]
                            + "}"
                        )
                        # Actually just find first complete JSON
                        first_brace = stdout.find("{")
                        if first_brace >= 0:
                            # Find matching closing brace for Lambda response
                            response_str = stdout[first_brace:]
                            # Find where the AWS metadata starts
                            meta_start = response_str.find('{\n    "StatusCode"')
                            if meta_start > 0:
                                response_str = response_str[:meta_start]

                    response = json.loads(response_str)
                    status_code = response.get("statusCode", 500)
                    body_str = response.get("body", "{}")
                    body = (
                        json.loads(body_str) if isinstance(body_str, str) else body_str
                    )

                    if status_code == 200:
                        print(f"  ✅ PASSED (status: {status_code})")
                        body_preview = (
                            json.dumps(body, indent=2)[:500]
                            if isinstance(body, dict)
                            else str(body)[:500]
                        )
                        print(f"  Response: {body_preview}...")
                        results.append(True)
                    else:
                        print(f"  ❌ FAILED (status: {status_code})")
                        error_msg = (
                            body.get("error", "Unknown")
                            if isinstance(body, dict)
                            else str(body)
                        )
                        print(f"  Error: {error_msg}")
                        results.append(False)
                except (json.JSONDecodeError, AttributeError) as e:
                    # Check if statusCode 200 appears in output
                    if '"statusCode": 200' in stdout or '"statusCode":200' in stdout:
                        print(f"  ✅ PASSED (response parsing note: {e})")
                        results.append(True)
                    else:
                        print(f"  ⚠️ Could not parse response: {stdout[:300]}")
                        results.append(False)
            else:
                print(f"  ❌ FAILED to invoke: {stderr}")
                results.append(False)
        finally:
            os.unlink(payload_file)

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n📊 Test Results: {passed}/{total} passed")

    return all(results)


def cleanup() -> bool:
    """Delete Lambda function and layer."""
    print("\n🧹 Cleaning up...")

    role_name = f"{LAMBDA_FUNCTION_NAME}-role"
    policy_name = f"{LAMBDA_FUNCTION_NAME}-idp-policy"

    # Delete function
    print(f"  Deleting function: {LAMBDA_FUNCTION_NAME}")
    run_command(
        ["aws", "lambda", "delete-function", "--function-name", LAMBDA_FUNCTION_NAME],
        capture=True,
    )

    # Delete role policy
    run_command(
        [
            "aws",
            "iam",
            "delete-role-policy",
            "--role-name",
            role_name,
            "--policy-name",
            policy_name,
        ],
        capture=True,
    )

    # Detach managed policy
    run_command(
        [
            "aws",
            "iam",
            "detach-role-policy",
            "--role-name",
            role_name,
            "--policy-arn",
            f"arn:{aws_partition()}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        ],
        capture=True,
    )

    # Delete role
    print(f"  Deleting role: {role_name}")
    run_command(
        ["aws", "iam", "delete-role", "--role-name", role_name],
        capture=True,
    )

    # Delete layer versions
    print(f"  Deleting layer versions: {LAYER_NAME}")
    success, stdout, _ = run_command(
        [
            "aws",
            "lambda",
            "list-layer-versions",
            "--layer-name",
            LAYER_NAME,
            "--query",
            "LayerVersions[*].Version",
            "--output",
            "json",
        ],
        capture=True,
    )

    if success and stdout:
        try:
            versions = json.loads(stdout)
            for version in versions:
                run_command(
                    [
                        "aws",
                        "lambda",
                        "delete-layer-version",
                        "--layer-name",
                        LAYER_NAME,
                        "--version-number",
                        str(version),
                    ],
                    capture=True,
                )
        except json.JSONDecodeError:
            pass

    print("  ✅ Cleanup complete")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Deploy and test IDP SDK Lambda example",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stack-name",
        type=str,
        required=True,
        help="IDP CloudFormation stack name",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Deploy Lambda layer and function",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test the deployed Lambda function",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete Lambda function and layer",
    )
    parser.add_argument(
        "--s3-uri",
        type=str,
        help="S3 URI for document processing test (optional)",
    )
    args = parser.parse_args()

    if not any([args.deploy, args.test, args.cleanup]):
        parser.print_help()
        print("\n❌ Must specify --deploy, --test, or --cleanup")
        return 1

    # Get SDK directory
    examples_dir = Path(__file__).parent
    sdk_dir = examples_dir.parent

    if args.cleanup:
        cleanup()
        return 0

    if args.deploy:
        # Create layer
        layer_arn = create_layer(sdk_dir)
        if not layer_arn:
            return 1

        # Deploy function
        if not deploy_function(layer_arn, args.stack_name):
            return 1

        print("\n✅ Deployment complete!")
        print(f"   Function: {LAMBDA_FUNCTION_NAME}")
        print(f"   Layer: {LAYER_NAME}")

    if args.test:
        # Get layer ARN if not just deployed
        if not args.deploy:
            layer_arn = get_latest_layer_arn()
            if not layer_arn:
                print("❌ No layer found. Run with --deploy first.")
                return 1

        if not test_function(args.stack_name, args.s3_uri):
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
