# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OCR function that processes PDFs and extracts text using AWS Textract.
Uses the idp_common.ocr package for OCR functionality.
"""

import json
import logging
import os
import time

import boto3
from aws_xray_sdk.core import patch_all, xray_recorder
from idp_common import get_config, metrics, ocr
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Page, Status
from idp_common.utils import calculate_lambda_metering, merge_metering_data

patch_all()

# Custom exception for throttling scenarios - re-raised so Step Functions can match
# the error name in its Retry configuration and retry the OCR step appropriately.
class ThrottlingException(Exception):
    """Exception raised when throttling is detected in OCR processing results."""
    pass

# Throttling detection constants (shared pattern with assessment_function)
THROTTLING_KEYWORDS = [
    "throttlingexception",
    "provisionedthroughputexceededexception",
    "servicequotaexceededexception",
    "toomanyrequestsexception",
    "requestlimitexceeded",
    "too many tokens",
    "please wait before trying again",
    "reached max retries",
    "provisioned rate exceeded",
]

THROTTLING_EXCEPTIONS = [
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "ServiceQuotaExceededException",
    "TooManyRequestsException",
    "RequestLimitExceeded",
]

# Configuration will be loaded in handler function

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

# Initialize settings
region = os.environ['AWS_REGION']
METRIC_NAMESPACE = os.environ.get('METRIC_NAMESPACE')
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 20))


def is_throttling_exception(exception):
    """
    Check if an exception is related to throttling.

    Args:
        exception: The exception to check

    Returns:
        bool: True if the exception is throttling-related, False otherwise
    """
    from botocore.exceptions import ClientError

    if isinstance(exception, ClientError):
        error_code = exception.response.get('Error', {}).get('Code', '')
        return error_code in THROTTLING_EXCEPTIONS

    exception_name = type(exception).__name__
    exception_message = str(exception).lower()

    return (
        exception_name in THROTTLING_EXCEPTIONS or
        any(keyword in exception_message for keyword in THROTTLING_KEYWORDS)
    )


def check_document_for_throttling_errors(document):
    """
    Check if a document has throttling errors in its errors field.

    Args:
        document: The document object to check

    Returns:
        tuple: (has_throttling_errors: bool, first_throttling_error: str or None)
    """
    if document.status != Status.FAILED or not document.errors:
        return False, None

    for error_msg in document.errors:
        error_lower = str(error_msg).lower()
        if any(keyword in error_lower for keyword in THROTTLING_KEYWORDS):
            return True, error_msg

    return False, None


def discover_existing_ocr_pages(output_bucket, input_key):
    """
    Discover OCR page results that already exist in S3 from a previous (throttled) attempt.

    On Step Functions retry, the document object is reloaded from the original compressed
    state with pages={}, losing all progress from the failed attempt. This function scans
    S3 for existing page results so the service can skip already-completed pages.

    Expected S3 layout per page:
      {input_key}/pages/{page_id}/image.jpg
      {input_key}/pages/{page_id}/rawText.json
      {input_key}/pages/{page_id}/result.json
      {input_key}/pages/{page_id}/textConfidence.json

    A page is considered complete if it has all 4 files (image, rawText, result, textConfidence).

    Args:
        output_bucket: S3 bucket where OCR results are stored
        input_key: Document input key (S3 prefix for pages)

    Returns:
        dict: Mapping of page_id -> Page object for completed pages, empty dict if none found
    """
    s3_client = boto3.client("s3")
    prefix = f"{input_key}/pages/"
    completed_pages = {}

    try:
        # Single list-objects call to discover all existing page files
        paginator = s3_client.get_paginator("list_objects_v2")
        page_files = {}  # page_id -> set of file types found

        for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                # Parse: {input_key}/pages/{page_id}/{filename}
                parts = key[len(prefix):].split("/", 1)
                if len(parts) == 2:
                    page_id = parts[0]
                    filename = parts[1]
                    if page_id not in page_files:
                        page_files[page_id] = {}
                    page_files[page_id][filename] = f"s3://{output_bucket}/{key}"

        # A page is complete if it has all required files
        required_files = {"rawText.json", "result.json", "textConfidence.json"}
        for page_id, files in page_files.items():
            # Check for required files (image can be .jpg, .png, etc.)
            image_uri = None
            for fname, uri in files.items():
                if fname.startswith("image."):
                    image_uri = uri
                    break

            if image_uri and required_files.issubset(files.keys()):
                completed_pages[page_id] = Page(
                    page_id=page_id,
                    image_uri=image_uri,
                    raw_text_uri=files["rawText.json"],
                    parsed_text_uri=files["result.json"],
                    text_confidence_uri=files["textConfidence.json"],
                    # pageData.json is optional — older pages predate it
                    ocr_page_data_uri=files.get("pageData.json"),
                )

    except Exception as e:
        logger.warning(f"Failed to discover existing OCR pages from S3: {e}")
        # Non-fatal — proceed with full OCR processing

    return completed_pages

@xray_recorder.capture('ocr_function')
def handler(event, context): 
    """
    Lambda handler for OCR processing.
    """
    start_time = time.time()  # Capture start time for Lambda metering
    logger.info(f"Event: {json.dumps(event)}")
    
    # Get document from event - handle both compressed and uncompressed
    working_bucket = os.environ.get('WORKING_BUCKET')
    document = Document.load_document(event["document"], working_bucket, logger)
    
    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {document.id}, input_key: {document.input_key}")
    logger.info(f"Document buckets - input_bucket: {document.input_bucket}, output_bucket: {document.output_bucket}")
    logger.info(f"Document status: {document.status}, num_pages: {document.num_pages}")
    logger.info(f"Document pages count: {len(document.pages)}, sections count: {len(document.sections)}")
    logger.info(f"Full document content: {json.dumps(document.to_dict(), default=str)}")
    
    # X-Ray annotations
    xray_recorder.put_annotation('document_id', {document.id})
    xray_recorder.put_annotation('processing_stage', 'ocr')

    # Intelligent OCR detection: Skip if pages already have OCR data
    pages_with_ocr = 0
    for page in document.pages.values():
        if page.image_uri and page.raw_text_uri:
            pages_with_ocr += 1
    
    if pages_with_ocr == len(document.pages) and len(document.pages) > 0:
        logger.info(f"Skipping OCR processing for document {document.id} - all {len(document.pages)} pages already have OCR data")
        
        # Ensure document has the expected execution ARN
        document.workflow_execution_arn = event.get("execution_arn")
        
        # Update document execution ARN for tracking
        if document.status == Status.QUEUED:
            document_service = create_document_service()
            logger.info("Updating document execution ARN for OCR skip")
            document_service.update_document(document)
        
        # Add Lambda metering for OCR skip execution
        try:
            lambda_metering = calculate_lambda_metering("OCR", context, start_time)
            document.metering = merge_metering_data(document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering for OCR skip: {str(e)}")
        
        # Prepare output with existing document data
        working_bucket = os.environ.get('WORKING_BUCKET')
        response = {
            "document": document.serialize_document(working_bucket, "ocr_skip", logger)
        }
        
        logger.info(f"OCR skipped - Response: {json.dumps(response, default=str)}")
        return response
    
    # Normal OCR processing
    # Update document status to OCR and update in AppSync
    document.status = Status.OCR
    document.workflow_execution_arn = event.get("execution_arn")
    document_service = create_document_service()
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)
    
    t0 = time.time()
    
    # Load configuration and initialize the OCR service using new simplified pattern
    # Use document's version if specified, otherwise use active version
    config_version = getattr(document, 'config_version', None)
    config_revision = getattr(document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    backend = config.ocr.backend
    
    logger.info(f"Initializing OCR with backend: {backend}")
    service = ocr.OcrService(
        region=region,
        config=config,
        backend=backend
    )
    
    # Retry-safe: discover OCR pages from previous (throttled) attempts in S3.
    # On Step Functions retry, the document is reloaded from compressed state with pages={},
    # losing progress. Pre-populating document.pages lets the service skip completed pages.
    if not document.pages:
        existing_pages = discover_existing_ocr_pages(
            document.output_bucket, document.input_key
        )
        if existing_pages:
            document.pages = existing_pages
            # Clear any stale errors from previous failed attempt
            document.errors = []
            logger.info(
                f"Retry-safe OCR: recovered {len(existing_pages)} completed pages from S3, "
                f"only failed pages will be re-processed"
            )

    # Process the document - the service will read the PDF content directly
    document = service.process_document(document)
    
    # Check if document processing failed
    if document.status == Status.FAILED:
        error_message = f"OCR processing failed for document {document.id}"
        logger.error(error_message)
        
        # Check if failure was due to throttling - if so, raise ThrottlingException
        # so Step Functions can match it in the Retry configuration and retry the step.
        # Do NOT update document status to FAILED for throttling — Step Functions will retry
        # and the document should remain in OCR status until retries are exhausted.
        has_throttling, throttling_error = check_document_for_throttling_errors(document)
        if has_throttling:
            logger.error(f"Throttling error detected in OCR errors: {throttling_error}")
            logger.error("Raising ThrottlingException to trigger Step Functions retry (NOT marking document as FAILED)")
            # Emit CloudWatch metric for OCR throttling visibility
            metrics.put_metric('OCRThrottles', 1)
            raise ThrottlingException(
                f"Throttling detected during OCR processing: {throttling_error}"
            )
        
        # Non-throttling failure - update status to FAILED in AppSync and raise
        document_service.update_document(document)
        metrics.put_metric('OCRNonRetryableErrors', 1)
        raise Exception(error_message)
    
    t1 = time.time()
    logger.info(f"Total OCR processing time: {t1-t0:.2f} seconds")
    
    # Add Lambda metering for successful OCR execution
    try:
        lambda_metering = calculate_lambda_metering("OCR", context, start_time)
        document.metering = merge_metering_data(document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for OCR: {str(e)}")
    
    # Prepare output with automatic compression if needed
    working_bucket = os.environ.get('WORKING_BUCKET')
    response = {
        "document": document.serialize_document(working_bucket, "ocr", logger)
    }
    
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
