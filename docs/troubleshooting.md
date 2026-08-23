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

### Web UI Access Issues

| Issue                                | Resolution                                                                                                            |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| **Cannot login to Web UI**           | Verify Cognito user status and permissions in AWS Console. Check email for temporary credentials if first-time login. |
| **Web UI loads but shows errors**    | Check browser console for specific error messages. Verify API endpoints are accessible.                               |
| **Cannot see document history**      | Verify AWS AppSync API permissions. Check CloudWatch Logs for API errors.                                             |
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

1. Enable X-Ray tracing in the CloudFormation template
2. View service map in X-Ray console
3. Analyze trace details for latency and error hotspots

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
