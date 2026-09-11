# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda function to consolidate rule validation results using the RuleValidationOrchestratorService from idp_common.
"""
import json
import os
import logging
import time

# Import the RuleValidationOrchestratorService from idp_common
from idp_common import get_config, rule_validation
from idp_common.models import Document, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import calculate_lambda_metering, merge_metering_data

# X-Ray tracing
from aws_xray_sdk.core import xray_recorder

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

@xray_recorder.capture("rule_validation_orchestrator_handler")
def handler(event, context):
    """
    Lambda handler for rule validation consolidation.
    
    Args:
        event: Lambda event containing:
            - Result.document: Base document from ProcessResults
            - RuleValidationResults: Array of section results from Map state
        context: Lambda context
        
    Returns:
        Updated document with consolidated rule validation results
    """
    start_time = time.time()
    
    try:
        logger.info(f"Starting rule validation consolidation for event: {json.dumps(event, default=str)}")
        
        # Get working bucket for loading compressed states
        working_bucket = os.environ.get('WORKING_BUCKET')
        
        # Get base document from PolicyClassificationResult (has rule_validation_result with matched_policy_types)
        # Fall back to Result.document if PolicyClassificationResult not available
        policy_classification_result = event.get("PolicyClassificationResult", {})
        if policy_classification_result.get("document"):
            base_document_data = policy_classification_result.get("document", {})
            logger.info("Loading document from PolicyClassificationResult")
        else:
            base_document_data = event.get("Result", {}).get("document", {})
            logger.info("Loading document from Result (PolicyClassificationResult not available)")
        document = Document.load_document(base_document_data, working_bucket, logger)
        
        logger.info(f"Processing rule validation consolidation for document: {document.id}")
        logger.info(f"Document input_key: {document.input_key}")
        logger.info(f"Document output_bucket: {document.output_bucket}")
        logger.info(f"Document has {len(document.sections)} sections from ProcessResults")
        
        # Update document status
        document.status = Status.RULE_VALIDATION_ORCHESTRATOR
        
        # Intelligent Rule Validation Orchestration detection: Skip if document already has consolidated results
        if (document.rule_validation_result and 
            document.rule_validation_result.section_results):
            logger.info(f"Skipping rule validation orchestration - document already has section results")
            
            # Add Lambda metering for skip execution
            try:
                lambda_metering = calculate_lambda_metering("RuleValidation", context, start_time)
                document.metering = merge_metering_data(document.metering, lambda_metering)
            except Exception as e:
                logger.warning(f"Failed to add Lambda metering for orchestration skip: {str(e)}")
            
            # Return existing result without reprocessing
            response = {
                "document": document.serialize_document(working_bucket, "rule_validation_orchestration_skip", logger)
            }
            
            logger.info(f"Rule validation orchestration skipped - Response: {json.dumps(response, default=str)}")
            return response
        
        # Get rule validation results from Map state
        rule_validation_results = event.get("RuleValidationResults", [])
        logger.info(f"Received {len(rule_validation_results)} rule validation results")
        
        # Collect section URIs and check for chunking
        section_results = []
        chunking_occurred = False
        
        for result in rule_validation_results:
            document_data = result.get("document", {})
            section_document = Document.load_document(document_data, working_bucket, logger)
            
            # Collect section result info
            if section_document.rule_validation_result and section_document.rule_validation_result.output_uri:
                section_results.append({
                    "section_id": result.get("section_id"),
                    "section_uri": section_document.rule_validation_result.output_uri
                })
            
            # Check for chunking in this section
            if section_document.rule_validation_result:
                metadata = section_document.rule_validation_result.metadata or {}
                if metadata.get("chunking_occurred"):
                    chunking_occurred = True
                    logger.info(f"Chunking detected in section {result.get('section_id')}")
            
            # Merge metering from section processing
            document.metering = merge_metering_data(
                document.metering, section_document.metering
            )
        
        logger.info(f"Collected {len(section_results)} section results")
        
        # With two-step approach (fact extraction → orchestrator), 
        # orchestrator must ALWAYS run to make final compliance decision
        logger.info(f"Orchestrator will run for {len(document.sections)} section(s)")
        
        # Get configuration - use document's version if specified, otherwise use active version
        config_version = getattr(document, 'config_version', None)
        config_revision = getattr(document, 'config_revision', None)
        config = get_config(version=config_version, revision=config_revision)
        
        # Create rule validation orchestrator service
        summarization_service = rule_validation.RuleValidationOrchestratorService(
            config=config
        )
        
        # Call consolidate_and_save - it handles:
        # 1. Loading section results from S3 (using URIs from rule_validation_result)
        # 2. Performing LLM orchestration (fact extraction → compliance decision)
        # 3. Consolidating results into final files
        # 4. Updating document.rule_validation_result with consolidated URIs
        logger.info(f"Consolidating rule validation results for {len(document.sections)} section(s)")
        updated_document = summarization_service.consolidate_and_save(
            document=document,
            config=config,
            multiple_sections=True  # Always run orchestrator for fact extraction
        )
        
        # Add section results to the consolidated rule_validation_result
        if updated_document.rule_validation_result and section_results:
            updated_document.rule_validation_result.section_results = section_results
            # Update metadata with correct counts and chunking info
            updated_document.rule_validation_result.metadata.update({
                "sections_processed": len(section_results),
                "chunking_occurred": chunking_occurred
            })
            logger.info(f"Added {len(section_results)} section results to consolidated result")
            logger.info(f"Chunking occurred: {chunking_occurred}")
        
        # Track Lambda metering
        lambda_metering = calculate_lambda_metering("RuleValidation", context, start_time)
        updated_document.metering = merge_metering_data(updated_document.metering, lambda_metering)
        
        docs_service = create_document_service()
        docs_service.update_document(updated_document)
        
        # Save rule validation results to reporting bucket
        reporting_bucket = os.environ.get('REPORTING_BUCKET')
        save_reporting_function = os.environ.get('SAVE_REPORTING_FUNCTION_NAME')
        
        if reporting_bucket and save_reporting_function and updated_document.rule_validation_result:
            try:
                import boto3
                logger.info(f"Saving rule validation results to {reporting_bucket} via {save_reporting_function}")
                lambda_client = boto3.client('lambda')
                lambda_response = lambda_client.invoke(
                    FunctionName=save_reporting_function,
                    InvocationType='RequestResponse',
                    Payload=json.dumps({
                        'document': updated_document.to_dict(),
                        'reporting_bucket': reporting_bucket,
                        'data_to_save': ['rule_validation_results']
                    })
                )
                
                response_payload = json.loads(lambda_response['Payload'].read().decode('utf-8'))
                if response_payload.get('statusCode') != 200:
                    logger.warning(f"SaveReportingData returned non-200 status: {response_payload}")
                else:
                    logger.info("SaveReportingData executed successfully")
            except Exception as e:
                logger.error(f"Error invoking SaveReportingData: {str(e)}")
                # Continue - don't fail if reporting fails
        
        logger.info(f"Rule validation consolidation completed for document: {updated_document.id}")
        
        # Return the completed document with compression (like ProcessResults does)
        response = {
            "document": updated_document.serialize_document(working_bucket, "rule_validation_consolidation", logger)
        }
        
        logger.info(f"Response: {json.dumps(response, default=str)}")
        
        return response
        
    except Exception as e:
        logger.error(f"Error in rule validation orchestration: {str(e)}")
        
        # Update document status to error if possible
        try:
            if 'document' in locals():
                docs_service = create_document_service()
                docs_service.update_document_status(
                    document_id=document.id,
                    status=Status.ERROR,
                    error_message=str(e)
                )
        except Exception as status_error:
            logger.error(f"Failed to update document status: {str(status_error)}")
        
        raise e
