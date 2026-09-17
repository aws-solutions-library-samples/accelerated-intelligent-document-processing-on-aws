---
title: "GenAIIDP Deployment Guide"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# GenAIIDP Deployment Guide

This guide covers how to deploy, build, publish, and test the GenAI Intelligent Document Processing solution.

## Deployment Options

The GenAI IDP Accelerator can be deployed using either the AWS CloudFormation console (recommended for first-time users) or the IDP CLI (recommended for automation and programmatic deployments).

### Administrator Access Requirements

**Important**: Deploying the GenAI IDP Accelerator requires administrator access to your AWS account. However, for organizations that want to enable non-administrator users to deploy and manage IDP stacks, we provide an optional CloudFormation service role approach:

- **For Administrators**: Use the deployment options below with your existing administrator privileges
- **For Delegated Access**: See [iam-roles/cloudformation-management/README.md](../iam-roles/cloudformation-management/README.md) for instructions on provisioning a CloudFormation service role that allows non-administrator users to deploy and maintain IDP stacks without requiring administrator permissions

For the full breakdown of AWS services and the IAM permission scopes required for deployment and runtime, see [AWS Services and IAM Role Requirements](./aws-services-and-roles.md).

### Option 1: One-Click CloudFormation Console Deployment (Recommended for First-Time Users)

1. Choose your region and click the Launch Stack button:

| Region name | Region code | Launch |
| ----------- | ----------- | ------ |
| US West (Oregon) | us-west-2 | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://us-west-2.console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/create/review?templateURL=https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| US East (N.Virginia) | us-east-1 | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| EU Central (Frankfurt)      | eu-central-1   | [![Launch Stack](https://cdn.rawgit.com/buildkite/cloudformation-launch-stack-button-svg/master/launch-stack.svg)](https://eu-central-1.console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/create/review?templateURL=https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |

2. Review the template parameters and provide values as needed
3. Check the acknowledgment box and click **Create stack**
4. Wait for the stack to reach the `CREATE_COMPLETE` state (10-15 minutes)

> **Note**: When the stack is deploying for the first time, it will send an email with a temporary password to the address specified in the AdminEmail parameter. You will need to use this temporary password to log into the UI and set a permanent password.

> **Also confirm the alerts subscription.** The stack subscribes the same
> `AdminEmail` address to the CloudWatch alerts topic, so a **second** email
> arrives titled *"AWS Notification - Subscription Confirmation"*. Until someone
> clicks its confirmation link the subscription stays in `PendingConfirmation`
> and **no alarm notification is delivered** — the alarms still fire, they just
> reach nobody, and nothing about the deployment reports a problem. See
> [Who receives the alerts](./monitoring.md#who-receives-the-alerts) for how to
> check the status and how to alert a team or a pager rather than one mailbox.

---

### Option 2: CLI-Based Deployment (Recommended for Automation)

For programmatic deployment, updates, and batch processing, use the IDP CLI.

#### Install the CLI

```bash
make setup-venv
source .venv/bin/activate
```

#### Deploy a New Stack

```bash
idp-cli deploy \
    --stack-name my-idp-stack \
    --admin-email your.email@example.com \
    --max-concurrent 100 \
    --wait
```

**What this does:**
- Creates all CloudFormation resources (~120 resources)
- Waits for deployment to complete (10-15 minutes)
- Sends email with temporary admin password
- Subscribes the same address to the CloudWatch alerts topic — this needs a
  confirmation click before any alarm notification is delivered, see
  [Who receives the alerts](./monitoring.md#who-receives-the-alerts)
- Returns stack outputs including Web UI URL and bucket names

#### Deploy with Custom Configuration

```bash
# Deploy with local config file (automatically uploaded to S3)
idp-cli deploy \
    --stack-name my-idp-stack \
    --admin-email your.email@example.com \
    --custom-config ./config_library/unified/bank-statement-sample/config.yaml \
    --wait
```

#### Update an Existing Stack

```bash
# Update configuration
idp-cli deploy \
    --stack-name my-idp-stack \
    --custom-config ./updated-config.yaml \
    --wait

# Update parameters
idp-cli deploy \
    --stack-name my-idp-stack \
    --max-concurrent 200 \
    --log-level DEBUG \
    --wait
```

**Benefits of CLI deployment:**
- Scriptable and automatable
- Version-controlled deployments
- Rapid iteration for configuration testing
- Integration with CI/CD pipelines
- No manual console clicking required

**For complete CLI documentation**, see [IDP CLI Documentation](./idp-cli.md).

---

## Option 3: Build and Deploy from Source Code


### Dependencies

You need to have the following packages installed on your computer:

1. bash shell (Linux, MacOS, Windows-WSL)
2. aws (AWS CLI)
3. [sam (AWS SAM)](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
4. python 3.12 or later
5. Python packages: `pip install boto3 rich typer PyYAML botocore setuptools ruff build cfn-lint`
6. **Node.js 22.12+** and **npm** (required for UI validation in publish script)

> **Docker is NOT required on your workstation.** Lambda functions are packaged as source code by `idp-cli publish` and uploaded to S3; AWS CodeBuild builds the container images during stack deployment.

For guidance on setting up a development environment, see:

- [Development Environment Setup Guide on Linux](./setup-development-env-linux.md)
- [Development Environment Setup Guide on macOS](./setup-development-env-macos.md)
- [Development Environment Setup Guide on Windows (WSL)](./setup-development-env-WSL.md)

Copy the repo to your computer. Either:

- Use the git command to clone the repo, if you have access
- OR, download and expand the ZIP file for the repo, or use the ZIP file that has been shared with you

### Option A: IDP CLI `--from-code` (Recommended)

The easiest way to build, publish, and deploy from source in a single command is using the IDP CLI with the `--from-code` option. This builds all artifacts, publishes them to S3, and deploys the CloudFormation stack — all in one step.

#### Install the CLI

```bash
make setup-venv
source .venv/bin/activate
```

#### Deploy a New Stack from Source

```bash
idp-cli deploy \
    --stack-name my-idp-dev \
    --from-code . \
    --admin-email your.email@example.com \
    --wait
```

#### Update an Existing Stack from Source

```bash
idp-cli deploy \
    --stack-name my-idp-dev \
    --from-code . \
    --wait
```

**What `--from-code` does:**
- Builds SAM templates, Lambda layers, container images, and the UI
- Publishes all artifacts to an S3 bucket in your account
- Creates or updates the CloudFormation stack with the newly published template
- With `--wait`, monitors the deployment until completion

> **Tip**: Use `--from-code` for development and testing iterations. For production deployments from pre-published templates, use `--template-url` instead (see [Option 2: CLI-Based Deployment](#option-2-cli-based-deployment-recommended-for-automation)).

**For complete CLI documentation**, see [IDP CLI Documentation](./idp-cli.md).

---

### Option B: Publish Templates + Deploy Separately

If you prefer to publish artifacts first and then deploy as a separate step, use `idp-cli publish` to build and publish, then deploy using the AWS CloudFormation console or CLI.

#### Step 1: Build and Publish with `idp-cli publish`

```bash
idp-cli publish --source-dir . --region <region> [--bucket-basename <bucket>] [--prefix <prefix>] [--verbose] [--no-validate] [--clean-build] [--max-workers N]
```

**Parameters:**

- `--source-dir`: Path to the IDP project root directory (default: `.`)
- `--region`: AWS region for deployment (e.g., `us-east-1`)
- `--bucket-basename`: (Optional) S3 bucket basename for artifacts (auto-generated if not provided)
- `--prefix`: (Optional) S3 key prefix for artifacts (default: `idp-cli`)
- `--verbose` or `-v`: (Optional) Enable detailed error output for debugging build failures
- `--clean-build`: (Optional) Force a clean rebuild of all artifacts
- `--max-workers N`: (Optional) Number of parallel build workers
- `--headless`: (Optional) Also generate a **headless (no-UI) template variant**. Useful for API-only / pipeline integrations. See [Headless Deployment](./headless-deployment.md).
- `--govcloud`: (Optional) Also generate a **GovCloud template variant** that keeps the full Web UI: removes all `AWS::CloudFront::*` resources and Lambda Function URLs (unavailable in GovCloud) and forces API Gateway UI hosting. Mutually exclusive with `--headless`. See [GovCloud Deployment](./govcloud-deployment.md).

**Example:**

```bash
idp-cli publish --source-dir . --region us-east-1
```

With custom bucket and prefix:

```bash
idp-cli publish --source-dir . --bucket-basename idp-1234567890 --prefix idp --region us-east-1
```

The publish command:

- Checks your system dependencies for required packages
- Builds SAM templates, Lambda layers, and container images
- Packages and uploads the UI
- Publishes all templates and assets to an S3 bucket (auto-created if it doesn't exist)

When completed, the command displays:

- The CloudFormation template's S3 URL
- A 1-click URL for launching the stack creation in the CloudFormation console

> **Note**: Docker is **not required** on your workstation for `idp-cli publish`. The publish script packages source code and uploads it to S3; AWS CodeBuild builds the Lambda container images in the cloud during the subsequent CloudFormation stack deployment.

**Troubleshooting Build Issues:**
If the build fails, use the `--verbose` flag to see detailed error messages:

```bash
idp-cli publish --source-dir . --region us-east-1 --verbose
```

This will show:

- Exact SAM build commands being executed
- Complete error output from failed builds
- Python version compatibility issues
- Missing dependencies or configuration problems

#### Step 2: Deploy using AWS CloudFormation

**Recommended: Deploy using the CloudFormation console**

For your first deployment, use the `1-Click Launch URL` provided by the publish script. This lets you inspect the available parameter options in the console.

**CLI Deployment:**

For scripted/automated deployments, use the AWS CLI:

```bash
aws cloudformation deploy \
  --region <region> \
  --template-file <template-file> \
  --s3-bucket <bucket-name> \
  --s3-prefix <s3-prefix> \
  --capabilities CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides AdminEmail=<your-email> \
  --stack-name <your-stack-name>
```

Or to update an already-deployed stack:

```bash
aws cloudformation update-stack \
  --stack-name <your-stack-name> \
  --template-url <template URL output by publish script, e.g. https://s3.us-east-1.amazonaws.com/blahblah.yaml> \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \
  --region <region> \
  --parameters ParameterKey=AdminEmail,ParameterValue="<your-email>"
```

> **Note**: The legacy `publish.py` script is deprecated. Use `idp-cli publish` for all new deployments.

---

### Processing Mode

The solution deploys a **Unified Pattern** that supports both BDA and pipeline processing modes. The processing mode is controlled at runtime by the `use_bda` configuration flag (set via the UI), not at deployment time.

- **Pipeline mode** (`use_bda: false`, default) — Textract OCR → Bedrock Classification → Bedrock Extraction → Assessment → Rule Validation → Summarization
- **BDA mode** (`use_bda: true`) — Bedrock Data Automation for end-to-end processing → Rule Validation → Summarization

After deployment, check the Outputs tab in the CloudFormation console to find links to dashboards, buckets, workflows, and other solution resources.

## Container-Based Lambda Deployment

The solution **automatically** deploys all Lambda functions as container images — no manual configuration is required. During stack deployment, CodeBuild builds Docker images for each Lambda function, pushes them to ECR, and configures the Lambda functions to use the container images.

### Prerequisites

- **Docker is NOT required on your workstation** — `idp-cli publish` packages source code and uploads it to S3; AWS CodeBuild builds all Lambda container images in the cloud during stack deployment.
- Your AWS credentials must have **ECR permissions** (for CodeBuild to push images during deployment)

### How It Works

When you deploy (or build from source), the following happens automatically:

1. Creates/verifies an ECR repository for Lambda images
2. Builds optimized multi-stage Docker images for each Lambda function
3. Pushes images to ECR with appropriate tags
4. Configures CloudFormation templates to reference the container images

**Architecture:** Lambda functions use ARM64 (Graviton2) by default for better price/performance.

### Monitoring Container Deployments

```bash
# View ECR images
aws ecr list-images --repository-name idp-<stack-name>-lambda

# Check Lambda function configuration
aws lambda get-function --function-name <function-name>

# View container logs
aws logs tail /aws/lambda/<function-name> --follow
```

## Updating an Existing Stack

To update an existing GenAIIDP deployment to a new version:

1. Log into the [AWS console](https://console.aws.amazon.com/)
2. Navigate to CloudFormation in the AWS Management Console
3. Select your existing GenAIIDP stack
4. Click on the "Update" button
5. Select "Replace current template"
6. Provide the new template URL:
   - us-west-2: `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml`
   - us-east-1: `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml`
   - eu-central-1: `https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml`
7. Click "Next"
8. Review the parameters and make any necessary changes
   - The update will preserve your existing parameter values
   - Consider checking for new parameters that may be available in the updated template
9. Click "Next", then "Next" again on the Configure stack options page
10. Review the changes that will be made to your stack
11. Check the acknowledgment box for IAM capabilities
12. Click "Update stack"
13. Monitor the update process in the CloudFormation console

> **Note**: Updating the stack may cause some resources to be replaced, which could lead to brief service interruptions. Consider updating during a maintenance window if the solution is being used in production.

## Testing the Solution

### Method 1: CLI-Based Batch Testing (Recommended for Automation)

For batch processing, evaluation workflows, or automated testing:

#### Quick Batch Test

```bash
# Install CLI
cd lib/idp_cli_pkg && pip install -e .

# Process sample documents
idp-cli run-inference \
    --stack-name IDP \
    --dir ./samples/ \
    --batch-id sample-test \
    --monitor
```

**What you'll see:**
```
✓ Uploaded 3 documents to InputBucket
✓ Sent 3 messages to processing queue

Monitoring Batch: sample-test
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Status Summary
 ─────────────────────────────────────
 ✓ Completed      3     100%
 ⏸ Queued         0       0%
```

#### Download Results

```bash
# Download all results
idp-cli download-results \
    --stack-name IDP \
    --batch-id sample-test \
    --output-dir ./test-results/

# View extraction data
cat ./test-results/sample-test/lending_package.pdf/sections/1/result.json | jq .
```

#### Evaluation Workflow Testing

Test accuracy with validated baselines:

```bash
# 1. Process documents initially
idp-cli run-inference \
    --stack-name IDP \
    --dir ./test-docs/ \
    --batch-id baseline-run \
    --monitor

# 2. Download and validate results
idp-cli download-results \
    --stack-name IDP \
    --batch-id baseline-run \
    --output-dir ./baselines/ \
    --file-types sections

# 3. Manually review and correct baselines
# (Edit ./baselines/baseline-run/*/sections/*/result.json as needed)

# 4. Create manifest with baselines
cat > eval-manifest.csv << EOF
document_path,baseline_source
./test-docs/invoice.pdf,./baselines/baseline-run/invoice.pdf/
./test-docs/w2.pdf,./baselines/baseline-run/w2.pdf/
EOF

# 5. Reprocess with evaluation
idp-cli run-inference \
    --stack-name IDP \
    --manifest eval-manifest.csv \
    --batch-id eval-test \
    --monitor

# 6. Download evaluation results
idp-cli download-results \
    --stack-name IDP \
    --batch-id eval-test \
    --output-dir ./eval-results/ \
    --file-types evaluation

# 7. Review accuracy metrics
cat ./eval-results/eval-test/invoice.pdf/evaluation/report.md
```

**For complete evaluation workflow documentation**, see [IDP CLI - Complete Evaluation Workflow](./idp-cli.md#complete-evaluation-workflow).

---


### Method 2: Direct S3 Upload Testing (Simple)

1. Open the `S3InputBucketConsoleURL` and `S3OutputBucketConsoleURL` from the stack Outputs tab
2. Open the `StateMachineConsoleURL` from the stack Outputs tab
3. Upload a PDF form to the Input bucket (sample files are in the `./samples` folder):
   - Recommended: [samples/lending_package.pdf](../samples/lending_package.pdf)
4. Monitor the Step Functions execution to observe the workflow
5. When complete, check the Output bucket for the structured JSON file with extracted fields

#### Upload Multiple Files for Volume Testing

To copy a sample file multiple times:

```bash
n=10
for i in `seq 1 $n`; do 
  aws s3 cp ./samples/lending_package.pdf \
    s3://idp-inputbucket-kmsxxxxxxxxx/lending_package-$i.pdf
done
```

---

### Method 3: Web UI Testing (Interactive)

1. Open the Web UI URL from the CloudFormation stack's Outputs tab
2. Log in using your credentials (the temporary password from the email if this is your first login)
3. Navigate to the main dashboard
4. Click the "Upload Document" button
5. Select a sample PDF file (e.g., [samples/lending_package.pdf](../samples/lending_package.pdf))
6. Follow the upload process and observe the document processing in the UI
7. View the extraction results once processing is complete


This simulates incoming documents based on minute-by-minute rates in the schedule CSV file.