---
title: "Capacity Planning"
---

# Capacity Planning

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Overview

The GenAI IDP accelerator includes comprehensive capacity planning capabilities to help you optimize document processing performance, predict resource requirements, and ensure your system can handle expected workloads. This system provides real-time capacity analysis, AWS service quota recommendations, and performance optimization guidance.


https://github.com/user-attachments/assets/6e668c2f-c6dc-490e-a4ce-10c533cd330e


**The arithmetic assumes the Textract-plus-Bedrock pipeline**, whose well-defined
processing steps (OCR, Classification, Extraction, Assessment, Summarization) carry
predictable Bedrock model quota requirements. A deployment running documents through
BDA mode (`use_bda: true`) has a different step set and a different quota profile, so
set the pattern manually on the Capacity Planning screen before reading the numbers —
see [Pattern detection and the manual override](#pattern-detection-and-the-manual-override).

## Key Benefits

- **Predictive Analysis**: Calculate processing capacity requirements before scaling production workloads
- **Cost Optimization**: Right-size AWS resources based on actual processing patterns
- **Performance Planning**: Identify bottlenecks and optimize processing pipelines
- **Quota Management**: Automatically calculate required AWS service quotas (TPM and RPM)
- **Load Distribution**: Plan processing schedules to maximize throughput
- **Real-time Monitoring**: Track capacity utilization and adjust dynamically

## Architecture Overview

The capacity planning system consists of several integrated components that work together to provide comprehensive capacity analysis:

### Core Components

1. **GraphQL Resolver**: `CalculateCapacityResolverFunction` that handles capacity calculation requests
2. **Capacity Calculation Engine**: Lambda-based processing engine that analyzes document requirements and generates recommendations
3. **Web UI Interface**: Interactive React-based capacity planning calculator with real-time visualizations
4. **Token Usage Analysis**: Automatic population of token usage from processed documents' metering data
5. **Quota Analysis**: Automated AWS service quota requirement calculation (TPM and RPM) with direct links to AWS console
6. **Latency Distribution Modeling**: Statistical analysis of processing times with percentile-based predictions from real document data

### Data Flow

1. **Input Configuration**: Users define document types, token usage, and processing schedules through the Web UI
2. **Historical Analysis**: System extracts token usage and page counts from processed documents' metering data
3. **Capacity Calculation**: GraphQL resolver invokes calculation engine to process requirements
4. **Real Metrics**: Processing times and queue delays are calculated from actual processed document data
5. **Quota Assessment**: System calculates required AWS service quotas (TPM and RPM) with direct console links
6. **Visualization**: Results displayed through interactive dashboard with hourly token distribution charts

## Capacity Planning Features

### 1. Interactive Capacity Calculator

The Web UI provides an intuitive interface for capacity planning with real-time token usage population:

**Document Configuration**:
- Document type selection **only from classes defined in View/Edit Configuration**
- Average pages per document (automatically extracted from processed documents' metering data)
- Token usage configuration for each processing step (OCR, Classification, Extraction, Assessment, Summarization)
- Support for automatic token and page count population from processed documents' metering data
- CSV import/export functionality for bulk configuration

**Processing Schedule Configuration**:
- Hourly processing schedule with document type and volume specification
- Visual time slot selection (24-hour format)
- Document type filtering based on **configured document types only**
- CSV import/export for schedule management

**Real-time Token Analysis**:
- Hourly token distribution visualization with stacked bar charts
- Peak hour analysis with load distribution insights
- Processing time percentile analysis (P50, P75, P90, P95, P99) from **real document data**
- Quota-based status indicators (green/blue when within quota, red when quota exceeded)

### 2. Advanced Analytics Engine

The capacity calculation system provides sophisticated analysis through GraphQL resolvers:

**Token Usage Extraction**:
- Automatic extraction from processed documents' metering data
- Context-aware parsing of OCR, classification, extraction, assessment, and summarization tokens
- Request count estimation based on average requests per document from metering data
- Page count extraction from multiple sources (OCR requests, document fields, sections)

**Latency Distribution Modeling**:
- Statistical analysis of processing times from **actual processed documents** (P50, P75, P90, P95, P99)
- Queue delay calculation from **real QueuedTime/WorkflowStartTime timestamps**
- SLA compliance checking of the **P99** against configured maximum latency (in seconds)
- Performance warning alerts for quota exceedances

**RPM (Requests Per Minute) Calculation**:
- Calculates **average requests per document** from metering data samples, summing
  every Bedrock metering key a document recorded for that step
- Multiplies by the **busiest** hour's scheduled documents to get requests per hour
- Applies the 10% safety buffer
- Formula: `avg_requests_per_doc × peak_scheduled_docs_per_hour / 60 × 1.1`

### 3. Document Token Usage Population

**Automatic Token Population from Processed Documents**:
- Reads the completed documents in the Document List's current period. The list's rows
  carry no metering, so the full record of the most recent completed documents (up to 25)
  is fetched when you choose **Populate tokens from Documents**, one request per document.
  Opening Capacity Planning starts the list load if no other page has.
- Extraction of token usage from metering data structure
- Support for multiple document selection and batch population
- Document picker modal with filtering and selection capabilities
- **Single-class document validation** - multi-class documents are not supported

**Metering Data Processing**:
- Context-prefixed key parsing (OCR/, Classification/, Segmentation/, SectionClassification/, Extraction/, Assessment/, Summarization/, BDAProject/bda/)
- All three classification contexts are summed into the one classification figure: `Classification/` carries the Bedrock tokens under `strategy: single_pass`, and `Segmentation/` and `SectionClassification/` carry them under `strategy: two_stage` (see [Which context the tokens arrive under](#which-context-the-tokens-arrive-under))
- Token count aggregation from inputTokens, outputTokens, and totalTokens fields
- Request count calculation: average requests per document from metering samples
- Page count extraction from multiple sources:
  - OCR Bedrock requests (`metrics.requests` as page count)
  - Metering `pages`, `pageCount`, `PageCount` fields
  - Document-level `PageCount`, `pageCount`, `Pages` fields
  - Sections `EndPage` field as fallback

**Document Type Validation**:
- Document Type dropdown **only shows classes from View/Edit Configuration**
- Documents must be classified with types defined in configuration
- Multi-class documents are rejected with validation error
- Unclassified documents are not supported

### 4. AWS Service Quota Analysis

**Automated Quota Calculation**:
- Dynamic model configuration from deployment settings
- **Tokens Per Minute (TPM)** quota requirements for each model
- **Requests Per Minute (RPM)** quota requirements for each model
- Regional quota availability analysis with direct console links

**Quota Requirements Display**:
- Separate tables for TPM and RPM quotas
- Current vs. required quota comparison with utilization percentages
- Status indicators: ✅ Sufficient, ⚠️ Increase Needed
- Direct "Request Increase" buttons linking to AWS Service Quotas console

> ⚠️ **The RPM row is approximate for the Claude Sonnet models, and reads as
> authoritative.** AWS publishes no requests-per-minute quota for Claude Sonnet 5
> or Claude Sonnet 5.5, so `BEDROCK_MODEL_RPM_QUOTA_CODES` records their
> *tokens*-per-minute code for the RPM lookup. The consequence is a row that looks
> like every other one: a six-million "RPM quota", an affirmative ✅, and a
> "Request Increase" link that opens the TPM quota's console page. **The TPM rows
> are exact; treat the RPM row for either Sonnet as a placeholder rather than a
> measurement**, and read the TPM table when deciding whether to request an
> increase.
>
> This does not distort the capacity figure. The request limit is the **minimum RPM
> across every model in the TPM map**, not just the ones the plan calls, so an
> overstated value cannot bind a minimum. (The TPM map is what decides membership:
> the RPM lookup is built from its key set — `generate_rpm_quota_codes(
> tpm_model_quotas.keys())` — and `BEDROCK_MODEL_RPM_QUOTA_CODES` only supplies the
> code for each key it finds. The two maps carry the same keys today, and a model
> added to one and not the other is the case the checklist in
> `.claude/skills/add-model.md` exists to prevent.)
>
> ⚠️ **Note that this reduction is over a wider set than the TPM one, and the
> difference is not cosmetic.** The token limit *is* narrowed to the plan's step models, so a model
> the pipeline never calls cannot constrain it. The request limit is not, so a
> genuinely **low** RPM quota for a model you do not use still binds the RPM row and
> can send you to request an increase you do not need. Check which model the row
> names before acting on it.
>
> ⚠️ Separately: the quota maps cover a **subset** of the selectable models. A step
> model with no entry in them fails Capacity Planning outright with
> `ValueError: TPM quota not available for model <id>` rather than omitting a row,
> and the message names the variable to add it to.
>
> ⚠️ **The reverse also holds, and it is not scoped to one row: a quota code that
> does not resolve in the stack's Region fails the whole Capacity Planning
> response.** Every entry in both maps is fetched with `get_service_quota`, and a
> non-throttling error is re-raised rather than skipped — so one bad code takes out
> the report for every model, not just its own. Add a code only after confirming it
> resolves in the Regions you deploy to, with the `aws service-quotas
> list-service-quotas --service-code bedrock` query in
> `.claude/skills/add-model.md`. `BEDROCK_MODEL_QUOTA_CODES` and
> `BEDROCK_MODEL_RPM_QUOTA_CODES` in `template.yaml` are the authority for which
> models are covered — read them rather than a list here, which would go stale on
> the next model addition.

**Latency Bar Color Coding**:
- **Green** (P50, P75): When all model quotas are within limits
- **Blue** (P90, P95, P99): When all model quotas are within limits
- **Red**: Only when any model quota is exceeded (shows "Increase Needed")

**Environment Configuration Requirements** (Lambda):
- `BEDROCK_MODEL_QUOTA_CODES`: JSON mapping of model IDs to TPM quota codes (required)
- `BEDROCK_MODEL_RPM_QUOTA_CODES`: JSON mapping of model IDs to RPM quota codes (required)
- `METERING_TABLE_NAME`: DynamoDB table for metering data (required)
- `TRACKING_TABLE`: DynamoDB table for document tracking (required)
- `LAMBDA_MEMORY_GB`: Lambda memory size for gb_seconds to seconds conversion (required)

## Configuration and Customization

### Stack-Level Parameters

**Core Capacity Settings**:
- `MaxConcurrentWorkflows`: Maximum parallel executions (default: 100)
- `DataRetentionInDays`: Data retention period (default: 365)
- `ErrorThreshold`: Error alerting threshold (default: 1)
- `ExecutionTimeThresholdMs`: Slow-execution alarm threshold (default: 300000 ms); it alerts, it does not stop anything
- `WorkflowExecutionTimeoutSeconds`: Execution-level bound on a single document's workflow (default: 21600 s, i.e. 6 hours). Raise it if legitimate runs of your largest packets approach it; tripping it discards the execution's completed work
- `BDACallbackTimeoutSeconds`: BDA completion-callback bound (default: 7200 s); keep it below `WorkflowExecutionTimeoutSeconds`
- `QueueStalledAgeThresholdSeconds`: Stalled-queue alarm threshold (default: 1800 s). Raise it if bulk uploads against a low `MaxConcurrentWorkflows` alarm while progressing normally

**Processing Configuration**:
- `LogLevel`: Logging verbosity (DEBUG, INFO, WARN, ERROR)
- `LogRetentionDays`: CloudWatch log retention (default: 30)
- `EnableXRayTracing`: Distributed tracing enablement
- `EnableMCP`: Model Context Protocol integration

### Environment Variables (Lambda)

**Required Lambda Environment Variables**:
- `BEDROCK_MODEL_QUOTA_CODES`: JSON mapping of Bedrock model IDs to TPM quota codes
- `BEDROCK_MODEL_RPM_QUOTA_CODES`: JSON mapping of Bedrock model IDs to RPM quota codes
- `METERING_TABLE_NAME`: DynamoDB table name for metering data
- `TRACKING_TABLE`: DynamoDB table name for document tracking
- `LAMBDA_MEMORY_GB`: Lambda memory size (e.g., "1.0" for 1GB)
- `MIN_TOKENS_PER_REQUEST`: Minimum tokens per request for safety calculations

**Complexity Thresholds** (for recommendations):
- `MEDIUM_COMPLEXITY_THRESHOLD`: Token density threshold for medium complexity
- `HIGH_COMPLEXITY_THRESHOLD`: Token density threshold for high complexity
- `PAGE_COMPLEXITY_FACTOR`: Multiplier for page count complexity
- `HIGH_COMPLEXITY_MULTIPLIER`: Multiplier for high complexity documents
- `MEDIUM_COMPLEXITY_MULTIPLIER`: Multiplier for medium complexity documents

**Recommendation Thresholds**:
- `RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD`: Complexity factor threshold for high complexity warning
- `RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD`: Complexity factor threshold for medium complexity warning
- `RECOMMENDATION_HIGH_LOAD_THRESHOLD`: Load factor threshold for high load warning
- `RECOMMENDATION_MEDIUM_LOAD_THRESHOLD`: Load factor threshold for medium load warning
- `RECOMMENDATION_HIGH_LATENCY_THRESHOLD`: P99 latency threshold (seconds) for high latency warning
- `RECOMMENDATION_LARGE_DOC_THRESHOLD`: Token threshold for large document warning
- `RECOMMENDATION_HIGH_PAGE_THRESHOLD`: Page count threshold for high page warning

### Environment Variables (UI)

**UI Configuration Parameters**:
- `VITE_DEFAULT_MAX_LATENCY`: Default maximum latency setting in **seconds** (default: 60)
- `VITE_DEFAULT_TOKENS_BY_STEP`: JSON object with default token limits per processing step
- `VITE_DEFAULT_MAX_TOKENS_PER_REQUEST`: Default maximum tokens per API request (4000)
- `VITE_BDA_TOKENS_PER_PAGE`: Estimated tokens per page for BDA pattern processing (2000)
- `VITE_AWS_REGION`: AWS region for console URL generation (required)
- `VITE_BEDROCK_MODEL_QUOTA_CODES`: Optional JSON mapping for direct quota code links

## Using the Capacity Planning System

### 1. Accessing the Capacity Planner

Navigate to the Web UI and select the "Capacity Planning" section. The navigation link
is present in every deployment.

1. **Prerequisites**:
   - Process some documents first to populate metering data
   - Documents must be single-class and classified with configured document types
2. **Configuration**: Visit "View/Edit Configuration" tab to load your pattern configuration
3. **Navigation**: Click on "Capacity Planning" in the main navigation, under Configuration
4. **Pattern**: The screen sizes the Textract-plus-Bedrock pipeline unless you override
   it — see below

#### Pattern detection and the manual override

The screen plans against the Textract-plus-Bedrock pipeline by default, which is the
right basis for a deployment whose documents run in pipeline mode (`use_bda: false`).
It does not read `use_bda`, so **a BDA-mode deployment needs the override**: the quota
requirements it would otherwise report are for steps those documents never call.

The override lives behind the **⚠️ Pattern Detection Used Fallback** alert, which
appears when the selected configuration profile names no classification model. A
`use_bda: true` profile is merged onto the BDA defaults, which carry no `classification`
block at all, so the alert is present in exactly the case that needs the override.
Under **Manual Override Available** it offers three buttons — **Pattern 1 (BDA)**,
**Pattern 2 (Bedrock)** and **Pattern 3 (SageMaker)**. Choose **Pattern 1 (BDA)** for a
BDA-mode deployment. An active override is confirmed by a **⚠️ Manual Pattern Override
Active** alert carrying a **Reset to Automatic Detection** link.

### 2. Document Configuration

**Step 1: Document Type Setup**
- Select document types **only from classes configured in View/Edit Configuration**
- Use "Populate tokens from Documents" to automatically extract token usage from processed documents
- Average pages per document is automatically extracted from metering data
- Set token usage for each processing step (OCR, Classification, Extraction, Assessment, Summarization)

**Step 2: Token Population from Processed Documents**
```javascript
// Example of automatic token and page extraction from metering data
{
  "OCR/bedrock/us.amazon.nova-lite-v1:0": {
    "totalTokens": 1500,
    "requests": 3,  // Used as page count
    "pages": 3
  },
  "Classification/bedrock/anthropic.claude-3-haiku": {
    "inputTokens": 800,
    "outputTokens": 200,
    "requests": 1
  },
  "Extraction/bedrock/anthropic.claude-3-haiku": {
    "inputTokens": 2000,
    "outputTokens": 500,
    "requests": 2
  }
}
```

#### Which context the tokens arrive under

Classification is metered under a context named after the **stage**, not after
the step, and `classification.strategy` decides which stages run. The split is
not the same for Bedrock spend and for Lambda invocations, which is the part
worth reading before you write a report that filters on a prefix:

| Strategy | Bedrock token keys | Lambda keys |
|---|---|---|
| `single_pass` | `Classification/bedrock/<model>` | `Classification/lambda/requests`, `Classification/lambda/duration` |
| `two_stage` | `Segmentation/bedrock/<model>` and `SectionClassification/bedrock/<model>` — **no** `Classification/bedrock/*` key at all | `Classification/lambda/requests`, `Classification/lambda/duration` |

The Lambda keys stay under `Classification/` either way because one Lambda
function runs both stages, and its invocation is metered under the step's name.
So a report that filters on the `Classification/` prefix alone finds the Lambda
cost of a two-stage run and none of its Bedrock cost, and reads as a
classification step that invoked the model zero times. Sum all three prefixes,
as Capacity Planning does.

A two-stage run under a declared file shape
(`classification.file_shape.sectionSplitting` of `disabled` or `page`) emits no
`Segmentation/` keys at all, because the boundaries are asserted rather than
detected and the segmentation call never runs.

**Page Count Extraction Priority**:
1. OCR Bedrock requests count (`metrics.requests`)
2. BDA pattern pages (`BDAProject/bda/*.pages`)
3. Metering fields (`pages`, `pageCount`, `PageCount`)
4. Document-level fields (`doc.PageCount`, `doc.pageCount`, `doc.Pages`)
5. Sections EndPage field as fallback

**Step 3: CSV Import/Export**
- Import document configurations from CSV files
- Export current configurations for backup or sharing
- Validation for required OCR tokens when Bedrock OCR is configured

### 3. Processing Schedule Configuration

**Hourly Processing Schedule**:
- Configure processing volumes by hour using 24-hour time slots
- Select document types **only from types configured in Document Processing section**
- Specify documents per hour for each time slot and document type
- Visual time slot selection with hour range display (e.g., "09:00 - 10:00")

**Maximum Latency Configuration**:
- Enter maximum allowed latency in **seconds** (1-3600 seconds)
- Quick reference: 60s = 1 min | 120s = 2 min | 300s = 5 min | 600s = 10 min
- Used for SLA compliance checking and performance validation
- Displayed with automatic conversion to minutes for reference
- `0` is outside the range and is refused as such ("maxAllowedLatency must be positive"), since no plan can meet a zero-second budget. Leaving the field empty is a different case and reports the field as missing.

### 4. Capacity Calculation and Results

**Running Capacity Analysis**:
- Click "Calculate Capacity Requirements" to perform analysis
- System validates configuration and processes requirements
- **Real processing times** are extracted from actual processed documents
- **Real queue delays** are calculated from QueuedTime/WorkflowStartTime timestamps

**Capacity Metrics Display**:
- **Total Docs**: Aggregate documents per hour across all time slots
- **Total Pages**: Calculated from document volumes and average pages
- **Total Tokens**: Aggregated token usage across all processing steps (displayed in millions)

**Latency Distribution Analysis**:
- Processing time percentiles (P50, P75, P90, P95, P99) from **real document data**
- Base processing time: Median processing time from actual documents
- Queue delay: Actual queue delays from QueuedTime/WorkflowStartTime timestamps
- **Quota Status**: Shows "✅ Within Quota" or "⚠️ Quota Exceeded" based on all model quotas
- **SLA Target**: Configured maximum latency in seconds

**Which statistic the SLA is judged on**:

The SLA breach flag behind the performance warning compares the **P99** total
latency against the SLA target, not the median. An SLA is a promise about the documents
that go slowly, and the median is insensitive to those by construction: a plan
whose typical document finishes comfortably inside the limit while one document in
a hundred takes many times as long is not meeting the SLA. The warning quotes both
figures in the function's log, because the gap between them is what says whether to
chase a slow tail — a few unusually large packets, or a step that occasionally
retries — or a uniformly slow pipeline.

If your deployment reported "within SLA" before release 0.6.10 and reports a breach
afterwards with no change to your documents or configuration, this is why: the
comparison was previously made against the median. The reported percentiles and the
SLA target itself are unchanged, so the P99 row of the latency chart already showed
the value the flag now reads.

**Where the derived throughput comes from**:

The planner derives a throughput figure — the smaller of a token-limited and a
request-limited rate — which it does not display as a field of its own. It reaches
you through the load and bottleneck **recommendations**, and it is printed in full
in the function's CloudWatch log, so that log is where to look when a
recommendation about system load is not the one you expected.

The **token**-limited half is taken from the **most constrained model your plan's
steps actually call**, resolved from the models selected in View/Edit Configuration,
not from an account-wide figure and not from every model the stack supports. Both
halves of that matter. A document is throttled at the narrowest token limit any of
its steps touches, so a model with generous headroom cannot raise the token-limited
rate; and a model your pipeline never calls cannot lower it, however little TPM
quota your account has for that model. The log names the model that is binding,
which is the one to name in a quota increase request. Models with no quota at all,
and models absent from the stack's quota-code mapping, are skipped rather than
allowed to bind the figure — the per-step quota table below is where a single
model's shortfall is reported, against that model's own limit. If none of your
plan's models can be priced, the estimate falls back to the narrowest TPM limit in
the account's mapping and says so in the log.

⚠️ **The request-limited half is not narrowed this way.** It is the smallest RPM
quota across *every* model in the stack's quota-code mapping, whether or not your
plan calls it, so a model you never invoke with a low RPM limit still caps the
derived throughput — and because the figure is the smaller of the two halves, it can
cap it regardless of how much token headroom your plan's own models have. Read the
`Request-limited capacity` line in the function's log alongside the token-limited
one to see which of the two is deciding.

**Latency Bar Colors**:
- **Green/Blue**: When all model quotas are within limits (regardless of SLA)
- **Red**: Only when any model quota shows "Increase Needed"

### 5. AWS Service Quota Management

**Quota Requirements Analysis**:
- **Bedrock Models TPM**: Tokens Per Minute requirements by model and processing step
- **Bedrock Models RPM**: Requests Per Minute requirements by model and processing step
- Current vs. required quota comparison with utilization percentages
- Status indicators: ✅ Sufficient, ⚠️ Increase Needed

**RPM Calculation Method**:
```
1. Sample up to 100 documents with metering data
2. Calculate average requests per document for each processing step, adding up every
   Bedrock metering key that document recorded for the step
3. Multiply by the busiest hour's scheduled documents
4. Apply 10% safety buffer for burst traffic
5. Convert to per-minute rate

Formula: peak_rpm = (avg_requests_per_doc × peak_scheduled_docs_per_hour / 60) × 1.1

Where:
- avg_requests_per_doc: Calculated from metering samples. A step can record several
  keys for one document — the key is "{context}/bedrock/{model_id}", so a per-class
  model override or an escalation adds a key rather than adding to one — and all of
  them count towards the step's total
- peak_scheduled_docs_per_hour: The largest docsPerHour across the hourly time slots,
  not their sum. RPM is a per-minute limit, so it is scaled from one hour, as TPM is
- 1.1 = 10% safety buffer (not SLA factor)
```

**Direct AWS Console Integration**:
- "Request Increase" buttons that open AWS Service Quotas console
- Region-specific console URLs using configured AWS region
- Direct links to specific model quotas when `VITE_BEDROCK_MODEL_QUOTA_CODES` is configured
- Fallback to generic Bedrock quotas page when configuration is missing

**OCR Quota Handling**:
- OCR quota requirements are **automatically skipped** when OCR tokens are 0 (OCR not in use)
- This prevents errors when using Textract OCR instead of Bedrock OCR

### 6. Safety Buffer and Quota Calculation Details

**10% Safety Buffer**:

All TPM and RPM quota calculations include a **10% safety buffer** (multiplier of 1.1) to ensure adequate capacity. This buffer accounts for:
- **Burst traffic**: Documents arriving faster than scheduled averages
- **Token count variations**: Actual token usage may vary per document
- **Request count variations**: Some documents may require more API requests
- **System overhead**: Retries, error handling, and processing variations

**TPM (Tokens Per Minute) Calculation**:
```
Formula: peak_tpm = max(tokens_per_hour_for_each_hour / 60) × 1.1

Where:
- tokens_per_hour = Σ(scheduled_docs × tokens_per_doc)
- max() = peak across all 24 hours (not average)
- 1.1 = 10% safety buffer
- Each processing step calculated separately
```

**Example**:
```
Hour 9:  100 invoices × 5,000 extraction tokens = 500,000 tokens/hour
         500,000 / 60 = 8,333 tokens/minute
         8,333 × 1.1 = 9,166 TPM required for extraction
```

**Why Peak Hour (not Average)**:
- AWS Bedrock quotas are enforced per minute
- System must handle peak demand to avoid throttling
- Average load calculations would underestimate requirements

**Understanding Overload States**:

When scheduled demand exceeds quota capacity, the system reports:
- **Historical queue delays**: Actual delays from processed documents
- **Overload warning**: Indicates demand > capacity
- **Action required**: Increase quotas or reduce scheduled volume

**Important**: The system does NOT predict future queue delays during overload because they depend on:
- How long the overload persists
- Whether quota increases are approved
- Changes to the processing schedule
- System backlog at overload start

If overload persists, queue will grow indefinitely until quotas are increased or demand is reduced.

## Advanced Features

### 1. Token Usage Analysis and Visualization

**Hourly Token Distribution Chart**:
- Interactive stacked bar chart showing token usage by hour
- Color-coded by processing step:
  - **Purple**: OCR (only when Bedrock OCR is configured)
  - **Orange**: Classification
  - **Green**: Extraction
  - **Blue**: Assessment
  - **Red**: Summarization
- Dynamic scaling based on peak token usage
- Hover tooltips with detailed token counts per step

**Peak Hour Analysis**:
- Automatic identification of peak processing hours
- Peak vs. average load comparison with percentage differences
- Peak inference type identification (which step uses most tokens)
- Load distribution insights across active processing hours

### 2. Real Data Requirements

**No Estimation Mode**:
The capacity planning system requires **real processed documents** with metering data. It does not estimate or use default values for:
- Processing times (requires `/lambda/duration` gb_seconds or WorkflowStartTime/CompletionTime timestamps)
- Queue delays (requires QueuedTime/WorkflowStartTime timestamps)
- Request counts (requires metering data with requests field)
- Page counts (requires metering or document-level page data)

The two processing-time sources are genuine alternatives: either one on its own
produces a report, and the timestamp pair is preferred where both are present
because it measures the document end to end. A history carrying only the
timestamps therefore plans normally. The per-step figures the planner derives
from `gb_seconds` stay at zero in that case rather than being back-filled from
the document total, so no estimate is substituted for a measurement that was
never taken; those per-step figures feed the calculation and are not themselves
part of the report.

**Error Messages When Data is Missing**:
- "No processed documents found with metering data"
- "No processing time data found in documents"
- "No request count data found for [step_name]"

### 3. Data Import/Export and Integration

**CSV Import/Export Functionality**:
- Document configuration CSV import with validation
- Processing schedule CSV import/export
- Capacity plan export with comprehensive metrics
- Quota requirements export for documentation and planning

**Document Context Integration**:
- Integration with Documents context for processed document access
- Document filtering: Only COMPLETED documents with Metering data
- **Single-class validation**: Multi-class documents are rejected
- **Configuration validation**: Document type must exist in View/Edit Configuration

## Troubleshooting and Best Practices

### Common Issues and Solutions

**Configuration Not Loaded**:
- **Symptom**: Warning message "Configuration not loaded"
- **Solution**: Visit "View/Edit Configuration" tab first to load pattern configuration

**No Documents Available for Token Population**:
- **Symptom**: Alert "No documents available" saying the documents are still loading, or that none were found in the Document List period
- **Solution**: Wait for the load and try again, or choose a longer period with the Document List's **Load** menu

**Document Type Not in Configuration**:
- **Symptom**: "Document type 'X' is not defined in configuration"
- **Solution**: Add the document type to View/Edit Configuration, or use a document with a configured type

**Multi-Class Document Rejected**:
- **Symptom**: "Document has multiple classes. Capacity planning only supports single-class documents"
- **Solution**: Use single-class documents for capacity planning

**No Request Count Data Found**:
- **Symptom**: "No request count data found for [step_name]"
- **Solution**: Process documents through the full workflow to generate metering data with request counts

**Assessment RPM Row Missing On A Stack With Pre-Retirement Assessment History**:
- **Symptom**: The report is produced and carries an Assessment TPM row, but no Assessment RPM row
- **Cause**: Granular assessment used to record its Bedrock calls under `GranularAssessment/...` keys, and those keys are excluded from the request count. Granular assessment has since been retired and its service removed, so nothing writes such a key any more — but documents processed before the retirement still carry them. An Assessment history recorded entirely under those keys leaves the step with token demand and nothing countable, so its request rate is dropped rather than sized from a rate the current configuration has never produced. The Lambda log names the step and the reason.
- **Why the TPM row is still there**: the token requirement is your own scheduled token demand measured against the account's Service Quotas value, and reads no metering at all — so nothing about it is unknown in this state, and processing more documents cannot change it. Only the request rate has no measurement behind it, and only the request rate is withheld.
- ⚠️ **Read the per-step table, not the per-model summary, in this state**: the per-step table shows the step with `N/A` for its RPM status, which is accurate. The aggregate-by-model table lists the step under "Used for" but its `Required RPM` total sums only the steps that still have a request row, and it shows a green sufficiency badge with nothing marking the gap — so that total understates the model's request demand by however much the withheld step contributes. The Lambda log names the step and the reason.
- **Solution**: Process a document under the current configuration, which records an `Assessment/...` key. That is the only route: granular assessment is retired and the `granular` configuration keys are dropped on load, so the old history cannot be brought back into scope by any setting. Every other step is reported either way.

**No Processing Time Data**:
- **Symptom**: "No processing time data found in documents"
- **Solution**: Ensure documents have `/lambda/duration` gb_seconds or WorkflowStartTime/CompletionTime timestamps. Either is sufficient, so this message means **neither** was found in the sampled documents.

**OCR Quota Error When Not Using Bedrock OCR**:
- **Symptom**: Error about missing OCR metering data
- **Solution**: This is fixed - OCR requirements are now skipped when OCR tokens are 0

### Best Practices

**Capacity Planning Workflow**:
1. **Process Sample Documents**: Run real documents through the full workflow first
2. **Load Configuration**: Visit View/Edit Configuration to load pattern settings
3. **Populate from Documents**: Use "Populate tokens from Documents" for accurate data
4. **Configure Schedule**: Define realistic processing schedules
5. **Calculate**: Run capacity calculations to identify quota requirements
6. **Request Quota Increases**: Use direct AWS console links to request needed quotas

**Token Usage Management**:
- Always use actual processed documents for token population
- Validate that document types match View/Edit Configuration classes
- Use single-class documents for accurate per-type analysis
- Export configurations as CSV for backup

**Quota Management**:
- Request quota increases proactively based on capacity analysis
- Check both TPM and RPM quotas - both can be limiting factors
- Monitor utilization percentages to avoid service limits
- The Lambda requires `BEDROCK_MODEL_QUOTA_CODES` and `BEDROCK_MODEL_RPM_QUOTA_CODES` to be configured

**Performance Optimization**:
- Analyze peak hour token distribution to optimize schedules
- Monitor latency distribution percentiles against SLA requirements
- Use the "Quota Status" indicator to quickly assess if quotas are sufficient
- Green/blue bars indicate healthy quota status; red indicates quota issues

## Integration with Other Features

### Evaluation Framework Integration

The capacity planning system integrates with the evaluation framework to provide:
- **Accuracy vs. Performance Trade-offs**: Balance processing speed with extraction accuracy
- **Baseline Performance Metrics**: Use evaluation results to establish capacity baselines
- **Quality-Adjusted Capacity Planning**: Factor accuracy requirements into capacity calculations

### Cost Calculator Integration

Capacity planning works with the cost calculator to provide:
- **Volume-Based Cost Projections**: Calculate costs based on planned processing volumes
- **Optimization Cost Analysis**: Assess cost impact of performance optimizations
- **ROI Analysis**: Evaluate return on investment for capacity increases

### Monitoring System Integration

The capacity planning system leverages monitoring capabilities for:
- **Real-time Capacity Tracking**: Monitor actual vs. planned capacity utilization
- **Performance Trend Analysis**: Use historical data for future capacity planning
- **Automated Alerting**: Trigger alerts when capacity thresholds are exceeded

## Testing and Quality Assurance

The capacity planning feature includes comprehensive unit tests to ensure reliability and correctness.

### Running Tests

```bash
# From project root
make setup              # Install dependencies
make test              # Run all tests (includes capacity planning)
make test-capacity     # Run only capacity planning tests
make test-capacity-coverage  # Run with coverage report
```

### Test Coverage

- **80%+ code coverage** on critical paths
- **45+ unit tests** covering:
  - Environment variable validation
  - Input sanitization and validation
  - Quota calculation logic
  - Latency distribution calculations
  - Decimal conversion utilities
  - Recommendation generation

### Test Documentation

For how to run the suites, what each one covers and the coverage goals, see the
[Capacity Planning Tests README](../src/lambda/calculate_capacity/README_TESTS.md).

---

## Version History

### Latest Updates

**Security & Quality Improvements** (2026-02-18):
- ✅ Added comprehensive unit tests (80%+ coverage)
- ✅ Added environment variable validation with startup checks
- ✅ Added input sanitization with size limits (1MB)
- ✅ Added DynamoDB pagination for large datasets
- ✅ Replaced browser alerts with Cloudscape Flashbar notifications
- ✅ Integrated tests into main test suite (`make test`)

**Feature Updates**:
- **Max Latency Unit Change**: Changed from minutes to **seconds** for more precise SLA configuration
- **Quota-Based Bar Colors**: Latency bars now show green/blue when quota is sufficient, red only when quota exceeded
- **Document Type Filtering**: Dropdown now shows only classes from View/Edit Configuration
- **RPM Calculation Fix**: Fixed calculation to use average requests per document from metering samples
- **OCR Quota Skip**: Lambda now skips OCR quota requirements when OCR tokens are 0 (OCR not in use)
- **avgPages Extraction**: Improved extraction from multiple sources (metering, document fields, sections)
- **Removed "Exceeds SLA" Badge**: Replaced with quota-based color coding for clarity

---

This comprehensive capacity planning system ensures your GenAI IDP deployment can handle current and future document processing requirements while optimizing for performance, cost, and reliability.
