---
title: "Pattern 1: Bedrock Data Automation (BDA) Workflow"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Pattern 1: Bedrock Data Automation (BDA) Workflow

> **⚠️ DEPRECATED**: Pattern 1 has been superseded by the **Unified Pattern**, which combines both BDA and pipeline processing modes into a single deployment. The `use_bda` configuration flag (set via the UI) controls whether documents are processed via BDA or the step-by-step pipeline. See [architecture.md](./architecture.md) for details on the unified architecture.
>
> This document is retained as a reference for BDA-specific concepts and behavior that still apply when `use_bda: true` is set in the unified pattern.

This pattern implements an intelligent document processing workflow using Amazon Bedrock Data Automation (BDA) for orchestrating ML-powered document processing tasks. It leverages BDA's ability to extract insights from documents using pre-configured templates and workflows.

For the current architecture diagram, see the unified architecture in [architecture.md](./architecture.md).

## Table of Contents

- [Architecture Overview](#architecture-overview)
  - [Flow Overview](#flow-overview)
  - [Components](#components)
  - [State Machine Workflow](#state-machine-workflow)
- [Deployment](#deployment)
  - [Prerequisites](#prerequisites)
  - [Configuration](#configuration)
- [Monitoring and Metrics](#monitoring-and-metrics)
  - [CloudWatch Metrics](#cloudwatch-metrics)
  - [Dashboard Components](#dashboard-components)
- [Concurrency and Throttling](#concurrency-and-throttling)
  - [BDA API Throttling](#bda-api-throttling)
  - [Error Handling](#error-handling)
- [Workflow Details](#workflow-details)
  - [Invocation Step](#invocation-step)
  - [Processing Step](#processing-step)
  - [Results Processing](#results-processing)
  - [Human-in-the-Loop (HITL)](#human-in-the-loop-hitl)

## Architecture Overview

### Flow Overview
1. Document events from S3 trigger workflow execution
2. BDA Invoke Lambda starts BDA job asynchronously with a task token
3. BDA Completion Lambda processes job completion events from EventBridge
4. Completion Lambda sends task success/failure to Step Functions using the stored token
5. Process Results Lambda copies output files to designated location



https://github.com/user-attachments/assets/24547356-6d68-4935-b0fd-ddeed9c25ba8



### Components
- **Main Functions**:
  - BDA Invoke Function (bda_invoke_function): Initiates BDA jobs and stores task tokens
  - BDA Completion Function (bda_completion_function): Handles job completion events
  - Process Results Function (processresults_function): Copies and organizes output files
- **State Machine**: Coordinates workflow execution using waitForTaskToken pattern
- **EventBridge**: Routes BDA job completion events to the Completion Function
- **DynamoDB**: Tracks task tokens for asynchronous callback
- **S3 Buckets**: Input, Working, and Output storage

### State Machine Workflow
```
InvokeDataAutomation (with waitForTaskToken)
    |
    ├── Success -> ProcessResultsStep
    |
    └── Failure -> FailState
```

## Deployment

### Prerequisites
- A BDA project does not have to exist beforehand. The solution creates one for a
  configuration profile when the profile first needs one — for example the first
  time the profile is synced with BDA — and records it on the profile
- Required AWS permissions for Bedrock, Lambda, Step Functions, and S3

### Configuration

**Stack Deployment Parameters:**

BDA mode has no deployment parameters of its own. The stack is deployed with the
main template's parameters, and the starting configuration is chosen with
`ConfigurationPreset` (a directory under `config_library/unified/`) or
`CustomConfigPath` (the S3 URI of your own configuration file). The BDA project is
not a stack parameter: it is linked to each configuration profile (see
[discovery.md](./discovery.md#bdaidp-sync-feature)). Summarization is controlled by
the configuration's `summarization.enabled` property.

**Configuration Management:**
- The presets are the directories under `config_library/unified/`. None is specific to BDA mode: set `use_bda: true` on whichever you choose
- Configuration can be updated through the Web UI without stack redeployment
- Summarization functionality is controlled through the configuration file `summarization.enabled` property rather than CloudFormation parameters

**Note on BDA Configuration:**
In BDA mode, classification and extraction are performed by BDA, using the
blueprints of the BDA project linked to the configuration profile. Those blueprints
correspond to the profile's document classes, and **Sync to BDA…** and **Sync from
BDA…** on the Configuration page keep the two in step in either direction, so the classes
can be defined in the solution's configuration and pushed to BDA, or defined in BDA
and pulled into the configuration. See [discovery.md](./discovery.md#bdaidp-sync-feature).

## Monitoring and Metrics

### CloudWatch Metrics
The pattern publishes detailed metrics to CloudWatch:

- **BDA API Metrics**:
  - `BDARequestsTotal`: Total number of API requests
  - `BDARequestsSucceeded`: Successful API requests
  - `BDARequestsFailed`: Failed API requests
  - `BDARequestsThrottles`: API throttling events
  - `BDARequestsRetrySuccess`: Successful retries after throttling
  - `BDARequestsMaxRetriesExceeded`: Cases where max retries were exhausted
  - `BDARequestsLatency`: API request duration in milliseconds
  - `BDARequestsTotalLatency`: Total duration including retries

- **BDA Job Metrics**:
  - `BDAJobsTotal`: Total number of BDA jobs
  - `BDAJobsSucceeded`: Successfully completed jobs
  - `BDAJobsFailed`: Failed job executions

### Dashboard Components
The included CloudWatch dashboard provides visibility into the workflow:

- **API Request Panels**:
  - API request success/failure rates per minute
  - API throttling and retry metrics
  - Job execution success/failure trends

- **Lambda Performance**:
  - Function duration for all Lambda functions
  - Long-running invocation tracking
  - Memory utilization metrics

- **Error Tracking**:
  - Log-based panels for API throttling events
  - Job execution failures with detailed error messages
  - Lambda function errors and timeouts

## Concurrency and Throttling

### BDA API Throttling
Implements exponential backoff with retry handling for transient errors:
```python
MAX_RETRIES = 7
INITIAL_BACKOFF = 2  # seconds
MAX_BACKOFF = 300   # 5 minutes

# Retryable error codes
retryable_errors = [
    'ThrottlingException',
    'ServiceQuotaExceededException',
    'RequestLimitExceeded',
    'TooManyRequestsException',
    'InternalServerException'
]
```

### Error Handling
- Retries on transient failures with exponential backoff
- Clear distinction between retryable and non-retryable errors
- Detailed metrics for tracking throttling events and retries
- Dead Letter Queue for EventBridge target to capture unprocessed events
- Comprehensive error logging with cause and stack traces

## Workflow Details

### Invocation Step
```python
# Example BDA invocation payload
payload = {
    "inputConfiguration": {
        "s3Uri": input_s3_uri
    },
    "outputConfiguration": {
        "s3Uri": output_s3_uri
    },
    "dataAutomationConfiguration": {
        "dataAutomationProjectArn": data_project_arn,
        "stage": "LIVE"
    },
    "dataAutomationProfileArn": f"arn:aws:bedrock:{region}:{account_id}:data-automation-profile/us.data-automation-v1",
    "notificationConfiguration": {
        "eventBridgeConfiguration": {
            "eventBridgeEnabled": True
        }
    }
}
```

### Processing Step
- Tracks execution task tokens in DynamoDB with expiration time
- Listens for EventBridge events from BDA job completion
- Retrieves task token from DynamoDB when job completes
- Sends success or failure to Step Functions workflow
- Publishes detailed metrics for monitoring

### Results Processing
- Copies BDA output files from working bucket to final output location
- Organizes results in the same directory structure as input
- Produces standardized output format for UI consumption
- Updates execution status with job result information

### Human-in-the-Loop (HITL)

Human review is built into the solution's Web UI; it does not use Amazon SageMaker
Augmented AI (A2I). Reviewers validate and correct extracted information when the
system's confidence falls below a specified threshold.

**Configuration:** HITL is enabled through the configuration
(Web UI **Configuration → Human-in-the-Loop (HITL) Review (Optional)**, or the
`hitl.enabled` key in the config YAML),
not a CloudFormation parameter — the `EnableHITL` parameter was removed in v0.4.11.

For comprehensive HITL documentation including workflow details, configuration steps, best practices, and troubleshooting, see the [Human-in-the-Loop Review Guide](./human-review.md). 

## Editing BDA Documents

A document processed in BDA mode (its configuration profile sets `use_bda`) is corrected
on its edit page, without re-invoking Bedrock Data Automation. The Web UI decides this
from the document's configuration profile, and the `processChanges` operation refuses any
section or page change to such a document for every role; see
[BDA-mode documents offer data-only editing](./web-ui.md#bda-mode-documents-offer-data-only-editing).

### Capabilities

- **Edit Extraction Data**: Choose **Edit Data** in a section row's **⋮** menu (or
  **Edit** in the document page's action bar) to open the document's edit page, whose
  **Visual Editor** tab edits the fields
- **Modify Predictions**: Update predicted field values and review confidence scores
- **Edit the evaluation baseline**: With **Show Evaluation** on, edit the baseline values the document is evaluated against. A test set's ground truth is edited in Test Studio
- **Process the corrections**: The edit page's **Process Changes** saves the edits and
  re-runs evaluation and summarization on them, without BDA re-invocation
- **Regroup pages**: **More Actions › Edit Page Grouping › Save page grouping** on the
  document page saves a new page grouping and keeps the extracted values; nothing is
  reprocessed

### Limitations

BDA splits the document into sections, classifies them and extracts their data, so
the document page's structural and re-extraction actions are disabled in BDA mode: **Edit
Page Class** (on the document page and the edit page), **Save and Re-extract**,
**Re-extract Sections**, **Edit Page Text** and **Reset Page Class** show the reason.
Correct values with **Edit Data** (the edit page) instead.

### How It Works

When the edit page's **Process Changes** runs on a document with existing pages and
sections data:

1. The workflow detects existing document data (pages > 0 and sections present)
2. BDA invocation step is automatically skipped
3. Process proceeds directly to evaluation and summarization
4. Document status updates to COMPLETED when finished

This is useful for:
- Correcting extraction errors on the edit page
- Adding baseline data for evaluation comparison
- Re-running evaluation after data corrections
- Updating document summaries after data modifications

## Best Practices
1. **BDA Project Configuration**:
   - Define the document classes in the configuration profile, or in the BDA project's blueprints, and keep the two in step with **Sync to BDA…** and **Sync from BDA…**
   - Use BDA's built-in capabilities for document type detection and field extraction
   - Test BDA configuration thoroughly before integrating with IDP stack

2. **Configuration Management**:
   - Use the configuration library for IDP-specific settings (summarization, evaluation, etc.)
   - Leverage the Web UI for IDP configuration updates without redeployment

3. **Monitoring and Scaling**:
   - Monitor BDA service quotas and adjust concurrency as needed
   - Implement exponential backoff with jitter for API throttling
   - Set up EventBridge rules to capture all job status events
   - Include DLQ for EventBridge targets to capture unprocessed events

4. **Error Handling**:
   - Ensure token storage has appropriate TTL to avoid stale tokens
   - Handle partial successes appropriately in the results processor
   - Maintain comprehensive logging for troubleshooting
   - Use CloudWatch dashboards to monitor performance metrics

5. **Security and Reliability**:
   - Enable detailed CloudWatch metrics for API requests and job executions
   - Configure alerts for unusual throttling or error patterns
   - Use appropriate IAM roles with least privilege principles
   - Implement proper error handling for BDA job failures

6. **HITL Management**:
   - Set the confidence threshold (`hitl.confidence_threshold`) in the configuration based on business requirements
   - Regularly check the review queues for pending items to avoid processing delays
   - Establish consistent correction guidelines if multiple reviewers are involved
