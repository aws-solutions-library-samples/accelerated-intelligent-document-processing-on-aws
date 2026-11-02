#!/usr/bin/env bash
# Creates all VPC endpoints required for GenAI IDP VPC-only deployment (pipeline mode: Textract + Bedrock).
# Usage: ./create_vpc_endpoints.sh --vpc-id vpc-xxx --subnet-ids subnet-a,subnet-b --security-group-id sg-xxx --region us-gov-west-1
set -euo pipefail

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --vpc-id              VPC ID
  --subnet-ids          Comma-separated private subnet IDs
  --security-group-id   Security group ID (must allow inbound 443 from either: VPC CIDR or SG of resource using vpce)
  --region              AWS region
  --dry-run             Print commands without executing
  --help                Show this help
EOF
  exit 1
}

# Parse args
VPC_ID="" SUBNET_IDS="" SG_ID="" REGION="" DRY_RUN=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --vpc-id)           VPC_ID="$2"; shift 2;;
    --subnet-ids)       SUBNET_IDS="$2"; shift 2;;
    --security-group-id) SG_ID="$2"; shift 2;;
    --region)           REGION="$2"; shift 2;;
    --dry-run)          DRY_RUN=true; shift;;
    --help)             usage;;
    *)                  echo "Unknown option: $1"; usage;;
  esac
done

[[ -z "$VPC_ID" || -z "$SUBNET_IDS" || -z "$SG_ID" || -z "$REGION" ]] && { echo "Error: --vpc-id, --subnet-ids, --security-group-id, and --region are required"; usage; }

# Gateway endpoints (no subnet/SG needed — attached to route tables)
GATEWAY_ENDPOINTS=(
  "s3"
  "dynamodb"
)

# Interface endpoints for pipeline mode (Textract + Bedrock)
INTERFACE_ENDPOINTS=(
  "sqs"
  "states"
  "lambda"
  "logs"
  "monitoring"
  "kms"
  "ssm"
  "sts"
  "bedrock-runtime"
  "textract"
  "events"
  "codebuild"
  "ecr.api"
  "ecr.dkr"
  "glue"
)

ALL_INTERFACE=("${INTERFACE_ENDPOINTS[@]}")

# Get existing endpoints to skip duplicates
echo "Checking existing VPC endpoints in $VPC_ID..."
EXISTING=$(aws ec2 describe-vpc-endpoints \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'VpcEndpoints[?State!=`deleted`].ServiceName' \
  --output text --region "$REGION" 2>/dev/null | tr '\t' '\n')

# Get route table IDs for gateway endpoints
ROUTE_TABLES=$(aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=$VPC_ID" \
  --query 'RouteTables[].RouteTableId' \
  --output text --region "$REGION" | tr '\t' ',')

created=0 skipped=0 failed=0

run_cmd() {
  if $DRY_RUN; then
    echo "[DRY RUN] $*"
  else
    eval "$@"
  fi
}

# Create gateway endpoints
for svc in "${GATEWAY_ENDPOINTS[@]}"; do
  SVC_NAME="com.amazonaws.${REGION}.${svc}"
  if echo "$EXISTING" | grep "$SVC_NAME"; then
    echo "SKIP  (exists) $SVC_NAME"
    ((skipped++))
    continue
  fi
  echo "CREATE gateway  $SVC_NAME"
  if run_cmd aws ec2 create-vpc-endpoint \
    --vpc-id "$VPC_ID" \
    --service-name "$SVC_NAME" \
    --route-table-ids "$ROUTE_TABLES" \
    --vpc-endpoint-type Gateway \
    --region "$REGION" \
    --output text --query 'VpcEndpoint.VpcEndpointId' 2>&1; then
    ((created++))
  else
    echo "  FAILED to create $SVC_NAME"
    ((failed++))
  fi
done

# Create interface endpoints
for svc in "${ALL_INTERFACE[@]}"; do
  SVC_NAME="com.amazonaws.${REGION}.${svc}"
  if echo "$EXISTING" | grep "$SVC_NAME"; then
    echo "SKIP  (exists) $SVC_NAME"
    ((skipped++))
    continue
  fi
  echo "CREATE interface $SVC_NAME"
  if run_cmd aws ec2 create-vpc-endpoint \
    --vpc-id "$VPC_ID" \
    --service-name "$SVC_NAME" \
    --subnet-ids "${SUBNET_IDS//,/ }" \
    --security-group-ids "$SG_ID" \
    --vpc-endpoint-type Interface \
    --private-dns-enabled \
    --region "$REGION" \
    --output text --query 'VpcEndpoint.VpcEndpointId' 2>&1; then
    ((created++))
  else
    echo "  FAILED to create $SVC_NAME"
    ((failed++))
  fi
done

echo ""
echo "Done: $created created, $skipped skipped (already exist), $failed failed"
