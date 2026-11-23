#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP SDK Example: Lambda Function

This example demonstrates how to use the IDP SDK in an AWS Lambda function
for document processing automation.

Deployment Instructions:
------------------------
1. Create a Lambda layer with the IDP SDK:
   cd lib/idp_sdk
   pip install . -t python/
   zip -r idp-sdk-layer.zip python/
   aws lambda publish-layer-version \
       --layer-name idp-sdk \
       --zip-file fileb://idp-sdk-layer.zip \
       --compatible-runtimes python3.11 python3.12

2. Create the Lambda function with this code and attach the layer.

3. Configure environment variables:
   - IDP_STACK_NAME: Your IDP stack name (e.g., "IDP-Nova-1")
   - OUTPUT_BUCKET: Bucket to write results (optional)

4. Required IAM permissions:
   - cloudformation:DescribeStacks
   - cloudformation:ListStackResources
   - s3:GetObject, s3:PutObject, s3:ListBucket (on IDP buckets)
   - sqs:SendMessage (on DocumentQueue)
   - dynamodb:GetItem, dynamodb:Query, dynamodb:Scan (on TrackingTable)
   - lambda:InvokeFunction (on LookupFunction)

Example Event:
--------------
{
    "action": "process",
    "source": "s3://my-bucket/documents/",
    "batch_prefix": "lambda-batch"
}

Or for status check:
{
    "action": "status",
    "batch_id": "lambda-batch-20260123-123456"
}
"""

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """
    Lambda handler for IDP SDK operations.

    Supports actions:
    - process: Submit documents for processing
    - status: Check batch status
    - download: Download results (to S3)
    - manifest: Generate manifest
    - config: Configuration operations
    """

    # Get stack name from environment or event
    stack_name = event.get("stack_name") or os.environ.get("IDP_STACK_NAME")
    region = event.get("region") or os.environ.get("AWS_REGION")

    action = event.get("action", "process")

    logger.info(f"Action: {action}, Stack: {stack_name}")

    try:
        if action == "process":
            return process_documents(event, stack_name, region)
        elif action == "status":
            return get_status(event, stack_name, region)
        elif action == "download":
            return download_results(event, stack_name, region)
        elif action == "manifest":
            return generate_manifest(event)
        elif action == "config":
            return config_operation(event, stack_name, region)
        else:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": f"Unknown action: {action}"}),
            }

    except Exception as e:
        logger.exception(f"Error in {action}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def process_documents(event, stack_name, region):
    """Submit documents for processing."""
    from idp_sdk import IDPClient

    if not stack_name:
        return {"statusCode": 400, "body": json.dumps({"error": "stack_name required"})}

    client = IDPClient(stack_name=stack_name, region=region)

    # Get source from event
    source = event.get("source")  # Can be S3 URI, manifest path, or directory
    s3_uri = event.get("s3_uri")
    manifest = event.get("manifest")
    test_set = event.get("test_set")

    if not any([source, s3_uri, manifest, test_set]):
        return {
            "statusCode": 400,
            "body": json.dumps(
                {"error": "One of source, s3_uri, manifest, or test_set required"}
            ),
        }

    # Process
    result = client.batch.process(
        source=source,
        s3_uri=s3_uri,
        manifest=manifest,
        test_set=test_set,
        batch_prefix=event.get("batch_prefix", "lambda-batch"),
        file_pattern=event.get("file_pattern", "*.pdf"),
        number_of_files=event.get("number_of_files"),
        config_path=event.get("config_path"),
        context=event.get("context"),
    )

    logger.info(f"Batch submitted: {result.batch_id}")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "batch_id": result.batch_id,
                "document_count": len(result.document_ids),
                "documents_queued": result.queued,
                "documents_failed": result.failed,
            }
        ),
    }


def get_status(event, stack_name, region):
    """Get status of a batch or document."""
    from idp_sdk import IDPClient

    if not stack_name:
        return {"statusCode": 400, "body": json.dumps({"error": "stack_name required"})}

    batch_id = event.get("batch_id")
    document_id = event.get("document_id")

    if not batch_id and not document_id:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "batch_id or document_id required"}),
        }

    client = IDPClient(stack_name=stack_name, region=region)

    if batch_id:
        status = client.batch.get_status(batch_id=batch_id)
    else:
        doc_status = client.document.get_status(document_id=document_id)
        from idp_sdk import BatchStatus, DocumentBucket, classify_document_state

        # Count the document under the same progress bucket `batch.get_status`
        # uses. A document mid-pipeline carries its current step (OCR,
        # EXTRACTING, ...) as its status, never "PROCESSING", so comparing the
        # status with a hand-picked list of names misses most of them.
        bucket = classify_document_state(doc_status.status)
        status = BatchStatus(
            batch_id="single-doc",
            documents=[doc_status],
            total=1,
            completed=int(bucket == DocumentBucket.COMPLETED),
            failed=int(bucket == DocumentBucket.FAILED),
            in_progress=int(bucket == DocumentBucket.RUNNING),
            queued=int(bucket == DocumentBucket.QUEUED),
            awaiting_review=int(bucket == DocumentBucket.AWAITING_REVIEW),
            success_rate=1.0 if bucket == DocumentBucket.COMPLETED else 0.0,
            all_complete=bucket
            in (
                DocumentBucket.COMPLETED,
                DocumentBucket.FAILED,
                DocumentBucket.AWAITING_REVIEW,
            ),
        )

    # Convert documents to serializable format
    documents = []
    for doc in status.documents[:20]:  # Limit to avoid large responses
        documents.append(
            {
                "document_id": doc.document_id,
                "status": doc.status.value,
                "duration_seconds": doc.duration_seconds,
                "error": doc.error,
            }
        )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "batch_id": status.batch_id,
                "total": status.total,
                "completed": status.completed,
                "failed": status.failed,
                "in_progress": status.in_progress,
                "queued": status.queued,
                "success_rate": status.success_rate,
                "all_complete": status.all_complete,
                "documents": documents,
            }
        ),
    }


def download_results(event, stack_name, region):
    """
    Download results to S3.

    Note: Lambda has limited /tmp storage (512MB default, up to 10GB).
    This function downloads to /tmp and optionally copies to another S3 bucket.
    """
    import os as os_module
    import tempfile

    import boto3

    from idp_sdk import IDPClient

    if not stack_name:
        return {"statusCode": 400, "body": json.dumps({"error": "stack_name required"})}

    batch_id = event.get("batch_id")
    if not batch_id:
        return {"statusCode": 400, "body": json.dumps({"error": "batch_id required"})}

    output_bucket = event.get("output_bucket") or os_module.environ.get("OUTPUT_BUCKET")
    output_prefix = event.get("output_prefix", f"idp-results/{batch_id}")
    file_types = event.get("file_types", ["sections", "summary"])

    client = IDPClient(stack_name=stack_name, region=region)

    # Download to temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        result = client.batch.download_results(
            batch_id=batch_id,
            output_dir=tmpdir,
            file_types=file_types,
        )

        logger.info(f"Downloaded {result.files_downloaded} files to {tmpdir}")

        # If output bucket specified, copy files there
        if output_bucket:
            s3 = boto3.client("s3")
            files_copied = 0

            for root, dirs, files in os_module.walk(tmpdir):
                for filename in files:
                    local_path = os_module.path.join(root, filename)
                    rel_path = os_module.path.relpath(local_path, tmpdir)
                    s3_key = f"{output_prefix}/{rel_path}"

                    s3.upload_file(local_path, output_bucket, s3_key)
                    files_copied += 1

            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "files_downloaded": result.files_downloaded,
                        "files_copied": files_copied,
                        "output_location": f"s3://{output_bucket}/{output_prefix}/",
                    }
                ),
            }

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "files_downloaded": result.files_downloaded,
                    "documents_downloaded": result.documents_downloaded,
                    "note": "Files downloaded to Lambda /tmp. Specify output_bucket to persist.",
                }
            ),
        }


def generate_manifest(event):
    """Generate a manifest from S3 URI."""
    from idp_sdk import IDPClient

    s3_uri = event.get("s3_uri")
    if not s3_uri:
        return {"statusCode": 400, "body": json.dumps({"error": "s3_uri required"})}

    client = IDPClient()
    result = client.manifest.generate(
        s3_uri=s3_uri,
        file_pattern=event.get("file_pattern", "*.pdf"),
        recursive=event.get("recursive", True),
    )

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "document_count": result.document_count,
                "baselines_matched": result.baselines_matched,
            }
        ),
    }


def config_operation(event, stack_name, region):
    """Configuration operations."""
    from idp_sdk import IDPClient

    operation = event.get("operation", "create")

    if operation == "create":
        # No stack required
        client = IDPClient()
        result = client.config.create(
            features=event.get("features", "min"),
            pattern=event.get("pattern", "pattern-2"),
        )
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "yaml_content": result.yaml_content[
                        :5000
                    ],  # Truncate for response size
                }
            ),
        }

    elif operation == "validate":
        # Requires config content in event
        config_content = event.get("config_content")
        if not config_content:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "config_content required for validate"}),
            }

        import tempfile

        import yaml

        client = IDPClient()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            if isinstance(config_content, dict):
                yaml.dump(config_content, f)
            else:
                f.write(config_content)
            temp_path = f.name

        try:
            result = client.config.validate(
                config_file=temp_path,
                pattern=event.get("pattern", "pattern-2"),
            )
            return {
                "statusCode": 200,
                "body": json.dumps(
                    {
                        "valid": result.valid,
                        "errors": result.errors,
                        "warnings": result.warnings,
                    }
                ),
            }
        finally:
            import os

            os.unlink(temp_path)

    elif operation == "download":
        if not stack_name:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "stack_name required for download"}),
            }

        client = IDPClient(stack_name=stack_name, region=region)
        result = client.config.download(format=event.get("format", "full"))

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "config_keys": list(result.config.keys()),
                    "yaml_content": result.yaml_content[:5000],  # Truncate
                }
            ),
        }

    else:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Unknown config operation: {operation}"}),
        }


# For local testing
if __name__ == "__main__":
    import sys

    # Test events
    test_events = {
        "process": {
            "action": "process",
            "stack_name": "IDP-Nova-1",
            "s3_uri": "s3://test-bucket/documents/",
        },
        "status": {
            "action": "status",
            "stack_name": "IDP-Nova-1",
            "batch_id": "test-batch-123",
        },
        "config": {
            "action": "config",
            "operation": "create",
            "features": "min",
        },
    }

    if len(sys.argv) > 1:
        event_name = sys.argv[1]
        event = test_events.get(event_name)
        if event:
            result = handler(event, None)
            print(json.dumps(result, indent=2))
        else:
            print(f"Unknown test event: {event_name}")
            print(f"Available: {list(test_events.keys())}")
    else:
        print("Usage: python lambda_function.py <event_name>")
        print(f"Available events: {list(test_events.keys())}")
