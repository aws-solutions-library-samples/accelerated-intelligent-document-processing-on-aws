---
title: "AWS Services and IAM Role Requirements for GenAI IDP Accelerator"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# AWS Services and IAM Role Requirements for GenAI IDP Accelerator

This document outlines the AWS services used by the GenAI Intelligent Document Processing (IDP) Accelerator solution, along with the IAM role scopes needed for deployment and operation.

> **Architecture note:** The solution now uses a **unified pattern stack** (`patterns/unified/`) controlled by the `use_bda` configuration flag, rather than the historical separate Pattern 1/2/3 stacks. "BDA mode" (`use_bda: true`) uses Bedrock Data Automation; "Pipeline mode" (`use_bda: false`) uses Textract OCR + Bedrock classification/extraction. References to "Pattern 1/2/3" below are retained only where they aid historical understanding.

## AWS Services Used

### Core Infrastructure Services

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **Amazon S3** | Stores input documents, processed outputs, and web UI assets | ✓ | ✓ |
| **Amazon DynamoDB** | Tracks document processing, manages configurations and concurrency | ✓ | ✓ |
| **AWS Lambda** | Executes document processing functions and business logic | ✓ | ✓ |
| **AWS Step Functions** | Orchestrates document processing workflows | ✓ | ✓ |
| **Amazon SQS** | Queues documents for processing and handles throttling | ✓ | ✓ |
| **Amazon EventBridge** | Triggers document processing workflows when files are uploaded | ✓ | ✓ |
| **Amazon CloudFront** | Delivers the web UI with global distribution (default hosting mode) | ✓ | ✓ |
| **Amazon API Gateway** | Backs the web UI's data API, and can alternatively serve the web UI itself (S3 proxy) for VPC-based deployments (see [API Gateway Hosting](./apigateway-hosting.md)) | ✓ | ✓ |
| **Amazon ECR** | Stores container images for the pattern processing Lambda functions (OCR, classification, extraction, etc., which are deployed as container images) | ✓ | ✓ |
| **AWS CloudFormation** | Deploys and manages the solution infrastructure | ✓ | |
| **AWS SAM** | Simplifies serverless application deployment | ✓ | |
| **AWS CodeBuild** | Builds and packages the web UI assets and pattern container images | ✓ | |
| **AWS Systems Manager (Parameter Store)** | Stores and retrieves runtime configuration/settings parameters | ✓ | ✓ |

### AI/ML Services

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **Amazon Bedrock** | Provides foundation models for document understanding | ✓ | ✓ |
| **Amazon Bedrock Guardrails** | Enforces content safety, information security, model usage policies, and [Automated Reasoning Checks](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-automated-reasoning.html) | ✓ | ✓ |
| **Amazon Textract** | Extracts text and data from documents (OCR) in Pipeline mode | | ✓ |
| **Amazon SageMaker (MLflow)** | Optional managed MLflow tracking server for logging processing metrics/experiments (enabled via `MlflowTrackingServerArn`) | | ✓ |
| **Amazon Bedrock Knowledge Base** | Enables semantic document querying (optional) — backed by S3 Vectors (default) or OpenSearch Serverless | ✓ | ✓ |
| **Bedrock Data Automation (BDA)** | Automates document processing workflows (BDA mode, `use_bda: true`) | ✓ | ✓ |
| **Amazon Bedrock AgentCore** | Optional MCP gateway for external application access (enabled via `EnableMCP`) | ✓ | ✓ |

### Auth & API Services

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **Amazon Cognito** | Manages user authentication and authorization | ✓ | ✓ |
| **AWS AppSync** | Provides GraphQL API for the web UI | ✓ | ✓ |
| **AWS WAF** | Protects web applications from web exploits (optional) | ✓ | ✓ |
| **AWS Marketplace (Agreement / Catalog / Entitlement)** | Subscription checks for paid Feature Platform extensions. In the **host** stack, buyer-side `SearchAgreements`. In the optional **Seller Entitlement Service** (deployed separately, into a *seller* account), seller-side `SearchAgreements` + `ListEntities` | — | ✓ |

### Monitoring & Operations

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **Amazon CloudWatch** | Provides monitoring, logging, and alerting | ✓ | ✓ |
| **AWS SNS** | Delivers operational alerts and notifications | ✓ | ✓ |
| **AWS KMS** | Manages encryption keys for secure data storage | ✓ | ✓ |

### Analytics & Reporting

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **AWS Glue** | Data Catalog (database + tables) and crawler for evaluation/reporting metrics, including the `metering_hourly`, `metering_daily`, `metering_docs_hourly`, `metering_docs_daily`, `control_plane_hourly`, and `data_plane_lambda_hourly` rollup tables added by the Reporting SQL Layer | ✓ | ✓ |
| **Amazon Athena** | Queries evaluation/metering/rollup tables for analytics; scheduled `DataMartRollupFunction` writes `INSERT INTO` the rollup tables hourly + daily | ✓ | ✓ |
| **AWS Resource Groups Tagging API** | The `DataMartRollupFunction` uses `tag:GetResources` to discover Lambdas in this stack's tree (root + nested) for control-plane cost attribution | | ✓ |
| **Amazon OpenSearch Serverless** | Optional vector store for the Bedrock Knowledge Base (the default vector store is S3 Vectors; `KnowledgeBaseVectorStore: OPENSEARCH_SERVERLESS` selects this instead) | ✓ | ✓ |

## IAM Role Requirements

### Enterprise Deployment Considerations

For organizations with Service Control Policies (SCPs) that mandate permissions boundaries on all IAM roles, the solution provides comprehensive support through the `PermissionsBoundaryArn` parameter. This optional parameter can be specified during deployment to attach a permissions boundary to all IAM roles (both explicit roles and implicit roles created by AWS SAM functions).

**Usage:**
```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --parameter-overrides PermissionsBoundaryArn=arn:aws:iam::123456789012:policy/MyPermissionsBoundary \
  --capabilities CAPABILITY_IAM
```

When no permissions boundary is specified, roles deploy normally, ensuring backward compatibility.

### Deployment Roles

Deploying this solution requires an IAM role/user with the following permissions.

> **Ready-to-use CloudFormation service role:** Rather than granting these
> permissions directly to deploying users, administrators can provision the
> example **CloudFormation service role** in
> [iam-roles/cloudformation-management/](../iam-roles/cloudformation-management/README.md).
> It bundles the deployment permissions below into a single role that
> CloudFormation assumes on a user's behalf, so developers/DevOps can deploy and
> manage IDP stacks with only `iam:PassRole` instead of broad administrator
> access. See also [Deployment → Administrator Access Requirements](./deployment.md#administrator-access-requirements).

#### Essential Permissions
* `cloudformation:*` - Create and manage CloudFormation stacks
* `iam:*` - Create and manage IAM roles and policies
* `lambda:*` - Create and configure Lambda functions
* `states:*` - Create and manage Step Functions state machines
* `s3:*` - Create buckets and manage S3 resources
* `dynamodb:*` - Create and configure DynamoDB tables
* `sqs:*` - Create and configure SQS queues
* `events:*` - Create and configure EventBridge rules
* `cloudfront:*` - Create and configure CloudFront distributions
* `cognito-idp:*` - Create and configure Cognito user pools 
* `cognito-identity:*` - Create and configure Cognito identity pools for AWS service access
* `appsync:*` - Create and configure AppSync APIs
* `logs:*` - Create and configure CloudWatch log groups
* `cloudwatch:*` - Create and configure CloudWatch dashboards and alarms
* `sns:*` - Create and configure SNS topics

#### Feature-Specific Permissions
* `bedrock:*` - Create and invoke Bedrock resources (all modes)
* `textract:*` - OCR via Amazon Textract (Pipeline mode)
* `ecr:*` - Create ECR repositories and push pattern container images
* `glue:*`, `athena:*` - Create the reporting database/tables and run analytics queries (evaluation reporting)
* `aoss:*` / `opensearch-serverless:*` - Create OpenSearch Serverless collections (Knowledge Base feature, only when `KnowledgeBaseVectorStore: OPENSEARCH_SERVERLESS`; the default S3 Vectors store does not need this)
* `sagemaker:*` - Optional MLflow tracking server integration (only when MLflow is enabled)
* `kms:*` - Create KMS keys for encryption
* `wafv2:*` - Configure WAF rules (optional)
* `glue:*` / `codebuild:*` / `ssm:*` - Supporting build, configuration, and reporting infrastructure

> **Note:** Earlier releases used Amazon SageMaker to host a UDOP classification endpoint (the former "Pattern 3"). The unified architecture no longer deploys a SageMaker inference endpoint; document classification is performed by Bedrock foundation models (with optional custom/fine-tuned model ARNs). SageMaker now appears only in the optional MLflow tracking integration.

### Runtime Roles

The solution creates various IAM roles to run different components of the system. Key role scopes include:

#### Document Processing Roles
* **Queue Processing Role**:
  * `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`
  * `states:StartExecution`
  * `states:ListExecutions` (read-only, scoped to this stack's state machine — used to reconcile the workflow-concurrency counter against the executions that are really running, so a leaked counter cannot permanently stop the stack admitting documents)
  * `cloudwatch:PutMetricData` (restricted by an `IAM` condition to the stack's own metric namespace — publishes the concurrency-counter drift the `ConcurrencyCounterDriftAlarm` watches)
  * `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

* **Step Functions Execution Role**:
  * `lambda:InvokeFunction`
  * `states:*`
  * `events:PutEvents`

* **OCR Processing Role**:
  * `textract:AnalyzeDocument`, `textract:DetectDocumentText`
  * `s3:GetObject`, `s3:PutObject`
  * `logs:*`

* **Classification Role**:
  * `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`, `bedrock:GetInferenceProfile`
  * `bedrock:ApplyGuardrail` (when Guardrails configured)
  * `s3:GetObject`, `s3:PutObject`
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` (tracking & configuration tables)
  * `cloudwatch:PutMetricData`
  * `logs:*`
  * (Optional custom classification model invoked via Lambda hook / custom model ARN; no SageMaker endpoint is used.)

* **Extraction Role**:
  * `bedrock:InvokeModel`
  * `bedrock:ApplyGuardrail` (when Guardrails configured)
  * `s3:GetObject`, `s3:PutObject`
  * `logs:*`

* **BDA Integration Role** (BDA mode, `use_bda: true`):
  * `bedrock:InvokeDataAutomationAsync`
  * `bedrock:GetDataAutomationProject`, `bedrock:ListDataAutomationProjects`, `bedrock:GetBlueprint`, `bedrock:GetBlueprintRecommendation`
  * `s3:GetObject`, `s3:PutObject`
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`
  * `ssm:GetParameter`, `ssm:PutParameter`
  * `cloudwatch:PutMetricData`
  * `logs:*`

#### Web UI & API Roles
* **AppSync Service Role**:
  * `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:Scan`
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`
  * `lambda:InvokeFunction`

* **API Gateway CloudWatch Logging Role** (created when `LogLevel` is `INFO` or `DEBUG`):
  * Managed policy `AmazonAPIGatewayPushToCloudWatchLogs` (assumed by `apigateway.amazonaws.com`)
  * Registered as the account-level API Gateway CloudWatch role (`AWS::ApiGateway::Account`) to enable REST API stage access logging. This setting is per account per region and is retained on stack deletion so other stacks' logging keeps working.

* **Configuration Resolver Role** (API resolver Lambda for configuration CRUD + Z3 RuleJSON generation):
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem`, `dynamodb:Query`
  * `s3:GetObject` (configuration bucket)
  * `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey*`
  * `bedrock:InvokeModel` (foundation models + inference profiles, for Z3 RuleJSON translation via `generateRuleJson` mutation)
  * `bedrock:DeleteDataAutomationProject`, `bedrock:GetDataAutomationProject`, `bedrock:DeleteBlueprint`, `bedrock:ListBlueprints`

* **Cognito Authentication Role**:
  * `appsync:GraphQL`
  * `s3:GetObject` (for UI assets and buckets)
  * `ssm:GetParameter` (for settings)

* **Knowledge Base Query Role**:
  * `bedrock:InvokeModel`
  * `bedrock:Retrieve`
  * `bedrock:RetrieveAndGenerate`
  * `bedrock:ApplyGuardrail` (when Guardrails configured)
  * `aoss:APIAccessAll` (when using the OpenSearch Serverless vector store) **or** `s3vectors:*` (when using the default S3 Vectors store)
  * `logs:*`

* **Knowledge Base Service Role**:
  * `bedrock:InvokeModel`
  * `aoss:APIAccessAll` (OpenSearch Serverless) **or** S3 Vectors access (default)
  * `s3:ListBucket`, `s3:GetObject` (when using S3 data source)

#### Monitoring & Evaluation Roles
* **CloudWatch Dashboard Role**:
  * `cloudwatch:GetDashboard`, `cloudwatch:PutDashboard`
  * `logs:DescribeLogGroups`

* **Workflow Tracking Role**:
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`
  * `cloudwatch:PutMetricData`
  * `logs:*`
  
* **Evaluation Function Role**:
  * `s3:GetObject` (from baseline bucket)
  * `s3:PutObject`, `s3:GetObject` (for output bucket)
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`
  * `bedrock:InvokeModel` (for LLM-based evaluations)
  * `appsync:GraphQL` (for updating evaluation results)
  * `cloudwatch:PutMetricData`
  * `logs:*`

* **Reporting / Analytics Roles** (evaluation reporting & analytics UI):
  * `glue:GetDatabase`, `glue:GetTable`, `glue:GetPartitions` (reporting database/tables)
  * `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution`
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` (reporting/Athena results buckets)
  * `logs:*`

* **Data-Mart Rollup Lambda Role** (`DataMartRollupFunction`, scheduled hourly + daily):
  * `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution` (writes rollup tables via `INSERT INTO`)
  * `glue:GetDatabase`, `glue:GetTable`, `glue:GetPartitions`, `glue:CreatePartition`, `glue:BatchCreatePartition` (partition management on rollup tables)
  * `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics` (`*` — API doesn't support resource-level scoping) for reading `AWS/Lambda/Duration`, `AWS/Lambda/Invocations`, `IDPControlPlane/AthenaBytesScanned`, `IDPControlPlane/BedrockInputTokens`, `IDPControlPlane/BedrockOutputTokens`
  * `tag:GetResources` (`*` — account-scoped API) for tag-based Lambda discovery
  * `cloudformation:ListStackResources` (scoped to this stack + its nested stacks) to walk the stack tree
  * `lambda:GetFunctionConfiguration` (scoped to functions in this account/region) for accurate per-Lambda memory + architecture in the cost estimate
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:GetBucketLocation` (reporting bucket only; `GetBucketLocation` is also needed by Athena's `StartQueryExecution` on the OutputLocation bucket). `HeadObject` calls are authorized by `s3:GetObject` — there is no `s3:HeadObject` IAM action
  * `s3:AbortMultipartUpload`, `s3:ListBucketMultipartUploads`, `s3:ListMultipartUploadParts` (reporting bucket only) — part of AWS's reference policy for Athena `INSERT INTO`, which switches to a multipart upload once a result part exceeds its buffer
  * `sqs:SendMessage` on its DLQ (async-failure destination)
  * KMS on the stack CMK

* **Metering Hour Migration Lambda Role** (`MeteringHourMigrationFunction`, one-shot CFN custom resource):
  * `s3:ListBucket` (reporting bucket) for listing pre-migration parquet files
  * `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` (scoped to `metering/*` under the reporting bucket) for the copy-then-delete relocation
  * KMS on the stack CMK
  * See [Reporting SQL Layer](reporting-sql-layer.md) §2.3 for the migration's purpose (backwards-compat upgrade to the `hour`-partitioned metering layout)

* **Control-Plane Lambda cost-telemetry (in-band, all control-plane Lambdas that hit Athena or Bedrock):**
  * `cloudwatch:PutMetricData` scoped to namespace `IDPControlPlane` (already granted via the existing app-metrics grant)
  * The `idp_common.metrics.emit_control_plane_cost_metric` helper emits `IDPControlPlane/{AthenaBytesScanned,BedrockInputTokens,BedrockOutputTokens}` with dims `[Component, FunctionName, Model?]` for the rollup Lambda to aggregate.

* **Glue Crawler Service Role**:
  * `glue:*` (managed `AWSGlueServiceRole`) for crawling reporting data
  * `s3:GetObject`, `s3:ListBucket`
  * `kms:Decrypt`, `kms:DescribeKey`

#### Build & Optional Feature Roles
* **CodeBuild Roles** (UI build and pattern container-image build):
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` (artifacts)
  * `ecr:*` (push/scan container images), `cloudfront:CreateInvalidation` (UI)
  * `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`
  * `ec2:*` networking actions (when deploying into a VPC)

* **AgentCore Gateway Execution Role** (optional MCP integration, `EnableMCP: true`):
  * `lambda:InvokeFunction` (MCP handler)
  * `bedrock-agentcore:InvokeAgentRuntime`
  * `logs:*`

* **Seller Entitlement Service — Activation Role** (`feature-platform/seller-entitlement-service/`, deployed **standalone into an AWS Marketplace seller account**, *not* part of the main stack):
  * `aws-marketplace:SearchAgreements`, `DescribeAgreement`, `GetAgreementTerms`, `GetEntitlements`, `ResolveCustomer` — seller-side entitlement reads. `Resource: "*"`: these actions do **not** support resource-level permissions. Read-only by design (no `BatchMeterUsage`/catalog writes), asserted by a static test.
  * `kms:Sign` on the stack's own asymmetric `TokenSigningKey` only — **not** `GetPublicKey`, `PutKeyPolicy`, or `ScheduleKeyDeletion`, so a compromised function cannot re-point trust.
  * `dynamodb:UpdateItem` on the stack's `ActivationsTable` only — write-only, so the function cannot read or delete the seller's customer roster.
  * `logs:CreateLogStream`, `logs:PutLogEvents` (via `AWSLambdaBasicExecutionRole`).
  * Accepts `PermissionsBoundaryArn` and attaches it when set.
  * Deploying operator additionally needs `marketplace-catalog:ListEntities` for the ownership preflight, plus the usual CloudFormation/IAM/KMS/DynamoDB create permissions.

> **Container-image Lambdas:** The pattern processing functions (OCR, classification, extraction, assessment, summarization, BDA, evaluation, rule validation, etc.) are deployed as **container images** from Amazon ECR. Each function's execution role therefore also includes `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`, and `ecr:BatchCheckLayerAvailability` (via a shared managed policy).

## Service Quotas Considerations

For high-volume document processing, consider requesting quota increases for:

| Service | Quota to Increase | Typical Default |
|---------|-------------------|----------------|
| Amazon Bedrock | On-demand InvokeModel tokens per minute | Varies by model |
| Amazon Bedrock | On-demand InvokeModel requests per minute | Varies by model |
| Amazon Bedrock | ApplyGuardrail requests per minute | Varies by region |
| Amazon Textract | DetectDocumentText / AnalyzeDocument transactions per second | 10-25 TPS |
| AWS Lambda | Concurrent executions | 1,000 executions |
| AWS Step Functions | State transitions per second | 2,000 transitions |
| Amazon SQS | API requests per queue | Very high by default |
| Amazon CloudWatch | PutMetricData API requests per second | 150 requests/second |
| Amazon Athena | Active DML/DDL queries | 20-25 queries |
| Bedrock Data Automation | Concurrent jobs (BDA mode) | Varies by region |

## Security Recommendations

When deploying this solution, consider the following security best practices:

1. **Encryption**:
   * Enable SSE-KMS encryption for all S3 buckets
   * Use customer-managed CMKs for sensitive data
   * Enable encryption for DynamoDB tables

2. **Network Security**:
   * Use CloudFront security features (geo-restrictions, HTTPS, etc.) or a private API Gateway endpoint for [VPC-based hosting](./apigateway-hosting.md)
   * Configure AWS WAF to protect web interfaces

3. **Authentication**:
   * Enforce MFA for admin users in Cognito
   * Set strong password policies
   * Limit admin access to necessary personnel

4. **IAM Best Practices**:
   * Use least privilege principles for all roles
   * Regularly audit and rotate credentials
   * Enable CloudTrail logging for all API actions

5. **Content Safety & Control**:
   * Configure Bedrock Guardrails with appropriate topic filters
   * Set up content blocking for sensitive information
   * Implement trace logging for guardrail activations
   * Use different guardrail configurations for different environments (dev/test/prod)

6. **Data Protection**:
   * Implement lifecycle policies for S3 objects
   * Configure appropriate retention policies for logs and data
   * Consider data residency requirements when selecting regions
