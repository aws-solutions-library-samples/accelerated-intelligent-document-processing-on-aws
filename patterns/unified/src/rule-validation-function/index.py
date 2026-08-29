# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to validate document content using the RuleValidationService from idp_common.
"""
import json
import os
import logging
import time

# Import the RuleValidationService from idp_common
from idp_common import get_config, rule_validation, metrics
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import calculate_lambda_metering, merge_metering_data

# X-Ray tracing
from aws_xray_sdk.core import xray_recorder
# from idp_common.rule_validation import RuleValidationService, RuleValidationResult



# Configuration will be loaded in handler function
region = os.environ['AWS_REGION']

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

@xray_recorder.capture('rule_validation_function')
def handler(event, context):
    """
    Process a single section of a document for rule validation
    """
    start_time = time.time()  # Capture start time for Lambda metering
    logger.info(f"Event: {json.dumps(event)}")

    # For Map state, we get just one section from the document
    # Extract the document and section from the event - handle both compressed and uncompressed
    working_bucket = os.environ.get('WORKING_BUCKET')
    full_document = Document.load_document(event.get("document", {}), working_bucket, logger)
    
    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(full_document, 'config_version', None)
    config_revision = getattr(full_document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)
    logger.info(f"Config: {json.dumps(config.model_dump(), default=str)}")
    
    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {full_document.id}, input_key: {full_document.input_key}")
    logger.info(f"Document buckets - input_bucket: {full_document.input_bucket}, output_bucket: {full_document.output_bucket}")
    logger.info(f"Document status: {full_document.status}, num_pages: {full_document.num_pages}")
    logger.info(f"Document pages count: {len(full_document.pages)}, sections count: {len(full_document.sections)}")
    logger.info(f"Full document content: {json.dumps(full_document.to_dict(), default=str)}")

    # X-Ray annotations
    xray_recorder.put_annotation('document_id', full_document.id)
    xray_recorder.put_annotation('processing_stage', 'rule_validation')
    
    # Get the section ID directly from the Map state input
    # Now using the simplified array of section IDs format
    section_id = event.get("section_id")
    
    if not section_id:
        raise ValueError("No section_id found in event")
    
    # Look up the full section from the decompressed document
    section = None
    for doc_section in full_document.sections:
        if doc_section.section_id == section_id:
            section = doc_section
            break
    
    if not section:
        raise ValueError(f"Section {section_id} not found in document")
    
    logger.info(f"Processing section {section_id} with {len(section.page_ids)} pages")
    
    # Capture section index BEFORE modifying full_document.sections
    # This is needed for atomic section updates to DynamoDB
    section_index = next(i for i, s in enumerate(full_document.sections) if s.section_id == section_id)
    logger.info(f"Section {section_id} is at index {section_index} in the Sections array")
    
    # Intelligent Rule Validation detection: Skip if section already has rule validation results
    if (full_document.rule_validation_result and 
        full_document.rule_validation_result.section_results):
        # Check if this specific section was already processed
        section_already_processed = any(
            sr.get("section_id") == section_id 
            for sr in full_document.rule_validation_result.section_results
        )
        if section_already_processed:
            logger.info(f"Skipping rule validation for section {section_id} - already has rule validation results")
            
            # Add Lambda metering for rule validation skip execution
            try:
                lambda_metering = calculate_lambda_metering("RuleValidation", context, start_time)
                full_document.metering = merge_metering_data(full_document.metering, lambda_metering)
            except Exception as e:
                logger.warning(f"Failed to add Lambda metering for rule validation skip: {str(e)}")
            
            # Return the section without processing
            response = {
                "section_id": section_id,
                "document": full_document.serialize_document(working_bucket, f"rule_validation_skip_{section_id}", logger)
            }
            
            logger.info(f"Rule validation skipped - Response: {json.dumps(response, default=str)}")
            return response
    
    logger.info(f"Processing section {section_id} - no existing rule validation results found, proceeding with processing")
    
    # Update document status to RULE_VALIDATION using lightweight status-only update
    # This reduces DynamoDB WCU consumption by ~94% (~500 bytes vs ~100KB)
    document_service = create_document_service()
    logger.info(f"Updating document status to RULE_VALIDATION (lightweight update) for document {full_document.input_key}")
    try:
        status_result = document_service.update_document_status(
            document_id=full_document.input_key,
            status=Status.RULE_VALIDATION,
            workflow_execution_arn=full_document.workflow_execution_arn,
        )
        logger.info(f"Status update result: {json.dumps(status_result, default=str)[:500]}")
    except Exception as e:
        logger.error(f"Failed to update document status: {str(e)}", exc_info=True)
    full_document.status = Status.RULE_VALIDATION
       
    # Create a section-specific document by modifying the original document
    section_document = full_document
    section_document.sections = [section]
    section_document.metering = {}
    
    # Filter to keep only the pages needed for this section
    needed_pages = {}
    for page_id in section.page_ids:
        if page_id in full_document.pages:
            needed_pages[page_id] = full_document.pages[page_id]
    section_document.pages = needed_pages
    
    # Initialize the rule validation service
    rule_validation_service = rule_validation.RuleValidationService(
        region=region,
        config=config
    )
    
    # Track metrics
    metrics.put_metric('InputDocuments', 1)
    metrics.put_metric('InputDocumentPages', len(section.page_ids))
    
    # Process the section in our focused document
    t0 = time.time()
    section_document = rule_validation_service.validate_document(section_document)
    t1 = time.time()
    logger.info(f"Total rule validation time: {t1-t0:.2f} seconds")
    
    # Check if document processing failed
    if section_document.status == Status.FAILED:
        error_message = f"Rule validation failed for document {section_document.id}, section {section_id}"
        logger.error(error_message)
        raise Exception(error_message)
    
    # Add Lambda metering for successful rule validation execution
    try:
        lambda_metering = calculate_lambda_metering("RuleValidation", context, start_time)
        section_document.metering = merge_metering_data(section_document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for rule validation: {str(e)}")
    
    # Prepare output with automatic compression if needed
    response = {
        "section_id": section_id,
        "document": section_document.serialize_document(working_bucket, f"rule_validation_{section_id}", logger)
    }
    
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
