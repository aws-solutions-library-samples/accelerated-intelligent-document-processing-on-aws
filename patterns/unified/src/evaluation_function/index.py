# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function for evaluating document extraction results.

This module provides a lambda handler that evaluates document extraction results by comparing
them against baseline results using the EvaluationService from idp_common.
"""

import json
import os
import logging
import time
import boto3
from enum import Enum
from typing import Dict, Any, Optional

from idp_common import get_config, evaluation
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service

# Environment variables
BASELINE_BUCKET = os.environ.get("BASELINE_BUCKET")
REPORTING_BUCKET = os.environ.get("REPORTING_BUCKET")
SAVE_REPORTING_FUNCTION_NAME = os.environ.get(
    "SAVE_REPORTING_FUNCTION_NAME", "SaveReportingData"
)

# Set up logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Create document service
document_service = create_document_service()


# Define evaluation status constants
class EvaluationStatus(Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NO_BASELINE = "NO_BASELINE"
    TIMED_OUT = "TIMED_OUT"


def update_document_evaluation_status(
    document: Document, status: EvaluationStatus
) -> Document:
    """
    Update document evaluation status via document service

    Args:
        document: The Document object to update
        status: The evaluation status

    Returns:
        The updated Document object

    Raises:
        DocumentServiceError: If the operation fails
    """
    document.status = Status.EVALUATING
    document.evaluation_status = status.value
    logger.info(
        f"Updating document via document service: {document.input_key} with status: {status.value}"
    )
    return document_service.update_document(document)


def extract_document_from_event(event: Dict[str, Any]) -> Optional[Document]:
    """
    Extract document from Lambda event (state machine format)

    Args:
        event: Lambda event containing document data

    Returns:
        Document object or None if not found

    Raises:
        ValueError: If document cannot be extracted from event
    """
    try:
        # State machine format: event['document'] contains the document data
        document_data = event.get("document")

        if not document_data:
            raise ValueError("No document data found in event")

        # Get document from state machine format
        working_bucket = os.environ.get("WORKING_BUCKET")
        document = Document.load_document(document_data, working_bucket, logger)
        logger.info(
            f"Successfully loaded document with {len(document.pages)} pages and {len(document.sections)} sections"
        )
        return document
    except Exception as e:
        logger.error(f"Error extracting document from event: {str(e)}")
        raise ValueError(f"Failed to extract document from event: {str(e)}")


def load_baseline_document(document_key: str) -> Optional[Document]:
    """
    Load baseline document from S3

    Args:
        document_key: The document key to load

    Returns:
        Document object or None if no baseline is found

    Raises:
        ValueError: If baseline document cannot be loaded
    """
    try:
        logger.info(
            f"Loading baseline document for {document_key} from {BASELINE_BUCKET}"
        )

        expected_document = Document.from_s3(
            bucket=BASELINE_BUCKET, input_key=document_key
        )

        # Check if the expected document has meaningful data
        if not expected_document.sections:
            logger.warning(
                f"No baseline data found for {document_key} in {BASELINE_BUCKET} (empty document)"
            )
            return None

        # Baseline data exists and is valid
        logger.info(
            f"Successfully loaded expected (baseline) document with {len(expected_document.pages)} pages and {len(expected_document.sections)} sections"
        )
        return expected_document

    except Exception as e:
        logger.error(f"Error loading baseline document: {str(e)}")
        raise ValueError(f"Failed to load baseline document: {str(e)}")


def create_response(
    status_code: int, message: str, additional_data: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Create a standardized response

    Args:
        status_code: HTTP status code
        message: Response message
        additional_data: Optional additional data to include in response

    Returns:
        Formatted response dictionary
    """
    response = {
        "statusCode": status_code,
        "body": json.dumps({"message": message, **(additional_data or {})}),
    }
    return response


def handler(event, context):
    """
    Lambda function handler

    Args:
        event: Lambda event
        context: Lambda context

    Returns:
        Document in state machine format: {'document': document.serialize_document()}
    """
    actual_document = None
    start_time = time.time()
    working_bucket = os.environ.get("WORKING_BUCKET")

    # Failure-recording mode. The state machine routes EvaluationStep's Catch here
    # so a document that could not be evaluated keeps its (expensive, already
    # written) OCR / classification / extraction / assessment / summarization
    # output instead of being discarded, while still recording an honest
    # evaluation status. Without this a Lambda timeout left EvaluationStatus at
    # RUNNING forever and failed the whole execution.
    #
    # This branch does the minimum: load the document, stamp the status, return.
    # It must not do anything that could fail for the same reason evaluation did.
    if event.get("record_failure_only"):
        try:
            actual_document = extract_document_from_event(event)
            reason = event.get("failure_reason") or "Evaluation did not complete"
            # Match AWS error codes by EXACT parse of Step Functions'
            # ``{Error, Cause}`` envelope, not by substring on the
            # JSON-serialized blob — a "connection timeout" mentioned in
            # a ``Cause`` prose message must NOT re-classify a non-
            # timeout failure as TIMED_OUT (finding from #625 high
            # review, replacing the earlier substring check). The state
            # machine's Catch delivers ``error`` as either the dict
            # ``{"Error": "States.Timeout", "Cause": "..."}`` or a
            # string / other shape on non-standard invocations; the
            # dict form is the one AWS retries as a timeout.
            # Lambda's own timeout surfaces via ``Sandbox.Timedout`` and Step
            # Functions' state-level timeout via ``States.Timeout`` — both are
            # unambiguous, and both are the codes ``EvaluationStep``'s
            # single-attempt Retry policy matches on.
            #
            # ``Lambda.Unknown`` is Step Functions' CATCH-ALL for a Lambda
            # failure it cannot classify — a timeout is one cause, but so is
            # any other unhandled runtime fault. Treating the bare code as a
            # timeout would mislabel those, so it only counts when the
            # ``Cause`` prose actually names ``Sandbox.Timedout`` (which is how
            # AWS reports a function timeout wrapped in ``Lambda.Unknown``).
            # That keeps the exact-``Error`` parse for the unambiguous codes
            # while still catching the wrapped-timeout shape the earlier
            # substring check happened to cover.
            TIMEOUT_ERROR_CODES = {
                "Sandbox.Timedout",
                "States.Timeout",
            }
            raw_error = event.get("error")
            error_code = raw_error.get("Error") if isinstance(raw_error, dict) else None
            error_cause = (
                str(raw_error.get("Cause") or "") if isinstance(raw_error, dict) else ""
            )
            is_timeout = error_code in TIMEOUT_ERROR_CODES or (
                error_code == "Lambda.Unknown" and "Sandbox.Timedout" in error_cause
            )
            status = (
                EvaluationStatus.TIMED_OUT if is_timeout else EvaluationStatus.FAILED
            )
            logger.error(
                f"Recording evaluation failure for {actual_document.input_key}: "
                f"status={status.value} reason={reason}"
            )
            update_document_evaluation_status(actual_document, status)
            return {
                "document": actual_document.serialize_document(
                    working_bucket, "evaluation"
                )
            }
        except Exception as e:
            # Never let the failure-recorder itself break the workflow — the whole
            # point of this branch is that the document survives.
            logger.error(
                f"Could not record evaluation failure: {str(e)}", exc_info=True
            )
            # If we managed to load ``actual_document`` before the failure,
            # prefer its clean serialization — the raw ``event.get('document')``
            # carries state-level keys the state machine's Catch merged in
            # (``EvaluationError`` and friends) because the ASL parameter
            # ``document.$: $`` passes the WHOLE state, and $ at this point
            # is the doc dict itself with Catch-injected siblings alongside
            # its fields. Downstream ``$.document.<field>`` would otherwise
            # see the merged shape. See the state's Comment for why we can't
            # simply switch to ``$.document`` at the ASL level.
            if actual_document is not None:
                # Best-effort: try to stamp the status even on the outer-
                # except path. The whole point of this state is to record
                # that evaluation did not complete; returning a document
                # without stamping ``EvaluationStatus`` leaves it stuck
                # at RUNNING forever (finding from #625 review). Each
                # step is guarded so a further failure inside status
                # update or serialize still lets the document survive.
                try:
                    fallback_status = (
                        status if "status" in locals() else EvaluationStatus.FAILED
                    )
                    update_document_evaluation_status(actual_document, fallback_status)
                except Exception as status_err:
                    logger.error(
                        f"Failed to stamp fallback evaluation status: {status_err}"
                    )
                try:
                    return {
                        "document": actual_document.serialize_document(
                            working_bucket, "evaluation"
                        )
                    }
                except Exception:
                    pass
            raw = event.get("document") or {}
            if isinstance(raw, dict):
                raw = {
                    k: v
                    for k, v in raw.items()
                    if k
                    not in (
                        "EvaluationError",
                        "record_failure_only",
                        "failure_reason",
                        "error",
                        "execution_arn",
                    )
                }
            return {"document": raw}

    try:
        logger.info(f"Starting evaluation process: {json.dumps(event)}")

        # Extract document from event
        actual_document = extract_document_from_event(event)

        # Load configuration - use document's version if specified, otherwise use active version
        config_version = getattr(actual_document, "config_version", None)
        config_revision = getattr(actual_document, "config_revision", None)
        config = get_config(
            as_model=True, version=config_version, revision=config_revision
        )

        if config_version:
            logger.info(
                f"Using configuration version {config_version} for document {actual_document.id}"
            )
        else:
            logger.info(f"Using active configuration for document {actual_document.id}")

        if not config.evaluation.enabled:
            logger.info("Evaluation is disabled in configuration, skipping evaluation")
            # Return document unchanged
            return {
                "document": actual_document.serialize_document(
                    working_bucket, "evaluation"
                )
            }

        # Set document status to EVALUATING before processing
        actual_document.status = Status.EVALUATING
        document_service.update_document(actual_document)

        # Update document evaluation status to RUNNING
        update_document_evaluation_status(actual_document, EvaluationStatus.RUNNING)

        # Load baseline document
        expected_document = load_baseline_document(actual_document.input_key)

        # If no baseline document is found, update status and exit
        if not expected_document:
            # Update status in AppSync but keep using actual_document (don't overwrite)
            update_document_evaluation_status(
                actual_document, EvaluationStatus.NO_BASELINE
            )
            logger.info("Evaluation skipped - no baseline data available")
            return {
                "document": actual_document.serialize_document(
                    working_bucket, "evaluation"
                )
            }

        # Create evaluation service
        evaluation_service = evaluation.EvaluationService(config=config)

        # Snapshot pre-existing errors from earlier pipeline steps (e.g., page image creation)
        # so we only check for NEW errors introduced during evaluation
        pre_existing_errors = (
            set(actual_document.errors) if actual_document.errors else set()
        )
        if pre_existing_errors:
            logger.info(
                f"Document has {len(pre_existing_errors)} pre-existing error(s) from earlier pipeline steps (will not cause evaluation failure)"
            )

        # Run evaluation
        logger.info(f"Starting evaluation for document {actual_document.id}")
        evaluated_document = evaluation_service.evaluate_document(
            actual_document=actual_document,
            expected_document=expected_document,
            store_results=True,
        )

        # Check for evaluation-specific errors only (ignore pre-existing errors from earlier steps)
        new_errors = [
            e for e in evaluated_document.errors if e not in pre_existing_errors
        ]
        if new_errors:
            error_msg = f"Evaluation encountered errors: {new_errors}"
            logger.error(error_msg)
            # Update status in AppSync but keep using evaluated_document (don't overwrite)
            update_document_evaluation_status(
                evaluated_document, EvaluationStatus.FAILED
            )
            return {
                "document": evaluated_document.serialize_document(
                    working_bucket, "evaluation"
                )
            }

        # Save evaluation results to reporting bucket for analytics using the SaveReportingData Lambda
        try:
            logger.info(
                f"Saving evaluation results to {REPORTING_BUCKET} by calling Lambda {SAVE_REPORTING_FUNCTION_NAME}"
            )
            lambda_client = boto3.client("lambda")
            lambda_response = lambda_client.invoke(
                FunctionName=SAVE_REPORTING_FUNCTION_NAME,
                InvocationType="RequestResponse",
                Payload=json.dumps(
                    {
                        "document": evaluated_document.to_dict(),
                        "reporting_bucket": REPORTING_BUCKET,
                        "data_to_save": ["evaluation_results"],
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

        # Update document evaluation status to COMPLETED
        # Note: We discard the return value to keep using evaluated_document with correct URIs
        update_document_evaluation_status(
            evaluated_document, EvaluationStatus.COMPLETED
        )
        logger.info(
            f"Evaluation process completed successfully in {time.time() - start_time:.2f} seconds"
        )

        # Return document in state machine format
        return {
            "document": evaluated_document.serialize_document(
                working_bucket, "evaluation"
            )
        }

    except Exception as e:
        error_msg = f"Error in handler: {str(e)}"
        logger.error(error_msg)

        # Update document status to FAILED if we have the document
        if actual_document:
            try:
                # Update status in AppSync but keep using actual_document (don't overwrite)
                update_document_evaluation_status(
                    actual_document, EvaluationStatus.FAILED
                )
                return {
                    "document": actual_document.serialize_document(
                        working_bucket, "evaluation"
                    )
                }
            except Exception as update_error:
                logger.error(f"Failed to update evaluation status: {str(update_error)}")

        # Re-raise exception to let Step Functions handle the error
        raise
