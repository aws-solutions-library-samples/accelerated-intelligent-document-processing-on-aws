# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
import logging
import os
from datetime import datetime, timezone

import boto3
from idp_common.docs_service import create_document_service

# Import IDP Common modules
from idp_common.models import Section, Status
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")

# Environment variables
QUEUE_URL = os.environ.get("QUEUE_URL")

# Groups permitted per operation in this resolver. updateDocumentSections matches
# completeSectionReview (Admin, Reviewer, Annotator) — the sibling that edits a
# section's CONTENT — rather than processChanges' narrower pair, because correcting a
# packet split is the same kind of annotation work and, unlike processChanges, it does
# not requeue the document through the pipeline.
_OPERATION_GROUPS = {
    "processChanges": {"Admin", "Reviewer"},
    "updateDocumentSections": {"Admin", "Reviewer", "Annotator"},
}

# A document being processed must not be written underneath the pipeline. This is the
# hazard processChanges avoids by not persisting at all (it hands the document to SQS
# and lets the run save it); a direct write has to check for itself.
#
# Stated as the statuses that ARE safe, not the ones that are not. The first version of
# this was a denylist of {QUEUED, RUNNING}, which let through every intermediate status
# the pipeline actually spends its time in — OCR, CLASSIFYING, EXTRACTING, ASSESSING,
# SUMMARIZING, RULE_VALIDATION* and the rest — so the guard missed 14 of the 16
# non-terminal states it existed to catch. A denylist over an enum somebody else extends
# is the wrong shape: adding a pipeline stage to Status silently widened the hole. An
# allow-list fails closed instead, and a new status is refused until it is considered.
#
# REDACTED_SUPERSEDED is terminal too: a preprocessing hook produced a redacted copy
# that processes separately, and this original is deliberately left alone.
_EDITABLE_STATUSES = {
    Status.COMPLETED.value,
    Status.FAILED.value,
    Status.ABORTED.value,
    Status.REDACTED_SUPERSEDED.value,
}


def update_document_sections(event):
    """Re-group a processed document's pages, keeping its extracted values.

    The document-side counterpart to updateTestSetDocumentSections, and deliberately
    NOT processChanges. processChanges clears each changed section's extraction data and
    requeues the document, because it delegates persistence to the pipeline — it does not
    write the document back at all, so the SQS run is the only thing that saves the
    change. Reprocessing is its persistence mechanism, not a consequence of re-grouping.

    A grouping-only edit can persist itself, exactly as complete_section_review does for
    field edits: `document_service.update_document(document)` writes Sections with their
    PageIds and Class. So the extracted values, and any HITL corrections made to them,
    survive. They may no longer match their pages afterwards, which the UI says while
    offering processChanges as the opt-in reprocess.

    Refuses while the document is in flight. That is the one hazard a direct write
    introduces: processChanges is safe to call mid-run because it never writes, whereas
    this would race the pipeline's own save.
    """
    args = event.get("arguments", {})
    object_key = args.get("objectKey")
    incoming = args.get("sections") or []

    if not object_key:
        return {
            "success": False,
            "message": "objectKey is required",
            "processingJobId": None,
        }
    if not incoming:
        return {
            "success": False,
            "message": "A document must have at least one section",
            "processingJobId": None,
        }

    dynamodb_service = create_document_service(mode="dynamodb")
    document = dynamodb_service.get_document(object_key)
    if not document:
        return {
            "success": False,
            "message": f"Document {object_key} not found",
            "processingJobId": None,
        }

    status = str(getattr(document.status, "value", document.status) or "").upper()
    if status not in _EDITABLE_STATUSES:
        return {
            "success": False,
            "message": (
                f"{object_key} is currently {status.lower() or 'in an unknown state'}. "
                "Wait for processing to finish before changing its page grouping, or "
                "the change would be overwritten by the run."
            ),
            "processingJobId": None,
        }

    # A partition of the pages we are given: no page in two sections, none empty. The
    # client additionally requires every page of the document to be assigned, which it
    # can check because it has the rendered page list.
    seen = {}
    section_ids = set()
    for section in incoming:
        section_id = str(section.get("sectionId") or "")
        page_ids = section.get("pageIds") or []
        # A repeated id corrupts silently rather than failing. The rebuild below looks
        # each id up in existing_by_id, so two entries sharing one id resolve to the
        # *same* Section object: it is appended twice and the second assignment
        # overwrites page_ids, so the document ends up holding two references to one
        # section and the first group's pages belong to none. The page-level checks
        # cannot catch it — both groups' pages are accounted for in `seen`.
        if section_id in section_ids:
            return {
                "success": False,
                "message": f"Section '{section_id}' appears more than once",
                "processingJobId": None,
            }
        section_ids.add(section_id)
        if not page_ids:
            return {
                "success": False,
                "message": f"Section '{section_id}' has no pages",
                "processingJobId": None,
            }
        for raw in page_ids:
            page_id = str(raw)
            # Refused by name rather than left to fail later. A non-numeric id reaches
            # int() in the sort below and surfaces as a generic caught error, and an id
            # for a page the document does not have was simply accepted into a section
            # and then skipped by the classification loop — a grouping claiming a page
            # that does not exist, saved without complaint.
            if not page_id.isdigit():
                return {
                    "success": False,
                    "message": (
                        f"Section '{section_id}' has an invalid page id {raw!r}; "
                        "expected a page number"
                    ),
                    "processingJobId": None,
                }
            if page_id not in document.pages:
                return {
                    "success": False,
                    "message": (
                        f"Page {page_id} is not in {object_key}, so section "
                        f"'{section_id}' cannot claim it"
                    ),
                    "processingJobId": None,
                }
            if page_id in seen:
                return {
                    "success": False,
                    "message": (
                        f"Page {page_id} is in both section '{seen[page_id]}' and "
                        f"section '{section_id}'"
                    ),
                    "processingJobId": None,
                }
            seen[page_id] = section_id

    previously_grouped = {pid for sec in document.sections for pid in sec.page_ids}
    lost = sorted(previously_grouped - set(seen))
    if lost:
        return {
            "success": False,
            "message": (
                f"Page{'s' if len(lost) > 1 else ''} {', '.join(lost)} would no longer "
                "belong to any section"
            ),
            "processingJobId": None,
        }

    existing_by_id = {section.section_id: section for section in document.sections}
    rebuilt = []
    for section in incoming:
        source_id = str(section.get("sectionId") or "")
        page_ids = [str(pid) for pid in section.get("pageIds") or []]
        classification = section.get("classification")
        carried = existing_by_id.get(source_id)
        if carried is not None:
            # Content follows its pages: the extraction result, attributes and any
            # confidence alerts stay attached to the section that keeps them.
            carried.page_ids = page_ids
            if classification:
                carried.classification = classification
            rebuilt.append(carried)
        else:
            # A section the reviewer added. No extraction result, which is the truth —
            # nothing has run over these pages as a group yet.
            rebuilt.append(
                Section(
                    section_id=source_id,
                    classification=classification or "",
                    page_ids=page_ids,
                )
            )

    # By first page, as processChanges does (:240), so a section's position matches its
    # place in the document — consumers take a group index from list position.
    rebuilt.sort(
        key=lambda sec: min([int(pid) for pid in sec.page_ids] + [float("inf")])
    )
    document.sections = rebuilt

    # Page-level classification follows the section that now owns the page, or a page
    # would keep a class its section no longer has.
    for section in document.sections:
        for page_id in section.page_ids:
            if page_id in document.pages and section.classification:
                document.pages[page_id].classification = section.classification

    # Persisted here, unlike processChanges: nothing else is going to save it, and
    # nothing is running that we could race.
    dynamodb_service.update_document(document)

    logger.info(
        f"Re-grouped {object_key} into {len(document.sections)} section(s) without "
        "reprocessing; extracted values preserved"
    )
    return {
        "success": True,
        "message": (
            f"Page grouping saved as {len(document.sections)} section(s). Extracted "
            "values were kept and may no longer match their pages."
        ),
        "processingJobId": None,
    }


def handler(event, context):
    logger.info(
        f"ProcessChanges resolver invoked with event: {json.dumps(sanitize_event_for_logging(event))}"
    )

    # Add comprehensive error handling
    try:
        field_name = (event.get("info") or {}).get("fieldName") or "processChanges"

        # Defense-in-depth RBAC. The schema enforces this via
        # @aws_cognito_user_pools(cognito_groups), but we also gate it server-side so a
        # caller can never reach an operation even if the schema directive is
        # missing/misconfigured.
        caller_groups = (
            event.get("identity", {}).get("claims", {}).get("cognito:groups") or []
        )
        if isinstance(caller_groups, str):
            caller_groups = [caller_groups]
        required = _OPERATION_GROUPS.get(field_name, {"Admin", "Reviewer"})
        if not (required.intersection(caller_groups)):
            logger.warning(
                f"Forbidden: caller (groups={caller_groups}) attempted {field_name}"
            )
            # Raise (not return a 200 dict) so the dispatcher maps to 403.
            raise PermissionError(
                f"Unauthorized: {field_name} requires one of "
                f"{' or '.join(sorted(required))} group"
            )

        if field_name == "updateDocumentSections":
            return update_document_sections(event)

        # Extract arguments from the GraphQL event
        args = event.get("arguments", {})
        logger.info(f"Arguments received: {json.dumps(args)}")

        object_key = args.get("objectKey")
        modified_sections = args.get("modifiedSections", [])
        modified_pages = args.get("modifiedPages", [])

        if not object_key:
            logger.error("objectKey is required but not provided")
            return {
                "success": False,
                "message": "objectKey is required",
                "processingJobId": None,
            }

        # Allow empty arrays for evaluation-only reprocessing
        # When both are empty, the document will be resubmitted and the existing
        # Lambda skip logic will bypass OCR/Classification/Extraction/Assessment
        # and proceed directly to Summarization and Evaluation steps
        if not modified_sections and not modified_pages:
            logger.info(
                "No section or page modifications - reprocessing for evaluation/summarization only"
            )

        logger.info(f"Processing changes for document: {object_key}")
        logger.info(f"Modified sections: {json.dumps(modified_sections)}")
        logger.info(f"Modified pages: {json.dumps(modified_pages)}")

        # Use DynamoDB service to get the document (only service that supports get_document)
        try:
            dynamodb_service = create_document_service(mode="dynamodb")
            document = dynamodb_service.get_document(
                object_key
            )  # Returns Document object directly

            if not document:
                raise ValueError(f"Document {object_key} not found")

            # Set bucket names from environment variables (fix for null bucket issue)
            input_bucket = os.environ.get("INPUT_BUCKET")
            output_bucket = os.environ.get("OUTPUT_BUCKET")
            document.input_bucket = input_bucket
            document.output_bucket = output_bucket
            logger.info(
                f"Set document buckets - input_bucket: {input_bucket}, output_bucket: {output_bucket}"
            )

            logger.info(f"Found document: {document.id}")

            # Mark HITL review as completed when processing changes
            # This handles the case where user edits data and clicks "Process Changes"
            hitl_status_lower = (document.hitl_status or "").lower().replace(" ", "")
            completed_statuses = [
                "completed",
                "reviewcompleted",
                "skipped",
                "reviewskipped",
            ]
            if document.hitl_status and hitl_status_lower not in completed_statuses:
                identity = event.get("identity", {})
                username = identity.get("username", "system")
                user_email = identity.get("claims", {}).get("email", "")

                # Mark all pending sections as completed
                pending_sections = document.hitl_sections_pending or []
                completed_sections = list(document.hitl_sections_completed or [])
                completed_sections.extend(pending_sections)

                document.hitl_status = "Review Completed"
                document.hitl_sections_pending = []
                document.hitl_sections_completed = completed_sections

                # Update review fields in DynamoDB (not in document model)
                # HITLReviewedBy tracks who completed the review via Process Changes
                tracking_table = os.environ.get("TRACKING_TABLE")
                if tracking_table:
                    dynamodb_resource = boto3.resource("dynamodb")
                    table = dynamodb_resource.Table(tracking_table)
                    table.update_item(
                        Key={"PK": f"doc#{object_key}", "SK": "none"},
                        UpdateExpression="SET HITLStatus = :status, HITLReviewedBy = :reviewedBy, HITLReviewedByEmail = :reviewedByEmail, HITLCompleted = :completed REMOVE HITLPendingReview",
                        ExpressionAttributeValues={
                            ":status": "Review Completed",
                            ":reviewedBy": username,
                            ":reviewedByEmail": user_email,
                            ":completed": True,
                        },
                    )

                logger.info(
                    f"Marked HITL review as completed by {username} via Process Changes"
                )

        except Exception as e:
            logger.error(f"Error retrieving document {object_key}: {str(e)}")
            raise ValueError(
                f"Document {object_key} not found or error retrieving: {str(e)}"
            )

        # Track modified section IDs for selective processing
        modified_section_ids = []

        # Process page-level modifications first (if any)
        if modified_pages:
            process_page_changes(document, modified_pages, modified_section_ids)

        # Process each section modification
        for modified_section in modified_sections:
            section_id = modified_section["sectionId"]
            classification = modified_section["classification"]
            page_ids = [
                int(pid) for pid in modified_section["pageIds"]
            ]  # Ensure integer page IDs
            is_new = modified_section.get("isNew", False)
            is_deleted = modified_section.get("isDeleted", False)

            if is_deleted:
                # Find section to delete BEFORE removing it
                section_to_delete = None
                for s in document.sections:
                    if s.section_id == section_id:
                        section_to_delete = s
                        break

                if section_to_delete:
                    # Clear S3 extraction data before removing section
                    if section_to_delete.extraction_result_uri:
                        clear_extraction_data(section_to_delete.extraction_result_uri)
                        logger.info(
                            f"Cleared extraction data for deleted section: {section_id}"
                        )

                    # Remove section from document
                    document.sections = [
                        s for s in document.sections if s.section_id != section_id
                    ]
                    logger.info(f"Deleted section: {section_id}")
                else:
                    logger.warning(
                        f"Section {section_id} marked for deletion but not found"
                    )

                continue

            elif is_new:
                # Create new section (don't search for existing)
                logger.info(f"Creating new section: {section_id}")
                new_section = Section(
                    section_id=section_id,
                    classification=classification,
                    confidence=1.0,
                    page_ids=[str(pid) for pid in page_ids],
                    extraction_result_uri=None,
                    attributes=None,
                    confidence_threshold_alerts=[],
                )
                document.sections.append(new_section)

            else:
                # Update existing section
                existing_section = None
                for section in document.sections:
                    if section.section_id == section_id:
                        existing_section = section
                        break

                if existing_section:
                    logger.info(f"Updating existing section: {section_id}")
                    existing_section.classification = classification
                    existing_section.page_ids = [str(pid) for pid in page_ids]
                    # The class is now the operator's assertion, not the model's
                    # prediction, so the model's score for the class it replaced
                    # no longer describes this section. 1.0 follows the same
                    # convention as the other deterministic assertions (a
                    # document-name regex match, a single-class configuration).
                    existing_section.confidence = 1.0

                    # Clear extraction data for reprocessing
                    if existing_section.extraction_result_uri:
                        clear_extraction_data(existing_section.extraction_result_uri)
                        existing_section.extraction_result_uri = None
                        existing_section.attributes = None

                    # Clear confidence threshold alerts for modified sections
                    existing_section.confidence_threshold_alerts = []
                    logger.info(
                        f"Cleared confidence alerts for modified section: {section_id}"
                    )
                else:
                    logger.warning(
                        f"Section {section_id} marked as update but not found - treating as new"
                    )
                    # Treat as new section if not found
                    new_section = Section(
                        section_id=section_id,
                        classification=classification,
                        confidence=1.0,
                        page_ids=[str(pid) for pid in page_ids],
                        extraction_result_uri=None,
                        attributes=None,
                        confidence_threshold_alerts=[],
                    )
                    document.sections.append(new_section)

            # Only add to modified list if not deleted
            modified_section_ids.append(section_id)

            # Update page classifications to match section classification (only if not deleted)
            for page_id in page_ids:
                page_id_str = str(page_id)
                if page_id_str in document.pages:
                    document.pages[page_id_str].classification = classification
                    # Operator-asserted class: score it 1.0 like the section
                    # above, and DROP the model's reason — it argues for the
                    # class that was just replaced, so keeping it would show a
                    # justification for "invoice" on a page relabelled "receipt".
                    document.pages[page_id_str].confidence = 1.0
                    document.pages[page_id_str].classification_reason = None
                    logger.info(
                        f"Updated page {page_id} classification to {classification}"
                    )

        # Update document status and timing - reset for reprocessing
        current_time = datetime.now(timezone.utc).isoformat()
        document.status = Status.QUEUED
        document.initial_event_time = document.queued_time or current_time
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None

        # Sort sections by starting page ID
        document.sections.sort(
            key=lambda s: min([int(pid) for pid in s.page_ids] + [float("inf")])
        )

        # Clear rule validation data when sections are modified
        if modified_sections or modified_pages:
            clear_rule_validation_data(document.output_bucket, document.input_key)
            # Reset rule validation result on document
            document.rule_validation_result = None
            logger.info("Cleared rule validation data for reprocessing")

        logger.info(
            f"Document updated with {len(document.sections)} sections and {len(document.pages)} pages"
        )

        # Log uncompressed document for troubleshooting
        uncompressed_document_json = json.dumps(document.to_dict(), default=str)
        logger.info(
            f"Uncompressed document (size: {len(uncompressed_document_json)} chars): {uncompressed_document_json}"
        )

        # NOTE: We intentionally do NOT write the document back to the database here.
        # The processing pipeline will handle document updates via AppSync as it processes.
        # This avoids race conditions and ensures consistent state management.

        # Compress document before sending to SQS for large document optimization
        working_bucket = os.environ.get("WORKING_BUCKET")
        if working_bucket:
            # Use document compression (always compress with 0KB threshold)
            sqs_message_content = document.serialize_document(
                working_bucket, "process_changes", logger
            )
            logger.info("Document compressed for SQS (always compress)")
        else:
            # Fallback to direct document dict if no working bucket
            sqs_message_content = document.to_dict()
            logger.warning(
                "No WORKING_BUCKET configured, sending uncompressed document"
            )

        # Log the SQS message for debugging
        message_body = json.dumps(sqs_message_content, default=str)
        logger.info(f"SQS message prepared (size: {len(message_body)} chars)")
        logger.info(f"SQS message content: {message_body}")
        logger.info(f"Modified sections will be reprocessed: {modified_section_ids}")

        if QUEUE_URL:
            response = sqs_client.send_message(
                QueueUrl=QUEUE_URL, MessageBody=message_body
            )

            logger.info(
                f"Sent document to SQS queue. MessageId: {response.get('MessageId')}"
            )
            processing_job_id = response.get("MessageId")
        else:
            logger.warning("QUEUE_URL not configured, skipping SQS message")
            processing_job_id = None

        # Update document status for immediate UI feedback
        try:
            status_service = create_document_service(mode="dynamodb")
            document.status = Status.QUEUED  # Ensure status is QUEUED for UI
            status_service.update_document(document)
            logger.info("Updated document status to QUEUED for immediate UI feedback")
        except Exception as e:
            logger.warning(f"Failed to update document status: {str(e)}")
            # Don't fail the entire operation if the status update fails

        # Log successful completion
        logger.info(
            f"Successfully processed changes for {len(modified_sections)} sections"
        )

        response = {
            "success": True,
            "message": f"Successfully processed changes for {len(modified_sections)} sections",
            "processingJobId": processing_job_id,
        }

        logger.info(f"Returning response: {json.dumps(response)}")
        return response

    except PermissionError:
        # RBAC denial must reach the dispatcher as 403; do not swallow into 200.
        raise
    except Exception as e:
        logger.error(f"Error processing changes: {str(e)}", exc_info=True)

        error_response = {
            "success": False,
            "message": f"Error processing changes: {str(e)}",
            "processingJobId": None,
        }

        logger.error(f"Returning error response: {json.dumps(error_response)}")
        return error_response


def process_page_changes(document, modified_pages, modified_section_ids):
    """
    Process page-level modifications

    Args:
        document: Document object to modify
        modified_pages: List of modified page dictionaries
        modified_section_ids: List to track which sections need reprocessing
    """
    for modified_page in modified_pages:
        page_id = modified_page["pageId"]
        page_id_str = str(page_id)
        text_modified = modified_page.get("textModified", False)
        class_reset = modified_page.get("classReset", False)
        new_text_uri = modified_page.get("newTextUri")
        new_confidence_uri = modified_page.get("newConfidenceUri")

        logger.info(
            f"Processing page {page_id}: textModified={text_modified}, classReset={class_reset}"
        )

        # Check if page exists in document
        if page_id_str not in document.pages:
            logger.warning(f"Page {page_id} not found in document, skipping")
            continue

        page = document.pages[page_id_str]

        # Handle class reset - removes sections containing this page
        if class_reset:
            logger.info(f"Resetting classification for page {page_id}")
            page.classification = None  # Reset to unclassified
            # There is no class left, so its confidence and the model's reasoning
            # for it must go too — otherwise an unclassified page keeps a score
            # and an argument for a class it no longer has.
            page.confidence = None
            page.classification_reason = None

            # Find and remove all sections containing this page
            sections_to_remove = []
            for section in document.sections:
                if page_id_str in section.page_ids:
                    sections_to_remove.append(section)
                    logger.info(
                        f"Marking section {section.section_id} for removal (contains page {page_id})"
                    )

            # Clear extraction data and remove sections
            for section in sections_to_remove:
                if section.extraction_result_uri:
                    clear_extraction_data(section.extraction_result_uri)
                document.sections = [
                    s for s in document.sections if s.section_id != section.section_id
                ]
                logger.info(
                    f"Removed section {section.section_id} due to page {page_id} class reset"
                )

        # Handle text modification - clears extraction results but keeps sections
        elif text_modified:
            logger.info(f"Text modified for page {page_id}")

            # Update page URIs if provided
            if new_text_uri:
                page.text_uri = new_text_uri
                logger.info(f"Updated text URI for page {page_id}: {new_text_uri}")

            if new_confidence_uri:
                page.text_confidence_uri = new_confidence_uri
                logger.info(
                    f"Updated confidence URI for page {page_id}: {new_confidence_uri}"
                )

            # Find sections containing this page and clear their extraction results
            for section in document.sections:
                if page_id_str in section.page_ids:
                    if section.extraction_result_uri:
                        clear_extraction_data(section.extraction_result_uri)
                        section.extraction_result_uri = None
                        section.attributes = None
                        logger.info(
                            f"Cleared extraction results for section {section.section_id} (page {page_id} text modified)"
                        )

                    # Track section for reprocessing
                    if section.section_id not in modified_section_ids:
                        modified_section_ids.append(section.section_id)


def clear_extraction_data(s3_uri):
    """Clear extraction data from S3"""
    try:
        if not s3_uri or not s3_uri.startswith("s3://"):
            return

        # Parse S3 URI
        parts = s3_uri.replace("s3://", "").split("/", 1)
        if len(parts) != 2:
            return

        bucket, key = parts

        # Delete the object
        s3_client.delete_object(Bucket=bucket, Key=key)
        logger.info(f"Cleared extraction data: {s3_uri}")

    except Exception as e:
        logger.warning(f"Failed to clear extraction data {s3_uri}: {str(e)}")


def clear_rule_validation_data(bucket, input_key):
    """Clear all rule validation data for a document from S3"""
    try:
        prefix = f"{input_key}/rule_validation/"

        # List all objects with the prefix
        response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)

        if "Contents" not in response:
            logger.info(f"No rule validation files found at {prefix}")
            return

        # Delete all objects
        objects_to_delete = [{"Key": obj["Key"]} for obj in response["Contents"]]

        if objects_to_delete:
            s3_client.delete_objects(
                Bucket=bucket, Delete={"Objects": objects_to_delete}
            )
            logger.info(
                f"Cleared {len(objects_to_delete)} rule validation files from {prefix}"
            )

    except Exception as e:
        logger.warning(
            f"Failed to clear rule validation data for {input_key}: {str(e)}"
        )
