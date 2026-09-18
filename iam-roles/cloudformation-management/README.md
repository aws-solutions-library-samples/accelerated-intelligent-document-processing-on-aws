# CloudFormation Service Role for GenAI IDP Accelerator

This directory contains the `IDP-Cloudformation-Service-Role.yaml` CloudFormation template that creates a dedicated IAM Cloudformation service role for CloudFormation to deploy, manage and modify all GenAI IDP Accelerator patterns deployments.

## <span style="color: blue;">Administrator Access and Deployment Options</span>

**Note**: As detailed in [docs/deployment.md](../../docs/deployment.md), administrator access is required to deploy the GenAI IDP Accelerator solution. However, this directory provides an example CloudFormation service role that administrators can provision to allow other users to pass this role to CloudFormation for deploying and maintaining the solution stack without themselves needing administrator permissions.

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

## <span style="color: blue;">Read This Before Granting the Role</span>

This is a **deployment role, not a least-privilege role.** An earlier version of
this document and of the template itself claimed it "follows the principle of
least privilege". That was wrong, and the wording has been corrected
([issue #927](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/927)).
Deciding whether to create this role means understanding both halves of what it
does.

**What it can do.** It holds `cloudformation:*` and a `<service>:*` wildcard on
24 other services, all on `Resource: "*"`. Whoever can pass this role to
CloudFormation can create, modify or delete any resource in those services in
this account — not only the ones belonging to an IDP stack. It can also create
IAM roles and customer managed policies.

**What contains it.** Three mechanisms, in order of how much they actually
constrain:

1. **A required permissions boundary.** The `PermissionsBoundaryArn` parameter is
   mandatory. Every `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`
   and related grant carries an `iam:PermissionsBoundary` condition requiring
   exactly that policy, so **no role created through this identity can exceed the
   boundary you supply.** This is what prevents the role from being a transitive
   account administrator, and it is only as strong as the boundary you choose.
   Pass the same ARN as the IDP stack's own `PermissionsBoundaryArn` parameter.
2. **Explicit denies.** Stripping a permissions boundary, editing the boundary
   policy, modifying the service role itself, and creating IAM users, access
   keys, or SAML/OIDC providers are denied outright. An explicit `Deny` cannot be
   overridden by any `Allow`, including a future edit to this template.
3. **Name and destination scoping.** IAM writes and `iam:PassRole` are limited to
   principals whose names begin with `ManagedStackNamePrefix`, and `PassRole`
   additionally requires an `iam:PassedToService` in a fixed list. **Your IDP
   stack name must start with that prefix** — CloudFormation derives generated
   role and policy names from the stack name.

**What is still broad.** `cloudformation:*` and the 24 service wildcards. See
["What Remains Broad, and Why"](#what-remains-broad-and-why) for the reasoning
and for what it would take to narrow them.

**Instance profiles.** The optional bastion host
(`ShouldDeployBastionHost` in `template.yaml`) declares an
`AWS::IAM::InstanceProfile`, which is a distinct IAM resource type with its own
lifecycle actions. Those are granted, scoped to the same name prefix. They do not
support `iam:PermissionsBoundary`, so the prefix is the only scope available —
but the escalation that matters is still contained, because AWS requires the
caller of
[`AddRoleToInstanceProfile`](https://docs.aws.amazon.com/IAM/latest/APIReference/API_AddRoleToInstanceProfile.html)
to hold `iam:PassRole` on the role being added, and this role holds `PassRole`
only for prefix-named roles. `scripts/sdlc/validate_service_role_permissions.py`
now derives this requirement from any `AWS::IAM::InstanceProfile` in the
templates, so a future feature that adds one cannot silently go ungranted.

## <span style="color: blue;">Security Features</span>

### Session Management
- **Administrator Note**: This role also creates an IAM Managed Policy to allow passing the Cloudformation service role.  Administrators must attach this managed policy to users wanting to deploy or modify CloudFormation IDP stacks with this service role, allowing them to pass the service role to the CloudFormation principal:

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
            # Without this condition, the grant lets the holder hand a
            # role-creating identity to ANY service that accepts one, not
            # just to CloudFormation.
            Condition:
              StringEquals:
                iam:PassedToService: !Sub 'cloudformation.${AWS::URLSuffix}'
  ```

  You can tighten this further on the deploying principal's own policy with a
  `cloudformation:RoleArn` condition, which restricts *which* service role that
  user may pass and *which* stacks they may pass it to.

### Access Control
- **Account-Scoped**: The trust policy allows only the CloudFormation service
  principal, and only for stack operations in this account.
- **Confused-deputy conditions**: The trust policy applies
  `aws:SourceAccount` and `aws:SourceArn` conditions when CloudFormation supplies
  them, with an explicit `Null`-guarded fallback for the case where it does not.
  See ["Why the trust policy has two statements"](#why-the-trust-policy-has-two-statements).


## <span style="color: blue;">Files in this Directory</span>

- `IDP-Cloudformation-Service-Role.yaml` - CloudFormation service role template 
- `README.md` - This documentation file
- `testing-guide.md` - Testing procedures and validation steps

## <span style="color: blue;">Console Deployment Steps</span>

### Prerequisites
- AWS Administrator access or IAM permissions to create roles and policies
- **An existing IAM permissions boundary policy.** `PermissionsBoundaryArn` is a
  required parameter with no default; create the boundary policy first. It should
  allow no more than the services an IDP stack's Lambda functions actually need,
  because it is the ceiling on every role this service role creates. If you have
  no boundary policy yet, start from the runtime permissions documented in
  [../docs/aws-services-and-roles.md](../../docs/aws-services-and-roles.md).
- **A stack naming convention.** Decide the prefix your IDP stack names will
  share and pass it as `ManagedStackNamePrefix` (default `idp`). Keep it short —
  12 characters or fewer is a good rule — because CloudFormation truncates
  generated role names at the 64-character IAM limit and only the leading
  characters are guaranteed to survive.

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
   - **Stack name**: Enter a name for this service-role stack (this is *not* the
     IDP stack name; it only determines the role name,
     `<StackName>-CFServiceRole`)
   - **`PermissionsBoundaryArn`** (required): the ARN of your permissions boundary
     policy, e.g. `arn:aws:iam::123456789012:policy/IDPDeploymentBoundary`
   - **`ManagedStackNamePrefix`** (default `idp`): the shared name prefix of the
     IDP stacks this role may deploy
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

8. **Retrieve the Outputs**
   - Go to the **Outputs** tab
   - `ServiceRoleArn` — pass this to CloudFormation as the stack's service role
   - `PassRolePolicyArn` — attach this managed policy to whoever will deploy
   - `RequiredStackNamePrefix` and `RequiredPermissionsBoundaryArn` — the two
     constraints every IDP stack deployed with this role must satisfy

### Post-Deployment
- The role is now ready to be used with `--role-arn` parameter in CloudFormation deployments via CLI or as a "an existing AWS Identity and Access Management (IAM) service role that CloudFormation can assume" from the Permissions-Optional section in the Cloudformation Console. 
- Users will need `iam:PassRole` permission to use this role — attach the
  `PassRolePolicyArn` managed policy this stack creates
- **The IDP stack you deploy with this role must**: (a) have a name starting with
  `ManagedStackNamePrefix`, and (b) set its own `PermissionsBoundaryArn` parameter
  to the same ARN you passed here. Both are enforced by the role's policy, so a
  mismatch surfaces as `AccessDenied` on `iam:CreateRole` — see
  [Troubleshooting](#troubleshooting).

```bash
aws cloudformation deploy \
  --stack-name idp-prod \
  --template-file <idp-template> \
  --role-arn "$(aws cloudformation describe-stacks --stack-name <this-stack> \
      --query 'Stacks[0].Outputs[?OutputKey==`ServiceRoleArn`].OutputValue' \
      --output text)" \
  --parameter-overrides PermissionsBoundaryArn=<the-same-boundary-arn> \
  --capabilities CAPABILITY_NAMED_IAM
```

## <span style="color: blue;">AWS Service Permissions</span>

The role grants `cloudformation:*`, a **scoped and conditioned** set of IAM actions, and a wildcard (`<service>:*`) on **24 other AWS services** — every one of which backs at least one CloudFormation resource type the solution declares. Below is a detailed breakdown organized by category.

Four wildcards that earlier versions of this role carried have been removed
because nothing in the solution declares a resource in them: **AppSync**
(replaced by API Gateway), **Textract** and **SageMaker** (runtime-only, called
by Lambda execution roles rather than CloudFormation), and **Application Auto
Scaling** (no scalable target is declared anywhere). See the collapsed sections
below for the evidence in each case.

### Services Summary

| Category | Services Count | Services |
|----------|---------------|----------|
| Core Infrastructure | 2 | CloudFormation, IAM (scoped — see below) |
| Compute & Serverless | 3 | Lambda, Step Functions, CodeBuild |
| AI/ML Services | 1 | Bedrock |
| Storage Services | 3 | S3, DynamoDB, ECR |
| API & Application | 1 | API Gateway |
| Security & Identity | 5 | Cognito User Pools, Cognito Identity, KMS, Secrets Manager, WAF v2 |
| Messaging & Events | 4 | SNS, SQS, EventBridge, EventBridge Scheduler |
| Monitoring & Management | 3 | CloudWatch, CloudWatch Logs, Systems Manager |
| Analytics & Data | 2 | Glue, OpenSearch Serverless |
| Networking & CDN | 2 | CloudFront, EC2 (VPC) |

### Complete Service List

`Full Access` below means literally `<service>:*` on `Resource: "*"`. Read
["What Remains Broad, and Why"](#what-remains-broad-and-why) before treating
this role as least-privilege — it is not.

| Service | Access | Utility |
|---------|--------|---------|
| CloudFormation | Full Access | Full stack management, including nested stacks and change sets |
| IAM | **Scoped** — specific actions, name-prefixed resources, permissions-boundary and PassedToService conditions | Role and policy management for IDP components |
| Lambda | Full Access | Function creation and management |
| Step Functions | Full Access | State machine orchestration |
| CodeBuild | Full Access | Build automation for custom container images |
| Bedrock | Full Access | Foundation models, Data Automation projects, Knowledge Bases |
| S3 | Full Access | Bucket and object management |
| DynamoDB | Full Access | Table and data management |
| ECR | Full Access | Container image registry |
| API Gateway | Full Access | REST API management (web UI backend and hosting variants) |
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
| EC2 (VPC) | Full Access (`ec2:*`) | VPC, subnet, security group and interface-endpoint management for the private hosting variants. The `Utility` column previously said "Limited Access"; the grant is and was `ec2:*`. |
| ~~AppSync~~ | Removed | Replaced by API Gateway; zero `AWS::AppSync::*` resources remain |
| ~~Textract~~ | Removed | Runtime-only; no CloudFormation resource type |
| ~~SageMaker~~ | Removed | Runtime-only; MLflow server referenced by ARN, not created |
| ~~Application Auto Scaling~~ | Removed | No scalable target or scaling policy declared |

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
<summary><strong>AWS IAM</strong> (<code>iam</code>) — scoped, not <code>iam:*</code></summary>

**Permission Level**: Specific actions, split across eight statements, each scoped by resource and/or condition. This is the only service in the template where the grant is genuinely narrowed rather than wildcarded, because IAM is the only one where a wide grant makes the role an account administrator.

**Purpose**: Create and manage the IAM roles and customer managed policies that the IDP stack's Lambda functions, state machines and service integrations need.

**1. Create or re-permission a role — only with the permissions boundary**

```
iam:CreateRole
iam:PutRolePermissionsBoundary
iam:PutRolePolicy
iam:DeleteRolePolicy
iam:AttachRolePolicy
iam:DetachRolePolicy
```
- **Resource**: `arn:<partition>:iam::<account>:role/<ManagedStackNamePrefix>*` (and the same under an IAM path)
- **Condition**: `StringEquals { iam:PermissionsBoundary: <PermissionsBoundaryArn> }`

Every one of these actions supports the `iam:PermissionsBoundary` condition key, which is what makes this containment real: a role created or re-permissioned through this identity cannot exceed the boundary you supply, no matter what policy the template attaches to it. This is the delegation pattern from [Delegating responsibility using permissions boundaries](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_boundaries_delegate.html) in the IAM User Guide.

The IDP templates set `PermissionsBoundary` on every role they create — the explicit `AWS::IAM::Role` resources and the roles SAM generates for each `AWS::Serverless::Function` alike — so this condition holds for a real deployment **provided you deploy the IDP stack with the same `PermissionsBoundaryArn`**. Deploying the IDP stack with an empty `PermissionsBoundaryArn` while using this service role fails on `iam:CreateRole`, by design.

**2. Role lifecycle — scoped by name only**

```
iam:DeleteRole
iam:UpdateRole
iam:UpdateAssumeRolePolicy
iam:TagRole
iam:UntagRole
```
- **Resource**: same `<ManagedStackNamePrefix>*` role ARNs
- **Condition**: none

These five actions **do not support** the `iam:PermissionsBoundary` condition key, so conditioning them would deny every stack delete and every rollback. They are constrained by the role-name prefix alone. `iam:DeleteRolePermissionsBoundary` is **not** granted and is additionally denied (see statement 8): the ability to strip a boundary would defeat the boundary.

**3. Pass a role to the service that will use it**

```
iam:PassRole
```
- **Resource**: same `<ManagedStackNamePrefix>*` role ARNs — never `*`
- **Condition**: `StringEquals { iam:PassedToService: [ apigateway, bedrock, bedrock-agentcore, cloudfront, cloudwatch, codebuild, cognito-identity, dynamodb, ec2, events, glue, indexing.s3vectors, lambda, logging.s3, logs, scheduler, states ] }`

A second statement allows the pass when `iam:PassedToService` is **absent**, guarded with `Null`. Not every service populates that key, and a service that omits it would otherwise get `AccessDenied` on the pass and wedge the rollback. Both statements stay bounded by the role-name prefix.

**4. Service-linked roles**

```
iam:CreateServiceLinkedRole
iam:DeleteServiceLinkedRole
```
- **Resource**: `arn:<partition>:iam::<account>:role/aws-service-role/*`

Service-linked roles live at a fixed IAM path and **cannot carry a permissions boundary** (`PutRolePermissionsBoundary` rejects them), so they get their own path-scoped statement. Their permissions are defined by AWS, not by this role.

**5. Instance profiles — scoped by name only**

```
iam:CreateInstanceProfile
iam:DeleteInstanceProfile
iam:AddRoleToInstanceProfile
iam:RemoveRoleFromInstanceProfile
iam:TagInstanceProfile
iam:UntagInstanceProfile
```
- **Resource**: `arn:<partition>:iam::<account>:instance-profile/<ManagedStackNamePrefix>*`

Only the optional bastion host declares an `AWS::IAM::InstanceProfile`, and CloudFormation names it after the stack, so the same prefix applies. None of these actions supports `iam:PermissionsBoundary`. The escalation that matters is nonetheless contained: AWS requires the caller of [`AddRoleToInstanceProfile`](https://docs.aws.amazon.com/IAM/latest/APIReference/API_AddRoleToInstanceProfile.html) to hold `iam:PassRole` on the role being added, and statement 3 grants `PassRole` only for prefix-named roles.

**6. Customer managed policies**

```
iam:CreatePolicy
iam:DeletePolicy
iam:CreatePolicyVersion
iam:DeletePolicyVersion
iam:SetDefaultPolicyVersion
iam:TagPolicy
iam:UntagPolicy
```
- **Resource**: `arn:<partition>:iam::<account>:policy/<ManagedStackNamePrefix>*`

**7. Read-only IAM** — on `Resource: "*"`, because these grant no ability to change anything

```
iam:GetRole              iam:GetPolicy
iam:GetRolePolicy        iam:GetPolicyVersion
iam:ListRoles            iam:ListPolicies
iam:ListRolePolicies     iam:ListPolicyVersions
iam:ListAttachedRolePolicies
iam:ListRoleTags
iam:GetInstanceProfile   iam:ListInstanceProfiles
iam:ListInstanceProfilesForRole
```

**8. Explicit denies** — a `Deny` cannot be overridden by any `Allow`, including a future edit to this template

| Denied | Why |
|---|---|
| `iam:DeleteRolePermissionsBoundary`, `iam:DeleteUserPermissionsBoundary` | Stripping a boundary defeats the mechanism that contains every role this identity creates. |
| `iam:CreatePolicyVersion`, `iam:DeletePolicy`, `iam:DeletePolicyVersion`, `iam:SetDefaultPolicyVersion` **on the boundary policy ARN** | Editing the boundary is equivalent to removing it. |
| The role-write actions **on `<StackName>-CFServiceRole`** | The role must not be able to widen itself. |
| `iam:CreateUser`, `iam:CreateLoginProfile`, `iam:CreateAccessKey`, `iam:UpdateAccessKey`, `iam:CreateSAMLProvider`, `iam:UpdateSAMLProvider`, `iam:CreateOpenIDConnectProvider`, `iam:UpdateOpenIDConnectProviderThumbprint` | Long-lived credentials and federation trust are never part of deploying this solution, and both are standard persistence mechanisms. |

> **Why the update-only actions are here at all.** CloudFormation sets a role's
> trust policy and permissions boundary during `iam:CreateRole`, so a fresh
> deploy succeeds without `iam:UpdateAssumeRolePolicy` or
> `iam:PutRolePermissionsBoundary` — but modifying either on an existing role is
> a separate API call. Without them, a release that changes a role's trust policy
> fails mid-update and wedges the stack in `UPDATE_ROLLBACK_FAILED`, because the
> rollback needs the same permission. See
> [issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632).
> This is also why the lifecycle statement is unconditioned: a condition that
> blocks a delete blocks every rollback too.

> **The cost of dropping `iam:DeleteRolePermissionsBoundary`.** An operator who
> wants to *remove* the boundary from an existing IDP stack's roles — i.e. go
> from bounded to unbounded — has to make that change with their own credentials
> rather than through this service role. That is the intended trade.

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
<summary><strong>Amazon Textract</strong> and <strong>Amazon SageMaker</strong> — no longer granted</summary>

`textract:*` and `sagemaker:*` were **removed** from this role.

Both services are used by the solution, but only at **runtime**, by the Lambda
execution roles in `patterns/unified/template.yaml` — which carry their own
narrowly scoped Textract and SageMaker grants. Neither service has a
CloudFormation resource type anywhere in the solution: Textract has no
`AWS::Textract::*` types at all, and the optional MLflow tracking server is
referenced by ARN through the `MlflowTrackingServerArn` parameter rather than
created by the stack. CloudFormation therefore never calls either service, so
granting them to the deployment role added reachable permissions with no
deployment benefit.

An earlier release used a SageMaker-hosted UDOP classification endpoint, the
former "Pattern 3". The unified architecture no longer deploys a SageMaker
inference endpoint — classification is performed by Bedrock foundation models.

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
<summary><strong>AWS AppSync</strong> (<code>appsync</code>) — no longer granted</summary>

`appsync:*` was **removed** from this role.

The web UI used to talk to the backend through an AppSync GraphQL API. It now
uses an API Gateway REST API with a Lambda dispatcher, in
`nested/api-resolvers/template.yaml` (the stack is still named `APIRESOLVERSTACK`
and was historically `nested/appsync`). There are **zero** `AWS::AppSync::*`
resources left anywhere in the solution, so this grant could not be exercised by
any deployment — it was dead permission.

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
<summary><strong>Amazon EC2</strong> (<code>ec2</code>)</summary>

**Permission Level**: Full (`*`)

**Purpose**: Security groups and VPC interface endpoints for the private hosting
variants, plus the optional bastion host (`AWS::EC2::Instance` and
`AWS::EC2::LaunchTemplate` behind the `ShouldDeployBastionHost` condition).

**Actions Granted**:
```
ec2:*
```

> **Correction.** This section previously claimed EC2 access was "intentionally
> limited to VPC-related resources only, excluding compute instances", and listed
> 17 specific actions. The template has always granted `ec2:*`, and the solution
> does declare `AWS::EC2::Instance` and `AWS::EC2::LaunchTemplate`. The list was
> aspirational, not what was deployed. It has been corrected rather than
> implemented, because narrowing `ec2:*` to a fixed action list is the same
> untested-narrowing risk described in
> ["What Remains Broad, and Why"](#what-remains-broad-and-why) — an
> `ec2:*Tags` or `ec2:*NetworkInterface*` call missing from the list would fail
> a stack update and its rollback together. If you need EC2 narrowed, derive the
> action list from CloudTrail across a create, update and delete of the specific
> hosting variant you deploy.

</details>

---

#### Removed Wildcards

<details>
<summary><strong>AWS Application Auto Scaling</strong> (<code>application-autoscaling</code>) — no longer granted</summary>

`application-autoscaling:*` was **removed** from this role.

It was documented as covering auto-scaling for DynamoDB tables and Lambda
provisioned concurrency, but the solution declares neither. There is no
`AWS::ApplicationAutoScaling::ScalableTarget` or `ScalingPolicy` anywhere, no
`ProvisionedConcurrencyConfig` on any function, and every DynamoDB table uses
on-demand billing rather than provisioned throughput — so CloudFormation never
registers a scalable target and never calls this service.

</details>

---

## <span style="color: blue;">What Remains Broad, and Why</span>

Two grants in this role are not narrowed, and this section explains the reasoning
so you can decide whether the trade is acceptable in your account rather than
discovering it later.

### `cloudformation:*` on `Resource: "*"`

The stack creates nested stacks and change sets, and several CloudFormation read
APIs (`DescribeStacks`, `ListStacks`, `ValidateTemplate`) are not
resource-scopable at all. Narrowing this to an action list would need a
CloudTrail-derived inventory from a real create, update and delete.

### `<service>:*` on `Resource: "*"` for 24 services

Two separate reasons, and both need to be addressed to narrow either half:

**The resource half.** CloudFormation assigns physical resource names at deploy
time, long after this role is created, so there is no ARN pattern to match on
beyond the stack name. The IAM statements can use a name prefix because IAM role
names are predictable from the stack name; a KMS key ID or a CloudFront
distribution ID is not. And unlike IAM, none of these services has an equivalent
of `iam:PermissionsBoundary` to bound the result of a create call.

**The action half.** An action list that turns out to be one call short does not
merely fail the deployment — it fails the deployment **and its rollback**, which
leaves the stack in `UPDATE_ROLLBACK_FAILED` and recoverable only with
`continue-update-rollback --resources-to-skip`. That has happened in this project
before ([issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632)),
from a *missing* IAM action rather than a superfluous one. Given that only
CloudFormation can assume this role and every call is recorded in CloudTrail, a
wedged stack is a more likely and more damaging outcome than the escalation a
speculative action list would prevent.

**What it would take to narrow them safely.** Deploy each variant with the role
as-is, collect the `eventName` set that CloudFormation's assumed-role session
produced from CloudTrail across a full create, update and delete, take the union
across variants (BDA and Pipeline mode, each hosting variant, with and without
the optional Knowledge Base and multi-doc discovery stacks), then add a margin
for the AWS-side calls that only appear on failure paths. Until that data exists,
a role documented as broad is safer than one claimed narrow and not.

### Why the trust policy has two statements

For a CloudFormation **stack service role** (`create-stack --role-arn`), AWS
documents the trust policy with the service principal and *no* conditions, and
does not state that CloudFormation populates `aws:SourceAccount` or
`aws:SourceArn` on that `sts:AssumeRole` call. Those keys are documented for
StackSets administration roles and for registry-extension execution roles, which
are different mechanisms.

A single `StringEquals` on a key that is never populated evaluates false and would
deny **every** stack operation, including rollbacks — an untestable-here change
that could brick all deployments. So the template applies the conditions when the
keys are present and allows the absent case explicitly with a `Null` guard. The
net effect is strictly tighter than an unconditioned trust policy wherever
CloudFormation does supply the keys, with no deployment cliff where it does not.

`sts:ExternalId` is deliberately **not** used: it addresses cross-account role
assumption by a third party, and CloudFormation does not send one. For this role
the load-bearing controls are who holds `iam:PassRole` on it and, optionally, a
`cloudformation:RoleArn` condition on that principal's own policy.

## <span style="color: blue;">Security Considerations</span>

### Regional Restrictions
- **No region condition is applied.** The trust policy's `aws:SourceArn`
  condition uses `stack/*` across all regions, because a stack service role is
  commonly used in more than one. If you need a single-region role, add
  `aws:RequestedRegion` to the trust policy or to a `Deny` statement — this
  template does not do it for you. An earlier version of this document claimed
  role assumption was "restricted to deployment region"; it never was.

### Session Security
- **Account Isolation**: Only the CloudFormation service principal can assume the
  role, and only for stack operations in this account. No user or role can assume
  it directly, so there are no sessions to time out or credentials to rotate.

### Permission Scope
- **Broad service access**: `cloudformation:*` plus `<service>:*` on 24 services,
  all on `Resource: "*"`. See ["What Remains Broad, and Why"](#what-remains-broad-and-why).
- **IAM is the exception**: scoped by action, by resource name prefix, and by
  `iam:PermissionsBoundary` / `iam:PassedToService` conditions, with explicit
  denies on boundary tampering, self-modification and credential creation.
- **Boundary is mandatory**: `PermissionsBoundaryArn` has no default. The strength
  of the containment is the strength of the boundary policy you write.
- **Compliance note**: Organizations should refine the 24 service wildcards to
  their own least-privilege requirements. The method for doing that safely is in
  ["What Remains Broad, and Why"](#what-remains-broad-and-why).

## <span style="color: blue;">Troubleshooting</span>

### Common Issues

1. **`AccessDenied` on `iam:CreateRole` during IDP stack deployment**:
   - Almost always one of the two constraints this role enforces. Check the error
     message for the role ARN it was trying to create.
   - **Missing or mismatched boundary**: the IDP stack must be deployed with
     `PermissionsBoundaryArn` set to the *same* ARN you passed to this role's
     stack. An empty value fails by design.
   - **Stack name prefix mismatch**: the IDP stack name must start with
     `ManagedStackNamePrefix`. CloudFormation derives generated role names from
     the stack name, so `my-idp-prod` does not match a prefix of `idp`.
   - Both values are echoed in this stack's `RequiredPermissionsBoundaryArn` and
     `RequiredStackNamePrefix` outputs.

2. **`AccessDenied` on `iam:PassRole` during IDP stack deployment**:
   - The role being passed does not match the name prefix, or the destination
     service is not in the `iam:PassedToService` list in the template. Add the
     service principal to that list.

3. **`AccessDenied` on `iam:DeleteRolePermissionsBoundary`**:
   - Expected. This role cannot remove a boundary from a role, by design. If you
     are intentionally moving an existing IDP stack from bounded to unbounded,
     make that update with your own credentials instead.

4. **Access Denied when Using Role**:
   - Verify your user/role has `iam:PassRole` permission for this specific role
     ARN, with `iam:PassedToService` allowing `cloudformation.amazonaws.com`
   - Ensure the role exists and is in the same account
   - Remember: Users cannot assume this role directly - only CloudFormation service can

5. **CloudFormation Deployment Failures**:
   - If using the CLI, ensure you're using `CAPABILITY_IAM` and `CAPABILITY_NAMED_IAM`
   - Check CloudWatch logs for specific service errors
   - For a wedged `UPDATE_ROLLBACK_FAILED` stack, see
     [../docs/troubleshooting.md](../../docs/troubleshooting.md)

## <span style="color: blue;">Best Practices</span>

1. **Write the boundary policy first, and take it seriously.** It is the only
   thing standing between this role and account administrator. Grant it no more
   than the runtime permissions in [../docs/aws-services-and-roles.md](../../docs/aws-services-and-roles.md).
2. **Regular Auditing**: Periodically review who holds `iam:PassRole` on this
   role — that list is the real blast radius, not the role itself.
3. **Constrain the caller too**: add a `cloudformation:RoleArn` condition to the
   deploying principal's policy so they can pass this role only to the stacks you
   intend.
4. **Monitoring**: Enable CloudTrail and alert on `iam:*` calls made by this
   role's assumed-role session. Every one of them is a deployment event, so
   anything outside a deployment window is worth investigating.
5. **Narrow the wildcards from data, not from guesswork.** See
   ["What Remains Broad, and Why"](#what-remains-broad-and-why).
