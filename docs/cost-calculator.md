---
title: "GenAI IDP Accelerator Cost Considerations"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# GenAI IDP Accelerator Cost Considerations

> **INFORMATION DOCUMENT**  
> This document provides conceptual guidance on the cost factors to consider when using the GenAI Intelligent Document Processing (GenAIIDP) Accelerator solution.

This document provides a framework for understanding the cost elements of running the GenAI IDP Accelerator solution. It outlines the primary contributors to cost and provides guidance on cost optimization across the different processing patterns.

## Key Cost Drivers

The primary cost drivers for the GenAI IDP Accelerator solution include:

### 1. Document Processing Services

#### Pattern 1: Bedrock Data Automation (BDA)
- **BDA Processing**: The main cost component for Pattern 1, charged per document processed.
- **Amazon Bedrock**: Used for summarization (if enabled).

#### Pattern 2: Textract and Bedrock
- **Amazon Textract**: Costs based on the number of pages processed.
- **Amazon Bedrock**: Costs based on the models used, input tokens processed, and output tokens generated.

#### Pattern 3: Textract, SageMaker (UDOP), and Bedrock
- **Amazon Textract**: Costs based on the number of pages processed.
- **Amazon SageMaker**: Costs based on the instance type used and running time.
- **Amazon Bedrock**: Costs for extraction and optional summarization.

### 2. Storage Costs

- **Amazon S3**: Costs based on the amount of data stored and storage duration.
- **Amazon DynamoDB**: Costs based on the stored document metadata.

### 3. Processing Infrastructure

- **AWS Lambda**: Costs based on request count, duration, and memory usage.
- **AWS Step Functions**: Costs based on state transitions for workflow orchestration.
- **Amazon SQS**: Costs based on message count for document queue management.

### 4. Additional Services

- **Amazon CloudWatch**: Costs for logs and metrics.
- **Amazon Cognito**: Costs based on monthly active users.
- **AWS AppSync**: Costs based on GraphQL API queries.
- **Bedrock Knowledge Base**: Costs for queries and storage if this optional feature is used.

## Cost Optimization Strategies

1. **Right-size your model selection**:
   - Use simpler models for routine document processing
   - Reserve more powerful models for complex documents requiring higher accuracy

2. **Configure OCR features appropriately**:
   - Only enable Textract features you need (e.g., TABLES, FORMS, SIGNATURES)
   - Select processing options based on document requirements

3. **Implement prompt caching**:
   - The solution supports prompt caching to significantly reduce costs when processing similar documents
   - Especially effective when using few-shot examples, as these can be cached across invocations

4. **Optimize document preprocessing**:
   - Compress images before processing to reduce Token costs

5. **Implement tiered storage**:
   - Move older processed documents to S3 Infrequent Access or Glacier
   - Implement lifecycle policies based on document age

6. **Monitor and alert on costs**:
   - Set up AWS Budgets to track spending
   - Create alerts for unusual processing volumes

7. **Optimize knowledge base usage** (if used):
   - Limit knowledge base queries to essential use cases
   - Implement caching for common queries

## Cost Monitoring and Estimation

### Built-in Web UI Cost Estimation

The GenAI IDP Accelerator solution includes a built-in cost estimation feature in the web UI that calculates and displays the actual processing costs for each document. This feature:

- Tracks and displays costs per service/API used during document processing
- Breaks down costs by input tokens, output tokens, and page processing
- Shows the total estimated cost for each document processed
- Enables per-page cost analysis for detailed cost monitoring
- Uses service pricing from the solution configuration, which can be modified to reflect any pricing variations or special agreements

This real-time cost tracking helps you monitor actual usage patterns and optimize costs based on real-world usage.

### What the estimate cannot express: prices that change with request size

The estimate multiplies a token count by one rate per unit. Some models instead
charge a different rate above a size threshold, and a single rate cannot say both
things, so those models are reported at one of the two rates:

- **Long-context Claude models (a `:1m` model ID)** are reported at the
  **standard** rate. Anthropic's long-context premium — 2x input, 1.5x output —
  applies only to a request whose input exceeds **200,000 tokens** (cache reads
  count toward that total), and virtually every request this solution makes is
  well under it: a request is one section or one shard, not a whole document.
  Reporting the standard rate is therefore right for nearly all traffic. The
  residual gap is that a genuinely long request — over 200K input tokens in a
  single call, which requires a very large section — is **under-reported**, by up
  to 2x on its input tokens.

  Before v0.6.9 these models were reported at the premium rate on *every*
  request, which overstated their cost by up to 1.8x in document cost tables,
  the Athena rollups, benchmark summaries and evaluation reports. Nothing was
  ever overspent — the defect was in the rate card, not in what Bedrock
  charged — but `:1m` models looked far more expensive than they are. If you
  compare reports across that upgrade, expect reported `:1m` costs to drop.

- **OpenAI GPT-6 Astra** has the mirror-image case: it bills input above
  **272,000 tokens** at roughly double, and is reported at its standard rate, so
  a very large request is under-reported.

Neither threshold can be applied by the cost report itself. Token counts are
summed per processing step and model across every call made on a document before
any price is looked up, so a report cannot tell one 250,000-token request from
ten 25,000-token ones. Only the per-request numbers in your AWS bill, or a Cost
and Usage Report, can settle a long-context bill exactly.

### AWS Cost Management Tools

In addition to the built-in cost tracking, consider using these AWS tools:

- **AWS Cost Explorer**: Analyze and visualize your costs and usage over time
- **AWS Budgets**: Set custom budgets and receive alerts when costs exceed thresholds
- **AWS Cost and Usage Reports**: Generate detailed reports on your AWS costs and usage

## Cost Attribution with Bedrock Application Inference Profiles

Amazon Bedrock [Application Inference Profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-create.html) let you tag Bedrock model invocations with custom cost-allocation tags (e.g., project, team, or migration program identifiers). Because all Bedrock calls in the GenAI IDP Accelerator are driven by model IDs in the configuration, you can enable cost attribution **without any code changes** — just create an inference profile and update the configuration.

### Why Use Application Inference Profiles?

By default, Bedrock usage appears in AWS Cost Explorer as a single line item per model. Application inference profiles enable you to:

- **Attribute Bedrock costs** to specific projects, teams, or migration programs using AWS cost-allocation tags
- **Track costs per workload** when multiple applications share the same AWS account
- **Support MAP (Migration Acceleration Program) tagging** — e.g., `map-migrated: migDNDBZMXMLZ`
- **Separate cost reporting** across different IDP document processing pipelines

### Step-by-Step Setup

#### Step 1 — Create an Application Inference Profile in the Bedrock Console

1. Open the **Amazon Bedrock Console** → **Inference** → **Inference profiles**
2. Click **Create inference profile**
3. Select the **foundation model** currently used in your IDP configuration (e.g., `us.anthropic.claude-3-7-sonnet-20250219-v1:0`)
4. Add your cost-allocation tags. For example:
   - `map-migrated`: `migDNDBZMXMLZ`
   - `project`: `my-idp-workload`
5. Note the **Application Inference Profile ARN** after creation — it will look like:
   ```
   arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcdef123456
   ```

> **💡 Tip:** Create separate inference profiles for each processing stage (classification, extraction, assessment) if you need per-stage cost breakdowns.

#### Step 2 — Update the IDP Configuration (No Code Change Needed)

1. In the IDP web UI, go to **View/Edit Configuration**
2. Toggle to **JSON View** or **YAML View**
3. Find the `model` or `model_id` fields in the relevant processing step sections — for example:
   - **Classification**: `classification.model_id`
   - **Extraction**: `extraction.model_id`
   - **Assessment (confidence)**: `extraction.confidence.model`
   - **Summarization**: `summarization.model`
4. **Replace** the standard model ID or cross-region inference profile ID with the new **Application Inference Profile ARN**

   **Before:**
   ```yaml
   model_id: us.anthropic.claude-3-7-sonnet-20250219-v1:0
   ```

   **After:**
   ```yaml
   model_id: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcdef123456
   ```

5. Click **Save Changes**

> **⚠️ Note:** Application inference profiles are region-specific. If you are using cross-region inference profiles (e.g., `us.*` or `eu.*` prefixes) for multi-region routing, be aware that an application inference profile pins invocations to the region where it was created. See the [EU Region Model Support](./eu-region-model-support.md) doc for details on cross-region behavior.

### Verifying Cost Attribution

After processing documents with the updated configuration:

1. Go to **AWS Cost Explorer**
2. Group by your cost-allocation tag (e.g., `map-migrated`)
3. Bedrock invocations made through the application inference profile will now appear under the tagged allocation

> **📝 Note:** Cost allocation tags may take up to 24 hours to appear in Cost Explorer after first use. You must also [activate the tags](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html) in the Billing console if they are user-defined tags.

## Disclaimer

The GenAI IDP Accelerator solution is designed to provide cost transparency and efficiency. However, actual costs will depend on your specific implementation, document characteristics, and processing needs. Always refer to the official AWS pricing pages for the most current pricing information for all services used.

## References

- [AWS Lambda Pricing](https://aws.amazon.com/lambda/pricing/)
- [Amazon S3 Pricing](https://aws.amazon.com/s3/pricing/)
- [Amazon Textract Pricing](https://aws.amazon.com/textract/pricing/)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Amazon Bedrock Data Processing Jobs Pricing](https://aws.amazon.com/bedrock/pricing/data-processing-jobs/)
- [AWS Step Functions Pricing](https://aws.amazon.com/step-functions/pricing/)
- [Amazon SageMaker Pricing](https://aws.amazon.com/sagemaker/pricing/)
- [Amazon DynamoDB Pricing](https://aws.amazon.com/dynamodb/pricing/)
- [Amazon CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)
