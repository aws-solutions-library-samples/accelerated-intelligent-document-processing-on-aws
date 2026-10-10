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
| **AWS Step Functions** | Orchestrates document processing workflows and the data-mart rollup schema-migration (`DataMartMigrationStateMachine`, retry-safe/chunked/resumable — see [reporting-sql-layer.md](./reporting-sql-layer.md) §Track A and [data-mart-migration-runbook.md](./data-mart-migration-runbook.md)) | ✓ | ✓ |
| **Amazon SQS** | Queues documents for processing and handles throttling | ✓ | ✓ |
| **Amazon EventBridge** | Triggers document processing workflows when files are uploaded | ✓ | ✓ |
| **Amazon CloudFront** | Delivers the web UI with global distribution (default hosting mode) | ✓ | ✓ |
| **Amazon API Gateway** | Backs the web UI's data API — a REST API with a Cognito User Pools authorizer in front of a dispatcher Lambda, which is how every UI query and mutation reaches the backend (see [AppSync → REST API Migration](./migration-appsync-to-rest.md)). Can alternatively serve the web UI itself (S3 proxy) for VPC-based deployments (see [API Gateway Hosting](./apigateway-hosting.md)) | ✓ | ✓ |
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
| **Amazon Cognito** | Manages user authentication and authorization. The User Pool fronts the UI's REST API as a **User Pools authorizer** (authentication only — per-role authorization is enforced in each resolver, see [rbac.md](./rbac.md)), and the Identity Pool's authenticated role SigV4-signs the chat streaming Lambda Function URL | ✓ | ✓ |
| **AWS WAF** | Protects the UI's REST API from unwanted sources (optional) — a REGIONAL WAFv2 WebACL associated with the REST API stage, enabled when `WAFAllowedIPv4Ranges` is set to anything other than the allow-all default | ✓ | ✓ |
| **AWS Marketplace (Agreement / Catalog / Entitlement)** | Subscription checks for paid Feature Platform extensions. In the **host** stack, buyer-side `SearchAgreements`. In the optional **Seller Entitlement Service** (deployed separately, into a *seller* account), seller-side `SearchAgreements` + `ListEntities` | — | ✓ |

### Monitoring & Operations

| Service | Usage | Deployment | Runtime |
|---------|-------|------------|---------|
| **Amazon CloudWatch** | Provides monitoring, logging, and alerting | ✓ | ✓ |
| **AWS X-Ray** | Distributed tracing for the Lambda functions and state machines, controlled by `EnableXRayTracing` (default `true`). Traced functions need `xray:PutTraceSegments` / `xray:PutTelemetryRecords`: SAM attaches its X-Ray managed policy to any execution role it generates, and the roles declared explicitly in the templates carry `AWSXrayWriteOnlyAccess`. Billed per trace recorded — see [monitoring.md](./monitoring.md#x-ray-tracing), which also covers what `EnableXRayTracing=false` does not reach | — | ✓ |
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

> **The boundary is optional for the stack but required by the delegated
> deployment role.** If you deploy through the example CloudFormation service role
> in [iam-roles/cloudformation-management/](../iam-roles/cloudformation-management/README.md),
> a boundary is **mandatory**: that stack's `CreatedRolePermissionsBoundaryArn`
> parameter has no default, and the same ARN must be passed here as
> `PermissionsBoundaryArn`. The service role's `iam:CreateRole` grant carries an
> `iam:PermissionsBoundary` condition, which is the mechanism that stops a
> delegated deployment identity from being able to create a role more powerful
> than itself. Deploying with an empty `PermissionsBoundaryArn` through that role
> fails on `iam:CreateRole` by design. Deploying with administrator credentials is
> unaffected.
>
> Do **not** confuse that with the service-role template's second, optional
> parameter `ServiceRolePermissionsBoundaryArn`, which caps the deployment role
> itself and must be left blank or set to a *wide* policy. Passing the tight
> runtime boundary there stops the role deploying anything.
>
> If the IDP stack already exists and was deployed before that role was hardened,
> read "Updating an Existing Deployment" in the service role's README first: three
> detectable configurations wedge the update in `UPDATE_ROLLBACK_FAILED`.

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
>
> That role is a **deployment** role, not a least-privilege one. It still holds
> `cloudformation:*` plus service wildcards on 25 services, because a
> CloudFormation service role must be able to create, update, **and roll back**
> every resource type in every optional feature of the templates. What contains
> it is not narrow actions but three constraints, which its own template now
> requires: a mandatory `CreatedRolePermissionsBoundaryArn` (its `iam:CreateRole`
> grant carries an `iam:PermissionsBoundary` condition, so it cannot mint a role
> outside the boundary), a `ManagedStackNamePrefix` that scopes its IAM
> role/policy grants to resource names beginning with that prefix, and a trust
> policy that admits only the CloudFormation service principal in the same
> account. Read the "Read This Before Granting the Role" and "What Remains Broad,
> and Why" sections of that
> [README](../iam-roles/cloudformation-management/README.md) before granting it.

#### Essential Permissions
* `cloudformation:*` - Create and manage CloudFormation stacks
* `iam:*` - Create and manage IAM roles and policies. This is the one entry the
  example service role deliberately does **not** grant as a wildcard: it holds a
  named list of role/policy actions, scoped to the stack name prefix, with
  `iam:CreateRole` gated on the permissions boundary and explicit denies on
  removing that boundary, editing the boundary policy, editing the service role
  itself, and creating IAM users or access keys
* `lambda:*` - Create and configure Lambda functions
* `states:*` - Create and manage Step Functions state machines
* `s3:*` - Create buckets and manage S3 resources
* `dynamodb:*` - Create and configure DynamoDB tables
* `sqs:*` - Create and configure SQS queues
* `events:*` - Create and configure EventBridge rules
* `cloudfront:*` - Create and configure CloudFront distributions
* `cognito-idp:*` - Create and configure Cognito user pools 
* `cognito-identity:*` - Create and configure Cognito identity pools for AWS service access
* `apigateway:*` - Create and configure the UI ⇄ backend REST API (and the optional API Gateway UI host)
* `appsync:*` - **Legacy, retained for upgrades only.** No template creates an AppSync API any more (see [AppSync → REST API Migration](./migration-appsync-to-rest.md)); the grant remains so that an in-place update of a stack created *before* that migration can delete the AppSync resources it still owns. Safe to drop once no pre-migration stacks remain.
* `logs:*` - Create and configure CloudWatch log groups
* `cloudwatch:*` - Create and configure CloudWatch dashboards and alarms
* `sns:*` - Create and configure SNS topics

#### Feature-Specific Permissions
* `bedrock:*` - Create and invoke Bedrock resources (all modes)
* `ecr:*` - Create ECR repositories and push pattern container images
* `glue:*` - Create the reporting database and tables (evaluation reporting).
  `athena:*` is needed to *query* that data, not to deploy it — no template
  declares an `AWS::Athena` resource, so the example service role does not grant it
* `aoss:*` / `opensearch-serverless:*` - Create OpenSearch Serverless collections (Knowledge Base feature, only when `KnowledgeBaseVectorStore: OPENSEARCH_SERVERLESS`; the default S3 Vectors store does not need this)
* `kms:*` - Create KMS keys for encryption
* `wafv2:*` - Configure WAF rules (optional)
* `ec2:*` - Create the VPC-attached resources used by the private/VPC hosting
  variants and the optional bastion host
* `scheduler:*` / `secretsmanager:*` - Optional scheduled jobs and stored secrets
* `glue:*` / `codebuild:*` / `ssm:*` - Supporting build, configuration, and reporting infrastructure

> **Runtime-only services are not deployment permissions.** `textract:*` and
> `sagemaker:*` were previously listed here, but neither is needed to *create* the
> stack: no template declares an `AWS::Textract` or `AWS::SageMaker` resource.
> Textract is called at runtime by the OCR Lambda, and SageMaker (MLflow tracking)
> at runtime by the evaluation path — both via the scoped Lambda execution roles
> described under [Runtime Roles](#runtime-roles). They have been removed from the
> example CloudFormation service role for the same reason.

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
* **API Dispatcher Role** (the single Lambda behind `POST /op/{field}`, which fans a request out to the per-field resolver Lambdas and serves the DynamoDB-direct fields in process):
  * `dynamodb:GetItem`, `dynamodb:Query`, `dynamodb:Scan`
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`
  * `lambda:InvokeFunction` (only the resolver functions in its field → function map)

* **API Gateway CloudWatch Logging Role** (created when `LogLevel` is `INFO` or `DEBUG`):
  * Managed policy `AmazonAPIGatewayPushToCloudWatchLogs` (assumed by `apigateway.amazonaws.com`)
  * Registered as the account-level API Gateway CloudWatch role (`AWS::ApiGateway::Account`) to enable REST API stage access logging. This setting is per account per region and is retained on stack deletion so other stacks' logging keeps working.

* **Configuration Resolver Role** (API resolver Lambda for configuration CRUD + Z3 RuleJSON generation):
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:DeleteItem`, `dynamodb:Query`
  * `s3:GetObject` (configuration bucket)
  * `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey*`
  * `bedrock:InvokeModel` (foundation models + inference profiles, for Z3 RuleJSON translation via `generateRuleJson` mutation)
  * `bedrock:DeleteDataAutomationProject`, `bedrock:GetDataAutomationProject`, `bedrock:DeleteBlueprint`, `bedrock:ListBlueprints`

* **Cognito Authentication Role** (the Identity Pool's authenticated role, assumed by the browser):
  * `lambda:InvokeFunction` on the chat streaming function — the browser SigV4-signs its Function URL directly (a Function URL invocation needs `InvokeFunction`, *not* `InvokeFunctionUrl`). The REST API itself is reached with the Cognito **ID token**, not IAM, so no `execute-api:Invoke` grant is required.
  * `s3:GetObject`, `s3:GetObjectVersion` and `s3:ListBucket` on the Input and Output buckets, plus `kms:Decrypt` on the customer-managed key. This is the browser reading document bytes with the signed-in user's own credentials — the file viewer, the page thumbnails, the page-image viewer and the document export all sign S3 GETs client-side. ⚠️ A single `authenticated` role is attached with no `RoleMappings`, so **every** signed-in user holds this regardless of Cognito group, with no resolver in the path to apply one. The buckets partitioned **per user** — Configuration (configuration-profile revisions) and Test Set — are deliberately **absent**, because no IAM grant on one shared role can express `allowedConfigVersions` or `allowedTestSets`; those objects are served only through `getFileContents` / `getFilePresignedUrl`, which check the key against the caller's scope. Narrowing what remains is [issue #1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033); see [RBAC](./rbac.md) for the boundary this leaves.
  * `ssm:GetParameter` (for settings — the UI reads its runtime settings blob, which includes the stack's bucket names, directly from Parameter Store)

* **Knowledge Base Query Role**:
  * `bedrock:InvokeModel`
  * `bedrock:Retrieve`
  * `bedrock:RetrieveAndGenerate`
  * `bedrock:ApplyGuardrail` (when Guardrails configured)
  * No direct vector-store permissions: `bedrock:Retrieve` reaches the vector store through the Knowledge Base Service Role below
  * `logs:*`

* **Knowledge Base Service Role**:
  * `bedrock:InvokeModel`
  * `aoss:APIAccessAll` (OpenSearch Serverless) **or** `s3vectors:GetIndex`, `s3vectors:QueryVectors`, `s3vectors:PutVectors`, `s3vectors:GetVectors`, `s3vectors:DeleteVectors` on the stack's single vector index (default S3 Vectors store)
  * `s3:ListBucket`, `s3:GetObject` (when using S3 data source)

* **S3 Vectors Manager Role** (custom-resource Lambda, default S3 Vectors store only):
  * `s3vectors:CreateVectorBucket`, `s3vectors:GetVectorBucket`, `s3vectors:DeleteVectorBucket` on `bucket/*` in the deploying account and Region — the bucket segment is a wildcard because the Lambda lowercases and sanitizes the stack-derived bucket name, which CloudFormation cannot reproduce
  * `s3vectors:CreateIndex`, `s3vectors:DeleteIndex` on `bucket/*/index/<index name>` (the index name is exact)
  * `bedrock:CreateKnowledgeBase`, `bedrock:DeleteKnowledgeBase`, `bedrock:GetKnowledgeBase`, `bedrock:UpdateKnowledgeBase`, `bedrock:ListKnowledgeBases`
  * `iam:PassRole` (to hand the Knowledge Base Service Role to Bedrock)

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
  * `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem` — evaluation results are written straight to the tracking table; backend workers do not call the UI API
  * `bedrock:InvokeModel` (for LLM-based evaluations)
  * `cloudwatch:PutMetricData`
  * `logs:*`

* **Reporting / Analytics Roles** (evaluation reporting & analytics UI):
  * `glue:GetDatabase`, `glue:GetTable`, `glue:GetPartitions` (reporting database/tables)
  * `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution`
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` (reporting/Athena results buckets)
  * `logs:*`

* **Data-Mart Rollup Lambda Role** (`DataMartRollupFunction`, scheduled hourly + daily + reconciler + migration state-machine task-mode invocations):
  * `athena:StartQueryExecution`, `athena:GetQueryExecution`, `athena:GetQueryResults`, `athena:StopQueryExecution` (writes rollup tables via `INSERT INTO`)
  * `glue:GetDatabase`, `glue:GetTable`, `glue:GetPartitions`, `glue:CreatePartition`, `glue:BatchCreatePartition` (partition management on rollup tables)
  * `glue:GetDatabases`, `glue:GetTables` — required by the rollup Lambda's direct Glue-catalog scan of `document_sections_*` tables (used to build the `doc_class` CTE that fills in `document_class` for historical metering rows). On a missing grant, `get_tables` raises `AccessDeniedException` and the discovery function re-raises it so the invocation errors and the DLQ alarm fires — a permissions regression here surfaces immediately rather than degrading every historical row to `'unknown'` in the rollup output
  * `ssm:GetParameter`, `ssm:PutParameter` on `/idp/<stack-name>/data-mart-rollup/*` — the state machine's two-phase migration marker (`state=in_progress` after purge, `state=completed` on success). Read by `_check_marker_state`, written by `_write_marker` — both are task-mode invocations of this Lambda
  * `s3:DeleteObject` on the four rollup prefixes (`metering_hourly/*`, `metering_daily/*`, `metering_docs_hourly/*`, `metering_docs_daily/*`) under the reporting bucket — used by the migration state machine's `InitialPurge` task, scoped to date= partitions inside the migration window so older aggregates outside the window are preserved
  * `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics` (`*` — API doesn't support resource-level scoping) for reading `AWS/Lambda/Duration`, `AWS/Lambda/Invocations`, `IDPControlPlane/AthenaBytesScanned`, `IDPControlPlane/BedrockInputTokens`, `IDPControlPlane/BedrockOutputTokens`
  * `tag:GetResources` (`*` — account-scoped API) for tag-based Lambda discovery
  * `cloudformation:ListStackResources` (scoped to this stack + its nested stacks) to walk the stack tree
  * `lambda:GetFunctionConfiguration` (scoped to functions in this account/region) for accurate per-Lambda memory + architecture in the cost estimate
  * `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:GetBucketLocation` (reporting bucket only; `GetBucketLocation` is also needed by Athena's `StartQueryExecution` on the OutputLocation bucket). `HeadObject` calls are authorized by `s3:GetObject` — there is no `s3:HeadObject` IAM action
  * `s3:AbortMultipartUpload`, `s3:ListBucketMultipartUploads`, `s3:ListMultipartUploadParts` (reporting bucket only) — part of AWS's reference policy for Athena `INSERT INTO`, which switches to a multipart upload once a result part exceeds its buffer
  * `sqs:SendMessage` on its DLQ (async-failure destination)
  * KMS on the stack CMK

* **Data-Mart Migration State-Machine Role** (`DataMartMigrationStateMachine`, invoked by the CFN custom-resource dispatcher on every `MigrationVersion` change):
  * `lambda:InvokeFunction` on `DataMartRollupFunction` only — the state machine drives every migration step by invoking that Lambda in task modes (`check_marker_state`, `check_lake_state`, `purge_rollup_prefixes`, `write_marker`, `plan_migration_chunks`, `backfill`, `backfill_daily_range`, `check_hours_failed`)
  * CloudWatch Logs delivery — `logs:CreateLogDelivery`, `logs:GetLogDelivery`, `logs:UpdateLogDelivery`, `logs:DeleteLogDelivery`, `logs:ListLogDeliveries`, `logs:PutResourcePolicy`, `logs:DescribeResourcePolicies`, `logs:DescribeLogGroups` (`*` resource — the SFN service creates the delivery, not the state machine itself)
  * X-Ray write permissions — `xray:PutTraceSegments`, `xray:PutTelemetryRecords`, `xray:GetSamplingRules`, `xray:GetSamplingTargets`. Granted **unconditionally** in the template — `EnableXRayTracing` gates whether the function's `Tracing` mode is `Active` or `PassThrough`, not the permissions themselves

* **Data-Mart Migration Dispatcher Role** (`DataMartMigrationDispatcherFunction`, CFN custom-resource entry point):
  * `states:StartExecution` on `DataMartMigrationStateMachine` only — starts the state machine asynchronously and returns SUCCESS to CFN immediately; the migration continues after the CustomResource completes
  * `ssm:DeleteParameter` on `/idp/<stack-name>/data-mart-rollup/*` — deletes the SSM migration marker on `ForceFresh=true` (so the state machine's `CheckMarker` sees `ParameterNotFound` and takes the full-flow branch), and cleans it up on stack Delete so a same-name recreate starts clean. Read access is deliberately NOT granted here — the marker is read from the rollup Lambda's own role, not this one
  * No KMS grant on the dispatcher role. The dispatcher does not encrypt or decrypt any customer data — its only writes are `states:StartExecution` (no customer-data payload beyond `days` / `chunk_hours` / `version` / `anchor`) and `ssm:DeleteParameter`; the SSM parameter's own encryption is handled by SSM's service-owned key, not the stack CMK

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
