# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import time
import json
import os
from datetime import datetime, timedelta, timezone
import logging
from idp_common.models import Document, Status, Page, Section  # type: ignore[import-untyped]
from idp_common.docs_service import create_document_service  # type: ignore[import-untyped]
from idp_common.document_versions import build_run_id, snapshot_output_versions  # type: ignore[import-untyped]
from botocore.exceptions import ClientError
from typing import Dict, Any, Optional

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)
# Get LOG_LEVEL from environment variable with INFO as default

METRIC_NAMESPACE = os.environ["METRIC_NAMESPACE"]
REPORTING_BUCKET = os.environ.get("REPORTING_BUCKET")
SAVE_REPORTING_FUNCTION_NAME = os.environ.get("SAVE_REPORTING_FUNCTION_NAME")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET")
DATA_RETENTION_IN_DAYS = int(os.environ.get("DATA_RETENTION_IN_DAYS", "365"))
# Document version history (run records + S3 version manifests). On by
# default; set DOCUMENT_VERSIONING_ENABLED=false to skip run-record creation.
DOCUMENT_VERSIONING_ENABLED = (
    os.environ.get("DOCUMENT_VERSIONING_ENABLED", "true").lower() == "true"
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table
else:
    Table = object

dynamodb = boto3.resource("dynamodb")
cloudwatch = boto3.client("cloudwatch")
s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")
sns = boto3.client("sns")
document_service = create_document_service()
concurrency_table: Table = dynamodb.Table(os.environ["CONCURRENCY_TABLE"])
COUNTER_ID = "workflow_counter"
# A lost decrement leaks a workflow slot permanently, so the realistic failure
# (transient DynamoDB throttling) is retried in-process before the event is
# handed back to EventBridge for its own retry.
DECREMENT_MAX_ATTEMPTS = int(os.environ.get("DECREMENT_MAX_ATTEMPTS", "4"))
CIRCUIT_BREAKER_ID = "circuit_breaker"
CIRCUIT_BREAKER_ENABLED = (
    os.environ.get("CIRCUIT_BREAKER_ENABLED", "false").lower() == "true"
)
CIRCUIT_BREAKER_MANAGER_ARN = os.environ.get("CIRCUIT_BREAKER_MANAGER_ARN", "")
ALERTS_TOPIC_ARN = os.environ.get("ALERTS_TOPIC_ARN", "")
INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "")
TRACKING_TABLE = os.environ.get("TRACKING_TABLE", "")


def _delete_superseded_original(object_key: str) -> None:
    """Fully delete an original that a preprocessing hook replaced with a
    redacted copy (REDACTED_SUPERSEDED). The tracker is the LAST writer for the
    execution, so it owns the delete — doing it in the hook mid-execution races
    with this final write. Best-effort: never raise (the redacted copy already
    exists and is processing independently)."""
    try:
        from idp_common.delete_documents import delete_single_document

        if not TRACKING_TABLE or not INPUT_BUCKET:
            logger.warning(
                "Cannot delete superseded original %s: TRACKING_TABLE/INPUT_BUCKET unset",
                object_key,
            )
            return
        result = delete_single_document(
            object_key=object_key,
            tracking_table=dynamodb.Table(TRACKING_TABLE),
            s3_client=s3,
            input_bucket=INPUT_BUCKET,
            output_bucket=OUTPUT_BUCKET or "",
        )
        logger.info(
            "Deleted REDACTED_SUPERSEDED original %s: %s",
            object_key,
            result.get("deleted"),
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to delete superseded original %s: %s", object_key, e)


def update_document_completion(
    object_key: str, workflow_status: str, output_data: Dict[str, Any]
) -> Document:
    """
    Update document completion status via document service

    Args:
        object_key: The document object key (ID)
        workflow_status: The final workflow status (SUCCEEDED, FAILED, ABORTED, TIMED_OUT)
        output_data: The output data from the workflow execution

    Returns:
        The updated Document object
    """
    # Map workflow status to document status
    # ABORTED workflows should keep ABORTED status (set by abort_workflow_resolver)
    if workflow_status == "SUCCEEDED":
        doc_status = Status.COMPLETED
    elif workflow_status == "ABORTED":
        doc_status = Status.ABORTED
    else:
        doc_status = Status.FAILED

    # Create a document with basic properties (fallback for failed workflows)
    document = Document(
        id=object_key,
        input_key=object_key,
        status=doc_status,
        completion_time=datetime.now(timezone.utc).isoformat(),
    )

    # Get sections, pages, and metering data if workflow succeeded
    if workflow_status == "SUCCEEDED" and output_data:
        try:
            # Get working bucket for decompression
            working_bucket = os.environ.get("WORKING_BUCKET")

            # Handle multiple possible output structures from different Step Functions patterns
            # After evaluation refactoring, output is at root level 'document' for Pattern 2/3
            # Pattern 1 may still use 'Result.document' wrapper
            logger.info(f"Output data keys: {list(output_data.keys())}")

            if "document" in output_data:
                # Pattern 2/3 structure after evaluation refactoring: at root level
                document_data = output_data["document"]
                logger.info("Using output_data['document'] structure")
            elif "Result" in output_data and "document" in output_data.get(
                "Result", {}
            ):
                # Pattern 1 structure: wrapped in Result
                document_data = output_data["Result"]["document"]
                logger.info("Using output_data['Result']['document'] structure")
            else:
                # Fallback: entire output is the document
                document_data = output_data
                logger.info("Using entire output_data as document (fallback)")

            # Log compression status for debugging
            wrapper_status = None
            if isinstance(document_data, dict):
                is_compressed = document_data.get("compressed", False)
                # A terminal status the state machine set on the compressed
                # WRAPPER (e.g. REDACTED_SUPERSEDED from the preprocessing halt
                # path) lives on the wrapper, NOT inside the compressed snapshot
                # — that snapshot was serialized earlier in the run (status
                # RUNNING). Capture it here before load_document decompresses and
                # overwrites the in-memory status from the older snapshot.
                wrapper_status = document_data.get("status")
                logger.info(f"Document data is compressed: {is_compressed}")
                if is_compressed:
                    logger.info(
                        f"Compressed document S3 URI: {document_data.get('s3_uri', 'N/A')}"
                    )

            # Load document with proper decompression handling
            processed_doc = Document.load_document(
                document_data, working_bucket, logger
            )

            # Log what we got from decompression/loading
            logger.info(
                f"Loaded document has {processed_doc.num_pages} pages, "
                f"{len(processed_doc.sections)} sections, "
                f"{len(processed_doc.metering)} metering entries"
            )

            # A preprocessing hook (e.g. PII anonymization "redact copy and
            # stop") sets REDACTED_SUPERSEDED on the wrapper to signal that this
            # original was replaced by a redacted copy and should NOT remain.
            # The tracker is the LAST writer for the execution, so it owns the
            # delete — doing it in the hook mid-execution races with this write
            # (the tracker would re-create the row). Delete the whole document
            # (input S3 + tracking rows) here; the handler detects this status
            # and skips run-record/metrics persistence. We still return a real
            # Document (never None) so the caller never dereferences None.
            if workflow_status == "SUCCEEDED" and (
                processed_doc.status == Status.REDACTED_SUPERSEDED
                or wrapper_status == Status.REDACTED_SUPERSEDED.value
            ):
                _delete_superseded_original(object_key)
                processed_doc.status = Status.REDACTED_SUPERSEDED
                processed_doc.completion_time = datetime.now(timezone.utc).isoformat()
                return processed_doc

            # Use the processed document directly and update status. This is
            # safer than copying fields and ensures we don't miss any data.
            processed_doc.status = (
                Status.COMPLETED if workflow_status == "SUCCEEDED" else Status.FAILED
            )
            processed_doc.completion_time = datetime.now(timezone.utc).isoformat()
            document = processed_doc

        except Exception as e:
            logger.error(f"Could not extract document data: {e}", exc_info=True)
            # Keep the fallback document with minimal data

    # Update document in document service
    logger.info(
        f"Updating document via document service with {len(document.metering)} metering entries "
        f"and {len(document.sections)} sections"
    )
    updated_doc = document_service.update_document(document)

    # Save reporting data to reporting bucket if available
    if REPORTING_BUCKET and SAVE_REPORTING_FUNCTION_NAME:
        # Determine what data to save based on what's available in the document
        data_to_save = []

        if document.metering:
            data_to_save.append("metering")

        if document.sections:
            # Check if any sections have extraction results
            sections_with_results = [
                s for s in document.sections if s.extraction_result_uri
            ]
            if sections_with_results:
                data_to_save.append("sections")
                logger.info(
                    f"Found {len(sections_with_results)} sections with extraction results"
                )

        # Check if rule validation results are available
        if (
            hasattr(document, "rule_validation_result")
            and document.rule_validation_result
        ):
            data_to_save.append("rule_validation_results")
            logger.info("Found rule validation results")

        if data_to_save:
            try:
                logger.info(
                    f"Saving reporting data ({', '.join(data_to_save)}) to {REPORTING_BUCKET} by calling Lambda {SAVE_REPORTING_FUNCTION_NAME}"
                )
                lambda_response = lambda_client.invoke(
                    FunctionName=SAVE_REPORTING_FUNCTION_NAME,
                    InvocationType="RequestResponse",
                    Payload=json.dumps(
                        {
                            "document": document.to_dict(),
                            "reporting_bucket": REPORTING_BUCKET,
                            "data_to_save": data_to_save,
                        }
                    ),
                )

                # Check the response
                response_payload = json.loads(
                    lambda_response["Payload"].read().decode("utf-8")
                )
                if response_payload.get("statusCode") != 200:
                    logger.warning(
                        f"SaveReportingData Lambda returned non-200 status: {response_payload}"
                    )
                else:
                    logger.info("SaveReportingData Lambda executed successfully")
            except Exception as e:
                logger.error(f"Error invoking SaveReportingData Lambda: {str(e)}")
                # Continue execution - don't fail the entire function if reporting fails
        else:
            logger.info(
                "No reporting data available to save (no metering data or sections with extraction results)"
            )

    return updated_doc


def record_document_run(
    document: Document, run_timestamp: Optional[str] = None
) -> None:
    """
    Record this successful execution as an immutable document version.

    Output artifacts live at deterministic keys under {input_key}/ in the
    versioned output bucket, so the next run will overwrite them in place.
    Snapshotting the current VersionId of every output object into a per-run
    manifest (s3://<output>/<key>/runs/<run_id>/manifest.json) pins this run's
    exact bytes; the run item (PK=doc#<key>, SK=run#<run_id>) carries the
    metadata snapshot the UI/CLI use to list and render prior versions.

    Args:
        document: The completed document.
        run_timestamp: Stable completion timestamp to derive the run_id from —
            pass the Step Functions event's ``stopDate`` so a redelivered
            SUCCEEDED event (EventBridge is at-least-once) yields the SAME
            run_id and is deduped, rather than minting a phantom duplicate
            version. Falls back to the document's completion_time.

    Never raises: version history is best-effort and must not fail tracking.
    """
    if not DOCUMENT_VERSIONING_ENABLED:
        return
    if not OUTPUT_BUCKET:
        logger.warning("OUTPUT_BUCKET not set - skipping document run record")
        return
    try:
        # Derive run_id from a STABLE timestamp + the execution name (also
        # stable) so retries collapse onto the same run_id. Using
        # document.completion_time here would break dedup: it is regenerated as
        # now() on every invocation.
        run_id = build_run_id(
            run_timestamp
            or document.completion_time
            or datetime.now(timezone.utc).isoformat(),
            document.workflow_execution_arn,
        )
        manifest, manifest_uri = snapshot_output_versions(
            s3,
            OUTPUT_BUCKET,
            document.input_key,
            run_id,
            metadata={
                "completion_time": document.completion_time,
                "config_version": document.config_version,
                "workflow_execution_arn": document.workflow_execution_arn,
            },
        )
        expires_after = int(
            (
                datetime.now(timezone.utc) + timedelta(days=DATA_RETENTION_IN_DAYS)
            ).timestamp()
        )
        document_service.create_document_run(
            document,
            run_id=run_id,
            manifest_uri=manifest_uri,
            file_count=manifest.get("file_count", 0),
            expires_after=expires_after,
        )
    except Exception as e:
        logger.error(
            f"Failed to record document run for {document.input_key}: {e}",
            exc_info=True,
        )


def put_latency_metrics(document: Document) -> None:
    """
    Publish latency metrics to CloudWatch

    Args:
        document: Document object containing timestamps

    Raises:
        ValueError: If required timestamps are missing
        ClientError: If CloudWatch operation fails
    """
    try:
        # Check required timestamps
        if not document.queued_time or not document.start_time:
            missing = []
            if not document.queued_time:
                missing.append("queued_time")
            if not document.start_time:
                missing.append("start_time")
            raise ValueError(f"Missing required timestamps: {', '.join(missing)}")

        now = datetime.now(timezone.utc)
        initial_time = datetime.fromisoformat(document.start_time)
        queued_time = datetime.fromisoformat(document.queued_time)
        workflow_start_time = datetime.fromisoformat(document.start_time)

        queue_latency = (workflow_start_time - queued_time).total_seconds() * 1000
        workflow_latency = (now - workflow_start_time).total_seconds() * 1000
        total_latency = (now - initial_time).total_seconds() * 1000

        logger.info(
            f"Publishing latency metrics - queue: {queue_latency}ms, "
            f"workflow: {workflow_latency}ms, total: {total_latency}ms"
        )

        cloudwatch.put_metric_data(
            Namespace=f"{METRIC_NAMESPACE}",
            MetricData=[
                {
                    "MetricName": "QueueLatencyMilliseconds",
                    "Value": queue_latency,
                    "Unit": "Milliseconds",
                },
                {
                    "MetricName": "WorkflowLatencyMilliseconds",
                    "Value": workflow_latency,
                    "Unit": "Milliseconds",
                },
                {
                    "MetricName": "TotalLatencyMilliseconds",
                    "Value": total_latency,
                    "Unit": "Milliseconds",
                },
            ],
        )
    except ValueError as e:
        logger.error(f"Invalid timestamps in metrics data: {e}")
        raise
    except ClientError as e:
        logger.error(f"Failed to publish CloudWatch metrics: {e}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"Unexpected error publishing metrics: {e}", exc_info=True)
        raise


def decrement_counter() -> Optional[int]:
    """
    Decrement the concurrency counter.

    A LOST decrement is permanent and unrecoverable-by-itself: the counter drifts
    up, and once it reaches MaxConcurrentWorkflows the stack stops admitting
    documents. This function used to swallow every failure and return None, and
    the handler treated that as success and returned HTTP 200 — so a single
    throttled DynamoDB write silently cost a slot forever, with no retry, no
    alarm, and nothing in the response to say it had happened.

    (For the record: reviewing the leak that motivated this found ZERO decrement
    failures in the entire retained log history, and 177 decrements for 177
    terminal executions across the incident window — so this is a latent gap
    being closed, not the identified cause of that leak.)

    Now: retried with backoff for the realistic failure (transient throttling),
    and if it still cannot be done the exception propagates so EventBridge/Lambda
    retry the whole event rather than losing the slot quietly.

    Returns:
        The new counter value.

    Raises:
        ClientError / Exception if the decrement could not be applied after
        retries — deliberately, so the caller fails and the event is retried.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, DECREMENT_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"Decrementing concurrency counter (attempt {attempt}/"
                f"{DECREMENT_MAX_ATTEMPTS})"
            )
            response = concurrency_table.update_item(
                Key={"counter_id": COUNTER_ID},
                UpdateExpression="ADD active_count :dec",
                ExpressionAttributeValues={":dec": -1},
                ReturnValues="UPDATED_NEW",
            )
        except Exception as e:  # ClientError and anything else
            last_error = e
            logger.warning(
                f"Decrement attempt {attempt}/{DECREMENT_MAX_ATTEMPTS} failed: {e}"
            )
            if attempt < DECREMENT_MAX_ATTEMPTS:
                time.sleep(0.2 * (2 ** (attempt - 1)))
            continue

        # Past this point the decrement HAS been applied. Nothing below may
        # re-enter the retry loop, or we would decrement a second time.
        new_count = response.get("Attributes", {}).get("active_count")
        logger.info(f"Counter decremented. New value: {new_count}")
        try:
            _emit_counter_metric(new_count)
        except Exception as metric_error:
            logger.warning(f"Could not emit concurrency counter metric: {metric_error}")
        return new_count

    logger.error(
        "FAILED to decrement the concurrency counter after "
        f"{DECREMENT_MAX_ATTEMPTS} attempts: {last_error}. Raising so this event "
        "is retried — swallowing it would leak a workflow slot permanently.",
        exc_info=True,
    )
    raise last_error if last_error else RuntimeError("decrement failed")


def _emit_counter_metric(value) -> None:
    """Publish the post-decrement counter value.

    The leak that motivated this work could not be attributed to a moment in time
    because nothing ever recorded the counter, so there was no history to inspect
    — only the end state. One data point per completed document is a cheap,
    continuous series that makes the next divergence attributable.

    Never allowed to affect the decrement it reports on.
    """
    if value is None:
        return
    try:
        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "ConcurrencyCounterActive",
                    "Value": float(value),
                    "Unit": "Count",
                }
            ],
        )
    except Exception as e:
        logger.warning(f"Could not emit concurrency counter metric: {e}")


def notify_circuit_breaker_success() -> None:
    """
    Transition circuit breaker to CLOSED if currently in HALF_OPEN state.

    Called after a successful workflow completion to signal that Bedrock
    has recovered and normal operation can resume. The DynamoDB update is
    conditional on the state still being HALF_OPEN so a concurrent alarm
    firing at the same moment (HALF_OPEN->OPEN) is not clobbered.
    """
    if not CIRCUIT_BREAKER_ENABLED:
        return

    try:
        response = concurrency_table.get_item(
            Key={"counter_id": CIRCUIT_BREAKER_ID},
            ProjectionExpression="#state",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = response.get("Item")

        if not (item and item.get("state") == "HALF_OPEN"):
            return

        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            concurrency_table.update_item(
                Key={"counter_id": CIRCUIT_BREAKER_ID},
                UpdateExpression="SET #state = :closed, last_checked_at = :ts REMOVE last_error, opened_at",
                ConditionExpression="#state = :expected",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":closed": "CLOSED",
                    ":expected": "HALF_OPEN",
                    ":ts": timestamp,
                },
            )
        except ClientError as ce:
            if ce.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info(
                    "Circuit breaker no longer HALF_OPEN - skipping CLOSED transition"
                )
                return
            raise

        logger.info("Circuit breaker transitioned to CLOSED - service recovered")

        if ALERTS_TOPIC_ARN:
            try:
                sns.publish(
                    TopicArn=ALERTS_TOPIC_ARN,
                    Subject="Circuit Breaker CLOSED: Bedrock Service Recovered",
                    Message=json.dumps(
                        {
                            "circuit_breaker_state": "CLOSED",
                            "reason": "Successful workflow execution in HALF_OPEN state",
                            "timestamp": timestamp,
                        },
                        indent=2,
                    ),
                )
            except Exception as sns_error:
                logger.warning(
                    f"Failed to publish circuit breaker notification: {sns_error}"
                )

        cloudwatch.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=[
                {"MetricName": "CircuitBreakerClosed", "Value": 1, "Unit": "Count"}
            ],
        )

        if CIRCUIT_BREAKER_MANAGER_ARN:
            try:
                lambda_client.invoke(
                    FunctionName=CIRCUIT_BREAKER_MANAGER_ARN,
                    InvocationType="Event",
                    Payload=json.dumps({"action": "broadcast"}).encode("utf-8"),
                )
            except Exception as invoke_error:
                logger.warning(
                    f"Failed to broadcast circuit breaker CLOSED to AppSync: {invoke_error}"
                )
    except Exception as e:
        logger.warning(f"Error notifying circuit breaker of success: {e}")


def handler(event, context):
    logger.info(f"Processing event: {json.dumps(event)}")
    counter_value = None
    decrement_attempted = False

    try:
        # Extract data from event
        input_data = json.loads(event["detail"]["input"])
        output_data = None

        if event["detail"].get("output"):
            output_data = json.loads(event["detail"]["output"])

        # Get object key from document
        try:
            if "document" in input_data:
                object_key = input_data["document"]["document_id"]
            else:
                raise ValueError("Unable to find document_id in input")
        except (KeyError, TypeError) as e:
            logger.error(f"Error extracting object_key from input: {e}")
            logger.error(f"Input data structure: {input_data}")
            raise

        workflow_status = event["detail"]["status"]

        # Update document completion status
        updated_doc = update_document_completion(
            object_key, workflow_status, output_data
        )

        # A REDACTED_SUPERSEDED original was deleted (redact-copy-and-stop): there
        # is no document to record a run/metrics for, and its output artifacts are
        # gone. Skip run-record + metrics; the counter is still decremented below
        # exactly once. (updated_doc is a real Document, so no None deref.)
        superseded = (
            updated_doc is not None
            and getattr(updated_doc, "status", None) == Status.REDACTED_SUPERSEDED
        )

        # Publish metrics for successful executions
        if workflow_status == "SUCCEEDED" and not superseded:
            # Record this run as an immutable document version (S3-version
            # manifest + run record) before anything can overwrite outputs.
            if not updated_doc.workflow_execution_arn:
                updated_doc.workflow_execution_arn = event["detail"].get("executionArn")
            # Use the execution's stable stopDate (epoch millis) for the run_id
            # timestamp so an at-least-once redelivery of this SUCCEEDED event
            # dedupes to the same run_id instead of minting a duplicate version.
            run_timestamp = None
            stop_date = event["detail"].get("stopDate")
            if stop_date:
                run_timestamp = datetime.fromtimestamp(
                    stop_date / 1000, tz=timezone.utc
                ).isoformat()
            record_document_run(updated_doc, run_timestamp=run_timestamp)

            try:
                logger.info("Workflow succeeded, publishing latency metrics")
                put_latency_metrics(updated_doc)
            except Exception as metrics_error:
                logger.error(
                    f"Failed to publish metrics: {metrics_error}", exc_info=True
                )
                # Continue processing even if metrics fail

            # Notify circuit breaker of successful workflow (for HALF_OPEN recovery)
            notify_circuit_breaker_success()
        else:
            logger.info(
                f"Workflow did not succeed (status: {workflow_status}), "
                "skipping latency metrics"
            )

        # Always decrement counter
        decrement_attempted = True
        counter_value = decrement_counter()

        return {
            "statusCode": 200,
            "body": {
                "object_key": object_key,
                "workflow_status": workflow_status,
                "completion_time": updated_doc.completion_time,
                "counter_value": counter_value,
            },
        }

    except Exception as e:
        logger.error(f"Unexpected error in handler: {str(e)}", exc_info=True)
        # Always try to decrement counter in case of any error — but only if we
        # never got as far as trying. decrement_counter() now retries internally
        # and raises when it truly cannot apply the write, so calling it a second
        # time here would only risk a double-subtract (a write that landed but
        # answered with an error) without adding a chance of success.
        if not decrement_attempted:
            decrement_counter()
        raise
