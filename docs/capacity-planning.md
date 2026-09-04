---
title: "Capacity Planning"
---

# Capacity Planning

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Overview

The GenAI IDP accelerator includes comprehensive capacity planning capabilities to help you optimize document processing performance, predict resource requirements, and ensure your system can handle expected workloads. This system provides real-time capacity analysis, AWS service quota recommendations, and performance optimization guidance.


https://github.com/user-attachments/assets/6e668c2f-c6dc-490e-a4ce-10c533cd330e


**This feature is designed for Pattern 2 and the Unified pattern** due to their well-defined processing steps (OCR, Classification, Extraction, Assessment, Summarization) and predictable Bedrock model quota requirements. The Unified pattern uses the same Bedrock pipeline as Pattern 2 for capacity planning purposes.

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
- SLA compliance checking against configured maximum latency (in seconds)
- Performance warning alerts for quota exceedances

**RPM (Requests Per Minute) Calculation**:
- Calculates **average requests per document** from metering data samples
- Multiplies by scheduled documents per hour to get total requests per hour
- Applies SLA factor for peak demand calculation
- Formula: `avg_requests_per_doc × scheduled_docs_per_hour / 60 × sla_factor`

### 3. Document Token Usage Population

**Automatic Token Population from Processed Documents**:
- Integration with Documents context to access processed document data
- Extraction of token usage from metering data structure
- Support for multiple document selection and batch population
- Document picker modal with filtering and selection capabilities
- **Single-class document validation** - multi-class documents are not supported

**Metering Data Processing**:
- Context-prefixed key parsing (OCR/, Classification/, Extraction/, Assessment/, Summarization/, BDAProject/bda/)
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
- `ExecutionTimeThresholdMs`: Processing timeout (default: 300000ms)
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

**⚠️ Pattern 2 or Unified Only**: This feature is available for Pattern 2 and Unified pattern deployments. The navigation link will not appear for Pattern 1 or Pattern 3.

Navigate to the Web UI and select the "Capacity Planning" section:

1. **Prerequisites**:
   - **Must be using Pattern 2 or Unified pattern** (Textract + Bedrock pipeline)
   - Process some documents first to populate metering data
   - Documents must be single-class and classified with configured document types
2. **Configuration**: Visit "View/Edit Configuration" tab to load your pattern configuration
3. **Navigation**: Click on "Capacity Planning" in the main navigation (visible for Pattern 2 and Unified)
4. **Pattern Detection**: System automatically detects your deployment pattern. The Unified pattern is treated as Pattern 2 for capacity planning purposes

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
2. Calculate average requests per document for each processing step
3. Multiply by scheduled documents per hour
4. Apply 10% safety buffer for burst traffic
5. Convert to per-minute rate

Formula: peak_rpm = (avg_requests_per_doc × scheduled_docs_per_hour / 60) × 1.1

Where:
- avg_requests_per_doc: Calculated from metering samples
- scheduled_docs_per_hour: Sum across all hourly time slots
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
- **Symptom**: Alert "No documents available. Please visit the Documents tab first"
- **Solution**: Visit Documents tab to load document data, then return to Capacity Planning

**Document Type Not in Configuration**:
- **Symptom**: "Document type 'X' is not defined in configuration"
- **Solution**: Add the document type to View/Edit Configuration, or use a document with a configured type

**Multi-Class Document Rejected**:
- **Symptom**: "Document has multiple classes. Capacity planning only supports single-class documents"
- **Solution**: Use single-class documents for capacity planning

**No Request Count Data Found**:
- **Symptom**: "No request count data found for [step_name]"
- **Solution**: Process documents through the full workflow to generate metering data with request counts

**No Processing Time Data**:
- **Symptom**: "No processing time data found in documents"
- **Solution**: Ensure documents have `/lambda/duration` gb_seconds or WorkflowStartTime/CompletionTime timestamps

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

For detailed testing information, see:
- [Capacity Planning Tests README](../src/lambda/calculate_capacity/README_TESTS.md)
- [Developer Guide](capacity-planning-developer-guide.md)
- [Security Mitigations](capacity-planning-mitigations.md)

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
