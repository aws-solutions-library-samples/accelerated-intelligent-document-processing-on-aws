# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Classification function that processes documents and classifies them using LLMs.
Uses the idp_common.classification package for classification functionality.
"""

import json
import logging
import os
import time

from idp_common import classification, metrics, get_config
from idp_common.classification import apply_forced_document_class
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import calculate_lambda_metering, merge_metering_data
from aws_xray_sdk.core import xray_recorder, patch_all

patch_all()

# Configuration will be loaded in handler function
region = os.environ['AWS_REGION']
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 20))

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

@xray_recorder.capture('classification_function')
def handler(event, context):
    """
    Lambda handler for document classification.
    """
    start_time = time.time()  # Capture start time for Lambda metering
    logger.info(f"Event: {json.dumps(event)}")
    
    # Extract document from the OCR result - handle both compressed and uncompressed
    working_bucket = os.environ.get('WORKING_BUCKET')
    document = Document.load_document(event["OCRResult"]["document"], working_bucket, logger)
    
    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(document, 'config_version', None)
    config_revision = getattr(document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    # Use default=str to handle Decimal and other non-serializable types
    logger.info(f"Config: {json.dumps(config.model_dump(), default=str)}, version name: {config_version}")
    
    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {document.id}, input_key: {document.input_key}")
    logger.info(f"Document buckets - input_bucket: {document.input_bucket}, output_bucket: {document.output_bucket}")
    logger.info(f"Document status: {document.status}, num_pages: {document.num_pages}")
    logger.info(f"Document pages count: {len(document.pages)}, sections count: {len(document.sections)}")
    logger.info(f"Full document content: {json.dumps(document.to_dict(), default=str)}")

    # X-Ray annotations
    xray_recorder.put_annotation('document_id', {document.id})
    xray_recorder.put_annotation('processing_stage', 'classification')
    
    # A reviewer corrected this document's class and asked for it to be
    # re-extracted. Stamping the class onto every page routes into the existing
    # skip below, so extraction runs against the class they chose.
    #
    # This has to override the model rather than seed it: the request means "the
    # class you picked is wrong", so classifying again would re-derive the same
    # wrong answer and silently discard the correction. That is exactly the bug
    # this exists to fix — the class was previously pinned only in the test set's
    # baseline, which the harvest then overwrote with the pipeline's own guess.
    apply_forced_document_class(document)

    # Intelligent Classification detection: Skip if pages already have classifications
    pages_with_classification = 0
    for page in document.pages.values():
        if page.classification and page.classification.strip():
            pages_with_classification += 1
    
    if pages_with_classification == len(document.pages) and len(document.pages) > 0:
        logger.info(f"Skipping classification for document {document.id} - all {len(document.pages)} pages already classified")
        
        # Ensure document has the expected execution ARN
        document.workflow_execution_arn = event.get("execution_arn")
        
        # Update document execution ARN for tracking
        document_service = create_document_service()
        logger.info(f"Updating document execution ARN for classification skip")
        document_service.update_document(document)
        
        # Add Lambda metering for classification skip execution
        try:
            lambda_metering = calculate_lambda_metering("Classification", context, start_time)
            document.metering = merge_metering_data(document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering for classification skip: {str(e)}")
        
        # Prepare output with existing document data
        response = {
            "document": document.serialize_document(working_bucket, "classification_skip", logger)
        }
        
        logger.info(f"Classification skipped - Response: {json.dumps(response, default=str)}")
        return response
    
    # Normal classification processing
    # Update document status to CLASSIFYING
    document.status = Status.CLASSIFYING
    document.workflow_execution_arn = event.get("execution_arn")
    document_service = create_document_service()
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)
    
    if not document.pages:
        error_message = "Document has no pages to classify"
        logger.error(error_message)
        document.status = Status.FAILED
        document.errors.append(error_message)
    
    t0 = time.time()
    
    # Track pages processed for metrics
    total_pages = len(document.pages)
    metrics.put_metric('BedrockRequestsTotal', total_pages)
    
    # Initialize classification service with DynamoDB caching
    cache_table = os.environ.get('TRACKING_TABLE')
    service = classification.ClassificationService(
        region=region,
        max_workers=MAX_WORKERS,
        config=config,
        cache_table=cache_table
    )
    
    # Classify the document - the service will update the Document directly
    document = service.classify_document(document)
    
    # Check if document processing failed or has pages that failed to classify
    failed_page_exceptions = None
    primary_exception = None
    
    # Check for failed page exceptions in metadata
    if document.metadata and "failed_page_exceptions" in document.metadata:
        failed_page_exceptions = document.metadata["failed_page_exceptions"]
        primary_exception = document.metadata.get("primary_exception")
        
        # Log details about failed pages
        logger.error(f"Document {document.id} has {len(failed_page_exceptions)} pages that failed to classify:")
        for page_id, exc_info in failed_page_exceptions.items():
            logger.error(f"  Page {page_id}: {exc_info['exception_type']} - {exc_info['exception_message']}")
    
    # Check if document processing completely failed or has critical page failures
    if document.status == Status.FAILED or failed_page_exceptions:
        error_message = f"Classification failed for document {document.id}"
        if failed_page_exceptions:
            error_message += f" - {len(failed_page_exceptions)} pages failed to classify"
        
        logger.error(error_message)
        # Update document status in AppSync before raising exception
        document_service.update_document(document)
        
        # Raise the original exception type if available, otherwise raise generic exception
        if primary_exception:
            logger.error(f"Re-raising original exception: {type(primary_exception).__name__}")
            raise primary_exception
        else:
            raise Exception(error_message)
    
    t1 = time.time()
    logger.info(f"Time taken for classification: {t1-t0:.2f} seconds")
    
    # Add Lambda metering for successful classification execution
    try:
        lambda_metering = calculate_lambda_metering("Classification", context, start_time)
        document.metering = merge_metering_data(document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for classification: {str(e)}")
    
    # Persist classifications and sections to DynamoDB for immediate UI visibility
    # This allows the UI to show document classes and empty sections right after classification
    logger.info("Persisting classification results to DynamoDB for UI visibility")
    document_service.update_document(document)
    
    # Prepare output with automatic compression if needed
    response = {
        "document": document.serialize_document(working_bucket, "classification", logger)
    }
    
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
