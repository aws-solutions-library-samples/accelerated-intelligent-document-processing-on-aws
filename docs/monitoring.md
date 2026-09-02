---
title: "Monitoring and Logging"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Monitoring and Logging

The GenAIIDP solution provides comprehensive monitoring through Amazon CloudWatch to give you visibility into the document processing pipeline.

## CloudWatch Dashboard

The solution automatically creates an integrated dashboard that displays:

### Latency Metrics

- **End-to-End Processing Time**: Total time from document upload to completion
- **Step Function Execution Duration**: Time spent in workflow orchestration
- **Lambda Function Latency**: Processing time per function (OCR, Classification, Extraction)
- **Queue Wait Time**: Time documents spend in processing queues
- **Model Inference Time**: Bedrock model response latencies

![Latency Metrics Dashboard](../images/Dashboard1.png)

### Throughput Metrics

- **Documents Processed per Hour**: Overall system throughput
- **Pages Processed per Minute**: OCR processing rate
- **Classification Requests per Second**: Page classification throughput
- **Extraction Completions per Hour**: Field extraction processing rate
- **Queue Message Rate**: SQS message processing velocity

![Throughput Metrics Dashboard](../images/Dashboard2.png)

### Error Tracking

- **Workflow Failures**: Step Function execution failures with error categorization
- **Lambda Timeouts**: Function timeout events and duration analysis
- **Model Throttling**: Bedrock throttling events and retry patterns
- **Dead Letter Queue Messages**: Failed messages requiring manual intervention
- **Validation Errors**: Data validation failures and format issues

![Error Tracking Dashboard](../images/Dashboard3.png)

### Workflow Concurrency Counter

The stack limits in-flight workflows with a DynamoDB counter: the queue processor
increments it before `StartExecution`, and the workflow tracker decrements it on
the execution's terminal event. If a decrement is ever lost, the counter drifts
**upward** and nothing puts it back — so once it reaches
`MaxConcurrentWorkflows`, documents stop starting **permanently**.

That failure is quiet. Every other signal looks *idle* rather than broken: no
errors, no failed executions, latency graphs simply stop. The usual first symptom
is a person noticing that nothing has processed for hours.

Two metrics in the stack's own namespace (`<StackName>`) make it visible, both on
the **Workflow Concurrency Counter** widget:

- **`ConcurrencyCounterActive`** — the counter value, published on every document
  completion. Continuous, so there is a history to inspect after the fact.
- **`ConcurrencyCounterDrift`** — claimed slots minus executions actually
  running. Sampled only when an increment is *refused*, i.e. when drift is
  actually blocking work.

Two alarms publish to `AlertsTopic`:

- **`ConcurrencyCounterDriftAlarm`** — sustained drift (> 0 for 15 minutes). This
  fires on the *symptom*, once slots are already being held wrongly.
- **`WorkflowTrackerDLQAlarm`** — any message in the Workflow Tracker
  dead-letter queue. This fires on the *cause*: the tracker owns the decrement,
  so an event it could not process is a slot that was never released, and it
  alarms on the first message rather than waiting for drift to accumulate.

The queue processor also **self-heals**: on a refused increment it
reconciles the counter against `ListExecutions`, requiring the same discrepancy
in two samples at least five minutes apart, only ever lowering it, and writing
conditionally on the value it sampled.

**Reading the widget:** the counter tracking a busy queue is normal. The counter
sitting at or near `MaxConcurrentWorkflows` while the SQS widget shows messages
in flight and the Step Functions widget shows nothing starting is the leak.

### Stale Output Purge on Re-upload

OCR has a retry-safe recovery path: on a Step Functions retry the document is
reloaded with `pages={}`, so before re-OCRing it scans
`s3://<OutputBucket>/<key>/pages/` and reuses any page that already has all four
of its files (`rawText.json`, `result.json`, `textConfidence.json`, `image.*`).
That is what makes a throttled OCR retry cheap — and it is also why uploading a
**different** document under an **existing** filename used to produce the
previous document's extraction ([#719](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/719)).
Two paths now purge before processing: the queue sender removes `<key>/pages/`
on every upload event, and the **Reprocess** action removes everything under
`<key>/` except `<key>/runs/` (matching its "start over" intent).

This failure is quiet in the same way the concurrency leak is: if the purge only
partly succeeds, the document processes, reports success, and silently carries
text from the old document — recovery needs just **one** surviving complete page
to skip OCR for it. Processing deliberately continues on a purge failure (a
possibly-stale extraction beats a dropped upload or a refused reprocess), so the
signal has to come from a metric rather than the document's own status.

One metric in the stack's own namespace (`<StackName>`):

- **`StaleOutputPurgeFailed`** — published (value `1`) whenever a purge raises.
  Both the ingest path and the reprocess path publish it, with no dimensions and
  into the **root** stack's namespace, so one metric and one alarm cover both.
  Published only on failure, so no data means every purge succeeded.

One alarm publishes to `AlertsTopic`:

- **`StaleOutputPurgeFailedAlarm`** — any occurrence within 5 minutes. Unlike
  concurrency drift there is no self-healing path: the stale pages sit in S3
  until someone removes them, and every later upload of that key inherits the
  same wrong results — so this alarms on the **first** failure rather than on a
  sustained trend.

Two dashboard widgets are paired on the main dashboard: **Stale Output Purge
Failures** (the count across both paths) and **Stale Output Purge Failures —
affected keys** (a Logs Insights table over the Queue Sender log group).

**Recovering:** identify the affected keys, then delete
`s3://<OutputBucket>/<key>/pages/` and re-upload or reprocess the document.
The log widget covers the ingest path; for the reprocess path, query the
`ReprocessDocumentResolverFunction` log group (in the API-resolvers nested
stack) instead. The two paths log different messages:

| Path | Log group | Message |
|---|---|---|
| Upload / re-upload | `QueueSender` | `Failed to purge previous output data for <key>` |
| Reprocess action | `ReprocessDocumentResolverFunction` | `Failed to delete previous output data for <key>` |

The most common cause is a KMS or bucket-policy change that denies
`s3:DeleteObject` to the purging role — check that before assuming a transient
S3 error.

**Note:** because the purge runs on every upload, a re-upload of a
byte-identical file no longer reuses the prior OCR cache; it re-OCRs from
scratch.

## Log Groups

The solution creates centralized logging across all components:

- `/aws/stepfunctions/IDPWorkflow`: Step Function execution logs
- `/aws/lambda/QueueProcessor`: Document queue processing logs
- `/aws/lambda/OCRFunction`: OCR processing logs and errors
- `/aws/lambda/ClassificationFunction`: Classification processing logs
- `/aws/lambda/ExtractionFunction`: Extraction processing logs
- `/aws/lambda/TrackingFunction`: Document tracking and status logs
- `/aws/appsync/GraphQLAPI`: Web UI API access logs

All logs include correlation IDs for tracing individual document processing journeys.

## Pattern-Specific Monitoring

Each pattern includes additional monitoring tailored to its specific workflow:

### Pattern 1: Bedrock Data Automation (BDA)
- BDA project execution metrics
- API usage and throttling
- Media processor performance

### Pattern 2: Textract + Bedrock
- Textract OCR performance
- Bedrock model usage
- Classification confidence distribution
- Extraction completeness metrics

### Pattern 3: Textract + UDOP + Bedrock
- SageMaker endpoint performance
- UDOP model latency and throughput
- GPU utilization metrics

## Setting Up Alerts

You can configure CloudWatch alarms for critical metrics:

1. **Error Rate Thresholds**: Alert when error rates exceed acceptable levels
2. **Processing Time Anomalies**: Detect unusual latency spikes
3. **Queue Depth Monitoring**: Alert on potential backlogs
4. **Concurrency Limits**: Notify when approaching service limits
5. **Cost Controls**: Alert on unusual model usage patterns

Example alarm configuration:

```yaml
ErrorRateAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmDescription: Alert when error rate exceeds 5%
    MetricName: DocumentProcessingErrors
    Namespace: AWS/Lambda
    Statistic: Sum
    Period: 300
    EvaluationPeriods: 1
    Threshold: 5
    ComparisonOperator: GreaterThanThreshold
    TreatMissingData: notBreaching
    AlarmActions:
      - !Ref AlertSNSTopic
```

## Log Insights Queries

The solution includes predefined CloudWatch Log Insights queries for common analysis tasks:

### Error Analysis

```
filter @message like /ERROR/ or @message like /Exception/
| parse @message "Error: *" as errorMessage
| stats count(*) as errorCount by errorMessage
| sort by errorCount desc
| limit 10
```

### Processing Time Analysis

```
filter @message like /Processing complete/
| parse @message "Processing complete in * ms" as processingTime
| stats avg(processingTime) as avgTime, min(processingTime) as minTime, max(processingTime) as maxTime by bin(30m)
| sort by avgTime desc
```

### Document Volume Tracking

```
filter @message like /Document received/
| stats count(*) as documentCount by bin(1h)
| sort by bin(1h) asc
```

## Metric Dimensions

Key metrics are available with these dimensions:

- **DocumentType**: Break down metrics by document class
- **ProcessingPattern**: Compare metrics across different patterns
- **PageCount**: Analyze performance based on document complexity
- **Region**: Track regional performance differences

## Performance Benchmarks

The dashboard includes performance benchmark comparisons:

- **Current vs. Historical Performance**: Compare current metrics against previous periods
- **Pattern Comparison**: Side-by-side comparison of different processing patterns
- **Model Performance**: Comparison of different Bedrock models for similar tasks

## Operational Monitoring

The solution provides operational metrics for infrastructure health:

- **Lambda Concurrency**: Track function concurrency usage
- **Throttling Events**: Monitor service limits and throttling
- **DynamoDB Capacity**: Track consumed read/write capacity units
- **S3 Request Rates**: Monitor bucket operation rates and latency
- **Step Functions Execution Metrics**: Track state transitions and execution counts

## Cost Monitoring

Monitor resource usage and costs:

- **Bedrock Model Tokens**: Track token usage by model and operation
- **Lambda Execution Time**: Monitor function duration and memory usage
- **S3 Storage**: Track storage growth over time
- **Data Transfer**: Monitor network costs between services

## Custom Dashboard Creation

You can create custom dashboards focused on specific aspects:

1. Open the CloudWatch console
2. Go to Dashboards and select "Create dashboard"
3. Add widgets using metrics from the "GenAIIDP" namespace
4. Organize widgets logically by processing stage or metric type

## Exporting Metrics

To export metrics for external analysis:

1. Use CloudWatch Metric Streams to send metrics to:
   - Amazon Kinesis Data Firehose
   - Third-party monitoring tools
   - Custom analytics solutions

2. Configure the stream with:
   - Metrics namespace filters
   - Output format (JSON or OpenTelemetry)
   - Destination configuration
