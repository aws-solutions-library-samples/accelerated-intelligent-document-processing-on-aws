#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# check-vpc-endpoints.sh
#
# Checks which IDP-required VPC Interface Endpoints already exist in a VPC,
# then prints the exact `aws cloudformation deploy` command that creates only
# the MISSING ones — avoiding DNS conflicts on pre-existing endpoints.
#
# Usage:
#   ./scripts/check-vpc-endpoints.sh \
#     --vpc-id <vpc-id> \
#     --stack-name <idp-stack-name> \
#     --endpoints-stack-name <endpoints-stack-name> \
#     [--subnet-ids <subnet1,subnet2>] \
#     [--region <region>] \
#     [--profile <aws-profile>]
#
# If --subnet-ids is omitted, the script reads LambdaSubnetIds from the IDP stack.
# If --endpoints-stack-name is omitted, defaults to "<stack-name>-VPCEndpoints".
#
# Example:
#   ./scripts/check-vpc-endpoints.sh \
#     --vpc-id vpc-0f42ddb124c806ba1 \
#     --stack-name IDP-PRIVATE
#
# Output:
#   A ready-to-run aws cloudformation deploy command — copy and paste to deploy.

set -euo pipefail

# ──────────────────────────────────────────────────────────
# Defaults
# ──────────────────────────────────────────────────────────
VPC_ID=""
IDP_STACK_NAME=""
ENDPOINTS_STACK_NAME=""
SUBNET_IDS=""
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
PROFILE=""

# ──────────────────────────────────────────────────────────
# Parse arguments
# ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vpc-id)              VPC_ID="$2";                shift 2 ;;
    --stack-name)          IDP_STACK_NAME="$2";        shift 2 ;;
    --endpoints-stack-name) ENDPOINTS_STACK_NAME="$2"; shift 2 ;;
    --subnet-ids)          SUBNET_IDS="$2";            shift 2 ;;
    --region)              REGION="$2";                shift 2 ;;
    --profile)             PROFILE="$2";               shift 2 ;;
    -h|--help)
      sed -n '/^# Usage:/,/^#$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Run with --help for usage." >&2
      exit 1
      ;;
  esac
done

# ──────────────────────────────────────────────────────────
# Validate required args
# ──────────────────────────────────────────────────────────
if [[ -z "$VPC_ID" || -z "$IDP_STACK_NAME" ]]; then
  echo "Error: --vpc-id and --stack-name are required." >&2
  echo "Run with --help for usage." >&2
  exit 1
fi

[[ -z "$ENDPOINTS_STACK_NAME" ]] && ENDPOINTS_STACK_NAME="${IDP_STACK_NAME}-VPCEndpoints"

# Build AWS CLI flags
AWS_FLAGS="--region $REGION"
[[ -n "$PROFILE" ]] && AWS_FLAGS="$AWS_FLAGS --profile $PROFILE"

# ──────────────────────────────────────────────────────────
# Helper: check if an endpoint service already exists in the VPC
# Returns 0 (true) if found, 1 (false) if not
# ──────────────────────────────────────────────────────────
endpoint_exists() {
  local service_suffix="$1"
  local full_service="com.amazonaws.${REGION}.${service_suffix}"
  local count
  count=$(aws ec2 describe-vpc-endpoints $AWS_FLAGS \
    --filters \
      "Name=vpc-id,Values=${VPC_ID}" \
      "Name=service-name,Values=${full_service}" \
    --query 'length(VpcEndpoints[?State!=`deleted`])' \
    --output text 2>/dev/null || echo "0")
  [[ "$count" -gt 0 ]]
}

# ──────────────────────────────────────────────────────────
# Get Lambda SG from IDP stack output
# ──────────────────────────────────────────────────────────
echo "🔍 Checking IDP stack outputs from: $IDP_STACK_NAME" >&2
LAMBDA_SG=$(aws cloudformation describe-stacks $AWS_FLAGS \
  --stack-name "$IDP_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaVpcSecurityGroupId'].OutputValue" \
  --output text 2>/dev/null || true)

if [[ -z "$LAMBDA_SG" || "$LAMBDA_SG" == "None" ]]; then
  echo "Error: Could not read LambdaVpcSecurityGroupId from stack '$IDP_STACK_NAME'." >&2
  echo "Make sure the stack is CREATE_COMPLETE and ApiGatewayVisibility=PRIVATE." >&2
  exit 1
fi
echo "   Lambda SG: $LAMBDA_SG" >&2

# ──────────────────────────────────────────────────────────
# Get subnet IDs (from arg or from IDP stack params)
# ──────────────────────────────────────────────────────────
if [[ -z "$SUBNET_IDS" ]]; then
  echo "🔍 Reading LambdaSubnetIds from IDP stack parameters..." >&2
  SUBNET_IDS=$(aws cloudformation describe-stacks $AWS_FLAGS \
    --stack-name "$IDP_STACK_NAME" \
    --query "Stacks[0].Parameters[?ParameterKey=='LambdaSubnetIds'].ParameterValue" \
    --output text 2>/dev/null || true)
  if [[ -z "$SUBNET_IDS" || "$SUBNET_IDS" == "None" ]]; then
    echo "Error: Could not read LambdaSubnetIds from stack '$IDP_STACK_NAME'." >&2
    echo "Pass --subnet-ids manually." >&2
    exit 1
  fi
fi
echo "   Subnets: $SUBNET_IDS" >&2

# ──────────────────────────────────────────────────────────
# Interface endpoint services
#   14 required by the IDP application:
#     execute-api (the private API Gateway REST API the Web UI calls — and the
#       UI itself when WebUIHosting=APIGateway; a PRIVATE deployment is
#       unreachable without it),
#     sqs, states, kms, logs,
#     monitoring (CloudWatch — DashboardMerger custom resource),
#     bedrock-runtime, ssm (Lambda→SSM Parameter Store),
#     secretsmanager, lambda, events, athena,
#     textract (OCR pattern — ocr/service.py calls Textract),
#     sts (BDA pattern — bda/bda_service.py calls STS AssumeRole)
#   2 required only for SSM Session Manager testing bastion:
#     ssmmessages, ec2messages
#
# Deliberately NOT checked: appsync-api and appsync. AWS AppSync has been removed
# from the solution, so neither endpoint is required. vpc-endpoints.yaml still
# declares CreateAppSyncApiEndpoint / CreateAppSyncControlEndpoint so an older
# parameter file keeps deploying, but both default to "false" — checking for them
# here would report "missing — will create" for endpoints this script's own
# deploy command never creates.
# ──────────────────────────────────────────────────────────
declare -A ENDPOINTS=(
  [CreateExecuteApiEndpoint]="execute-api"
  [CreateSqsEndpoint]="sqs"
  [CreateStatesEndpoint]="states"
  [CreateKmsEndpoint]="kms"
  [CreateLogsEndpoint]="logs"
  [CreateMonitoringEndpoint]="monitoring"
  [CreateBedrockRuntimeEndpoint]="bedrock-runtime"
  [CreateSsmEndpoint]="ssm"
  [CreateSsmMessagesEndpoint]="ssmmessages"
  [CreateEc2MessagesEndpoint]="ec2messages"
  [CreateSecretsManagerEndpoint]="secretsmanager"
  [CreateLambdaEndpoint]="lambda"
  [CreateEventsEndpoint]="events"
  [CreateAthenaEndpoint]="athena"
  [CreateTextractEndpoint]="textract"
  [CreateStsEndpoint]="sts"
)

# ──────────────────────────────────────────────────────────
# Check each endpoint
# ──────────────────────────────────────────────────────────
echo "" >&2
echo "🔍 Checking existing VPC endpoints in $VPC_ID..." >&2
echo "" >&2

SKIP_PARAMS=""
SKIP_COUNT=0
CREATE_COUNT=0

# Sort for deterministic output
for PARAM in $(echo "${!ENDPOINTS[@]}" | tr ' ' '\n' | sort); do
  SERVICE="${ENDPOINTS[$PARAM]}"
  if endpoint_exists "$SERVICE"; then
    printf "   %-35s ✅ already exists — will skip\n" "com.amazonaws.<region>.$SERVICE" >&2
    SKIP_PARAMS="$SKIP_PARAMS $PARAM=false"
    ((SKIP_COUNT++)) || true
  else
    printf "   %-35s ➕ missing — will create\n" "com.amazonaws.<region>.$SERVICE" >&2
    ((CREATE_COUNT++)) || true
  fi
done

echo "" >&2
echo "📊 Summary: $CREATE_COUNT to create, $SKIP_COUNT already exist" >&2
echo "" >&2

if [[ "$CREATE_COUNT" -eq 0 ]]; then
  echo "✅ All required VPC endpoints already exist. No deployment needed." >&2
  exit 0
fi

# ──────────────────────────────────────────────────────────
# Build and print the deploy command
# ──────────────────────────────────────────────────────────
echo "📋 Run this command to deploy the missing endpoints:" >&2
echo "" >&2

PROFILE_FLAG=""
[[ -n "$PROFILE" ]] && PROFILE_FLAG=" \\\n  --profile $PROFILE"

# Build parameter-overrides string
PARAM_OVERRIDES="IDPStackName=$IDP_STACK_NAME \\\n"
PARAM_OVERRIDES+="    VpcId=$VPC_ID \\\n"
PARAM_OVERRIDES+="    SubnetIds=$SUBNET_IDS \\\n"
PARAM_OVERRIDES+="    LambdaSecurityGroupId=$LAMBDA_SG"

for PARAM in $SKIP_PARAMS; do
  PARAM_OVERRIDES+=" \\\n    $PARAM"
done

cat <<EOF
aws cloudformation deploy \\
  --stack-name ${ENDPOINTS_STACK_NAME} \\
  --template-file scripts/vpc-endpoints.yaml \\
  --capabilities CAPABILITY_IAM \\
  --region ${REGION}$(printf "%b" "$PROFILE_FLAG") \\
  --parameter-overrides \\
    $(printf "%b" "$PARAM_OVERRIDES")
EOF

# ──────────────────────────────────────────────────────────
# Check Lambda SG egress rules
# ──────────────────────────────────────────────────────────
echo "" >&2
echo "🔍 Checking Lambda SG ($LAMBDA_SG) egress rules..." >&2

EGRESS_RULES=$(aws ec2 describe-security-groups $AWS_FLAGS \
  --group-ids "$LAMBDA_SG" \
  --query 'SecurityGroups[0].IpPermissionsEgress' \
  --output json 2>/dev/null || echo "[]")

# Check if there's a broad egress rule (0.0.0.0/0 or ::/0)
HAS_BROAD_EGRESS=$(echo "$EGRESS_RULES" | python3 -c "
import json, sys
rules = json.load(sys.stdin)
for r in rules:
    for cidr in r.get('IpRanges', []):
        if cidr.get('CidrIp') == '0.0.0.0/0':
            print('true'); sys.exit(0)
    if r.get('IpProtocol') == '-1':
        print('true'); sys.exit(0)
print('false')
" 2>/dev/null || echo "false")

if [[ "$HAS_BROAD_EGRESS" == "true" ]]; then
  echo "   ✅ Lambda SG has broad egress — VPC endpoints and Gateway endpoints reachable" >&2
else
  echo "   ⚠️  Lambda SG has RESTRICTED egress (no 0.0.0.0/0 rule)" >&2
  echo "" >&2
  echo "   The vpc-endpoints.yaml stack will add egress to the VPC endpoints SG." >&2
  echo "   However, you ALSO need egress to S3 and DynamoDB Gateway endpoint prefix lists." >&2
  echo "   After deploying the endpoints stack, run:" >&2
  echo "" >&2
  # Get prefix list IDs for S3 and DynamoDB
  S3_PL=$(aws ec2 describe-prefix-lists $AWS_FLAGS \
    --filters "Name=prefix-list-name,Values=com.amazonaws.${REGION}.s3" \
    --query 'PrefixLists[0].PrefixListId' --output text 2>/dev/null || echo "pl-UNKNOWN")
  DYNAMO_PL=$(aws ec2 describe-prefix-lists $AWS_FLAGS \
    --filters "Name=prefix-list-name,Values=com.amazonaws.${REGION}.dynamodb" \
    --query 'PrefixLists[0].PrefixListId' --output text 2>/dev/null || echo "pl-UNKNOWN")
  echo "   aws ec2 authorize-security-group-egress --group-id $LAMBDA_SG \\" >&2
  echo "     --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=$S3_PL,Description=\"S3 Gateway\"}] \\" >&2
  echo "     --region $REGION" >&2
  echo "" >&2
  echo "   aws ec2 authorize-security-group-egress --group-id $LAMBDA_SG \\" >&2
  echo "     --ip-permissions IpProtocol=tcp,FromPort=443,ToPort=443,PrefixListIds=[{PrefixListId=$DYNAMO_PL,Description=\"DynamoDB Gateway\"}] \\" >&2
  echo "     --region $REGION" >&2
  echo "" >&2
fi
