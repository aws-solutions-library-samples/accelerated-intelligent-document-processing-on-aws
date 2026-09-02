# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import boto3
import os
import json
from datetime import datetime, timezone, timedelta
import logging
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service
from idp_common.document_versions import delete_current_output_objects
from aws_xray_sdk.core import xray_recorder, patch_all

# Patch AWS SDK calls for X-Ray tracing
patch_all()

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

# Initialize clients
sqs = boto3.client("sqs")
s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")
document_service = create_document_service()
queue_url = os.environ["QUEUE_URL"]
retentionDays = int(os.environ["DATA_RETENTION_IN_DAYS"])
# Matches queue_processor's namespace so both concurrency and ingest
# telemetry share the same operator-facing surface.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "IDP")


def resolve_active_config_version(config_table):
    """Return the name of the active Configuration Profile, or None.

    Reads the active-profile pointer item first — one get_item, on a path that
    runs for EVERY document queued. The scan below remains as the fallback for a
    stack that has not activated a profile since the pointer was introduced.

    The fallback paginates. DynamoDB applies the 1MB page size to the items
    EXAMINED, not the items matching FilterExpression, so a single scan call
    finds the active row only when it falls within the first page.
    ProjectionExpression keeps that page as wide as possible: without it the
    scan reads whole config bodies (tens to hundreds of KB each), so only a few
    profiles fit per page and the active one is easily missed. Missing it here
    silently processes the document under the DEFAULT config rather than the
    active one — the same filtered-scan defect as issue #599.
    """
    try:
        pointer = config_table.get_item(
            Key={"Configuration": "Config#__active"},
            ProjectionExpression="ActiveVersion",
        ).get("Item")
        if pointer and pointer.get("ActiveVersion"):
            return str(pointer["ActiveVersion"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read the active-profile pointer ({e}); scanning instead")

    scan_kwargs = {
        "FilterExpression": (
            "begins_with(Configuration, :config_prefix) AND IsActive = :active"
        ),
        "ExpressionAttributeValues": {
            ":config_prefix": "Config#",
            ":active": True,
        },
        "ProjectionExpression": "Configuration",
    }
    while True:
        response = config_table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            # Extract version from Config#v1 format
            config_key = item["Configuration"]
            if "#" in config_key:
                return config_key.split("#", 1)[1]
            return None
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return None
        scan_kwargs["ExclusiveStartKey"] = last_key


@xray_recorder.capture("queue_sender")
def handler(event, context):
    logger.info(f"Processing event: {json.dumps(event)}")

    detail = event["detail"]
    object_key = detail["object"]["key"]
    logger.info(f"Processing file: {object_key}")

    # Ignore S3 "folder" pseudo-objects. Creating a folder in the S3 console
    # writes a zero-byte object whose key ends with '/'. These are not real
    # documents, so skip them instead of starting a spurious workflow.
    if object_key.endswith("/"):
        logger.info(f"Skipping folder pseudo-object (key ends with '/'): {object_key}")
        return {"statusCode": 200, "detail": detail, "skipped": "folder_pseudo_object"}

    # Get output bucket from environment for the document
    output_bucket = os.environ.get("OUTPUT_BUCKET", "")
    if output_bucket == "":
        raise Exception("OUTPUT_BUCKET environment variable not set")

    # Purge any output data left in S3 from a previous upload of this same key.
    # Without this, a re-upload that reuses an existing filename (e.g. replacing
    # a W2 with a W3 under `test.pdf`) would let the OCR function's retry-safe
    # recovery mechanism (`discover_existing_ocr_pages`) resurrect the previous
    # document's OCR results, so classification/extraction would consume text
    # from the OLD document and the UI would show stale extraction. Issue #719.
    #
    # Scoped to `<key>/pages/` — that's the only subprefix
    # ``discover_existing_ocr_pages`` reads, so purging just pages/ fully
    # closes #719 while making it impossible for an upload of ``foo`` to
    # destroy a nested document at ``foo/bar.pdf/*`` (the reprocess resolver
    # keeps the broad ``<key>/*`` purge because its "start over" intent is
    # deliberate and the caller is an authenticated admin action).
    #
    # No-op on a fresh key. Preserves `<key>/runs/` (version-history manifests).
    #
    # Known trade-off (concurrent re-uploads mid-flight): if a prior
    # workflow for the same key is still writing to ``pages/*`` when a
    # NEW upload arrives, this purge deletes pages the running OCR is
    # actively writing. The prior workflow keeps writing whatever pages
    # come after the purge, so the new workflow's OCR discovery sees a
    # partial subset of stale pages and resurrects them — the resulting
    # extraction is a mix of old and new document text. This is a
    # regression only in the concurrent-race case (previously the new
    # workflow saw ALL of the old document's pages and was uniformly
    # wrong; now it can be mixed). A full fix needs content-etag-keyed
    # OCR recovery (out of scope for #719). For the non-concurrent
    # case — which is what #719 actually covers — the purge is correct.
    try:
        deleted = delete_current_output_objects(
            s3, output_bucket, object_key, subprefixes=("pages/",)
        )
        if deleted:
            logger.info(
                f"Purged {deleted} stale output objects for re-uploaded key "
                f"s3://{output_bucket}/{object_key}/pages/"
            )
    except Exception as e:
        # Non-fatal: OCR will still run but may recover stale partial data,
        # silently reproducing #719. Logged at ERROR AND emitted as a
        # CloudWatch metric so an operator can alarm on it without
        # depending on log-scraping (matches the round-16 pattern in
        # queue_processor._put_drift_sample). The alternative (raise →
        # EventBridge async retries exhaust → QueueSenderDLQ) is worse:
        # customers would rather see a possibly-stale extraction than
        # have the ingest silently disappear. queue_sender is
        # ``Type: CloudWatchEvent`` (EventBridge Object-Created rule),
        # so retries/DLQ behavior is EventBridge's async-invoke semantics,
        # not an SQS DLQ on the source.
        logger.error(f"Failed to purge previous output data for {object_key}: {e}")
        try:
            cloudwatch.put_metric_data(
                Namespace=METRIC_NAMESPACE,
                MetricData=[
                    {
                        "MetricName": "StaleOutputPurgeFailed",
                        "Value": 1,
                        "Unit": "Count",
                    }
                ],
            )
        except Exception:
            pass  # telemetry must not affect document ingest

    # Create document object - config version will be read from S3 metadata automatically
    current_time = datetime.now(timezone.utc).isoformat()
    document = Document.from_s3_event(event, output_bucket)
    document.status = Status.QUEUED
    document.queued_time = current_time

    # If no config version found in metadata or filename, get active config version
    if not document.config_version:
        try:
            import boto3

            config_table = boto3.resource("dynamodb").Table(os.environ["CONFIG_TABLE"])
            document.config_version = resolve_active_config_version(config_table)
            if document.config_version:
                logger.info(
                    f"Using active config version {document.config_version} "
                    f"for {object_key}"
                )
            else:
                logger.warning(
                    f"No active config version found for {object_key} after a "
                    "full scan; it will be processed under the default config"
                )
        except Exception as e:
            logger.warning(f"Could not retrieve active config version: {e}")
            document.config_version = None

    # Capture X-Ray trace ID for error analysis
    current_segment = xray_recorder.current_segment()
    if current_segment:
        document.trace_id = current_segment.trace_id
        xray_recorder.put_annotation("document_id", document.id)
        logger.info(f"X-Ray trace ID captured: {document.trace_id}")

    # Calculate expiry date
    expires_after = int(
        (datetime.now(timezone.utc) + timedelta(days=retentionDays)).timestamp()
    )

    # Create document in DynamoDB via document service
    logger.info(f"Creating document via document service: {document.input_key}")

    # Create document in document service with TTL
    created_key = document_service.create_document(
        document, expires_after=expires_after
    )
    logger.info(f"Document created with key: {created_key}")

    # Send serialized document to SQS queue
    doc_json = document.to_json()
    message = {
        "QueueUrl": queue_url,
        "MessageBody": doc_json,
        "MessageAttributes": {
            "EventType": {"StringValue": "DocumentQueued", "DataType": "String"},
            "ObjectKey": {"StringValue": object_key, "DataType": "String"},
        },
    }
    logger.info(f"Sending document to SQS queue: {object_key}")
    response = sqs.send_message(**message)
    logger.info(f"SQS response: {response}")

    return {"statusCode": 200, "detail": detail, "document_id": document.id}
