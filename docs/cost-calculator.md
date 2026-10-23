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

### 4. Additional Services (and the always-on floor)

- **Amazon CloudWatch**: Costs for logs and metrics.
- **Amazon Cognito**: Costs based on monthly active users.
- **Amazon API Gateway**: Costs based on REST API requests. The UI polls for document and job status, so request volume scales with the number of open browser tabs as well as document throughput; polling pauses on hidden tabs.
- **Bedrock Knowledge Base**: Costs for queries and storage if this optional feature is used.

### The split that matters for setting a budget

The drivers above divide into two groups that behave completely differently, and
a budget set without separating them will be wrong in one direction or the other:

| | What it is | How it scales |
|---|---|---|
| **Per-document** | Bedrock inference (classification, extraction, assessment, summarization), Textract or BDA, the Lambda and Step Functions work around them | Linear in documents × pages, and **dominated by Bedrock inference**. Retries multiply it: a document that retries a Bedrock call several times pays for each attempt |
| **Always-on** | S3 and DynamoDB storage, CloudWatch logs and metrics, Cognito monthly active users, the API Gateway requests the UI's polling makes, and — if enabled — a Bedrock Knowledge Base vector store or a Pattern 3 SageMaker endpoint | Roughly flat per month. Independent of how many documents you process, including when you process none |

Two consequences. First, a deployment that processes nothing still costs money,
so a budget must leave room for the floor. Second, an optional feature can move
the floor by far more than document volume does — a SageMaker endpoint for
Pattern 3 and an OpenSearch Serverless vector store for a Knowledge Base are both
billed for being provisioned, not for being used.

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

The estimate multiplies a token count by one rate per unit. A model that instead
charges a different rate above a size threshold cannot be expressed that way, so
it is reported at one of its two rates. One model offered here works like that:

- **OpenAI GPT-6 Astra** bills input above **272,000 tokens** at roughly double,
  and the estimate uses its standard rate, so a request over that line is
  **under-reported**. This is easy to hit by accident, because the accelerator
  derives its shard and summarization budgets from the model's declared window.
  See [OpenAI models](openai-models.md) for the derived budgets and two ways to
  remove the discrepancy.

**Long-context Claude models (a `:1m` model ID) are not such a case.** The `:1m`
suffix selects the 1M-token context window, and that window is priced at the
model's standard per-token rates — a 900K-token request costs the same per token
as a 9K-token one. There is one rate, and the estimate reports it. Bedrock's
published price list confirms it: every one of the six models offered with the
suffix has exactly one input-token rate per routing mode and service tier, with no
token-volume band at all (AWS Price List offer file
`AmazonBedrockFoundationModels`, version 20260911124410, retrieved 2026-09-17),
and Anthropic states the same directly in its
[long context pricing](https://platform.claude.com/docs/en/about-claude/pricing#long-context-pricing).

Before v0.6.9 these models were reported at a premium — 2× input, 1.5× output —
on every request. That premium was the rate card of an earlier Claude 1M-context
beta and applied to none of the models offered with the suffix, so it simply
overstated their cost, by up to 1.8× in document cost tables, the Athena rollups,
benchmark summaries and evaluation reports. Nothing was ever overspent; the defect
was in the rate card, not in what Bedrock charged. If you compare reports across
that upgrade, expect reported `:1m` costs to drop
([#899](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/899)).

A banded price like Astra's cannot be resolved by the cost report itself: token
counts are summed per processing step and model across every call made on a
document before any price is looked up, so the report cannot tell one
250,000-token request from ten 25,000-token ones. Charging a band off that sum
would be wrong, not approximate. It could be resolved where the metering record is
first written, which does see one request at a time — everywhere except the
agentic extraction path, where the agent framework reports only usage already
accumulated across the agent loop. Until then, only the per-request numbers in
your AWS bill, or a Cost and Usage Report, settle a large-request bill exactly.

### `DocumentEstimatedCost` — a CloudWatch metric you can alarm on

The per-document cost above is also emitted to CloudWatch when a document's
reporting data is written, in the namespace named after your stack, alongside a
companion metric:

| Metric | Unit | Meaning |
|---|---|---|
| `DocumentEstimatedCost` | None (USD) | One datapoint per completed document, carrying exactly the cost already written to that document's metering rows. Use `Sum` over a period for spend in that period, `Average` for cost per document, `Maximum` for the most expensive single document, `SampleCount` for documents completed |
| `UnpricedMeteringRows` | Count | How many of that document's metering rows had no price in `pricing.yaml`. **Read this alongside the cost.** A non-zero value means the cost figure is short by an unknown amount, which is what a model missing from the pricing table looks like |

A document for which *no* row could be priced emits **no** cost datapoint rather
than a zero — a zero would read as a free document, pull the average down and
make a rolling sum under-report. A document priced at a genuine zero does emit
one. This matches the metering table, which writes NULL rather than `0.00` for an
unpriced row.

**Neither metric carries dimensions, deliberately.** CloudWatch bills per metric
per distinct dimension combination, so a `DocumentId` dimension would be one
billable metric per document, and a `DocumentClass` or `Model` dimension grows
with your taxonomy and your model choices. A cost metric that costs more than the
spend it reports is not a cost control. The namespace is already the stack name,
so deployments are separated without a dimension, and the per-document,
per-model, per-class breakdown already exists in the metering table, the Athena
rollups and the UI's cost panel. What those cannot do is drive an alarm, which is
the only thing this metric adds.

> **The metric reports; it does not enforce.** Nothing in the stack throttles,
> queues or stops processing when spend rises. `MaxConcurrentWorkflows` is the
> only ex-ante lever the stack has, it is set at deploy time, and nothing moves it
> at runtime.

To alarm on a spend rate, create an alarm on `Sum` of `DocumentEstimatedCost`
over a period that matches how fast you want to know — a 1-hour period with a
threshold at a few percent of your monthly expectation catches a runaway within
the hour, long before AWS billing data exists for it. That alarm is yours to
create: the stack does not create one, because the right threshold depends
entirely on your volume.

### AWS Cost Management Tools

The stack creates no budget. In addition to the built-in cost tracking, set these
up yourself:

- **AWS Cost Explorer**: Analyze and visualize your costs and usage over time
- **AWS Budgets**: Set a monthly cost budget and receive alerts when spend crosses
  a threshold — see the recommended starting amount below
- **AWS Cost and Usage Reports**: Generate detailed reports on your AWS costs and usage

#### Recommended starting budget

⚠️ **Scope the budget before you choose an amount, because the obvious scoping
does not work.** An account-wide budget is the only one this solution can be sure
is measuring something, and it measures *everything* in the account — so it is
the right answer only where the stack has the account to itself. Filtering a
budget to this stack's resource tags is the natural alternative and it would
report a figure far below real spend, for two reasons that compound:

1. **This solution's dominant cost carries no taggable resource.** Per-document
   Bedrock inference is on-demand model invocation, and AWS is explicit that
   [you cannot assign tags to on-demand models](https://repost.aws/knowledge-center/bedrock-add-cost-allocation-tags).
   There is no tagged resource in the request, so no resource-tag cost filter can
   see that spend. Amazon Textract API calls are the same. A stack-tag filter
   captures the Lambda, DynamoDB and S3 line items — the small ones — and misses
   the inference bill entirely.
2. **A tag reaches a cost filter only after it is activated.** Cost-allocation
   tags, including the `aws:cloudformation:stack-name` tag CloudFormation applies
   for you, do nothing until someone **activates** them in the payer account's
   Billing console. That is an account-level manual step, it can take up to 24
   hours to take effect, and it is forward-looking only — it does not
   retroactively tag spend already recorded.

So: **deploy the stack into its own AWS account if you need the number to be this
stack's**, and budget the account. If you must share an account, the route to
per-stack attribution is
[Bedrock application inference profiles](#cost-attribution-with-bedrock-application-inference-profiles)
below plus activated cost-allocation tags — and confirm in Cost Explorer that
your filter actually returns your spend before you trust a budget built on it.

There is no single recommended amount, because per-document cost across the
configurations this solution supports spans roughly two orders of magnitude.
Measured figures from the benchmark suite bracket it: the OCR benchmark corpus
costs **$0.02–0.05 per document** in simple mode, while large multi-page
documents through advanced (agentic) mode measured **$1.22–$2.65 per document**
across Sonnet 4.6, Sonnet 5, Opus 5 and GPT-6 Astra on the v0.6.11 grid. See
[the configuration guidance](benchmarking/config-guidance.md) for the full
matrix. Pick from your own arm of that range rather than averaging it.

The method, in order:

1. **Measure before you guess.** Process 20–50 representative documents and read
   the per-document cost from the web UI's cost panel, from
   `DocumentEstimatedCost`, or by querying `SUM(estimated_cost)` grouped by
   `document_id` in the metering table. That is your real per-document figure,
   for your documents, with your configuration.
2. **Budget = (monthly document volume × measured cost per document) + the
   always-on floor, then add 50% headroom.** The headroom is not padding: it is
   what stops a normal month of growth, or a retry-heavy day, from alarming at
   100% and training you to ignore the alert.
3. **If you have no measurement yet, start at $500 for a pilot.** It is well
   above a thousand-document month at the cheap end of the range above and well
   below a runaway, so the first notification tells you which regime you are in.
   Then replace it with the figure from step 2 after the first full month.

Set the threshold where you want to *find out*, not where you want to stop. An
80% notification is the useful one; by 100% the month is already spent. And note
that AWS Budgets refreshes up to three times a day and billing data itself lags
usage, so a budget is a monthly guardrail — the CloudWatch metric above is what
tells you within the hour.

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
3. Select the **foundation model** currently used in your IDP configuration (e.g., `us.anthropic.claude-sonnet-4-5-20250929-v1:0`)
4. Add your cost-allocation tags. For example:
   - `map-migrated`: `migDNDBZMXMLZ`
   - `project`: `my-idp-workload`
5. Note the **Application Inference Profile ARN** after creation — it will look like:
   ```
   arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcdef123456
   ```

> **💡 Tip:** Create separate inference profiles for each processing stage (classification, extraction, assessment) if you need per-stage cost breakdowns.
>
> Under `classification.strategy: two_stage` classification is **two** separately
> metered stages — `Segmentation` and `SectionClassification` — each with its own
> model field, so it can take two inference profiles. Under `single_pass` (*One
> stage* in the editor) it is one stage metered as `Classification`.

#### Step 2 — Update the IDP Configuration (No Code Change Needed)

1. In the IDP web UI, go to **View/Edit Configuration**
2. Toggle to **JSON View** or **YAML View**
3. Find the `model` or `model_id` fields in the relevant processing step sections — for example:
   - **Classification**: `classification.one_stage.llm.model` (or, under `strategy: two_stage`, each call's `llm.model`: `classification.two_stage.segmentation.llm.model` and `classification.two_stage.section_classification.llm.model`)
   - **Extraction**: `extraction.model`
   - **Assessment (confidence)**: `extraction.confidence.model`
   - **Summarization**: `summarization.model`
4. **Replace** the standard model ID or cross-region inference profile ID with the new **Application Inference Profile ARN**

   **Before:**
   ```yaml
   model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
   ```

   **After:**
   ```yaml
   model: arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/abcdef123456
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
