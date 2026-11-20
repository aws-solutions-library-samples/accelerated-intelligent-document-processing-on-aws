# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to summarize document content using the SummarizationService from idp_common.
"""
import copy
import json
import os
import logging
import time

# Import the SummarizationService from idp_common
from idp_common import get_config, summarization
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import (
    calculate_lambda_metering,
    failure_metering,
    merge_metering_data,
)
from idp_common.utils.transient_errors import is_transient_error, raise_if_transient

# Configuration will be loaded in handler function

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

def handler(event, context):
    """Summarize one document. See ``_handle``.

    Transient causes are re-raised as ``TransientError``, the name
    SummarizationStep's retrier lists beside the modeled throttling codes, so a
    read timeout or a bare HTTP 5xx is retried the way a modeled
    ``ThrottlingException`` already was. The summarization service raises one
    itself when a section's model call fails transiently, rather than returning
    a FAILED document this handler could only report as an untyped error. Hard
    errors keep their own name and are not retried.
    """
    try:
        return _handle(event, context)
    except Exception as e:
        raise_if_transient(e, where="summarization")
        raise


def _handle(event, context):
    """
    Lambda handler for document summarization using the SummarizationService.
    
    Args:
        event: Lambda event containing document data and configuration
        context: Lambda context
        
    Returns:
        Dictionary with the summarization result
    """
    logger.info(f"Processing event: {json.dumps(event)}")
    start_time = time.time()
    
    try:
        # Get required parameters - handle both compressed and uncompressed
        document_data = event.get('document', {})
        
        if not document_data:
            raise ValueError("No document data provided")
        
        # Convert data to Document object - handle compression
        working_bucket = os.environ.get('WORKING_BUCKET')
        document = Document.load_document(document_data, working_bucket, logger)
        
        # Sync HITL completion status from DynamoDB if HITL was triggered
        if document.hitl_metadata:
            try:
                import boto3
                tracking_table = os.environ.get('TRACKING_TABLE_NAME')
                if tracking_table:
                    dynamodb = boto3.resource('dynamodb')
                    table = dynamodb.Table(tracking_table)
                    response = table.get_item(Key={'PK': f'doc#{document.input_key}', 'SK': 'none'})
                    if 'Item' in response:
                        hitl_completed = response['Item'].get('HITLCompleted', False)
                        if hitl_completed and document.hitl_metadata:
                            # Update all hitl_metadata entries with completed status
                            for hitl_meta in document.hitl_metadata:
                                hitl_meta.hitl_completed = True
                            logger.info(f"Synced HITL completion status from DynamoDB: {hitl_completed}")
            except Exception as e:
                logger.warning(f"Failed to sync Review Status from DynamoDB: {str(e)}")
        
        # Update document status to SUMMARIZING
        document.status = Status.SUMMARIZING
        document_service = create_document_service()
        logger.info(f"Updating document status to {document.status}")
        document_service.update_document(document)
        # The document as this attempt started it, before the service mutates it.
        # A transient failure writes this back with the attempt's usage added.
        attempt_start = copy.deepcopy(document)
        
        # Load configuration - use document's version if specified, otherwise use active version
        config_version = getattr(document, 'config_version', None)
        config_revision = getattr(document, 'config_revision', None)
        config = get_config(as_model=True, version=config_version, revision=config_revision)
        
        if config_version:
            logger.info(f"Using configuration version {config_version} for document {document.id}")
        else:
            logger.info(f"Using active configuration for document {document.id}")
        
        # Create the summarization service
        summarization_service = summarization.SummarizationService(
            config=config
        )        
        # Process the document using the service
        logger.info(f"Processing document with SummarizationService, document ID: {document.id}")
        processed_document = summarization_service.process_document(document)
        
        # Check if document processing failed
        if processed_document.status == Status.FAILED:
            # Surface the underlying cause (e.g. a Bedrock ResourceNotFoundException
            # for a retired model, a context-window overflow, etc.) instead of a
            # bare "Summarization failed" wrapper. The real error is otherwise only
            # visible in the Lambda logs, which sends failure triage down the wrong
            # path (GitHub #504). SummarizationService records the cause on
            # document.errors / status_reason.
            detail = getattr(processed_document, "status_reason", None)
            if not detail and processed_document.errors:
                detail = "; ".join(str(e) for e in processed_document.errors)
            error_message = (
                f"Summarization failed for document {processed_document.id}: {detail}"
                if detail
                else f"Summarization failed for document {processed_document.id}"
            )
            logger.error(error_message)
            raise Exception(error_message)
        
        # Log the result
        if hasattr(processed_document, 'summary_report_uri') and processed_document.summary_report_uri:
            logger.info(f"Document summarization successful, report URI: {processed_document.summary_report_uri}")
        else:
            logger.warning("Document summarization completed but no summary report URI was set")
        
        # Add Lambda metering for successful summarization execution
        try:
            lambda_metering = calculate_lambda_metering("Summarization", context, start_time)
            processed_document.metering = merge_metering_data(processed_document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering for summarization: {str(e)}")
        
        # Prepare output with automatic compression if needed
        return {
            'document': processed_document.serialize_document(working_bucket, "summarization", logger),
        }
        
    except Exception as e:
        logger.error(f"Error in summarization function: {str(e)}", exc_info=True)

        if is_transient_error(e):
            # Step Functions retries the step, so the document is not FAILED: it
            # keeps its SUMMARIZING status, and only a final failure is persisted.
            # The model calls the attempt completed were billed, so their usage
            # (attached to the error by the service) goes onto the tracking
            # record, which is what an execution that ends unsuccessfully
            # reports (#2122).
            if 'attempt_start' in locals():
                _record_transient_attempt(attempt_start, e)
            raise

        # Update document status to FAILED if we have a document object
        try:
            if 'document' in locals() and document:
                document.status = Status.FAILED
                document.status_reason = str(e)
                document_service = create_document_service()
                logger.info(f"Updating document status to {document.status} due to error")
                document_service.update_document(document)
        except Exception as status_error:
            logger.error(f"Failed to update document status: {str(status_error)}", exc_info=True)
            
        raise e


def _record_transient_attempt(attempt_start, error):
    """Write the attempt's usage to the tracking record, leaving its status alone.

    ``attempt_start`` is the document as the attempt wrote it at its start, with
    status SUMMARIZING. A failure here is logged rather than raised, so it cannot
    replace the transient error Step Functions retries on.
    """
    attempt_metering = failure_metering(error)
    if not attempt_metering:
        return
    try:
        attempt_start.metering = merge_metering_data(
            copy.deepcopy(attempt_start.metering), attempt_metering
        )
        logger.info(
            "Recording the usage of the summarization attempt that failed with a "
            "transient error; the document stays %s for the retry",
            attempt_start.status,
        )
        create_document_service().update_document(attempt_start)
    except Exception as record_error:
        logger.error(
            f"Failed to record the failed attempt's usage: {record_error}",
            exc_info=True,
        )
