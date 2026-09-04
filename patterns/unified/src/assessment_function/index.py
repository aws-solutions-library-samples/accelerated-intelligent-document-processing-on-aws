# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import copy
import json
import logging
import os
import time

from aws_xray_sdk.core import patch_all, xray_recorder
from idp_common import assessment, get_config, s3
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status
from idp_common.utils import (
    calculate_lambda_metering,
    merge_metering_data,
)

patch_all()

# Custom exception for throttling scenarios
class ThrottlingException(Exception):
    """Exception raised when throttling is detected in document processing results"""
    pass

# Throttling detection constants
THROTTLING_KEYWORDS = [
    "throttlingexception",
    "provisionedthroughputexceededexception", 
    "servicequotaexceededexception",
    "toomanyrequestsexception",
    "requestlimitexceeded",
    "too many tokens",
    "please wait before trying again",
    "reached max retries"
]

THROTTLING_EXCEPTIONS = [
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "ServiceQuotaExceededException", 
    "TooManyRequestsException",
    "RequestLimitExceeded"
]

# Configuration will be loaded in handler function

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

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

@xray_recorder.capture('assessment_function')
def handler(event, context):
    """
    Lambda handler for document assessment.
    This function assesses the confidence of extraction results for a document section
    using the Assessment service from the idp_common library.
    """
    start_time = time.time()  # Capture start time for Lambda metering
    logger.info(f"Starting assessment processing for event: {json.dumps(event, default=str)}")

    # Extract input from event - handle both compressed and uncompressed
    document_data = event.get('document', {})
    section_id = event.get('section_id')
    
    # Validate inputs
    if not document_data:
        raise ValueError("No document provided in event")
        
    if not section_id:
        raise ValueError("No section_id provided in event")
        
    # Convert document data to Document object - handle compression
    working_bucket = os.environ.get('WORKING_BUCKET')
    document = Document.load_document(document_data, working_bucket, logger)
    
    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(document, 'config_version', None)
    config_revision = getattr(document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    
    if config_version:
        logger.info(f"Using configuration version {config_version} for document {document.id}")
    else:
        logger.info(f"Using active configuration for document {document.id}")
    
    # Use default=str to handle Decimal and other non-serializable types
    logger.info(f"Config: {json.dumps(config.model_dump(), default=str)}")
    
    logger.info(f"Processing assessment for document {document.id}, section {section_id}")

    # X-Ray annotations
    xray_recorder.put_annotation('document_id', {document.id})
    xray_recorder.put_annotation('processing_stage', 'assessment')

    # Find the section we're processing
    section = None
    for s in document.sections:
        if s.section_id == section_id:
            section = s
            break
    
    if not section:
        raise ValueError(f"Section {section_id} not found in document")

    # Capture section index BEFORE any potential modifications to document.sections
    # This is needed for atomic section updates to DynamoDB
    section_index = next(i for i, s in enumerate(document.sections) if s.section_id == section_id)
    logger.info(f"Section {section_id} is at index {section_index} in the Sections array")

    # Assessment metering context (granular assessment has been retired; the
    # standalone AssessmentService batches large lists on its own).
    assessment_context = "Assessment"

    # Intelligent Assessment Skip: Check if extraction results already contain explainability_info
    if section.extraction_result_uri and section.extraction_result_uri.strip():
        try:
            logger.info(f"Checking extraction results for existing assessment: {section.extraction_result_uri}")
            extraction_data = s3.get_json_content(section.extraction_result_uri)
            
            # If explainability_info exists, assessment was already done
            if extraction_data.get('explainability_info'):
                logger.info(f"Skipping assessment for section {section_id} - extraction results already contain explainability_info")
                
                # Create section-specific document (same as normal processing) to match output format
                section_document = Document(
                    id=document.id,
                    input_bucket=document.input_bucket,
                    input_key=document.input_key,
                    output_bucket=document.output_bucket,
                    status=Status.ASSESSING,  # Keep status consistent with normal flow
                    initial_event_time=document.initial_event_time,
                    queued_time=document.queued_time,
                    start_time=document.start_time,
                    completion_time=document.completion_time,
                    workflow_execution_arn=document.workflow_execution_arn,
                    num_pages=len(section.page_ids),
                    summary_report_uri=document.summary_report_uri,
                    evaluation_status=document.evaluation_status,
                    evaluation_report_uri=document.evaluation_report_uri,
                    evaluation_results_uri=document.evaluation_results_uri,
                    errors=document.errors,
                    # Preserve the incoming metering (esp. the Extraction step's
                    # Bedrock token usage). Resetting to {} here dropped all
                    # extraction cost for agentic sections — which always write
                    # explainability_info during extraction and so hit this skip
                    # branch — making advanced modes look ~free vs simple. Deep-copy
                    # so later per-section metering merges never mutate the source.
                    metering=copy.deepcopy(document.metering) if document.metering else {},
                )
                
                # Add only the pages needed for this section
                for page_id in section.page_ids:
                    if page_id in document.pages:
                        section_document.pages[page_id] = document.pages[page_id]
                
                # Add only the section being processed (preserve existing data)
                section_document.sections = [section]
                
                # Add Lambda metering for assessment skip execution with dynamic context
                try:
                    lambda_metering = calculate_lambda_metering(assessment_context, context, start_time)
                    section_document.metering = merge_metering_data(section_document.metering, lambda_metering)
                except Exception as e:
                    logger.warning(f"Failed to add Lambda metering for assessment skip: {str(e)}")
                
                # Return consistent format for Map state collation
                response = {
                    "section_id": section_id, 
                    "document": section_document.serialize_document(working_bucket, f"assessment_skip_{section_id}", logger)
                }
                
                logger.info(f"Assessment skipped - Response: {json.dumps(response, default=str)}")
                return response
            else:
                logger.info(f"Assessment needed for section {section_id} - no explainability_info found in extraction results")
        except Exception as e:
            logger.warning(f"Error checking extraction results for assessment skip: {e}")
            # Continue with normal assessment if check fails

    # Normal assessment processing
    document.status = Status.ASSESSING

    # Update document status to ASSESSING using lightweight status-only update
    # This reduces DynamoDB WCU consumption by ~94% (~500 bytes vs ~100KB)
    # Previously we created a 'shell' document, but now we use update_document_status
    document_service = create_document_service()
    logger.info(f"Updating document status to ASSESSING (lightweight update) for document {document.input_key}")
    try:
        status_result = document_service.update_document_status(
            document_id=document.input_key,
            status=Status.ASSESSING,
            workflow_execution_arn=document.workflow_execution_arn,
        )
        logger.info(f"Status update result: {json.dumps(status_result, default=str)[:500]}")
    except Exception as e:
        logger.error(f"Failed to update document status: {str(e)}", exc_info=True)

    # Standalone assessment service (batches large lists via
    # extraction.confidence.list_batch_size; granular assessment is retired).
    assessment_service = assessment.AssessmentService(config=config)
    logger.info("Using assessment service")

    # Process the document section for assessment
    t0 = time.time()
    logger.info(f"Starting assessment for section {section_id}")

    # Wall-clock deadline for the self-healing ladder (1.5): convert the Lambda's
    # remaining-time budget into an absolute epoch so the assessment escalation
    # ladder can stop before a hard task timeout instead of losing the section.
    deadline_epoch = None
    try:
        deadline_epoch = time.time() + (context.get_remaining_time_in_millis() / 1000.0)
    except Exception:  # noqa: BLE001 - context may be absent in local/test runs
        deadline_epoch = None

    try:
        updated_document = assessment_service.process_document_section(
            document, section_id, deadline_epoch=deadline_epoch
        )
        t1 = time.time()
        logger.info(f"Total assessment time: {t1-t0:.2f} seconds")

        # Check for throttling errors in document status and errors field
        has_throttling, throttling_error = check_document_for_throttling_errors(updated_document)
        if has_throttling:
            logger.error(f"Throttling error detected in document errors: {throttling_error}")
            logger.error("Raising ThrottlingException to trigger Step Functions retry")
            raise ThrottlingException(f"Throttling detected in document processing: {throttling_error}")
        
    except Exception as e:
        t1 = time.time()
        logger.error(f"Assessment failed after {t1-t0:.2f} seconds: {str(e)}")
        
        # Check if this is a throttling exception that should trigger retry
        if is_throttling_exception(e):
            logger.error(f"Throttling exception detected: {type(e).__name__}. This will trigger state machine retry.")
            # Re-raise to trigger state machine retry (status already updated to ASSESSING)
            raise
        else:
            logger.error(f"Non-throttling exception: {type(e).__name__}. Marking document as failed.")
            # Set document status to failed for non-throttling exceptions
            updated_document = document
            updated_document.status = Status.FAILED
            updated_document.errors.append(str(e))

    # (The optional post-assessment validation checks — previously gated behind
    # the removed `confidence.validation_enabled` toggle — are no longer run
    # automatically. The AssessmentValidator class remains available for
    # programmatic/offline use.)

    # Add Lambda metering for successful assessment execution with dynamic context
    try:
        lambda_metering = calculate_lambda_metering(assessment_context, context, start_time)
        updated_document.metering = merge_metering_data(updated_document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for assessment: {str(e)}")

    # Update the section in DynamoDB for immediate UI visibility
    # This allows the UI to show assessment results (confidence alerts) as they complete
    try:
        # Use section_index captured at start (before any potential modifications)
        updated_section = next(s for s in updated_document.sections if s.section_id == section_id)
        original_input_key = document.input_key
        logger.info(f"Persisting assessment results for section {section_id} (index {section_index}) to DynamoDB for document {original_input_key}")
        result = document_service.update_document_section(
            document_id=original_input_key,
            section_index=section_index,
            section=updated_section,
        )
        logger.info(f"Section update result: {json.dumps(result, default=str)[:500]}")
    except Exception as e:
        logger.error(f"Failed to update section in DynamoDB: {str(e)}", exc_info=True)

    # Prepare output with automatic compression if needed
    result = {
        'document': updated_document.serialize_document(working_bucket, f"assessment_{section_id}", logger),
        'section_id': section_id
    }
    
    logger.info("Assessment processing completed")
    return result
