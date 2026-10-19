---
title: "IDP Accelerator + Amazon Quick Integration Workshop"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Accelerator + Amazon Quick Integration Workshop

> **Workshop Guide** — Build an end-to-end intelligent document processing workflow using the [GenAI IDP Accelerator](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws) and Amazon Quick.
>
> This is the self-guided integration workshop, which you run in your own AWS account. For a hands-on introduction to the accelerator as a whole, see the instructor-led [GenAI IDP Workshop on AWS Workshop Studio](https://catalog.workshops.aws/workshops/c384d63d-3090-44c9-a639-e06dd6d9164f). [Hands-on Workshops](../docs/workshops.md) compares the two.

## Overview

In this workshop, you will:
1. Deploy the GenAI IDP Accelerator stack via CloudFormation
2. Upload the sample loan document to S3
3. Configure an MCP (Model Context Protocol) integration in Amazon Quick to connect to the IDP backend
4. Build an Amazon Quick workflow that processes a loan document package through the IDP pipeline
5. Extract structured data from the processed document
6. Generate a formatted Excel spreadsheet with the results
7. Upload the output to S3 and download the results spreadsheet

### Architecture

```
┌─────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  Amazon Quick    │────▶│  Bedrock AgentCore   │────▶│  IDP Accelerator     │
│  Workflow        │     │  Gateway (MCP)       │     │  (Step Functions,    │
│                  │◀────│                      │◀────│   Lambda, Bedrock)   │
└─────────────────┘     └──────────────────────┘     └──────────────────────┘
        │                                                        │
        ▼                                                        ▼
┌─────────────────┐                                   ┌──────────────────────┐
│  S3 Output      │                                   │  S3 Input            │
│  (Spreadsheet)  │                                   │  (Loan Package PDF)  │
└─────────────────┘                                   └──────────────────────┘
```

### Prerequisites

- An AWS account with administrator access
- [Amazon Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) enabled for:
  - **Amazon**: All Nova models + Titan Text Embeddings V2
  - **Anthropic**: Claude 3.x and Claude 4.x models
- Access to Amazon Quick
- A sample loan package document (included in the accelerator at `samples/lending_package.pdf`)

---

## Part 1: Deploy the IDP Accelerator Stack

### Step 1.1: Launch the CloudFormation Template

Navigate to the AWS CloudFormation console and launch the IDP stack using one of the one-click deployment links:

| Region | Launch Link |
|--------|------------|
| US West (Oregon) `us-west-2` | [Launch Stack](https://us-west-2.console.aws.amazon.com/cloudformation/home?region=us-west-2#/stacks/create/review?templateURL=https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| US East (N. Virginia) `us-east-1` | [Launch Stack](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| EU Central (Frankfurt) `eu-central-1` | [Launch Stack](https://eu-central-1.console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/create/review?templateURL=https://s3.eu-central-1.amazonaws.com/aws-ml-blog-eu-central-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |
| Canada (Central) `ca-central-1` | [Launch Stack](https://ca-central-1.console.aws.amazon.com/cloudformation/home?region=ca-central-1#/stacks/create/review?templateURL=https://s3.ca-central-1.amazonaws.com/aws-bigdata-blog-replica-ca-central-1/artifacts/genai-idp/idp-main.yaml&stackName=IDP) |

### Step 1.2: Configure Stack Parameters

1. Review the template parameters
2. Provide your **Admin Email** (you'll receive temporary credentials here)
3. Ensure **EnableMCP** is set to `true` (this is required for the Amazon Quick integration)
4. Check the IAM acknowledgment box
5. Click **Create stack**

![CloudFormation Stack Creation](images/image_001.png)
![Stack Parameters](images/image_002.png)
![IAM Acknowledgment](images/image_003.png)
![Stack Creation In Progress](images/image_004.png)
![Stack Details](images/image_005.png)
![Stack Resources](images/image_006.png)
![Stack Progress](images/image_007.png)

### Step 1.3: Monitor Deployment Progress

The stack takes approximately **10–15 minutes** to deploy. You can monitor progress in the CloudFormation Events tab.

![Monitoring Deployment](images/image_008.png)
![Stack Events](images/image_009.png)
![Nested Stacks](images/image_010.png)

Once all stacks reach `CREATE_COMPLETE`:

![Stack Complete](images/image_011.png)

### Step 1.4: Retrieve Credentials

You will receive an email with a temporary password to access the IDP Web UI:

![Email with Credentials](images/image_012.png)

> **Important**: Note the following values from the CloudFormation **Outputs** tab — you'll need them for the MCP integration setup:
> - `MCPServerEndpoint` — The AgentCore Gateway URL
> - `MCPConnectorClientId` — The OAuth client ID for service-to-service (M2M) authentication
> - `MCPConnectorClientSecret` — The OAuth client secret for service-to-service (M2M) authentication
> - `MCPTokenURL` — The Cognito OAuth token endpoint URL

---

### Step 1.5: Upload the Sample Document to S3

Before running the workflow you need the sample loan package in an S3 bucket that the IDP stack can read.

1. Locate the sample file in the cloned repository:
   ```
   samples/lending_package.pdf
   ```
2. Upload it to an S3 bucket in the **same AWS region** as your IDP stack:
   ```bash
   aws s3 cp samples/lending_package.pdf \
       s3://<your-input-bucket>/inputs/lending_package.pdf \
       --region <stack-region>
   ```
3. Note the bucket name and key — you'll enter them as runtime config values when you run the workflow:
   - **`loan_package_bucket`** — the bucket name (e.g. `my-idp-input-bucket`)
   - **`loan_package_key`** — the object key (e.g. `inputs/lending_package.pdf`)

> **Tip:** You can also drag-and-drop the file using the [IDP Web UI](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/web-ui/) upload feature if you prefer a console-based approach.

---

## Part 2: Configure the MCP Integration in Amazon Quick

Before building the workflow, you need to set up the MCP connector that allows Amazon Quick to communicate with the IDP backend.

> **What is MCP?** The [Model Context Protocol](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/mcp-server.md) enables external applications to access IDP functionality through AWS Bedrock AgentCore Gateway. It exposes tools for document processing, status monitoring, result retrieval, and analytics queries.

### Step 2.1: Create a New MCP Integration

In Amazon Quick, navigate to **Integrations** → **Create Integration** → **MCP**.

Configure the following fields:

| Field | Value |
|-------|-------|
| **Name** | `mcp-idp-action` |
| **Description** | MCP action for the IDP workshop |
| **Connection Purpose** | Automated workflows |
| **Authentication Type** | Service-to-service OAuth |
| **Base URL** | *(Your `MCPServerEndpoint` from CloudFormation Outputs)* |
| **Client ID** | *(Your `MCPConnectorClientId` from CloudFormation Outputs)* |
| **Client Secret** | *(Your `MCPConnectorClientSecret` from CloudFormation Outputs)* |
| **Token URL** | *(Your `MCPTokenURL` from CloudFormation Outputs)* |
| **VPC Connection** | Public network |

> **Why `MCPConnectorClientId` and not `MCPClientId`?**
> The stack creates two separate Cognito clients. `MCPClientId` uses the `authorization_code` flow (interactive user login) and is for QuickSight/external apps. `MCPConnectorClientId` uses the `client_credentials` flow (machine-to-machine OAuth — no user login required), which is required for automated service-to-service workflows. See [MCP Server docs](../docs/mcp-server.md) for full details.

> **Example Base URL format:**
> ```
> https://genaiidp-workshop-{id}-analytics-gateway-{hash}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp
> ```

![MCP Integration Setup](images/image_022.png)
![Authentication Config](images/image_023.png)
![Connection Details](images/image_024.png)
![OAuth Configuration](images/image_025.png)
![Base URL Entry](images/image_026.png)
![Integration Complete](images/image_027.png)

### Step 2.2: Configure MCP Actions

After creating the integration, set up the following actions. Each action maps to an IDP MCP tool:

#### Action 1: `IDPTools__get_results`

Retrieve processing results and extracted metadata for documents in a batch.

```json
{
  "actionCapabilities": {
    "actionBased": true,
    "agentic": false
  },
  "description": "Retrieve processing results and extracted metadata for all documents in a batch. Returns document classification, extracted fields with values, field-level confidence scores, page counts, and processing status.",
  "name": "IDPTools___get_results",
  "actionType": "passthrough",
  "method": "read"
}
```

![get_results Action](images/image_028.png)

#### Action 2: `IDPTools__process`

Submit documents for processing through the IDP pipeline.

```json
{
  "actionCapabilities": {
    "actionBased": true,
    "agentic": false
  },
  "description": "Process documents through the IDP pipeline. Accepts S3 locations or base64-encoded content. Intelligently handles missing information by requesting specific details.",
  "name": "IDPTools___process",
  "actionType": "passthrough",
  "method": "write"
}
```

![process Action](images/image_029.png)

#### Action 3: `IDPTools__reprocess`

Reprocess documents from a specific pipeline step (classification or extraction).

```json
{
  "actionCapabilities": {
    "actionBased": true,
    "agentic": false
  },
  "description": "Reprocess documents from a specific pipeline step. Supports classification or extraction reprocessing. Returns batch ID for status tracking.",
  "name": "IDPTools___reprocess",
  "actionType": "passthrough",
  "method": "write"
}
```

![reprocess Action](images/image_030.png)

#### Action 4: `IDPTools__search`

Search and query processed documents using natural language.

```json
{
  "actionCapabilities": {
    "actionBased": true,
    "agentic": false
  },
  "description": "Search and query processed documents using natural language. Returns analytics, metrics, and document information from the IDP system.",
  "name": "IDPTools___search",
  "actionType": "passthrough",
  "method": "read"
}
```

![search Action](images/image_031.png)

---

## Part 3: Build the Amazon Quick Workflow

Now you'll create a workflow that orchestrates the full document processing pipeline.

### Phase 1: Document Retrieval from S3

#### Step 1.1 — Info Action (Log Start)

- **Action Type**: Info
- **Message**: `"Starting loan package document retrieval from S3"`

![Info Action](images/image_013.png)

#### Step 1.2 — Download File from S3

| Field | Value |
|-------|-------|
| **Action Title** | Download file |
| **Q action connector id** | S3 |
| **Bucket** | `runtime_config("loan_package_bucket")` |
| **Key** | `runtime_config("loan_package_key")` |
| **Output Variable** | `document` |

> **Note**: `runtime_config()` references are resolved at workflow execution time from the workflow's runtime configuration. Set these values when triggering the workflow.

![Download from S3](images/image_014.png)

#### Step 1.3 — Save Document Location

- **Value to Save**: `"s3://" + runtime_config("loan_package_bucket") + "/" + runtime_config("loan_package_key")`
- **Variable Name**: `document`

![Save Value](images/image_015.png)

#### Step 1.4 — Info Action (Log Success)

- **Message**: `"Successfully retrieved loan package document from S3"`

![Info Log](images/image_016.png)

---

### Phase 2: IDP MCP Document Processing

#### Step 2.1 — Log Start of Processing

- **Action Type**: Write log message
- **Message**: `"Starting IDP MCP document processing"`

![Log Start](images/image_017.png)

#### Step 2.2 — Custom Agent for IDP Processing

This is the core step — a custom agent that submits the document to the IDP pipeline and polls until processing completes.

| Field | Value |
|-------|-------|
| **Instruction** | `"Process the provided document using IDP MCP (Intelligent Document Processing). Document location: {document}. Submit the document to IDP MCP for processing using IDPTools__process, then continuously monitor the processing status by polling IDPTools__get_results until the batch status shows completion. Once complete, retrieve and return the final processing result including the completion status and any extracted data or metadata from the document."` |
| **Mode** | Pro |
| **Structured Output** | `status`, `extracted_data`, `metadata` |
| **Agent Response Variable** | `processing_result` |

**Available Actions (MCP tools):**
- `IDPTools__process`
- `IDPTools__get_results`

![Custom Agent Setup](images/image_018.png)
![Agent Actions](images/image_019.png)
![Structured Output Config](images/image_020.png)

#### Step 2.3 — Log Completion

- **Message**: `"IDP MCP document processing completed"`

![Processing Complete Log](images/image_021.png)

---

### Phase 3: Retrieve Processed Data

Extract the structured results from the processing agent's response:

#### Step 3.1 — Extract Status
- **Value**: `processing_result["status"]`

![Extract Status](images/image_032.png)

#### Step 3.2 — Extract Metadata
- **Value**: `processing_result["metadata"]`

![Extract Metadata](images/image_033.png)

#### Step 3.3 — Extract Data
- **Value**: `processing_result["extracted_data"]`

![Extract Data](images/image_034.png)

---

### Phase 4: Create Spreadsheet with Extracted Data

Build an Excel workbook with two sheets: **Metadata** and **Extracted Data**.

#### Step 4.1 — Log Start

- **Message**: `"Starting to format extracted loan package data into spreadsheet with metadata and extracted data sheets"`

![Log Spreadsheet Start](images/image_035.png)

#### Step 4.2 — Create Workbook

- **Output**: `resource_id` (workbook identifier for subsequent operations)

![Create Workbook](images/image_036.png)

#### Step 4.3 — Get Metadata Column Names

- **Value to Save**: `list(metadata.keys())`
- **Variable Name**: `metadata_column_names`

![Metadata Columns](images/image_037.png)

#### Step 4.4 — Create Metadata Table

- **Column Names**: `metadata_column_names`
- **New Table**: `metadata_table`

![Create Table](images/image_038.png)

#### Step 4.5 — Add Metadata Row

- **Data Table**: `metadata_table`
- **Row Values**: `[str(metadata[col]) for col in metadata_column_names]`
- **Updated Table**: `metadata_table`

![Add Row](images/image_039.png)

#### Step 4.6 — Rename Sheet to "Metadata"

- **Workbook Identifier**: `resource_id`
- **Current Sheet Name**: `"Sheet1"`
- **Updated Sheet Name**: `"Metadata"`

![Rename Sheet](images/image_040.png)

#### Step 4.7 — Write Metadata to Sheet

- **Workbook Identifier**: `resource_id`
- **Sheet Name**: `"Metadata"`
- **Start at Cell**: `"A1"`
- **Data Table**: `metadata_table`

![Write to Sheet](images/image_041.png)

#### Step 4.8 — Get Extracted Data Column Names

- **Value to Save**: `list(extracted_data.keys())`
- **Variable Name**: `extracted_data_column_names`

![Extracted Data Columns](images/image_042.png)

#### Step 4.9 — Create Extracted Data Table

- **Column Names**: `extracted_data_column_names`
- **New Table**: `extracted_data_table`

![Create Extracted Table](images/image_043.png)

#### Step 4.10 — Add Extracted Data Row

- **Data Table**: `extracted_data_table`
- **Row Values**: `[str(extracted_data[col]) for col in extracted_data_column_names]`
- **Updated Table**: `extracted_data_table`

![Add Extracted Row](images/image_044.png)

#### Step 4.11 — Create "Extracted Data" Sheet

- **Workbook Identifier**: `resource_id`
- **Sheet Name**: `"Extracted Data"`

![Create Sheet](images/image_045.png)

#### Step 4.12 — Write Extracted Data to Sheet

- **Workbook Identifier**: `resource_id`
- **Sheet Name**: `"Extracted Data"`
- **Data Table**: `extracted_data_table`

![Write Extracted Data](images/image_046.png)

#### Step 4.13 — Save Workbook

- **Workbook Identifier**: `resource_id`
- **Filename**: `"loan_package.xlsx"`
- **Output Variable**: `spreadsheet`

![Save Workbook](images/image_047.png)

#### Step 4.14 — Log Success

- **Message**: `"Loan package spreadsheet created successfully with Metadata and Extracted Data sheets"`

![Spreadsheet Success](images/image_048.png)

---

### Phase 5: Upload Spreadsheet to S3

#### Step 5.1 — Log Start of Upload

- **Message**: `"Starting spreadsheet upload to S3"`

![Log Upload Start](images/image_049.png)

#### Step 5.2 — Upload File to S3

| Field | Value |
|-------|-------|
| **Q action connector id** | `s3-action-connector` |
| **File** | `spreadsheet` |
| **Bucket** | `runtime_config("output_bucket")` |
| **Key** | `runtime_config("output_bucket_key")` |

![Upload to S3](images/image_050.png)

#### Step 5.3 — Save Output Location

- **Value to Save**: `f"s3://{runtime_config('output_bucket')}/{runtime_config('output_bucket_key')}"`
- **Variable Name**: `s3_location`

![Save Location](images/image_051.png)

#### Step 5.4 — Log Completion

- **Message**: `f"Spreadsheet successfully uploaded to {s3_location}"`

![Final Log](images/image_052.png)

#### Step 5.5 — Download the Result Spreadsheet from S3

Once the workflow completes, download the output spreadsheet directly from the S3 console:

1. Open the [Amazon S3 console](https://s3.console.aws.amazon.com/s3/home)
2. Navigate to your output bucket (the `output_bucket` value from your runtime configuration)
3. Locate the uploaded `.xlsx` file at the key path you configured
4. Click the file name, then click **Download**

---

## Runtime Configuration

When executing this workflow, provide the following runtime configuration values:

| Key | Description | Example |
|-----|-------------|---------|
| `loan_package_bucket` | S3 bucket containing the input document | `my-idp-input-bucket` |
| `loan_package_key` | S3 key (path) to the loan package PDF | `samples/lending_package.pdf` |
| `output_bucket` | S3 bucket for the output spreadsheet | `my-idp-output-bucket` |
| `output_bucket_key` | S3 key for the output Excel file | `results/loan_package.xlsx` |

---

## Additional Resources

### Documentation

- 📖 [GenAI IDP Accelerator Documentation Site](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/) — Full searchable documentation
- 🏗️ [Architecture Guide](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/architecture.md) — Detailed component architecture and data flow
- 🔌 [MCP Server Documentation](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/mcp-server.md) — MCP integration details and available tools
- 📋 [Configuration Guide](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/configuration.md) — Customizing document classification and extraction
- 🖥️ [Web UI Guide](https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws/web-ui/) — Using the IDP web interface

### Repository

- 🐙 [GitHub: accelerated-intelligent-document-processing-on-aws](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws)

### Alternative Implementations

- [GenAI IDP for AWS CDK](https://github.com/cdklabs/genai-idp) — CDK-based deployment
- [GenAI IDP Terraform](https://github.com/awslabs/genai-idp-terraform) — Terraform module

### Related Topics

- [Deployment Guide](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/deployment.md) — Build, publish, deploy, and test instructions
- [IDP CLI](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/idp-cli.md) — Command-line batch processing and evaluation
- [Human-in-the-Loop Review](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/human-review.md) — Human validation workflows
- [Evaluation Framework](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/evaluation.md) — Accuracy assessment

---

## Troubleshooting

| Issue | Resolution |
|-------|-----------|
| Stack deployment fails | Verify you have [Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html) enabled for required models |
| MCP connection fails | Verify the `MCPServerEndpoint`, `MCPConnectorClientId`, `MCPConnectorClientSecret`, and `MCPTokenURL` from CloudFormation Outputs match your integration config |
| Agent times out during processing | Large documents may take several minutes — increase the agent timeout or ensure the polling logic has adequate retries |
| No extracted data returned | Verify the input document is in a [supported format](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/docs/configuration.md) and that classification/extraction configs are properly set up |
| S3 upload permission denied | Ensure the Amazon Quick S3 connector has write access to the output bucket |
