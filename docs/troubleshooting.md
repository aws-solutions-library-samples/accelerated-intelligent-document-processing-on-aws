---
title: "Troubleshooting Guide"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Troubleshooting Guide

This guide provides solutions for common issues and optimization techniques for the GenAIIDP solution.

## AI-Powered Error Analysis

For automated troubleshooting, use the **Error Analyzer** tool:

- **What it is**: AI-powered agent that automatically diagnoses document processing failures
- **When to use**: Document-specific failures, system-wide error patterns, performance issues
- **How to access**: Web UI → Failed document → Troubleshoot button
- **Documentation**: See [Error Analyzer](error-analyzer.md) for complete guide

**Quick Start**:

```
# Document-specific analysis
Query: "document: filename.pdf"

# System-wide analysis
Query: "Show recent processing errors"
```

The Error Analyzer automatically:

- Searches CloudWatch Logs across all Lambda functions
- Correlates errors with DynamoDB tracking data
- Identifies root causes with AI reasoning
- Provides actionable recommendations

For issues not covered by the Error Analyzer, use the manual troubleshooting steps below.

---

## Common Issues and Resolutions

### Document Processing Failures

| Issue                              | Resolution                                                                                                                           |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Workflow execution fails**       | Check CloudWatch logs for specific error messages. Look in the Step Functions execution history to identify which step failed.       |
| **PDF document not processing**    | Verify the PDF is not password protected or encrypted. Ensure it's not corrupted by opening it in another application.               |
| **OCR fails on document**          | Check if the document is scanned at sufficient quality. Verify the document doesn't exceed size limits (typically 5MB for Textract). |
| **Classification returns "other"** | Review document class definitions. Consider adding more detailed class descriptions or adding few-shot examples.                     |
| **Extraction missing fields**      | Review attribute descriptions and prompt engineering. Check if fields are present but in an unusual format or location.              |

### Confidence (Assessment) Failures

Confidence scoring runs as its own step after extraction. Two failure shapes have
distinct symptoms and distinct fixes.

**The confidence model truncates its response at every batch size, down to a single
row.** No batch size can fit that row, so shrinking cannot converge. The ladder now
stops as soon as a **single-row** call truncates and reports
`assessment_row_too_large` (error severity) on the section, naming the confidence
model, its output-token cap, the list field and class, and the offending row's
approximate serialized size. Look for that issue in the section's **Status** column
or **Processing Report** tab. Before this, the section reported the generic
`assessment_incomplete`, which points at `list_batch_size` — the one setting that
cannot help here.

The known trigger is a class marked `x-aws-idp-multi-instance: true` whose instance
carries a long inner list — a 100-row bank statement, for example. The wrapper makes
the *instance* array the outer list, so one row of that list is a whole document
instance, and the batch sizer (which measures only the outer row's columns, logging
`cols=2 per_row~80`) derives a batch size that is wrong by orders of magnitude.
⚠️ **That sizing bug is not fixed** — it is tracked as open issue
[#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894),
so such a class still cannot be fully assessed; what it now gets is an actionable
message naming the real cause.

⚠️ If your symptom is instead **the document stuck in `ASSESSING` with the Assessment
Lambda hitting its 900-second limit and `Sandbox.Timedout` retried three times**
— the symptom originally reported on #894 — note that this guard is not known to fix
it. The cause of that timeout has not been established: the same-model retry rung
already stopped on no progress before this change, and the ladder's wall-clock
deadline guard was already in place in the release where the timeouts were observed.
The one gap in that guard has since been closed (#958): every recovery call now
checks the remaining Lambda time before it is made, not just further bisections and
whole escalation rounds, so a run that would previously have spent its entire budget
on partially-successful retries now stops at the last call that fits and keeps
everything it recovered. When that happens with rows still unscored the section
reports `assessment_incomplete` (error) with the time budget named in its message —
**not** `assessment_deadline_reached`, which is reserved for the case where recovery
was cut short and every row ended up scored anyway; `deadline_reached` is also set in
`metadata.assessment_batch_split_stats`. Whether any of this was the cause of the
reported timeouts is still unknown. Attach your Assessment Lambda log (the per-call timings
and `stopReason` lines) to #894. Until #894 is fixed, either point that class at a large-output-cap confidence model
(`extraction.confidence.escalation_model`, or the per-class
`x-aws-idp-confidence-escalation-model`), or restructure so the long list is its own
class rather than a field inside a multi-instance instance.

**`ValidationException: Input is too long for requested model.` from the Assessment
step.** This is deterministic — retrying sends the identical oversized request — and
it used to fail the whole document, discarding extraction that had already completed
and been paid for. The step now **degrades** instead: the extracted data is
returned, the document succeeds, and the missing confidence is recorded as an
error-severity `assessment_failed_confidence_unavailable` issue on the section.
Because that section has no confidence values, HITL confidence routing and the UI
threshold signals do not apply to it — the processing issue is the signal. Find it in
the **Status** column of the Sections panel; this one is written to the section
record only, not into the section's `result.json`, so it does not appear in the
Processing Report tab. Transient
failures (throttling, read timeouts) are unaffected and still retry. To get
confidence back, reduce the confidence request's size: `geometry.mode: ocr_only`,
a lower `extraction.confidence.list_batch_size`, or Advanced (agentic) extraction,
which shards the confidence pass. Automatically re-batching an oversized confidence
input so the pass succeeds rather than degrades remains open as part of
[#901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901).

**Some rows scored, most not, and nothing complained.** Sections whose scored rows
fall materially short of the extracted rows now emit
`assessment_coverage_incomplete` — a warning past 5% of rows unscored, an error at
25% or more **and** at least 10 unscored rows (a proportion alone would make one
unscored row in a four-row list an error) — carrying the expected/scored/unscored
counts and a per-field breakdown. The extracted values themselves are unaffected;
treat it as "do not trust this section's confidence surface as a whole". It is
suppressed when the self-healing ladder already reported an error for the same
section (`assessment_incomplete`, `assessment_row_too_large`,
`assessment_schema_mismatch`), because that issue describes the same unscored rows
with a cause attached.

### Web UI Access Issues

| Issue                                | Resolution                                                                                                            |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| **Cannot login to Web UI**           | Verify Cognito user status and permissions in AWS Console. Check email for temporary credentials if first-time login. |
| **Web UI loads but shows errors**    | Check browser console for specific error messages. Verify API endpoints are accessible.                               |
| **Cannot see document history**      | Check the dispatcher Lambda's CloudWatch Logs for the failing operation. A 403 means a resolver group check rejected your role (see [rbac.md](./rbac.md)); a 401 means the Cognito token was rejected.  |
| **Configuration changes not saving** | Check browser console for validation errors. Verify that the configuration Lambda function has correct permissions.   |

### Model and Service Issues

| Issue                         | Resolution                                                                                                                                  |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **Bedrock model throttling**  | Check CloudWatch metrics for throttling events. Consider increasing MaxConcurrentWorkflows parameter or requesting service quota increases. |
| **SageMaker endpoint errors** | Verify endpoint status in SageMaker console. Check endpoint logs for specific error messages.                                               |
| **Slow document processing**  | Monitor CloudWatch metrics to identify bottlenecks. Consider optimizing model selection or increasing concurrency limits.                   |

### Infrastructure Issues

| Issue                          | Resolution                                                                                                            |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| **Lambda function timeouts**   | Increase function timeout or memory allocation. Consider breaking processing into smaller chunks.                     |
| **DynamoDB capacity exceeded** | Check CloudWatch metrics for throttling. Consider increasing provisioned capacity or switching to on-demand capacity. |
| **DynamoDB config upload fails: "Item size has exceeded the maximum allowed size"** | This error occurred in versions prior to the compression fix when configurations had ~45+ document classes, exceeding DynamoDB's 400KB item limit. **Solution**: Upgrade to the latest version, which gzip-compresses configuration data (supporting 3,000+ classes). Existing configs auto-migrate on next write. See [GitHub Issue #200](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/200). |
| **Test Studio "Run Test" fails: "An error occurred (ValidationException) when calling the PutItem operation: Item size has exceeded the maximum allowed size"** | In versions before the fix, the test runner copied the selected configuration profile inline onto the run's DynamoDB record, uncompressed. The Configuration page accepted profiles past ~390KB of JSON (it compresses them), so a large profile saved fine and then failed every test run at submit. **Solution**: Upgrade to a version where runs store the captured configuration compressed. On an affected version, reduce the profile below ~390KB of JSON (fewer classes, shorter prompts or attribute descriptions, or split classes across profiles); export the profile from the Configuration page to check its size. |
| **S3 permission errors**       | Verify bucket policies and IAM role permissions. Check for cross-account access issues.                               |
| **Stack update fails with `iam:UpdateAssumeRolePolicy` AccessDenied on `CognitoAuthorizedRole`, then wedges in `UPDATE_ROLLBACK_FAILED`** | Affects upgrades from before v0.6.2 to v0.6.2–v0.6.4 when deploying with a CloudFormation service role (or permissions boundary) that lacks `iam:UpdateAssumeRolePolicy`. The rollback needs the same permission, so the stack cannot self-recover. **Recover:** `aws cloudformation continue-update-rollback --stack-name <StackName> --resources-to-skip <StackName>-CognitoAuthorizedRole`, then upgrade to a release that includes the fix (the GovCloud principals are now gated on the partition, so commercial deployments no longer change this trust policy). If you use your own service role, also grant `iam:UpdateAssumeRolePolicy` — see [iam-roles/cloudformation-management/README.md](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/iam-roles/cloudformation-management/README.md) and [GitHub Issue #632](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/632). |

### Agent Processing Issues

| Issue                                     | Resolution                                                                                                                                                                       |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Agent query shows "processing failed"** | Check CloudWatch logs for the Agent Processing Lambda function (`{StackName}-AgentProcessorFunction-*`). Look for specific error messages, timeout issues, or permission errors. |
| **External MCP agent not appearing**      | Verify the External MCP Agents secret is properly configured with valid JSON array format. Check CloudWatch logs for agent registration errors.                                  |
| **Agent responses are incomplete**        | Check CloudWatch logs for token limits, model throttling, or timeout issues in the Agent Processing function.                                                                    |

## Performance Considerations

### Resource Sizing

Optimize performance through proper resource sizing:

- **Lambda Memory**: Scale based on document complexity
  - OCR Function: 1024-2048 MB recommended
  - Classification/Extraction: 512-1024 MB for text-only, 1024-2048 MB for image-based processing
- **Timeouts**: Configure appropriate timeouts
  - Step Functions: 5-15 minutes for standard documents
  - Lambda functions: 1-3 minutes for individual processing steps
  - SQS visibility timeout: 5-6x Lambda function timeout

- **Concurrency Settings**
  - Set `MaxConcurrentWorkflows` parameter based on expected volume
  - Consider Lambda reserved concurrency for critical functions
  - Monitor and adjust based on actual usage patterns

### Performance Optimization Tips

1. **Document Size and Quality**
   - Optimize input document size (600-1200 DPI recommended for scans)
   - Reduce file size when possible without losing quality
   - Consider preprocessing large documents to split them

2. **Model Selection**
   - Balance accuracy vs. speed based on use case requirements
   - Test different models with representative documents
   - Consider smaller models for simple documents, larger models for complex extraction

3. **Batch Processing**
   - For high volumes, stagger document uploads
   - Use the load simulation scripts to test capacity
   - Monitor queue depth and processing latency

## Queue Management

### Dead Letter Queue (DLQ) Processing

If messages end up in a Dead Letter Queue:

1. Review the messages in the DLQ using the AWS Console
2. Check CloudWatch Logs for corresponding errors
3. Fix the underlying issue (permission, configuration, etc.)
4. Use the AWS SDK or Console to move messages back to the main queue:

```python
import boto3

sqs = boto3.client('sqs')

# Get messages from DLQ
response = sqs.receive_message(
    QueueUrl='dlq-url',
    MaxNumberOfMessages=10,
    VisibilityTimeout=30
)

# Move to main queue
for message in response.get('Messages', []):
    sqs.send_message(
        QueueUrl='main-queue-url',
        MessageBody=message['Body']
    )

    # Delete from DLQ
    sqs.delete_message(
        QueueUrl='dlq-url',
        ReceiptHandle=message['ReceiptHandle']
    )
```

### Stopping Runaway Workflows

If too many workflows are running and need to be stopped:

1. Use the provided script to stop workflows:

```bash
./scripts/stop_workflows.sh <stack-name> <pattern-name>
```

2. Purge the SQS queue if needed:
   - Navigate to SQS in the AWS Console
   - Select the queue
   - Choose "Purge" from the Actions menu

### Documents Processed More Than Once

**Symptom:** a document uploaded once shows several entries in the Web UI's
**Version History** that differ only in the execution that produced them, all
with the same queued time, and `AWS/States ExecutionsStarted` for the workflow is
higher than the number of documents you uploaded. Every extra execution was
billed for Bedrock, Textract and Lambda.

**Cause (fixed in 0.6.9,
[#904](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/904)):**
under a saturated queue the `QueueProcessor` Lambda timed out mid-batch. Lambda
then reported nothing to SQS, so the whole batch was redelivered — including
messages whose workflow had already started — and each redelivery started a new,
randomly named execution. Since 0.6.9 the execution is named after the SQS
message (`<basename>-<message-id>`), so a redelivered message is refused by Step
Functions and acked without a second execution, and each message is deleted as
soon as its execution exists. `QueueProcessorErrorsAlarm` now reports the
timeouts themselves; see [Monitoring](monitoring.md#queueprocessorerrorsalarm--a-processor-that-cannot-finish-its-batches).

**To confirm it on a stack you have not yet upgraded**, pick one affected
document and count how often its SQS message was received:

```
fields @timestamp, @requestId, @message
| filter @message like /Processing message/ and @message like /<object-key>/
| sort @timestamp asc
```

One `messageId` appearing many times is redelivery, not duplicate ingest. Check
the same log group for invocations ending in `Status: timeout`. Upgrading is the
fix; if you cannot yet, raising the function's `MemorySize` and lowering the
event source mapping's `BatchSize` reduce how often a batch fails to finish.

## Security Issues

### WAF Blocking Access

If the WAF is blocking legitimate access:

1. Check the `WAFAllowedIPv4Ranges` parameter value
2. Update with correct CIDR blocks for allowed IP ranges
3. Remember Lambda functions have automatic access regardless of WAF settings

### Authentication Issues

For Cognito authentication problems:

1. Verify user exists in Cognito User Pool
2. Check user attributes (email verified, status)
3. Reset user password if needed
4. Review identity pool configuration
5. Check browser console for specific authentication errors

## Model-Specific Troubleshooting

### Bedrock

- **Throttling**: Request quota increases or reduce concurrency
- **Content Filtering**: Review guardrail configuration if content is being filtered unexpectedly
- **Prompt Issues**: Test prompts directly in Bedrock console or notebook
- **Region Availability**: Verify model availability in your region

### SageMaker

- **Endpoint Cold Start**: Consider using provisioned concurrency
- **GPU Utilization**: Monitor utilization and adjust instance type if needed
- **Memory Errors**: Check inference logs for out-of-memory errors
- **Model Loading Errors**: Verify model artifacts are correct

## Advanced Troubleshooting

### End-to-End Tracing

Use X-Ray tracing for advanced diagnostics:

1. Deploy with `EnableXRayTracing=true` (the default). It controls the Lambda
   functions and both state machines together; set it to `false` to turn tracing
   off across the stack.
2. View service map in X-Ray console
3. Analyze trace details for latency and error hotspots

The document-processing Lambdas annotate their segments with `document_id` and
`processing_stage`, so a filter expression like
`annotation.document_id = "<id>"` narrows the console to one document.

### Log Correlation

Trace document processing across systems:

1. Extract correlation ID from log entries
2. Search across log groups using CloudWatch Insights:

```
fields @timestamp, @message
| filter @message like "correlation-id-here"
| sort @timestamp asc
```

### Performance Testing

Test system capacity and identify bottlenecks:

1. Use load testing scripts in `./scripts/` directory
2. Start with low document rates and increase gradually
3. Monitor CloudWatch metrics for saturation points
4. Identify bottlenecks and optimize configuration

## Build and Deployment Issues

### Publishing Script Failures

| Issue                               | Resolution                                                                                               |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------- |
| **Generic "Failed to build" error** | Use `--verbose` flag to see detailed error messages: `idp-cli publish --source-dir . --region <region> --verbose` |
| **Python version mismatch**         | Ensure Python 3.13 is installed and available in PATH. Check with `python3 --version`                    |
| **SAM build fails**                 | Verify SAM CLI is installed and up to date. Check Docker is running if using containerized builds        |
| **Missing dependencies**            | Install required packages: `pip install boto3 typer rich botocore`                                       |
| **Permission errors**               | Verify AWS credentials are configured and have necessary S3/CloudFormation permissions                   |

### Common Build Error Messages

**Python Runtime Error:**

```
Error: PythonPipBuilder:Validation - Binary validation failed for python, searched for python in following locations: [...] which did not satisfy constraints for runtime: python3.12
```

**Resolution:** Install Python 3.13 and ensure it's in your PATH, or use the `--use-container` flag for containerized builds.

**Docker Not Running:**

```
Error: Running AWS SAM projects locally requires Docker
```

**Resolution:** Start Docker daemon before running the publish script.

**AWS Credentials Not Found:**

```
Error: Unable to locate credentials
```

**Resolution:** Configure AWS credentials using `aws configure` or set environment variables.

### Verbose Mode Usage

For detailed debugging information, always use the `--verbose` flag when troubleshooting build issues:

```bash
# Standard usage
idp-cli publish --source-dir . --region us-east-1

# Verbose mode for troubleshooting
idp-cli publish --source-dir . --region us-east-1 --verbose
```

Verbose mode provides:

- Exact SAM build commands being executed
- Complete stdout/stderr from failed operations
- Python environment and dependency information
- Detailed error traces and stack traces

### Container-Based Lambda Deployment Issues

| Issue | Resolution |
|-------|------------|
| **Lambda package exceeds 250MB limit** | Pattern-2 uses container images automatically. For Pattern-1/3, consider reducing dependency size or switching to container images in a future update. |
| **Docker daemon not running** | Start Docker Desktop or Docker service before running container deployment |
| **ECR login failed** | Ensure AWS credentials have ECR permissions. The script will automatically handle ECR login |
| **Container build fails** | Check Dockerfile syntax and ensure all referenced files exist |
| **Image push timeout** | Check network connectivity and ECR repository permissions |

**Container Deployment Behavior:**
- Pattern-2 builds and pushes container images automatically when Pattern-2 changes are detected.
- Ensure Docker Desktop/service is running and your AWS credentials have ECR permissions.
- Use `--verbose` to see detailed build and push logs.
