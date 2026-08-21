# CloudFormation Service Role for GenAI IDP Accelerator

This directory contains the `IDP-Cloudformation-Service-Role.yaml` CloudFormation template that creates a dedicated IAM Cloudformation service role for CloudFormation to deploy, manage and modify all GenAI IDP Accelerator patterns deployments.

## <span style="color: blue;">Administrator Access and Deployment Options</span>

**Note**: As detailed in [./docs/deployment.md](../docs/deployment.md), administrator access is required to deploy the GenAI IDP Accelerator solution. However, this directory provides an example CloudFormation service role that administrators can provision to allow other users to pass this role to CloudFormation for deploying and maintaining the solution stack without themselves needing administrator permissions.

This approach enables a security model where:
- **Administrators** deploy this service role once with their elevated privileges
- **Developer/DevOps users** can then deploy and manage IDP stacks using this pre-provisioned service role
- **Operational teams** can maintain the solution without requiring ongoing administrator access

## <span style="color: blue;">What This Role Does</span>

The **IDPAcceleratorCloudFormationServiceRole** is a CloudFormation service role that provides the necessary permissions for AWS CloudFormation to deploy, update, and manage GenAI IDP Accelerator stacks. The solution now uses a single **unified pattern stack** controlled by the `use_bda` configuration flag — **BDA mode** (Bedrock Data Automation) or **Pipeline mode** (Amazon Textract OCR + Bedrock classification/extraction). This role can only be assumed by the CloudFormation service, not by users directly.

Demo (5 minutes)

### Key Capabilities
- **Full CloudFormation Management**: Create, update, delete IDP stacks - This IAM service role (which CloudFormation assumes) gives necessary privileges to create/update/delete the stack which is helpful in development and sandbox environments. In production environments, admins can further limit these permissions to their discretion (e.g. disabling stack deletion).

- **All Mode Support**: Works with both processing modes of the unified pattern stack — BDA mode (Bedrock Data Automation) and Pipeline mode (Textract + Bedrock)

- **Comprehensive AWS Service Access**: Supports all services required by IDP Accelerator


## <span style="color: blue;">Security Features</span>

### Session Management
- **Administrator Note**: This role also creates an IAM Managed Policy to allow passing the Cloudformation service role.  Administrators must add the inline IAM policy to users wanting to deploy or modify CloudFormation IDP stacks with this service role, allowing them to pass the `IDPAcceleratorCloudFormationServiceRole` role to the CloudFormation principal:

  ```yaml
  PassRolePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      ManagedPolicyName: IDP-PassRolePolicy
      Description: Policy to allow passing the IDP CloudFormation service role
      PolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Action:
              - iam:PassRole
            Resource: !GetAtt CloudFormationServiceRole.Arn
  ```

### Access Control
- **Account-Scoped**: Only IAM entities within the same AWS account can assume the role


## <span style="color: blue;">Files in this Directory</span>

- `IDP-Cloudformation-Service-Role.yaml` - CloudFormation service role template 
- `README.md` - This documentation file
- `testing-guide.md` - Testing procedures and validation steps

## <span style="color: blue;">Console Deployment Steps</span>

### Prerequisites
- AWS Administrator access or IAM permissions to create roles and policies

### Step-by-Step Deployment

1. **Navigate to CloudFormation Console**
   - Open the AWS Management Console
   - Go to **CloudFormation** service
   - Select your preferred region

2. **Create New Stack**
   - Click **"Create stack"** → **"With new resources (standard)"**

3. **Specify Template**
   - Select **"Upload a template file"**
   - Click **"Choose file"** and select `IDP-Cloudformation-Service-Role.yaml`
   - Click **"Next"**

4. **Stack Details**
   - **Stack name**: Enter your stack a name
   - **Parameters**: No parameters required
   - Click **"Next"**

5. **Configure Stack Options**
   - **Tags** (optional): Add any desired tags
   - **Permissions**: Leave as default
   - **Stack failure options**: Leave as default
   - Click **"Next"**

6. **Review and Create**
   - Review all settings
   - **Capabilities**: Check **"I acknowledge that AWS CloudFormation might create IAM resources with custom names"**
   - Click **"Submit"**

7. **Monitor Deployment**
   - Wait for stack status to show **"CREATE_COMPLETE"**
   - Check the **Events** tab for any issues

8. **Retrieve Role ARN**
   - Go to the **Outputs** tab
   - Copy the **CloudFormationServiceRoleArn** value for future use

### Post-Deployment
- The role is now ready to be used with `--role-arn` parameter in CloudFormation deployments via CLI or as a "an existing AWS Identity and Access Management (IAM) service role that CloudFormation can assume" from the Permissions-Optional section in the Cloudformation Console. 
- Users will need `iam:PassRole` permission to use this role

## <span style="color: blue;">AWS Service Permissions</span>

The role provides comprehensive access to **28 AWS services** required across both processing modes (BDA and Pipeline) of the unified pattern stack. Below is a detailed breakdown organized by category.

### Services Summary

| Category | Services Count | Services |
|----------|---------------|----------|
| Core Infrastructure | 2 | CloudFormation, IAM |
| Compute & Serverless | 3 | Lambda, Step Functions, CodeBuild |
| AI/ML Services | 3 | Bedrock, Textract, SageMaker (optional MLflow) |
| Storage Services | 3 | S3, DynamoDB, ECR |
| API & Application | 2 | API Gateway, AppSync |
| Security & Identity | 5 | Cognito User Pools, Cognito Identity, KMS, Secrets Manager, WAF v2 |
| Messaging & Events | 4 | SNS, SQS, EventBridge, EventBridge Scheduler |
| Monitoring & Management | 3 | CloudWatch, CloudWatch Logs, Systems Manager |
| Analytics & Data | 2 | Glue, OpenSearch Serverless |
| Networking & CDN | 2 | CloudFront, EC2 (VPC) |
| Scaling | 1 | Application Auto Scaling |

### Complete Service List

| Service | Access | Utility |
|---------|--------|---------|
| CloudFormation | Full Access | Full stack management |
| IAM | Full Access | Complete role and policy management for IDP components |
| Lambda | Full Access | Function creation and management |
| Step Functions | Full Access | State machine orchestration |
| CodeBuild | Full Access | Build automation for custom container images |
| Bedrock | Full Access | Foundation models for document understanding |
| Textract | Full Access | Document OCR and extraction capabilities |
| SageMaker | Full Access | Optional MLflow tracking server integration |
| S3 | Full Access | Bucket and object management |
| DynamoDB | Full Access | Table and data management |
| ECR | Full Access | Container image registry |
| API Gateway | Full Access | REST and HTTP API management |
| AppSync | Full Access | GraphQL API management |
| Cognito User Pools | Full Access | User authentication and management |
| Cognito Identity | Full Access | Federated identity and temporary credentials |
| KMS | Full Access | Encryption key management |
| Secrets Manager | Full Access | Secure credential storage |
| WAF v2 | Full Access | Web application firewall |
| SNS | Full Access | Notification services |
| SQS | Full Access | Message queue management |
| EventBridge | Full Access | Event-driven workflow triggers |
| EventBridge Scheduler | Full Access | Scheduled task management |
| CloudWatch | Full Access | Metrics, alarms, and dashboards |
| CloudWatch Logs | Full Access | Centralized logging |
| Systems Manager (SSM) | Full Access | Parameter Store configuration |
| Glue | Full Access | Data catalog and ETL jobs |
| OpenSearch Serverless | Full Access | Vector search for embeddings |
| CloudFront | Full Access | CDN for web hosting and API acceleration |
| EC2 (VPC) | Limited Access | VPC, subnet, and security group management only |
| Application Auto Scaling | Full Access | Auto-scaling for DynamoDB and Lambda |

---

### Detailed Service Breakdown

#### Core Infrastructure Services

<details>
<summary><strong>AWS CloudFormation</strong> (<code>cloudformation</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Stack management for IDP infrastructure deployment

**Actions Granted**:
```
cloudformation:*
```
- `CreateStack`, `UpdateStack`, `DeleteStack`
- `DescribeStacks`, `DescribeStackEvents`, `DescribeStackResources`
- `GetTemplate`, `ValidateTemplate`
- `CreateChangeSet`, `ExecuteChangeSet`, `DeleteChangeSet`
- `ListStacks`, `ListStackResources`
- All other CloudFormation operations

</details>

<details>
<summary><strong>AWS IAM</strong> (<code>iam</code>)</summary>

**Permission Level**: Full (Roles & Policies CRUD)

**Purpose**: Create and manage IAM roles/policies for Lambda functions, service integrations, and resource access

**Role Management Actions**:
```
iam:CreateRole
iam:DeleteRole
iam:UpdateRole
iam:UpdateAssumeRolePolicy
iam:PutRolePermissionsBoundary
iam:DeleteRolePermissionsBoundary
iam:GetRole
iam:GetRolePolicy
iam:ListRoles
iam:ListRolePolicies
iam:ListAttachedRolePolicies
iam:ListRoleTags
iam:PutRolePolicy
iam:DeleteRolePolicy
iam:AttachRolePolicy
iam:DetachRolePolicy
iam:TagRole
iam:UntagRole
iam:PassRole
iam:CreateServiceLinkedRole
iam:DeleteServiceLinkedRole
```

**Policy Management Actions**:
```
iam:CreatePolicy
iam:DeletePolicy
iam:GetPolicy
iam:GetPolicyVersion
iam:ListPolicies
iam:ListPolicyVersions
iam:CreatePolicyVersion
iam:DeletePolicyVersion
iam:SetDefaultPolicyVersion
iam:TagPolicy
iam:UntagPolicy
```

> **If you use your own service role or a permissions boundary**, include
> `iam:UpdateAssumeRolePolicy`, `iam:PutRolePermissionsBoundary` and
> `iam:DeleteRolePermissionsBoundary` alongside the role-lifecycle actions.
> CloudFormation sets a role's trust policy and permissions boundary during
> `iam:CreateRole`, so a fresh deploy succeeds without them — but modifying
> either on an existing role is a separate API call. Without these, a release
> that changes a role's trust policy fails mid-update and wedges the stack in
> `UPDATE_ROLLBACK_FAILED` (the rollback needs the same permission). See
> [issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632).

</details>

---

#### Compute & Serverless Services

<details>
<summary><strong>AWS Lambda</strong> (<code>lambda</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Deploy and manage Lambda functions for document processing, API backends, and workflow steps

**Actions Granted**:
```
lambda:*
```
- `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `DeleteFunction`
- `GetFunction`, `GetFunctionConfiguration`, `ListFunctions`
- `CreateEventSourceMapping`, `UpdateEventSourceMapping`, `DeleteEventSourceMapping`
- `AddPermission`, `RemovePermission`
- `PublishVersion`, `CreateAlias`, `UpdateAlias`, `DeleteAlias`
- `TagResource`, `UntagResource`, `ListTags`
- `InvokeFunction`, `InvokeAsync`
- `PutFunctionConcurrency`, `DeleteFunctionConcurrency`

</details>

<details>
<summary><strong>AWS Step Functions</strong> (<code>states</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Orchestrate document processing workflows and multi-step AI pipelines

**Actions Granted**:
```
states:*
```
- `CreateStateMachine`, `UpdateStateMachine`, `DeleteStateMachine`
- `DescribeStateMachine`, `ListStateMachines`
- `StartExecution`, `StopExecution`, `DescribeExecution`, `ListExecutions`
- `GetExecutionHistory`
- `CreateActivity`, `DeleteActivity`, `DescribeActivity`, `ListActivities`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS CodeBuild</strong> (<code>codebuild</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Build automation for custom container images and deployment artifacts

**Actions Granted**:
```
codebuild:*
```
- `CreateProject`, `UpdateProject`, `DeleteProject`
- `BatchGetProjects`, `ListProjects`
- `StartBuild`, `StopBuild`, `BatchGetBuilds`, `ListBuilds`
- `CreateReportGroup`, `DeleteReportGroup`
- `BatchGetReportGroups`, `ListReportGroups`

</details>

---

#### AI/ML Services

<details>
<summary><strong>Amazon Bedrock</strong> (<code>bedrock</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Foundation models for document understanding, extraction, classification, and generation

**Actions Granted**:
```
bedrock:*
```
- `InvokeModel`, `InvokeModelWithResponseStream`
- `GetFoundationModel`, `ListFoundationModels`
- `CreateModelCustomizationJob`, `GetModelCustomizationJob`
- `CreateProvisionedModelThroughput`, `UpdateProvisionedModelThroughput`, `DeleteProvisionedModelThroughput`
- `GetModelInvocationLoggingConfiguration`, `PutModelInvocationLoggingConfiguration`
- `CreateGuardrail`, `UpdateGuardrail`, `DeleteGuardrail`, `GetGuardrail`
- `CreateAgent`, `UpdateAgent`, `DeleteAgent` (for Bedrock Agents)
- `CreateKnowledgeBase`, `UpdateKnowledgeBase`, `DeleteKnowledgeBase`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon Textract</strong> (<code>textract</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Document OCR, form extraction, table extraction, and expense analysis

**Actions Granted**:
```
textract:*
```
- `DetectDocumentText`, `AnalyzeDocument`, `AnalyzeExpense`, `AnalyzeID`
- `StartDocumentTextDetection`, `GetDocumentTextDetection`
- `StartDocumentAnalysis`, `GetDocumentAnalysis`
- `StartExpenseAnalysis`, `GetExpenseAnalysis`
- `StartLendingAnalysis`, `GetLendingAnalysis`, `GetLendingAnalysisSummary`
- `CreateAdapter`, `UpdateAdapter`, `DeleteAdapter`, `GetAdapter`
- `CreateAdapterVersion`, `DeleteAdapterVersion`, `GetAdapterVersion`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon SageMaker</strong> (<code>sagemaker</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Optional Amazon SageMaker MLflow tracking server integration for logging processing metrics/experiments (enabled via `MlflowTrackingServerArn`). Note: earlier releases used a SageMaker-hosted UDOP classification endpoint (the former "Pattern 3"); the unified architecture no longer deploys a SageMaker inference endpoint — classification is performed by Bedrock foundation models.

**Actions Granted**:
```
sagemaker:*
```
- `CreateModel`, `DeleteModel`, `DescribeModel`, `ListModels`
- `CreateEndpointConfig`, `DeleteEndpointConfig`, `DescribeEndpointConfig`
- `CreateEndpoint`, `DeleteEndpoint`, `UpdateEndpoint`, `DescribeEndpoint`, `ListEndpoints`
- `InvokeEndpoint`, `InvokeEndpointAsync`
- `CreateProcessingJob`, `DescribeProcessingJob`, `StopProcessingJob`
- `CreateTrainingJob`, `DescribeTrainingJob`, `StopTrainingJob`
- `CreateTransformJob`, `DescribeTransformJob`, `StopTransformJob`
- `CreateNotebookInstance`, `DeleteNotebookInstance`, `DescribeNotebookInstance`
- `AddTags`, `DeleteTags`, `ListTags`

</details>

---

#### Storage Services

<details>
<summary><strong>Amazon S3</strong> (<code>s3</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Document storage, processing artifacts, model artifacts, and static website hosting

**Actions Granted**:
```
s3:*
```
- `CreateBucket`, `DeleteBucket`, `ListBuckets`, `GetBucketLocation`
- `PutBucketPolicy`, `GetBucketPolicy`, `DeleteBucketPolicy`
- `PutBucketEncryption`, `GetBucketEncryption`
- `PutBucketVersioning`, `GetBucketVersioning`
- `PutBucketNotification`, `GetBucketNotification`
- `PutBucketCors`, `GetBucketCors`, `DeleteBucketCors`
- `PutObject`, `GetObject`, `DeleteObject`, `ListObjects`
- `PutObjectTagging`, `GetObjectTagging`, `DeleteObjectTagging`
- `PutBucketLifecycleConfiguration`, `GetBucketLifecycleConfiguration`

</details>

<details>
<summary><strong>Amazon DynamoDB</strong> (<code>dynamodb</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Metadata storage, document tracking, extraction results, and configuration data

**Actions Granted**:
```
dynamodb:*
```
- `CreateTable`, `DeleteTable`, `UpdateTable`, `DescribeTable`, `ListTables`
- `CreateGlobalTable`, `UpdateGlobalTable`, `DescribeGlobalTable`
- `PutItem`, `GetItem`, `UpdateItem`, `DeleteItem`
- `Query`, `Scan`, `BatchGetItem`, `BatchWriteItem`
- `CreateBackup`, `DeleteBackup`, `DescribeBackup`, `ListBackups`
- `RestoreTableFromBackup`, `RestoreTableToPointInTime`
- `EnableKinesisStreamingDestination`, `DisableKinesisStreamingDestination`
- `TagResource`, `UntagResource`, `ListTagsOfResource`

</details>

<details>
<summary><strong>Amazon ECR</strong> (<code>ecr</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Container image registry for custom Lambda images and SageMaker containers

**Actions Granted**:
```
ecr:*
```
- `CreateRepository`, `DeleteRepository`, `DescribeRepositories`, `ListImages`
- `GetRepositoryPolicy`, `SetRepositoryPolicy`, `DeleteRepositoryPolicy`
- `GetAuthorizationToken`, `GetDownloadUrlForLayer`
- `BatchGetImage`, `BatchCheckLayerAvailability`
- `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`
- `PutImage`, `BatchDeleteImage`
- `PutImageScanningConfiguration`, `StartImageScan`, `DescribeImageScanFindings`
- `PutLifecyclePolicy`, `GetLifecyclePolicy`, `DeleteLifecyclePolicy`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### API & Application Services

<details>
<summary><strong>Amazon API Gateway</strong> (<code>apigateway</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: REST and HTTP APIs for document upload, status queries, and result retrieval

**Actions Granted**:
```
apigateway:*
```
- `CreateRestApi`, `DeleteRestApi`, `UpdateRestApi`, `GetRestApi`, `GetRestApis`
- `CreateResource`, `DeleteResource`, `GetResource`, `GetResources`
- `CreateMethod`, `DeleteMethod`, `PutMethod`, `GetMethod`
- `CreateIntegration`, `DeleteIntegration`, `PutIntegration`, `GetIntegration`
- `CreateDeployment`, `DeleteDeployment`, `GetDeployment`, `GetDeployments`
- `CreateStage`, `DeleteStage`, `UpdateStage`, `GetStage`, `GetStages`
- `CreateAuthorizer`, `DeleteAuthorizer`, `UpdateAuthorizer`, `GetAuthorizer`
- `CreateUsagePlan`, `DeleteUsagePlan`, `UpdateUsagePlan`, `GetUsagePlan`
- `CreateApiKey`, `DeleteApiKey`, `UpdateApiKey`, `GetApiKey`
- `TagResource`, `UntagResource`, `GetTags`

</details>

<details>
<summary><strong>AWS AppSync</strong> (<code>appsync</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: GraphQL APIs for real-time document processing updates and frontend integration

**Actions Granted**:
```
appsync:*
```
- `CreateGraphqlApi`, `DeleteGraphqlApi`, `UpdateGraphqlApi`, `GetGraphqlApi`, `ListGraphqlApis`
- `CreateDataSource`, `DeleteDataSource`, `UpdateDataSource`, `GetDataSource`
- `CreateResolver`, `DeleteResolver`, `UpdateResolver`, `GetResolver`, `ListResolvers`
- `CreateType`, `DeleteType`, `UpdateType`, `GetType`, `ListTypes`
- `CreateFunction`, `DeleteFunction`, `UpdateFunction`, `GetFunction`
- `CreateApiKey`, `DeleteApiKey`, `UpdateApiKey`, `ListApiKeys`
- `StartSchemaCreation`, `GetSchemaCreationStatus`, `GetIntrospectionSchema`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Security & Identity Services

<details>
<summary><strong>Amazon Cognito User Pools</strong> (<code>cognito-idp</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: User authentication, user management, and access token issuance

**Actions Granted**:
```
cognito-idp:*
```
- `CreateUserPool`, `DeleteUserPool`, `UpdateUserPool`, `DescribeUserPool`, `ListUserPools`
- `CreateUserPoolClient`, `DeleteUserPoolClient`, `UpdateUserPoolClient`, `DescribeUserPoolClient`
- `CreateUserPoolDomain`, `DeleteUserPoolDomain`, `DescribeUserPoolDomain`
- `CreateGroup`, `DeleteGroup`, `UpdateGroup`, `GetGroup`, `ListGroups`
- `AdminCreateUser`, `AdminDeleteUser`, `AdminUpdateUserAttributes`
- `AdminAddUserToGroup`, `AdminRemoveUserFromGroup`
- `AdminSetUserPassword`, `AdminResetUserPassword`
- `AdminInitiateAuth`, `AdminRespondToAuthChallenge`
- `SetUserPoolMfaConfig`, `GetUserPoolMfaConfig`

</details>

<details>
<summary><strong>Amazon Cognito Identity Pools</strong> (<code>cognito-identity</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Federated identity management and temporary AWS credentials for authenticated users

**Actions Granted**:
```
cognito-identity:*
```
- `CreateIdentityPool`, `DeleteIdentityPool`, `UpdateIdentityPool`, `DescribeIdentityPool`
- `ListIdentityPools`, `ListIdentities`
- `GetId`, `GetOpenIdToken`, `GetCredentialsForIdentity`
- `SetIdentityPoolRoles`, `GetIdentityPoolRoles`
- `LookupDeveloperIdentity`, `MergeDeveloperIdentities`
- `UnlinkDeveloperIdentity`, `UnlinkIdentity`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS KMS</strong> (<code>kms</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Encryption key management for S3, DynamoDB, Secrets Manager, and other encrypted resources

**Actions Granted**:
```
kms:*
```
- `CreateKey`, `ScheduleKeyDeletion`, `CancelKeyDeletion`, `DescribeKey`, `ListKeys`
- `EnableKey`, `DisableKey`, `EnableKeyRotation`, `DisableKeyRotation`
- `CreateAlias`, `DeleteAlias`, `UpdateAlias`, `ListAliases`
- `CreateGrant`, `RetireGrant`, `RevokeGrant`, `ListGrants`
- `Encrypt`, `Decrypt`, `ReEncrypt`, `GenerateDataKey`, `GenerateDataKeyWithoutPlaintext`
- `PutKeyPolicy`, `GetKeyPolicy`, `ListKeyPolicies`
- `TagResource`, `UntagResource`, `ListResourceTags`

</details>

<details>
<summary><strong>AWS Secrets Manager</strong> (<code>secretsmanager</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Secure storage for API keys, database credentials, and service integration secrets

**Actions Granted**:
```
secretsmanager:*
```
- `CreateSecret`, `DeleteSecret`, `UpdateSecret`, `DescribeSecret`, `ListSecrets`
- `GetSecretValue`, `PutSecretValue`
- `RotateSecret`, `CancelRotateSecret`
- `UpdateSecretVersionStage`, `ListSecretVersionIds`
- `RestoreSecret`, `ReplicateSecretToRegions`, `RemoveRegionsFromReplication`
- `GetResourcePolicy`, `PutResourcePolicy`, `DeleteResourcePolicy`, `ValidateResourcePolicy`
- `TagResource`, `UntagResource`

</details>

<details>
<summary><strong>AWS WAF v2</strong> (<code>wafv2</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Web application firewall for API Gateway and CloudFront protection

**Actions Granted**:
```
wafv2:*
```
- `CreateWebACL`, `DeleteWebACL`, `UpdateWebACL`, `GetWebACL`, `ListWebACLs`
- `CreateRuleGroup`, `DeleteRuleGroup`, `UpdateRuleGroup`, `GetRuleGroup`, `ListRuleGroups`
- `CreateIPSet`, `DeleteIPSet`, `UpdateIPSet`, `GetIPSet`, `ListIPSets`
- `CreateRegexPatternSet`, `DeleteRegexPatternSet`, `UpdateRegexPatternSet`
- `AssociateWebACL`, `DisassociateWebACL`, `GetWebACLForResource`, `ListResourcesForWebACL`
- `PutLoggingConfiguration`, `GetLoggingConfiguration`, `DeleteLoggingConfiguration`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Messaging & Event Services

<details>
<summary><strong>Amazon SNS</strong> (<code>sns</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Notifications for processing completion, errors, and system alerts

**Actions Granted**:
```
sns:*
```
- `CreateTopic`, `DeleteTopic`, `GetTopicAttributes`, `SetTopicAttributes`, `ListTopics`
- `Subscribe`, `Unsubscribe`, `ConfirmSubscription`, `ListSubscriptions`, `ListSubscriptionsByTopic`
- `Publish`, `PublishBatch`
- `GetSubscriptionAttributes`, `SetSubscriptionAttributes`
- `AddPermission`, `RemovePermission`
- `GetDataProtectionPolicy`, `PutDataProtectionPolicy`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon SQS</strong> (<code>sqs</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Message queues for asynchronous document processing and workflow decoupling

**Actions Granted**:
```
sqs:*
```
- `CreateQueue`, `DeleteQueue`, `GetQueueAttributes`, `SetQueueAttributes`, `ListQueues`
- `GetQueueUrl`, `ListQueueTags`
- `SendMessage`, `SendMessageBatch`
- `ReceiveMessage`, `DeleteMessage`, `DeleteMessageBatch`
- `ChangeMessageVisibility`, `ChangeMessageVisibilityBatch`
- `PurgeQueue`
- `AddPermission`, `RemovePermission`
- `TagQueue`, `UntagQueue`

</details>

<details>
<summary><strong>Amazon EventBridge</strong> (<code>events</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Event-driven triggers for document processing workflows and S3 event routing

**Actions Granted**:
```
events:*
```
- `CreateEventBus`, `DeleteEventBus`, `DescribeEventBus`, `ListEventBuses`
- `PutRule`, `DeleteRule`, `DescribeRule`, `EnableRule`, `DisableRule`, `ListRules`
- `PutTargets`, `RemoveTargets`, `ListTargetsByRule`
- `PutEvents`, `PutPartnerEvents`
- `CreateArchive`, `DeleteArchive`, `DescribeArchive`, `ListArchives`
- `CreateConnection`, `DeleteConnection`, `DescribeConnection`, `UpdateConnection`
- `CreateApiDestination`, `DeleteApiDestination`, `DescribeApiDestination`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon EventBridge Scheduler</strong> (<code>scheduler</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Scheduled tasks for batch processing, cleanup jobs, and periodic workflows

**Actions Granted**:
```
scheduler:*
```
- `CreateSchedule`, `DeleteSchedule`, `UpdateSchedule`, `GetSchedule`, `ListSchedules`
- `CreateScheduleGroup`, `DeleteScheduleGroup`, `GetScheduleGroup`, `ListScheduleGroups`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Monitoring & Management Services

<details>
<summary><strong>Amazon CloudWatch</strong> (<code>cloudwatch</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Metrics, alarms, and dashboards for monitoring IDP processing performance

**Actions Granted**:
```
cloudwatch:*
```
- `PutMetricData`, `GetMetricData`, `GetMetricStatistics`, `ListMetrics`
- `PutMetricAlarm`, `DeleteAlarms`, `DescribeAlarms`, `DescribeAlarmsForMetric`
- `EnableAlarmActions`, `DisableAlarmActions`, `SetAlarmState`
- `PutDashboard`, `DeleteDashboards`, `GetDashboard`, `ListDashboards`
- `PutCompositeAlarm`, `DescribeAnomalyDetectors`, `PutAnomalyDetector`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon CloudWatch Logs</strong> (<code>logs</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Centralized logging for Lambda functions, API Gateway, and all IDP components

**Actions Granted**:
```
logs:*
```
- `CreateLogGroup`, `DeleteLogGroup`, `DescribeLogGroups`, `ListTagsLogGroup`
- `CreateLogStream`, `DeleteLogStream`, `DescribeLogStreams`
- `PutLogEvents`, `GetLogEvents`, `FilterLogEvents`
- `PutRetentionPolicy`, `DeleteRetentionPolicy`
- `PutSubscriptionFilter`, `DeleteSubscriptionFilter`, `DescribeSubscriptionFilters`
- `CreateExportTask`, `DescribeExportTasks`
- `PutMetricFilter`, `DeleteMetricFilter`, `DescribeMetricFilters`
- `PutResourcePolicy`, `DeleteResourcePolicy`, `DescribeResourcePolicies`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>AWS Systems Manager (SSM)</strong> (<code>ssm</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Parameter Store for configuration management and secure parameter storage

**Actions Granted**:
```
ssm:*
```
- `PutParameter`, `GetParameter`, `GetParameters`, `GetParametersByPath`, `DeleteParameter`
- `DescribeParameters`, `GetParameterHistory`
- `AddTagsToResource`, `RemoveTagsFromResource`, `ListTagsForResource`
- `CreateDocument`, `DeleteDocument`, `UpdateDocument`, `DescribeDocument`
- `CreateAssociation`, `DeleteAssociation`, `UpdateAssociation`, `DescribeAssociation`
- `SendCommand`, `CancelCommand`, `ListCommands`, `ListCommandInvocations`
- `StartAutomationExecution`, `StopAutomationExecution`, `GetAutomationExecution`

</details>

---

#### Analytics & Data Services

<details>
<summary><strong>AWS Glue</strong> (<code>glue</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Data catalog, ETL jobs, and schema management for structured extraction data

**Actions Granted**:
```
glue:*
```
- `CreateDatabase`, `DeleteDatabase`, `UpdateDatabase`, `GetDatabase`, `GetDatabases`
- `CreateTable`, `DeleteTable`, `UpdateTable`, `GetTable`, `GetTables`
- `CreatePartition`, `DeletePartition`, `UpdatePartition`, `GetPartition`, `GetPartitions`
- `CreateCrawler`, `DeleteCrawler`, `UpdateCrawler`, `StartCrawler`, `StopCrawler`, `GetCrawler`
- `CreateJob`, `DeleteJob`, `UpdateJob`, `StartJobRun`, `BatchStopJobRun`, `GetJob`, `GetJobRun`
- `CreateTrigger`, `DeleteTrigger`, `UpdateTrigger`, `StartTrigger`, `StopTrigger`, `GetTrigger`
- `CreateConnection`, `DeleteConnection`, `UpdateConnection`, `GetConnection`
- `TagResource`, `UntagResource`, `GetTags`

</details>

<details>
<summary><strong>Amazon OpenSearch Serverless</strong> (<code>aoss</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Vector search for document embeddings, semantic search, and RAG implementations

**Actions Granted**:
```
aoss:*
```
- `CreateCollection`, `DeleteCollection`, `UpdateCollection`, `GetCollection`, `ListCollections`, `BatchGetCollection`
- `CreateSecurityPolicy`, `DeleteSecurityPolicy`, `UpdateSecurityPolicy`, `GetSecurityPolicy`, `ListSecurityPolicies`
- `CreateAccessPolicy`, `DeleteAccessPolicy`, `UpdateAccessPolicy`, `GetAccessPolicy`, `ListAccessPolicies`
- `CreateVpcEndpoint`, `DeleteVpcEndpoint`, `UpdateVpcEndpoint`, `GetVpcEndpoint`, `ListVpcEndpoints`, `BatchGetVpcEndpoint`
- `CreateSecurityConfig`, `DeleteSecurityConfig`, `UpdateSecurityConfig`, `GetSecurityConfig`
- `GetAccountSettings`, `UpdateAccountSettings`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

#### Networking & Content Delivery Services

<details>
<summary><strong>Amazon CloudFront</strong> (<code>cloudfront</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: CDN for web application hosting and API acceleration

**Actions Granted**:
```
cloudfront:*
```
- `CreateDistribution`, `DeleteDistribution`, `UpdateDistribution`, `GetDistribution`, `ListDistributions`
- `CreateOriginAccessControl`, `DeleteOriginAccessControl`, `UpdateOriginAccessControl`, `GetOriginAccessControl`
- `CreateCachePolicy`, `DeleteCachePolicy`, `UpdateCachePolicy`, `GetCachePolicy`, `ListCachePolicies`
- `CreateOriginRequestPolicy`, `DeleteOriginRequestPolicy`, `UpdateOriginRequestPolicy`, `GetOriginRequestPolicy`
- `CreateResponseHeadersPolicy`, `DeleteResponseHeadersPolicy`, `UpdateResponseHeadersPolicy`
- `CreateFunction`, `DeleteFunction`, `UpdateFunction`, `PublishFunction`, `GetFunction`
- `CreateInvalidation`, `GetInvalidation`, `ListInvalidations`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

<details>
<summary><strong>Amazon EC2 (VPC)</strong> (<code>ec2</code>)</summary>

**Permission Level**: Limited (VPC resources only)

**Purpose**: VPC, subnet, and security group management for private networking

**Actions Granted**:
```
ec2:CreateVpc
ec2:DeleteVpc
ec2:DescribeVpcs
ec2:CreateSubnet
ec2:DeleteSubnet
ec2:DescribeSubnets
ec2:CreateSecurityGroup
ec2:DeleteSecurityGroup
ec2:DescribeSecurityGroups
ec2:AuthorizeSecurityGroupIngress
ec2:AuthorizeSecurityGroupEgress
ec2:RevokeSecurityGroupIngress
ec2:RevokeSecurityGroupEgress
ec2:CreateTags
ec2:DeleteTags
ec2:DescribeTags
ec2:DescribeAvailabilityZones
```

**Note**: EC2 permissions are intentionally limited to VPC-related resources only, excluding compute instances.

</details>

---

#### Scaling Services

<details>
<summary><strong>AWS Application Auto Scaling</strong> (<code>application-autoscaling</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Auto-scaling for DynamoDB tables and Lambda provisioned concurrency

**Actions Granted**:
```
application-autoscaling:*
```
- `RegisterScalableTarget`, `DeregisterScalableTarget`, `DescribeScalableTargets`
- `PutScalingPolicy`, `DeleteScalingPolicy`, `DescribeScalingPolicies`
- `PutScheduledAction`, `DeleteScheduledAction`, `DescribeScheduledActions`
- `DescribeScalingActivities`
- `TagResource`, `UntagResource`, `ListTagsForResource`

</details>

---

## <span style="color: blue;">Security Considerations</span>

### Regional Restrictions
- **Deployment Region**: Role assumption restricted to deployment region
- **Compliance**: Helps meet data residency requirements

### Session Security
- **Account Isolation**: Cannot be assumed cross-account with the current trust policy

### Permission Scope
- **Broad Service Access**: Full service permissions for comprehensive IDP deployment services
- **No Resource Restrictions**: Allows flexibility but requires careful usage
- **Service Trust**: CloudFormation service can assume role for stack operations
- **Compliance Note**: Organizations may need to refine and make more granular the service action permissions based on their specific security compliance guidelines and least privilege requirements

## <span style="color: blue;">Troubleshooting</span>

### Common Issues

1. **Access Denied when Using Role**:
   - Verify your user/role has `iam:PassRole` permission for this specific role ARN

   - Ensure the role exists and is in the same account
   - Remember: Users cannot assume this role directly - only CloudFormation service can

2. **Region Restriction Errors**:
   - Role should be deployed in same region where IDP stacks are deployed

3. **Session Timeout**:
   - Re-assume the role to get fresh credentials

4. **CloudFormation Deployment Failures**:
   - If using the CLI, ensure you're using `CAPABILITY_IAM` and `CAPABILITY_NAMED_IAM`
   - Check CloudWatch logs for specific service errors



## <span style="color: blue;">Best Practices</span>

1. **Regular Auditing**: Periodically review who has access to assume this role
2. **Least Privilege**: Only grant this role to users who need to manage IDP stacks
3. **Session Management**: Use temporary credentials and limit session duration
4. **Monitoring**: Enable CloudTrail logging for role assumption and usage
5. **Rotation**: Regularly review and update the role permissions as needed
