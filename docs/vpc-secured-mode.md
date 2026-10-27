---
title: "VPC Secured Mode Guide"
---

# VPC Secured Mode Guide

This guide covers whats required for supporting the GenAI IDP solution in a VPC-secured environment for enhanced network isolation and security.

## Overview

VPC-secured mode enables the GenAI IDP solution to run within your private network infrastructure, providing:

- Network isolation for Lambda functions
- Private communication between AWS services through VPC endpoints
- Enhanced security through security group controls

## Prerequisites

### VPC Infrastructure

You need an existing VPC with:
- Private subnets (minimum 2 for high availability)
- Public subnets with route to Internet Gateway (minimum 2 for high availability)
- NAT Gateway in each public subnet, with a 0.0.0.0/0 route in each private subnet route table pointing to the respective AZ's NAT Gateway.
- VPC endpoints for required AWS services (see below)
- Security groups for lambda and VPC endpoints (see below)

> **Why a NAT Gateway?** When configured in `VPC Secured Mode`, AWS CodeBuild runs in the configured private subnets of your VPC to build Docker images and needs internet access to pull base images and Python dependencies. The NAT Gateway provides outbound internet access from private subnets without exposing inbound access.

#### Environments Without Internet Access

The solution above assumes a NAT Gateway with an Internet Gateway is available. If your environment does not permit outbound internet access (e.g. air-gapped or fully isolated VPCs), you must make the following external dependencies available internally:

Dependencies to mirror:

| Dependency Type | Source | Used By |
|-----------------|--------|---------|
| Container image | ghcr.io/astral-sh/uv:0.9.6 | Dockerfile.optimized (build stage) |
| Container image | public.ecr.aws/lambda/python:3.12-arm64 | Dockerfile.optimized (runtime base) |
| Container image | moby/buildkit:buildx-stable-1 | docker buildx (pulled implicitly) |
| Python packages | pypi.org | uv pip install during Docker build |

Options for providing these dependencies:

1. Private registry + code changes — Mirror images to your internal container registry (e.g., private ECR, Artifactory, Nexus) and update 
`Dockerfile.optimized` and `patterns/unified/buildspec.yml` to reference your internal URLs. For Python packages, configure uv to use your internal PyPI 
mirror (e.g., AWS CodeArtifact) by setting UV_INDEX_URL in the Dockerfile.

2. Private DNS overrides — Configure your VPC's private hosted zone to resolve public registry domains (ghcr.io, public.ecr.aws, pypi.org) to internal 
endpoints that serve mirrored content. This approach requires no code changes but requires DNS infrastructure and internal mirrors that respond at those 
domains.

### Required VPC Endpoints

> **Note:** VPC-secured deployment is not tied to a processing mode. `DeployInVPC`,
> `VpcId`, `PrivateSubnetIds`, `LambdaSubnetIds` and `LambdaSecurityGroupId` are
> top-level stack parameters, and no condition derives from the `use_bda`
> configuration flag — so both pipeline mode and BDA mode can run in the VPC.
> ⚠️ The endpoint list below, and the [automated script](#automated-endpoint-creation),
> cover **pipeline mode** (Textract plus the Bedrock runtime). BDA mode calls
> `bedrock-data-automation-runtime`, which neither the list nor the script
> includes; add an interface endpoint for it before processing documents with
> `use_bda: true` in an environment without outbound internet access.

#### Gateway Endpoints
- **S3**: `com.amazonaws.region.s3`
- **DynamoDB**: `com.amazonaws.region.dynamodb`

> **Note:** Gateway endpoints must be associated with the route tables of the private subnets that Lambda functions are deployed into. The [automated script](#automated-endpoint-creation) handles this automatically, but if creating endpoints manually, ensure you select the correct route tables during creation.

#### Interface Endpoints
- **SQS**: `com.amazonaws.region.sqs` — queue processing
- **Step Functions**: `com.amazonaws.region.states` — workflow orchestration
- **Lambda**: `com.amazonaws.region.lambda` — cross-function invocation
- **CloudWatch Logs**: `com.amazonaws.region.logs` — runtime logging (all functions)
- **CloudWatch Monitoring**: `com.amazonaws.region.monitoring` — metrics publishing
- **KMS**: `com.amazonaws.region.kms` — encryption/decryption
- **SSM**: `com.amazonaws.region.ssm` — Parameter Store lookups
- **STS**: `com.amazonaws.region.sts` — role assumption
- **Bedrock Runtime**: `com.amazonaws.region.bedrock-runtime` — classification, extraction, assessment, summarization, evaluation, rule validation
- **Textract**: `com.amazonaws.region.textract` — OCR processing
- **EventBridge**: `com.amazonaws.region.events` — workflow event routing
- **CodeBuild**: `com.amazonaws.region.codebuild` — Docker image builds
- **ECR API**: `com.amazonaws.region.ecr.api` — container registry
- **ECR Docker**: `com.amazonaws.region.ecr.dkr` — container image pulls
- **Glue**: `com.amazonaws.region.glue` — reporting catalog
- **Execute API**: `com.amazonaws.region.execute-api` — private API Gateway access

### Security Group Requirements

Create a security group for Lambda functions with the following rules:

**Outbound Rules:**
- Type: HTTPS
- Protocol: TCP
- Port: 443
- Destination: VPC Interface Endpoint SG
- Description: Allow HTTPS traffic from lambda functions to AWS resources via VPC Interface endpoint

- Type: HTTPS
- Protocol: TCP
- Port: 443
- Destination: S3 Gateway Endpoint 
- Description: Allow HTTPS traffic from lambda functions to S3 Gateway Endpoint

- Type: HTTPS
- Protocol: TCP
- Port: 443
- Destination: DynamoDB Gateway Endpoint 
- Description: Allow HTTPS traffic from lambda functions to  DynamoDB Gateway Endpoint
***Note:*** To re-use this security group for Bastion, use the default outbound rule instead

Create a security group for your VPCEs with the following rules:

**Inbound Rules:**
- Type: HTTPS
- Protocol: TCP
- Port: 443
- Source: Lambda SG
- Description: Allow traffic to resources associated with the SG

**Outbound Rules:**
- Default Outbound rule allowing for all ports on destination 0.0.0.0/0


### Automated Endpoint Creation

Use the provided script to create all required VPC endpoints:

```bash
./scripts/create_vpc_endpoints.sh \
  --vpc-id vpc-12345678 \
  --subnet-ids subnet-aaa,subnet-bbb \
  --security-group-id sg-12345678 \
  --region us-gov-west-1

# Preview without creating
./scripts/create_vpc_endpoints.sh \
  --vpc-id vpc-12345678 \
  --subnet-ids subnet-aaa,subnet-bbb \
  --security-group-id sg-12345678 \
  --region us-gov-west-1 \
  --dry-run
```

The script skips endpoints that already exist and reports a summary of created/skipped/failed.



## Validation

After deployment, verify VPC configuration:

1. **Lambda Functions**: Check that functions are deployed in the specified VPC and subnets
2. **Network Connectivity**: Test document processing to ensure all services can communicate through VPC endpoints

## Troubleshooting

### Common Issues

#### ECR Image Pull Failures
**Symptoms**: Lambda functions fail to start with image pull errors
**Cause**: Missing ECR VPC endpoints
**Solution**:
- Create both ECR API and ECR DKR VPC endpoints
- Verify security group allows HTTPS traffic to VPC endpoints

### Diagnostic Commands

Check VPC endpoint connectivity:
```bash
# Test from EC2 instance in same VPC/subnet
nslookup bedrock-runtime.region.amazonaws.com
curl -I https://bedrock-runtime.region.amazonaws.com
```

Verify security group rules:
```bash
aws ec2 describe-security-groups --group-ids sg-12345678
```

Check Lambda VPC configuration:
```bash
aws lambda get-function-configuration --function-name <function-name>
```