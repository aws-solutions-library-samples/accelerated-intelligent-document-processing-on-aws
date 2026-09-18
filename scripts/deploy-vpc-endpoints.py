#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
deploy-vpc-endpoints.py

Cross-platform script (Windows, macOS, Linux) that:
  1. Reads LambdaSubnetIds and LambdaVpcSecurityGroupId from the IDP stack
  2. Checks which of the required VPC Interface Endpoints already exist
  3. Deploys only the MISSING ones via CloudFormation
  4. Waits for the deployment to complete and reports the result

Usage:
    python scripts/deploy-vpc-endpoints.py \\
        --vpc-id <vpc-id> \\
        --stack-name IDP-PRIVATE \\
        [--endpoints-stack-name IDP-PRIVATE-VPCEndpoints] \\
        [--subnet-ids subnet-a,subnet-b] \\
        [--region us-east-1] \\
        [--profile <aws-profile>] \\
        [--dry-run]

Requirements:
    pip install boto3
    (boto3 is already required by publish.py)
"""

import argparse
import sys
import time
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# The 16 Interface endpoint services for an IDP private deployment
# (ApiGatewayVisibility=PRIVATE) — 14 required by the application plus
# ssmmessages/ec2messages, which are only needed for an SSM Session Manager
# bastion. Maps CFN parameter name → AWS service suffix.
#
# NOTE: Cognito endpoints (cognito-idp, cognito-identity) are intentionally
# NOT included. The IDP stack creates a Cognito domain (Hosted UI / Managed Login)
# which is incompatible with PrivateLink — AWS blocks all cognito-idp VPC endpoint
# calls when a domain is configured. Cognito auth traffic must flow through the
# NAT gateway to the public Cognito service instead.
#
# NOTE: appsync-api / appsync are deliberately absent. AWS AppSync has been
# removed from the solution, so neither endpoint is required. vpc-endpoints.yaml
# still declares CreateAppSyncApiEndpoint / CreateAppSyncControlEndpoint so an
# older parameter file keeps deploying, but both default to "false"; listing them
# here would report them as "missing — will create" when this script never
# creates them.
REQUIRED_ENDPOINTS = {
    # The private API Gateway REST API the Web UI calls — and the UI itself when
    # WebUIHosting=APIGateway. A PRIVATE deployment is unreachable without it.
    "CreateExecuteApiEndpoint":      "execute-api",
    "CreateSqsEndpoint":             "sqs",
    "CreateStatesEndpoint":          "states",
    "CreateKmsEndpoint":             "kms",
    "CreateLogsEndpoint":            "logs",
    "CreateBedrockRuntimeEndpoint":  "bedrock-runtime",
    "CreateSsmEndpoint":             "ssm",
    "CreateSsmMessagesEndpoint":     "ssmmessages",
    "CreateEc2MessagesEndpoint":     "ec2messages",
    "CreateSecretsManagerEndpoint":  "secretsmanager",
    "CreateLambdaEndpoint":          "lambda",
    "CreateEventsEndpoint":          "events",
    "CreateAthenaEndpoint":          "athena",
    # OCR pattern: ocr/service.py calls Textract API
    "CreateTextractEndpoint":        "textract",
    # BDA pattern: bda/bda_service.py and bda/blueprint_optimizer.py call STS AssumeRole
    "CreateStsEndpoint":             "sts",
    # CloudWatch Metrics & Dashboards — workflow_tracker publishes custom metrics,
    # dashboard_merger manages CloudWatch dashboards
    "CreateMonitoringEndpoint":      "monitoring",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Deploy IDP VPC Endpoints — skips any that already exist.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--vpc-id", required=True, help="VPC ID where IDP is deployed")
    parser.add_argument("--stack-name", required=True, help="Name of the IDP CloudFormation stack")
    parser.add_argument(
        "--endpoints-stack-name",
        help="Name for the VPC endpoints stack (default: <stack-name>-VPCEndpoints)",
    )
    parser.add_argument(
        "--subnet-ids",
        help="Comma-separated subnet IDs (auto-read from IDP stack if omitted)",
    )
    parser.add_argument(
        "--route-table-ids",
        help="Comma-separated route table IDs for S3/DynamoDB Gateway endpoints (free). "
             "When provided, creates S3 and DynamoDB Gateway endpoints in addition to "
             "Interface endpoints. Get the route table ID from your VPC stack outputs.",
    )
    parser.add_argument("--region", default=None, help="AWS region (default: from AWS config)")
    parser.add_argument("--profile", default=None, help="AWS CLI profile name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check and print what would be deployed without actually deploying",
    )
    return parser.parse_args()


def get_boto3_session(profile, region):
    kwargs = {}
    if profile:
        kwargs["profile_name"] = profile
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)


def get_stack_output(cf_client, stack_name, output_key):
    """Read a single Output value from a CloudFormation stack."""
    resp = cf_client.describe_stacks(StackName=stack_name)
    outputs = resp["Stacks"][0].get("Outputs", [])
    for o in outputs:
        if o["OutputKey"] == output_key:
            return o["OutputValue"]
    return None


def get_stack_parameter(cf_client, stack_name, param_key):
    """Read a single Parameter value from a CloudFormation stack."""
    resp = cf_client.describe_stacks(StackName=stack_name)
    params = resp["Stacks"][0].get("Parameters", [])
    for p in params:
        if p["ParameterKey"] == param_key:
            return p.get("ParameterValue", "")
    return None


def endpoint_exists(ec2_client, vpc_id, region, service_suffix):
    """Return True if an active endpoint for the service already exists in the VPC."""
    full_service = f"com.amazonaws.{region}.{service_suffix}"
    resp = ec2_client.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [full_service]},
        ]
    )
    active = [e for e in resp["VpcEndpoints"] if e["State"] != "deleted"]
    return len(active) > 0


def wait_for_stack(cf_client, stack_name, target_status="CREATE_COMPLETE"):
    """Poll until the stack reaches a terminal status."""
    terminal_states = {
        "CREATE_COMPLETE", "CREATE_FAILED", "ROLLBACK_COMPLETE",
        "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
        "DELETE_COMPLETE", "DELETE_FAILED",
    }
    print(f"⏳ Waiting for stack '{stack_name}' to reach {target_status}...")
    dots = 0
    while True:
        try:
            resp = cf_client.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
        except ClientError as e:
            if "does not exist" in str(e):
                print(f"\n❌ Stack '{stack_name}' does not exist.")
                return False
            raise
        if status in terminal_states:
            print()  # newline after dots
            return status == target_status
        print(".", end="", flush=True)
        dots += 1
        if dots % 30 == 0:
            print(f" [{status}]")
        time.sleep(10)


def main():
    args = parse_args()

    endpoints_stack_name = args.endpoints_stack_name or f"{args.stack_name}-VPCEndpoints"

    # ── Set up boto3 session ──────────────────────────────────────────────
    try:
        session = get_boto3_session(args.profile, args.region)
        region = args.region or session.region_name or "us-east-1"
        ec2 = session.client("ec2", region_name=region)
        cf = session.client("cloudformation", region_name=region)
    except NoCredentialsError:
        print("❌ No AWS credentials found. Configure with 'aws configure' or set environment variables.")
        sys.exit(1)

    # ── Read Lambda SG from IDP stack ────────────────────────────────────
    print(f"🔍 Reading IDP stack outputs from: {args.stack_name}")
    try:
        lambda_sg = get_stack_output(cf, args.stack_name, "LambdaVpcSecurityGroupId")
    except ClientError as e:
        print(f"❌ Could not read stack '{args.stack_name}': {e}")
        print("   Make sure the stack is CREATE_COMPLETE and ApiGatewayVisibility=PRIVATE.")
        sys.exit(1)

    if not lambda_sg:
        print(f"❌ Output 'LambdaVpcSecurityGroupId' not found in stack '{args.stack_name}'.")
        print("   Make sure ApiGatewayVisibility=PRIVATE was set when the stack was created.")
        sys.exit(1)
    print(f"   Lambda SG: {lambda_sg}")

    # ── Auto-lookup VPC CIDR ──────────────────────────────────────────────
    # The endpoint security group must also allow inbound HTTPS from the VPC
    # CIDR so that browsers running inside the VPC (WorkSpaces, VPN clients,
    # bastions) can reach the execute-api Interface Endpoint directly.
    print(f"🔍 Looking up VPC CIDR for {args.vpc_id}...")
    try:
        vpc_resp = ec2.describe_vpcs(VpcIds=[args.vpc_id])
        vpc_cidr = vpc_resp["Vpcs"][0]["CidrBlock"]
    except ClientError as e:
        print(f"❌ Could not look up VPC CIDR for '{args.vpc_id}': {e}")
        sys.exit(1)
    print(f"   VPC CIDR:  {vpc_cidr}")

    # ── Read subnet IDs ───────────────────────────────────────────────────
    subnet_ids = args.subnet_ids
    if not subnet_ids:
        print(f"🔍 Reading LambdaSubnetIds from IDP stack parameters...")
        subnet_ids = get_stack_parameter(cf, args.stack_name, "LambdaSubnetIds")
    if not subnet_ids:
        print("❌ Could not determine subnet IDs. Pass --subnet-ids explicitly.")
        sys.exit(1)
    print(f"   Subnets:   {subnet_ids}")

    # ── Check each endpoint ───────────────────────────────────────────────
    print(f"\n🔍 Checking existing VPC endpoints in {args.vpc_id} (region: {region})...\n")

    skip_params = {}    # CFN param name → "false"
    create_list = []    # service suffixes to create
    skip_list = []      # service suffixes to skip

    for param, service in sorted(REQUIRED_ENDPOINTS.items()):
        full = f"com.amazonaws.{region}.{service}"
        if endpoint_exists(ec2, args.vpc_id, region, service):
            print(f"   ✅ {full:<50} already exists — will skip")
            skip_params[param] = "false"
            skip_list.append(service)
        else:
            print(f"   ➕ {full:<50} missing — will create")
            create_list.append(service)

    # ── Warn if Cognito endpoints exist (they break login) ───────────────
    cognito_found = []
    for service in ("cognito-idp", "cognito-identity"):
        if endpoint_exists(ec2, args.vpc_id, region, service):
            cognito_found.append(service)
            full = f"com.amazonaws.{region}.{service}"
            print(f"   ⚠️  {full:<50} detected — will break login!")
    if cognito_found:
        print("\n⚠️  WARNING: Cognito VPC endpoints detected in this VPC!")
        print("   AWS blocks PrivateLink access when the User Pool has a domain configured.")
        print("   Login will fail with:")
        print('     "PrivateLink access is disabled for the user pool that has ManagedLogin configured"')
        print("   Fix: delete the cognito-idp and cognito-identity VPC endpoints from this VPC.\n")

    print(f"\n📊 Summary: {len(create_list)} to create, {len(skip_list)} already exist")

    if not create_list:
        print("\n✅ All required VPC endpoints already exist. Nothing to deploy.")
        sys.exit(0)

    # ── Deploy ────────────────────────────────────────────────────────────
    print(f"\n🚀 Deploying VPC endpoints stack: {endpoints_stack_name}")

    # Read template from local file
    template_path = "scripts/vpc-endpoints.yaml"
    try:
        with open(template_path, "r") as f:
            template_body = f.read()
    except FileNotFoundError:
        print(f"❌ Template not found: {template_path}")
        print("   Run this script from the repository root directory.")
        sys.exit(1)

    # Check if stack already exists (must happen before building parameters below)
    stack_exists = False
    try:
        resp = cf.describe_stacks(StackName=endpoints_stack_name)
        existing_status = resp["Stacks"][0]["StackStatus"]
        if existing_status == "ROLLBACK_COMPLETE":
            print(f"   Stack is in ROLLBACK_COMPLETE — deleting before re-creating...")
            cf.delete_stack(StackName=endpoints_stack_name)
            if not wait_for_stack(cf, endpoints_stack_name, "DELETE_COMPLETE"):
                # Stack deleted successfully (won't exist anymore)
                pass
            stack_exists = False
        else:
            stack_exists = True
    except ClientError as e:
        if "does not exist" in str(e):
            stack_exists = False
        else:
            raise

    # ── Build CFN parameters ──────────────────────────────────────────────
    parameters = [
        {"ParameterKey": "IDPStackName",           "ParameterValue": args.stack_name},
        {"ParameterKey": "VpcId",                  "ParameterValue": args.vpc_id},
        {"ParameterKey": "SubnetIds",              "ParameterValue": subnet_ids},
        {"ParameterKey": "LambdaSecurityGroupId",  "ParameterValue": lambda_sg},
        {"ParameterKey": "VpcCidr",                "ParameterValue": vpc_cidr},
    ]
    if args.route_table_ids:
        parameters.append({"ParameterKey": "RouteTableIds", "ParameterValue": args.route_table_ids})
        print(f"   Route Tables: {args.route_table_ids} (will create S3 + DynamoDB Gateway endpoints)")
    # IMPORTANT: For UPDATE operations, never pass CreateXxx=false for endpoints
    # that are already managed by the stack — CloudFormation would DELETE them.
    # Instead, use UsePreviousValue=true so existing resources are retained.
    # For CREATE operations (new stack), pass "false" explicitly so they are skipped.
    for param in skip_params:
        if stack_exists:
            parameters.append({"ParameterKey": param, "UsePreviousValue": True})
        else:
            parameters.append({"ParameterKey": param, "ParameterValue": "false"})

    # ── Dry run ───────────────────────────────────────────────────────────
    if args.dry_run:
        print("\n🔍 DRY RUN — would deploy with these parameters:")
        for p in parameters:
            print(f"   {p['ParameterKey']}={p.get('ParameterValue', '<UsePreviousValue>')}")
        print(f"\nStack: {endpoints_stack_name}")
        print("Run without --dry-run to deploy.")
        sys.exit(0)

    try:
        if stack_exists:
            print(f"   Stack exists — updating...")
            cf.update_stack(
                StackName=endpoints_stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=["CAPABILITY_IAM"],
            )
            target_status = "UPDATE_COMPLETE"
        else:
            print(f"   Creating new stack...")
            cf.create_stack(
                StackName=endpoints_stack_name,
                TemplateBody=template_body,
                Parameters=parameters,
                Capabilities=["CAPABILITY_IAM"],
            )
            target_status = "CREATE_COMPLETE"
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            print("✅ Stack is already up to date. No changes needed.")
            sys.exit(0)
        print(f"❌ Failed to deploy stack: {e}")
        sys.exit(1)

    # ── Wait for completion ───────────────────────────────────────────────
    success = wait_for_stack(cf, endpoints_stack_name, target_status)

    if success:
        print(f"✅ VPC endpoints deployed successfully!")
        print(f"\nEndpoints created:")
        for svc in create_list:
            print(f"   • com.amazonaws.{region}.{svc}")
        if skip_list:
            print(f"\nEndpoints skipped (already existed):")
            for svc in skip_list:
                print(f"   • com.amazonaws.{region}.{svc}")
    else:
        # Show failure reason
        try:
            events = cf.describe_stack_events(StackName=endpoints_stack_name)["StackEvents"]
            failed = [e for e in events if "FAILED" in e.get("ResourceStatus", "")]
            if failed:
                print("\nFailure reasons:")
                for e in failed[:5]:
                    print(f"   {e['LogicalResourceId']}: {e.get('ResourceStatusReason', 'unknown')}")
        except Exception:
            pass
        print(f"\n❌ Deployment failed. Check the CloudFormation console for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
