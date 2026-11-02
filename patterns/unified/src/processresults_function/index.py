# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import datetime
import json
import logging
import os

import boto3
from idp_common import s3, utils
from idp_common.config import get_config
from idp_common.docs_service import create_document_service
from idp_common.document_failure import (
    POSTPROCESSING_STAGE,
    SECTION_PROCESSING_FAILED_CODE,
    SECTION_PROCESSING_FAILED_MESSAGE,
    SectionDiagnosis,
    persist_failed_document,
    summarize_errors,
)
from idp_common.models import Document, HitlMetadata, Status
from idp_common.review_queues.routing import (
    legacy_section_triggers,
    review_mode,
    route_for_review,
)
from idp_common.review_queues.statuses import LegacyHitlStatus, normalize_hitl_status
from idp_common.review_queues.store import create_review_store

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)

# Initialize AWS clients
s3_client = boto3.client("s3")

# Review queues (idp_common.review_queues): None when HITL_TABLE_NAME is
# unset (headless), which keeps every profile on the legacy HITL flow.
_REVIEW_STORE = create_review_store()


def legacy_review_finished(hitl_status):
    """True when a stored ``HITLStatus`` records a finished legacy review.

    Finishing a legacy review (completed or skipped) triggers a reprocess, so the
    run it starts must keep that status rather than ask for the review again. The
    status is read through ``normalize_hitl_status`` rather than a list of spellings:
    a legacy (not queue-managed) completed or skipped review is finished.
    ``COMPLETED`` is the one finished spelling a real writer stored that the
    exact-match normaliser does not know: the earlier A2I flow wrote it, and
    retained rows may still carry it. ``process_changes_resolver`` makes the same
    exception, but it also counts a queue outcome as finished, which this does not.
    A queue outcome (``complete`` / ``rejected``) is not a legacy review: in the
    legacy flow such a document is asked for a review again, as reprocessing it
    under review queues starts a fresh cycle.
    """
    view = normalize_hitl_status(hitl_status)
    return (
        not view.queue_managed and view.kind in ("complete", "skipped")
    ) or hitl_status == "COMPLETED"


def handler(event, context):
    """
    Consolidates the results from multiple extraction steps into a single output.

    Args:
        event: Contains the document metadata and extraction results array
        context: Lambda context

    Returns:
        Dict containing the fully processed document
    """
    logger.info(f"Processing event: {json.dumps(event)}")

    # Get the base document from the original classification result - handle both compressed and uncompressed
    working_bucket = os.environ.get("WORKING_BUCKET")
    classification_document_data = event.get("ClassificationResult", {}).get(
        "document", {}
    )
    document = Document.load_document(
        classification_document_data, working_bucket, logger
    )

    # Load configuration - use document's version if specified, otherwise use active version
    config_version = getattr(document, 'config_version', None)
    config_revision = getattr(document, 'config_revision', None)
    config = get_config(as_model=True, version=config_version, revision=config_revision)

    extraction_results = event.get("ExtractionResults", [])
    execution_arn = event.get("execution_arn", "")
    execution_id = execution_arn.split(":")[-1] if execution_arn else "unknown"

    # HITL settings are read once per invocation from the config loaded above.
    hitl_enabled = bool(config.hitl.enabled)
    route_mode = review_mode(config, _REVIEW_STORE, document)

    # Get confidence threshold from configuration
    confidence_threshold = config.hitl.confidence_threshold
    logger.info(f"Using confidence threshold: {confidence_threshold}")

    # Update document status to POSTPROCESSING
    document.status = Status.POSTPROCESSING
    document_service = create_document_service()
    
    # Fetch current HITL status from DynamoDB (may have been updated by reviewer)
    current_doc = document_service.get_document(document.input_key)
    if current_doc:
        document.hitl_status = current_doc.hitl_status
        logger.info(f"Current HITL status from DynamoDB: {document.hitl_status}")
    
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)

    # Clear sections list to rebuild from extraction results
    document.sections = []
    validation_errors = []
    hitl_triggered = False
    # #1064: which section failed, and why, so the raise below can persist it on
    # the section rather than leaving it in the Step Functions cause alone.
    section_diagnoses = []

    # Combine all section results
    for i, result in enumerate(extraction_results):
        # New optimized format - document is at the top level
        document_data = result.get("document", {})
        section_document = Document.load_document(document_data, working_bucket, logger)
        logger.info(f"section_document: {section_document}")
        if section_document:
            # Add section to document if present
            if section_document.sections:
                # The section this map iteration was about, by id. A result
                # document can hold more than that one section — the extraction
                # skip branch returns the whole document, and an assessment that
                # then runs keeps it — so sections[0] turned every section of a
                # reprocessed document into a copy of section 1 (#1686).
                result_section_id = result.get("section_id")
                section = next(
                    (
                        s
                        for s in section_document.sections
                        if s.section_id == result_section_id
                    ),
                    section_document.sections[0],
                )
                logger.info(f"section: {section}")
                logger.info(
                    f"section.confidence_threshold_alerts: {section.confidence_threshold_alerts}"
                )
                logger.info(f"is_hitl_enabled: {hitl_enabled}")
                document.sections.append(section)

                # Check if HITL review is needed for this section (legacy flow;
                # with review queues the routing below decides instead)
                if route_mode == "legacy" and legacy_section_triggers(
                    section, hitl_enabled=hitl_enabled
                ):
                    logger.info(
                        f"Section {section.section_id} has {len(section.confidence_threshold_alerts)} confidence threshold alerts - marking for HITL review"
                    )
                    hitl_triggered = True

                    # Create HITL metadata entry for in-house portal review
                    section_page_numbers = list(range(1, len(section.page_ids) + 1))
                    hitl_metadata = HitlMetadata(
                        execution_id=execution_id,
                        record_number=int(section.section_id),
                        bp_match=True,
                        extraction_bp_name=section.classification,
                        hitl_triggered=True,
                        page_array=section_page_numbers,
                    )
                    document.hitl_metadata.append(hitl_metadata)

                # Create metadata file for section output
                if section.extraction_result_uri:
                    create_metadata_file(
                        section.extraction_result_uri, section.classification, "section"
                    )

                if section_document.status == Status.FAILED:
                    error_message = (
                        f"Processing failed for section {i + 1}: "
                        f"{'; '.join(section_document.errors)}"
                    )
                    validation_errors.append(error_message)
                    logger.error(f"Error: {error_message}")
                    # Attribute the verdict to the section it is about. The
                    # exception message below keeps the 1-based ordinal it has
                    # always used; the issue is keyed by the real section id,
                    # which is what the Sections panel reads.
                    section_diagnoses.append(
                        SectionDiagnosis(
                            section_id=section.section_id,
                            stage=POSTPROCESSING_STAGE,
                            code=SECTION_PROCESSING_FAILED_CODE,
                            message=SECTION_PROCESSING_FAILED_MESSAGE,
                            root_cause=summarize_errors(
                                section_document.errors,
                                fallback="The section reported status FAILED "
                                "without recording a reason.",
                            ),
                        )
                    )

            # Add metering from section processing
            document.metering = utils.merge_metering_data(
                document.metering, section_document.metering
            )

    # Create metadata files for pages
    for page_id, page in document.pages.items():
        if page.raw_text_uri:
            create_metadata_file(page.raw_text_uri, page.classification, "page")

    # Collect section IDs that need HITL review and update document model
    # Only set to PendingReview if not already reviewed (preserve completed/skipped status on reprocess)
    hitl_sections_pending = []
    if hitl_triggered:
        document.hitl_triggered = True
        existing_status = document.hitl_status
        if not legacy_review_finished(existing_status):
            for section in document.sections:
                if section.confidence_threshold_alerts:
                    hitl_sections_pending.append(section.section_id)
            # Set Review Status on document model
            document.hitl_status = LegacyHitlStatus.PENDING_REVIEW.value
            document.hitl_sections_pending = hitl_sections_pending
            document.hitl_sections_completed = []
            logger.info(f"Document requires human review. Sections pending: {hitl_sections_pending}")
        else:
            logger.info(f"Document already reviewed (status: {existing_status}), preserving HITL status on reprocess")

    # Compute confidence alert count from all sections
    document.confidence_alert_count = sum(
        len(section.confidence_threshold_alerts)
        for section in document.sections
    )
    logger.info(f"Total confidence alert count: {document.confidence_alert_count}")

    # Required fields left absent, counted separately from confidence alerts
    # (#1527). Informational: nothing above routes on it.
    document.missing_required_field_count = sum(
        len(section.missing_required_fields or []) for section in document.sections
    )

    # Update final status in AppSync / Document Service (includes Review Status)
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)

    # Check if rule validation is enabled in config AND has rules configured
    rule_validation_enabled = False
    if hasattr(config, 'rule_validation'):
        rule_validation_enabled = config.rule_validation.enabled
        if rule_validation_enabled:
            policy_classes = getattr(config, 'policy_classes', None) or []
            if len(policy_classes) == 0:
                logger.info("Rule validation is enabled but no policy_classes configured - skipping rule validation")
                rule_validation_enabled = False
        logger.info(f"Rule validation enabled: {rule_validation_enabled}")
    
    # Return the completed document with compression
    response = {
        "document": document.serialize_document(
            working_bucket, "processresults", logger
        ),
        "hitl_triggered": hitl_triggered,
        "rule_validation_enabled": rule_validation_enabled
    }

    logger.info(f"Response: {json.dumps(response, default=str)}")

    if document.errors:
        validation_errors.extend(document.errors)

    # Raise exception if there were validation errors
    if validation_errors:
        document.status = Status.FAILED
        # Create comprehensive error message
        error_summary = f"Processing failed for {len(validation_errors)} out of {len(extraction_results)} sections"
        combined_errors = "; ".join(validation_errors)
        full_error_message = f"{error_summary}: {combined_errors}"
        logger.error(f"Error: {full_error_message}")
        failure = Exception(full_error_message)
        # #1064: persist before raising. This handler owns the whole-document
        # write and already holds every section, so ONE `update_document` can carry
        # both the FAILED status just assigned — which was previously computed and
        # thrown away — and a per-section issue for each section that failed.
        #
        # It writes only when there is at least one section diagnosis, which means
        # the reachable path below does NOT write: `document.errors` is
        # document-scope, produces no diagnosis, and `persist_failed_document`
        # returns before writing, so neither the issues nor the FAILED status are
        # persisted there. The terminal status still comes from `workflow_tracker`
        # as it always did.
        #
        # `document.errors` is deliberately NOT given a home here. Those entries
        # are document-scope free text (OCR and classification append to it
        # without failing the document), they are persisted by no writer, and the
        # per-page failures behind them are already recorded as section-attributed
        # issues by classification. A document-level issue would bump
        # ProcessingIssueCount, which the document list reads, and then have no
        # text behind the badge — see `idp_common.document_failure`.
        persist_failed_document(
            document_service=document_service,
            document=document,
            error=failure,
            diagnoses=section_diagnoses,
        )
        raise failure

    # Review queues: enqueue last, after the validation raise above, so a failed
    # run never leaves a claimable item (feature off adds no keys).
    if route_mode == "queues":
        decision = route_for_review(
            document,
            config,
            mode="pipeline",
            execution_arn=execution_arn,
            store=_REVIEW_STORE,
            document_service=document_service,
            output_bucket=document.output_bucket or os.environ.get("OUTPUT_BUCKET"),
        )
        if decision.hold:
            response["review_hold"] = True
        if decision.outcome:
            response["review_outcome"] = decision.outcome

    return response


def create_metadata_file(file_uri, class_type, file_type=None):
    """
    Creates a metadata file alongside the given URI file with the same name plus '.metadata.json'
    """
    try:
        # Parse the S3 URI to get bucket and key. Use parse_s3_uri (plain
        # string split) rather than urllib.parse.urlparse: object keys may
        # contain '#', which urlparse treats as a URL fragment delimiter and
        # silently truncates, producing a wrong metadata key.
        bucket, key = utils.parse_s3_uri(file_uri)

        # Create the metadata key by adding '.metadata.json' to the original key
        metadata_key = f"{key}.metadata.json"

        # Determine the file type if not provided
        if file_type is None:
            if key.endswith(".json"):
                file_type = "section"
            else:
                file_type = "page"

        # Create metadata content
        metadata_content = {
            "metadataAttributes": {
                "DateTime": datetime.datetime.now().isoformat(),
                "Class": class_type,
                "FileType": file_type,
            }
        }

        # Upload metadata file to S3 using common library
        s3.write_content(
            metadata_content, bucket, metadata_key, content_type="application/json"
        )

        logger.info(f"Created metadata file at s3://{bucket}/{metadata_key}")
    except Exception as e:
        logger.error(f"Error creating metadata file for {file_uri}: {str(e)}")
