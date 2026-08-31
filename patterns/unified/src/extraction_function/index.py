# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import os
import json
import time
import logging

import boto3
from idp_common import metrics, get_config, extraction
from idp_common.models import Document, Section, Status
from idp_common.docs_service import create_document_service
from idp_common.utils import calculate_lambda_metering, merge_metering_data
from aws_xray_sdk.core import xray_recorder, patch_all

patch_all()

# Configuration will be loaded in handler function

OCR_TEXT_ONLY = os.environ.get('OCR_TEXT_ONLY', 'false').lower() == 'true'

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))

# --- Extraction checkpoint helpers for resume-on-timeout ---

_s3_client = None

def _get_s3_client():
    """Lazy-init S3 client for checkpoint operations."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _checkpoint_s3_key(execution_arn: str, section_id: str) -> str:
    """Build deterministic S3 key for extraction checkpoint."""
    # Sanitise execution ARN for use as S3 key prefix
    safe_arn = execution_arn.replace(":", "_").replace("/", "_")
    return f"checkpoints/{safe_arn}/{section_id}/extraction_state.json"


def load_extraction_checkpoint(bucket: str, execution_arn: str, section_id: str) -> dict | None:
    """
    Load an extraction checkpoint from S3 if one exists.

    Returns the checkpoint data dict, or None if no checkpoint found.
    """
    key = _checkpoint_s3_key(execution_arn, section_id)
    try:
        response = _get_s3_client().get_object(Bucket=bucket, Key=key)
        checkpoint = json.loads(response["Body"].read().decode("utf-8"))
        logger.info(
            f"Loaded extraction checkpoint from s3://{bucket}/{key} "
            f"(saved at {checkpoint.get('timestamp', 'unknown')})"
        )
        return checkpoint
    except _get_s3_client().exceptions.NoSuchKey:
        return None
    except Exception as e:
        logger.warning(f"Failed to load extraction checkpoint: {e}")
        return None


def save_extraction_checkpoint(
    bucket: str, execution_arn: str, section_id: str, extraction_data: dict
) -> None:
    """
    Save an extraction checkpoint to S3.

    Called incrementally after each successful extraction_tool or apply_json_patches
    invocation inside the agentic extraction loop.
    """
    key = _checkpoint_s3_key(execution_arn, section_id)
    checkpoint = {
        "extraction_data": extraction_data,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _get_s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(checkpoint, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        # Count items for logging (works for schemas with list fields like holdings_positions)
        item_count = _count_extraction_items(extraction_data)
        logger.info(
            f"Saved extraction checkpoint to s3://{bucket}/{key} "
            f"({item_count} items extracted so far)"
        )
    except Exception as e:
        logger.warning(f"Failed to save extraction checkpoint: {e}")


def delete_extraction_checkpoint(bucket: str, execution_arn: str, section_id: str) -> None:
    """Delete extraction checkpoint from S3 after successful completion."""
    key = _checkpoint_s3_key(execution_arn, section_id)
    try:
        _get_s3_client().delete_object(Bucket=bucket, Key=key)
        logger.info(f"Deleted extraction checkpoint s3://{bucket}/{key}")
    except Exception as e:
        logger.warning(f"Failed to delete extraction checkpoint: {e}")


def delete_shard_results(bucket: str, execution_arn: str, section_id: str) -> None:
    """Delete all per-shard result objects for a section after it completes.

    Per-shard results live under
    ``checkpoints/{safe_arn}/{section_id}/shards/`` (see
    ``idp_common.extraction.runtime.shard_result_key``). They must survive across
    SFN retries (to skip completed shards) but are removed once the whole section
    succeeds so a later re-process of the same execution+section starts clean.
    """
    safe_arn = execution_arn.replace(":", "_").replace("/", "_")
    prefix = f"checkpoints/{safe_arn}/{section_id}/shards/"
    s3 = _get_s3_client()
    try:
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        keys = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
            logger.info(f"Deleted {len(keys)} per-shard result(s) under {prefix}")
    except Exception as e:
        logger.warning(f"Failed to delete per-shard results: {e}")


def _count_extraction_items(extraction_data: dict) -> int:
    """Count total items across all list fields in extraction data for logging."""
    count = 0
    for value in extraction_data.values():
        if isinstance(value, list):
            count += len(value)
        else:
            count += 1
    return count

@xray_recorder.capture('extraction_function')
def handler(event, context):
    """
    Process a single section of a document for information extraction
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
    
    if config_version:
        logger.info(f"Using configuration version {config_version} for document {full_document.id}")
    else:
        logger.info(f"Using active configuration for document {full_document.id}")
    
    logger.info(f"Config: {json.dumps(config.model_dump(), default=str)}")
    
    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {full_document.id}, input_key: {full_document.input_key}")
    logger.info(f"Document buckets - input_bucket: {full_document.input_bucket}, output_bucket: {full_document.output_bucket}")
    logger.info(f"Document status: {full_document.status}, num_pages: {full_document.num_pages}")
    logger.info(f"Document pages count: {len(full_document.pages)}, sections count: {len(full_document.sections)}")
    logger.info(f"Full document content: {json.dumps(full_document.to_dict(), default=str)}")

    # X-Ray annotations
    xray_recorder.put_annotation('document_id', {full_document.id})
    xray_recorder.put_annotation('processing_stage', 'extraction')
    
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
    
    # Intelligent Extraction detection: Skip if section already has extraction data
    if section.extraction_result_uri and section.extraction_result_uri.strip():
        logger.info(f"Skipping extraction for section {section_id} - already has extraction data: {section.extraction_result_uri}")
        
        # Add Lambda metering for extraction skip execution
        try:
            lambda_metering = calculate_lambda_metering("Extraction", context, start_time)
            full_document.metering = merge_metering_data(full_document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering for extraction skip: {str(e)}")
        
        # Return the section without processing
        response = {
            "section_id": section_id,
            "document": full_document.serialize_document(working_bucket, f"extraction_skip_{section_id}", logger)
        }
        
        logger.info(f"Extraction skipped - Response: {json.dumps(response, default=str)}")
        return response
    else:
        logger.info(f"Processing section {section_id} - no extraction data found, proceeding with extraction")
    
    # Normal extraction processing or selective processing for modified sections
    # Update document status to EXTRACTING using lightweight status-only update
    # This reduces DynamoDB WCU consumption by ~94% (~500 bytes vs ~100KB)
    document_service = create_document_service()
    logger.info(f"Updating document status to EXTRACTING (lightweight update) for document {full_document.input_key}")
    try:
        status_result = document_service.update_document_status(
            document_id=full_document.input_key,
            status=Status.EXTRACTING,
            workflow_execution_arn=full_document.workflow_execution_arn,
        )
        logger.info(f"Status update result: {json.dumps(status_result, default=str)[:500]}")
    except Exception as e:
        logger.error(f"Failed to update document status: {str(e)}", exc_info=True)
    full_document.status = Status.EXTRACTING
       
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
    
    # Initialize the extraction service
    extraction_service = extraction.ExtractionService(config=config)
    
    # --- Checkpoint: load any existing checkpoint for resume-on-timeout ---
    execution_arn = getattr(full_document, 'workflow_execution_arn', '') or event.get('execution_arn', '')
    agentic_enabled = config.extraction.agentic.enabled if config.extraction and config.extraction.agentic else False
    checkpoint_data = None
    
    if agentic_enabled and working_bucket and execution_arn:
        checkpoint = load_extraction_checkpoint(working_bucket, execution_arn, section_id)
        if checkpoint and checkpoint.get("extraction_data"):
            raw_checkpoint = checkpoint["extraction_data"]
            # Handle new format: {"source": "...", "data": {...}}
            # and legacy format: direct extraction dict
            if isinstance(raw_checkpoint, dict) and "source" in raw_checkpoint and "data" in raw_checkpoint:
                checkpoint_source = raw_checkpoint["source"]
                checkpoint_data = raw_checkpoint["data"]
            else:
                # Legacy format — assume current_extraction
                checkpoint_source = "current_extraction"
                checkpoint_data = raw_checkpoint
            item_count = _count_extraction_items(checkpoint_data)
            logger.info(
                f"Resuming agentic extraction from checkpoint "
                f"(source={checkpoint_source}, {item_count} items, "
                f"saved at {checkpoint.get('timestamp', 'unknown')})"
            )
            # If checkpoint came from buffer (intermediate_extraction),
            # store it so service.py can load it into agent's buffer state
            if checkpoint_source == "intermediate_extraction":
                # Tag as buffer checkpoint so service layer handles it
                checkpoint_data = {"_checkpoint_source": "intermediate_extraction", **checkpoint_data}
        
        # Set up incremental checkpoint callback on the extraction service
        def _checkpoint_cb(extraction_data: dict) -> None:
            save_extraction_checkpoint(working_bucket, execution_arn, section_id, extraction_data)

        extraction_service._checkpoint_callback = _checkpoint_cb

        # Wire per-shard persistence so the concurrent/sharded path can resume
        # only the incomplete shards if this Lambda times out and Step Functions
        # retries the section (ExtractionStep Retry on Sandbox.Timedout). Completed
        # shards are loaded from S3; only incomplete shards re-infer. Keyed by
        # (execution_arn, section_id) so retries of the SAME section find them and
        # a fresh document never collides. Cleaned up on success below.
        max_concurrent = (
            config.extraction.agentic.max_concurrent_batches
            if config.extraction and config.extraction.agentic
            else 1
        )
        if max_concurrent and max_concurrent > 1:
            try:
                from idp_common.extraction.runtime import S3ShardPersistence

                extraction_service._shard_persistence = S3ShardPersistence(
                    bucket=working_bucket,
                    execution_arn=execution_arn,
                    s3_client=_get_s3_client(),
                )
                logger.info(
                    "Per-shard S3 persistence enabled for section %s "
                    "(skip-completed on retry)", section_id
                )
            except Exception as e:
                logger.warning(f"Could not enable per-shard persistence: {e}")
    
    # Track metrics
    metrics.put_metric('InputDocuments', 1)
    metrics.put_metric('InputDocumentPages', len(section.page_ids))
    
    # Wall-clock deadline for the in-shard assessment self-healing ladder (1.5):
    # convert the Lambda's remaining-time budget into an absolute epoch so the
    # assessment escalation ladder stops before a hard task timeout.
    deadline_epoch = None
    try:
        deadline_epoch = time.time() + (context.get_remaining_time_in_millis() / 1000.0)
    except Exception:  # noqa: BLE001 - context may be absent in local/test runs
        deadline_epoch = None

    # Process the section in our focused document
    t0 = time.time()
    section_document = extraction_service.process_document_section(
        document=section_document,
        section_id=section_id,
        checkpoint_data=checkpoint_data,
        deadline_epoch=deadline_epoch,
    )
    t1 = time.time()
    logger.info(f"Total extraction time: {t1-t0:.2f} seconds")
    
    # --- Checkpoint: cleanup on successful completion ---
    if agentic_enabled and working_bucket and execution_arn:
        delete_extraction_checkpoint(working_bucket, execution_arn, section_id)
        # Remove per-shard results now the section is fully merged & saved.
        delete_shard_results(working_bucket, execution_arn, section_id)
    
    # Check if document processing failed
    if section_document.status == Status.FAILED:
        error_message = f"Extraction failed for document {section_document.id}, section {section_id}"
        logger.error(error_message)
        raise Exception(error_message)
    
    # Add Lambda metering for successful extraction execution
    try:
        lambda_metering = calculate_lambda_metering("Extraction", context, start_time)
        section_document.metering = merge_metering_data(section_document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for extraction: {str(e)}")
    
    # Update the section in DynamoDB for immediate UI visibility
    # This allows the UI to show extraction results as they complete (not wait for collate)
    try:
        # Use section_index captured at start (before full_document was modified)
        updated_section = section_document.sections[0]
        # Get the original document input_key (stored before any modifications)
        original_input_key = section_document.input_key
        logger.info(f"Persisting extraction results for section {section_id} (index {section_index}) to DynamoDB for document {original_input_key}")
        result = document_service.update_document_section(
            document_id=original_input_key,
            section_index=section_index,
            section=updated_section,
        )
        logger.info(f"Section update result: {json.dumps(result, default=str)[:500]}")
    except Exception as e:
        logger.error(f"Failed to update section in DynamoDB: {str(e)}", exc_info=True)
    
    # Prepare output with automatic compression if needed
    response = {
        "section_id": section_id,
        "document": section_document.serialize_document(working_bucket, f"extraction_{section_id}", logger)
    }
    
    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
