# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda function to complete HITL section review."""

import json
import logging
import os
from datetime import datetime, timezone

import boto3

from idp_common.docs_service import create_document_service
from idp_common.models import Status
from idp_common.review_queues.statuses import SKIP_ALL_REASONS, LegacyHitlStatus
from idp_common.review_queues.testset_docs import (
    TestSetDocStore,
    store_assigned_count,
)
from idp_common.review_queues.testset_summaries import summarize_document
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
sqs_client = boto3.client("sqs")

TRACKING_TABLE_NAME = os.environ.get("TRACKING_TABLE_NAME", "")
HITL_TABLE_NAME = os.environ.get("HITL_TABLE_NAME", "")
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "")
TEST_SET_BUCKET = os.environ.get("TEST_SET_BUCKET", "")
#: Longest skip note kept, the review queues' note limit.
MAX_SKIP_NOTE = 1000


def handler(event, context):
    """Handle section review completion from AppSync."""
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    field_name = event.get("info", {}).get("fieldName", "")
    arguments = event.get("arguments", {})
    object_key = arguments.get("objectKey")
    section_id = arguments.get("sectionId")
    edited_data = arguments.get("editedData")

    # Extract user identity from AppSync event
    identity = event.get("identity", {})
    username = identity.get("username", "")
    user_email = identity.get("claims", {}).get("email", "")
    user_groups = identity.get("claims", {}).get("cognito:groups", [])
    if isinstance(user_groups, str):
        user_groups = [user_groups]
    is_admin = "Admin" in user_groups

    # Defense-in-depth RBAC: HITL review operations are Admin+Reviewer, plus
    # Annotator for test-set ground-truth annotation. The schema enforces this via
    # @aws_cognito_user_pools(cognito_groups), but we also gate it server-side so
    # a Viewer/Author can never reach these operations even if the schema
    # directive is missing or misconfigured (e.g. the prior @aws_auth directive,
    # which AppSync silently ignores on a multi-auth API).
    #
    # Annotator group membership only reaches the operation; _assert_annotator_scope
    # then narrows it to documents in the caller's allowedTestSets.
    if not ({"Admin", "Reviewer", "Annotator"}.intersection(user_groups)):
        logger.warning(
            f"Forbidden: caller {user_email} (groups={user_groups}) "
            f"attempted HITL operation '{field_name}'"
        )
        raise PermissionError(
            "Unauthorized: review operations require Admin, Reviewer or Annotator group"
        )

    # Documents in a review queue are worked only through the review-queue
    # operations (leases, dispositions), for every caller (D30).
    if object_key:
        _assert_not_queue_managed(object_key)

    if field_name == "claimReview":
        if not object_key:
            raise ValueError("objectKey is required")
        _assert_annotator_scope(event, object_key)
        return claim_review(object_key, username, user_email)

    if field_name == "releaseReview":
        if not object_key:
            raise ValueError("objectKey is required")
        _assert_annotator_scope(event, object_key)
        return release_review(object_key, username, user_email, is_admin)

    if field_name == "skipAllSectionsReview":
        # Not available to Annotator: skipping marks a document reviewed without
        # looking at it, which is the set owner's decision.
        is_reviewer = "Reviewer" in user_groups
        if not is_admin and not is_reviewer:
            logger.warning(
                f"Forbidden: caller {user_email} attempted skipAllSectionsReview"
            )
            raise PermissionError(
                "Only administrators and reviewers can skip sections review"
            )
        if not object_key:
            raise ValueError("objectKey is required")
        reason = arguments.get("reason")
        if reason not in SKIP_ALL_REASONS:
            raise ValueError(
                "reason is required and must be one of: " + ", ".join(SKIP_ALL_REASONS)
            )
        note = (arguments.get("note") or "").strip()[:MAX_SKIP_NOTE] or None
        return skip_all_sections_review(
            object_key, username, user_email, reason, note, is_admin=is_admin
        )

    if field_name == "saveSectionReviewEdits":
        # Not available to Annotator, whose Test Studio saves complete the section
        # by design (completeSectionReview).
        if not is_admin and "Reviewer" not in user_groups:
            logger.warning(
                f"Forbidden: caller {user_email} attempted saveSectionReviewEdits"
            )
            raise PermissionError(
                "Only administrators and reviewers can save review edits"
            )
        if not object_key or not section_id:
            raise ValueError("objectKey and sectionId are required")
        if not edited_data:
            raise ValueError("editedData is required")
        return save_section_review_edits(
            object_key, section_id, edited_data, username, is_admin
        )

    if not object_key or not section_id:
        raise ValueError("objectKey and sectionId are required")

    _assert_annotator_scope(event, object_key)
    _assert_test_set_assignee(event, object_key, user_email)
    result = complete_section_review(
        object_key, section_id, edited_data, username, user_email
    )
    _finish_test_set_task(object_key, user_email)
    return result


def _test_set_of(object_key):
    """``(test_set_id, filename)`` of a Test Studio review document, else None."""
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    doc = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get("Item", {})
    test_set_id = doc.get("TestSetId")
    if not test_set_id:
        return None, None, doc
    filename = object_key.split("/", 1)[1] if "/" in object_key else object_key
    return test_set_id, filename, doc


def _doc_store():
    return TestSetDocStore(dynamodb.Table(HITL_TABLE_NAME))


def _finish_test_set_task(object_key, user_email):
    """Refresh the document's queue summary after a section review.

    Its reviewed-section count shows the work in progress on HITL › Queue
    Management; once every section is reviewed the assignment ends and the document
    leaves the Annotator's My Work › Tasks. Best-effort: the review itself succeeded.
    """
    try:
        test_set_id, filename, _ = _test_set_of(object_key)
        if not test_set_id or not TEST_SET_BUCKET or not HITL_TABLE_NAME:
            return
        summary = summarize_document(s3_client, TEST_SET_BUCKET, test_set_id, filename)
        store = _doc_store()
        store.upsert_summary(test_set_id, summary)
        if summary.get("labelSource") == "reviewed-human":
            store.mark_reviewed(test_set_id, filename)
            logger.info(f"Task complete: {test_set_id}/{filename} by {user_email}")
        store_assigned_count(dynamodb.Table(TRACKING_TABLE_NAME), store, test_set_id)
    except Exception as e:  # noqa: BLE001 — the review itself succeeded
        logger.info(f"Queue summary of {object_key} not refreshed after review: {e}")


def _assert_test_set_assignee(event, object_key, user_email):
    """An Annotator saves a Test Studio document only when it is assigned to them.

    Admins and Authors assign the work and are not held to it. Documents outside a
    test set are unaffected. Refusals leave as ``ValueError`` (400).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if {"Admin", "Author"} & set(groups):
        return
    test_set_id, filename, _ = _test_set_of(object_key)
    if not test_set_id:
        return
    try:
        _doc_store().assert_assignee(test_set_id, filename, user_email)
    except ValueError as e:
        logger.warning(f"Rejecting save of {object_key}: {e}")
        raise ValueError(str(e)) from None


def _assert_annotator_scope(event, object_key):
    """Verify a scoped Annotator may touch this document's test set.

    A review document carries its originating ``TestSetId``, which is checked
    against the caller's allowedTestSets. A document with no test set is production
    HITL work and is refused.

    Only Admin and Author are exempt — the same set ``testset_scope`` treats as
    unscoped. Exempting Reviewer as well turned object scope *off* for anyone holding
    both groups instead of intersecting the two, so the two enforcement layers
    disagreed: the library refuses such a caller, this Lambda waved them through.
    Since annotators are assigned by hand in Cognito (there is no external-IdP
    mapping for the role), holding both is an easy mistake to make.
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if "Annotator" not in groups or {"Admin", "Author"}.intersection(groups):
        return

    from idp_common.testset_scope import (
        TestSetAccessDenied,
        assert_can_access_test_set,
    )

    table = dynamodb.Table(TRACKING_TABLE_NAME)
    doc = (
        table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get("Item") or {}
    )
    test_set_id = doc.get("TestSetId")
    if not test_set_id:
        logger.warning(
            f"Forbidden: annotator attempted review of non-test-set document "
            f"{object_key}"
        )
        raise PermissionError(
            "Unauthorized: annotators may only review test-set documents"
        )
    try:
        assert_can_access_test_set(event, test_set_id)
    except TestSetAccessDenied as e:
        # An authorization refusal: the dispatcher maps PermissionError to 403.
        raise PermissionError(str(e)) from None


def _assert_not_queue_managed(object_key):
    """Refuse a document whose review is managed by a review queue (D30)."""
    from idp_common.review_queues.statuses import normalize_hitl_status

    table = dynamodb.Table(TRACKING_TABLE_NAME)
    doc = (
        table.get_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"},
            ProjectionExpression="HITLStatus",
        ).get("Item")
        or {}
    )
    if normalize_hitl_status(doc.get("HITLStatus")).queue_managed:
        logger.warning("Rejecting legacy review operation on a queue-managed document")
        raise ValueError(
            "QUEUE_MANAGED: this document is in a review queue; work it from Review Queues"
        )


def _review_finished(hitl_status):
    """Whether a stored ``HITLStatus`` says the document's review has finished.

    The document lists' rule (``list_documents_gsi_resolver._review_finished``) and
    the web UI's ``isHitlReviewFinished``: a legacy review completed or skipped, or
    a queue review that ended complete or rejected. ``COMPLETED`` is the one
    finished spelling a real writer stored that the exact-match normaliser does not
    know: the earlier A2I flow wrote it, and retained rows may still carry it.
    """
    from idp_common.review_queues.statuses import normalize_hitl_status

    return (
        normalize_hitl_status(hitl_status).kind in ("complete", "rejected", "skipped")
        or hitl_status == "COMPLETED"
    )


def _finished_hitl_statuses():
    """Every stored ``HITLStatus`` value :func:`_review_finished` accepts.

    Derived from the declared vocabularies through the rule itself, so a
    ``ConditionExpression`` can test it. Exact values: the normaliser also accepts
    surrounding whitespace, which no writer stores, and which the consistent read
    before each guarded write refuses through the rule.
    """
    from idp_common.review_queues.statuses import LegacyHitlStatus, ReviewStatus

    return tuple(
        sorted(
            value
            for value in {
                *(s.value for s in ReviewStatus),
                *(s.value for s in LegacyHitlStatus),
                "COMPLETED",
            }
            if _review_finished(value)
        )
    )


def _not_finished_condition():
    """``(condition, values)`` refusing a write onto a finished review.

    An absent ``HITLStatus`` makes ``IN`` false, so the condition holds for it.
    """
    statuses = _finished_hitl_statuses()
    values = {f":finished{i}": value for i, value in enumerate(statuses)}
    return f"NOT (HITLStatus IN ({', '.join(values)}))", values


def _review_finished_error(action):
    return ValueError(
        f"REVIEW_FINISHED: this review is already finished, so there is nothing "
        f"to {action}"
    )


#: ``HITLStatus`` values the web UI reads as completed or skipped when it decides
#: whether to offer Skip All Reviews (``SectionsPanel.tsx``: lower-cased, all
#: whitespace removed). Looser than :func:`_review_finished`, which matches exactly
#: after ``strip()``; the server refuses wherever either one reads finished.
_UI_SKIP_HIDDEN_STATUSES = frozenset(
    {"completed", "reviewcompleted", "skipped", "reviewskipped"}
)


def _skip_all_offered(doc):
    """``True`` where the web UI offers Skip All Reviews, else why it does not.

    The rule behind ``showSkipAllButton``: a review was raised (``hitlTriggered`` in
    ``map-document-attributes.ts``: ``HITLTriggered`` is true, or ``HITLStatus`` is
    set and not ``N/A``) and it is not completed or skipped. Returns ``"finished"``
    or ``"not raised"`` otherwise. A queue-managed row never reaches here
    (``_assert_not_queue_managed``).
    """
    status = doc.get("HITLStatus")
    compact = "".join(str(status or "").lower().split())
    if _review_finished(status) or compact in _UI_SKIP_HIDDEN_STATUSES:
        return "finished"
    if _review_raised(doc):
        return True
    return "not raised"


def _review_raised(doc):
    """Whether a review was raised on a tracking row, finished or not.

    The web UI's ``hitlTriggered`` (``map-document-attributes.ts``): ``HITLTriggered``
    is true, or ``HITLStatus`` is set and not ``N/A``. An ownerless ``Review
    Pending`` row written by an earlier version counts, by its status. "Set" means a
    non-empty string, which is what every writer stores, so that
    :func:`_review_raised_condition` can say the same thing at the write.
    """
    status = doc.get("HITLStatus")
    return doc.get("HITLTriggered") is True or (
        isinstance(status, str) and status not in ("", "N/A")
    )


def _review_raised_condition():
    """``(condition, values)`` holding :func:`_review_raised` at the moment of a write.

    ``attribute_type(..., S)`` rather than ``attribute_exists``, so a stored NULL or
    any other non-string ``HITLStatus`` reads as unset here as it does in the rule.
    """
    return (
        "(HITLTriggered = :raisedFlag OR (attribute_type(HITLStatus, :raisedType) "
        "AND HITLStatus <> :raisedUnset AND HITLStatus <> :raisedNone))",
        {
            ":raisedFlag": True,
            ":raisedType": "S",
            ":raisedUnset": "",
            ":raisedNone": "N/A",
        },
    )


def _no_review_pending_error(action):
    return ValueError(
        f"NO_REVIEW_PENDING: this document has no review waiting, so there is "
        f"nothing to {action}"
    )


def _read_review_row(object_key):
    """The tracking row, read consistently so a review finished a moment ago shows."""
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    return (
        table.get_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"}, ConsistentRead=True
        ).get("Item")
        or {}
    )


def _save_section_edits(document, object_key, section_id, edited_data):
    """Write a section's edited result JSON to its output URI, or raise."""
    section = next((s for s in document.sections if s.section_id == section_id), None)
    if section is None:
        raise ValueError(
            f"Cannot save edited data: section '{section_id}' not found in "
            f"document '{object_key}'"
        )
    if not section.extraction_result_uri:
        raise ValueError(
            f"Cannot save edited data: section '{section_id}' in document "
            f"'{object_key}' has no output URI to write to"
        )
    save_edited_data_to_s3(section.extraction_result_uri, edited_data)


def save_section_review_edits(
    object_key, section_id, edited_data, username="", is_admin=False
):
    """Save a reviewer's edits to a section without completing its review.

    The review stays In Progress with its owner; it completes only through
    processChanges or skipAllSectionsReview. Only the review owner, or an Admin,
    may save, and not once the review has finished.
    """
    document_service = create_document_service(mode="dynamodb")
    document = document_service.get_document(object_key)
    if not document:
        raise ValueError(f"Document {object_key} not found")

    table = dynamodb.Table(TRACKING_TABLE_NAME)
    doc = (
        table.get_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"},
            ProjectionExpression="HITLStatus, HITLReviewOwner",
        ).get("Item")
        or {}
    )
    # Queue statuses never reach here (_assert_not_queue_managed).
    if _review_finished(doc.get("HITLStatus")):
        logger.warning(
            f"Rejecting save of review edits on finished review {object_key}"
        )
        raise ValueError("This review is already finished; its edits are closed")
    if not is_admin and doc.get("HITLReviewOwner", "") != username:
        logger.warning(f"Denied: save of review edits on {object_key} by a non-owner")
        raise PermissionError(
            "Only the review owner or an admin can save edits; start the review first"
        )

    _save_section_edits(document, object_key, section_id, edited_data)
    logger.info(
        f"Saved review edits for section {section_id} of {object_key} by {username}"
    )
    return build_document_response(object_key)


def complete_section_review(
    object_key, section_id, edited_data=None, username="", user_email=""
):
    """Mark a section as review complete and update document status."""
    logger.info(
        f"Completing review for section {section_id} of document {object_key} by user {username}"
    )

    # Load document using document service
    document_service = create_document_service(mode="dynamodb")
    document = document_service.get_document(object_key)

    if not document:
        raise ValueError(f"Document {object_key} not found")

    # If the caller supplied edited data, we MUST be able to persist it. Fail
    # loudly instead of marking the section reviewed with the edits silently
    # dropped (which would return success while losing the reviewer's work).
    if edited_data:
        _save_section_edits(document, object_key, section_id, edited_data)
        write_correction_to_test_set_baseline(
            object_key, section_id, edited_data, username, user_email
        )
    else:
        # Confirming labels unchanged is still a review: it is a human asserting
        # the extraction is correct, which is exactly what a golden dataset
        # records. Without this the baseline kept its draft-machine tag, so the
        # test set showed "Awaiting review" after every document had been
        # confirmed, a later labeling run could overwrite the confirmation, and
        # the confidence curve never learned that those fields were right.
        confirm_test_set_baseline_reviewed(object_key, section_id, username, user_email)

    # Get current pending and completed sections from document model
    pending = set(document.hitl_sections_pending or [])
    completed = set(document.hitl_sections_completed or [])

    # Get skipped from DynamoDB (not in document model)
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    response = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
    doc = response.get("Item", {})
    skipped = set(doc.get("HITLSectionsSkipped", []) or [])

    # If HITLSectionsPending was never initialized, initialize it from all sections.
    # Every section starts pending, including the one being reviewed now: the move to
    # completed just below is the single place a section leaves the pending set, on
    # this path and on the one where the set came from the stored item.
    if not pending and not completed and not skipped:
        pending = {
            section.section_id for section in document.sections if section.section_id
        }
        logger.info(f"Initialized HITLSectionsPending from sections: {pending}")

    # Move section from pending to completed
    if section_id in pending:
        pending.remove(section_id)
    completed.add(section_id)

    # Check if all sections are reviewed (completed or skipped)
    all_completed = len(pending) == 0
    has_skipped = len(skipped) > 0

    # Determine new Review Status
    if all_completed:
        new_hitl_status = (
            LegacyHitlStatus.SKIPPED.value
            if has_skipped
            else LegacyHitlStatus.COMPLETED.value
        )
    else:
        new_hitl_status = LegacyHitlStatus.IN_PROGRESS.value

    # Update document model with Review Status
    document.hitl_status = new_hitl_status
    document.hitl_sections_pending = list(pending)
    document.hitl_sections_completed = list(completed)

    # Update via document service
    document_service.update_document(document)
    logger.info(
        f"Updated HITLStatus to '{new_hitl_status}' for document {object_key}. "
        f"Pending: {list(pending)}, Completed: {list(completed)}, All done: {all_completed}"
    )

    # Update review-specific fields in DynamoDB (not in document model)
    review_record = {
        "sectionId": section_id,
        "reviewedBy": username or "unknown",
        "reviewedByEmail": user_email or "",
        "reviewedAt": datetime.now(timezone.utc).isoformat(),
    }
    # Appended by DynamoDB rather than by this process, so a second annotator
    # finishing another section in the same window cannot overwrite this record.
    # Reading the array, appending in Python and writing the whole array back is
    # a lost update: both writers succeed, and the loser's entry is gone with
    # nothing reporting it. `list_append` is the right remedy here rather than a
    # version check because the attribute is append-only and order-insensitive —
    # no writer needs to see another's entry, so there is no conflict to detect
    # and no retry to get wrong. `if_not_exists` covers the first entry on a
    # document that has never been reviewed, where the attribute is absent.
    update_expr = (
        "SET HITLReviewHistory = "
        "list_append(if_not_exists(HITLReviewHistory, :empty_history), :new_history)"
    )
    expr_values = {":empty_history": [], ":new_history": [review_record]}

    if all_completed:
        # Record who finished the review, as skipAllSectionsReview and
        # processChanges do; without it "Review Completed By" reads blank.
        update_expr += (
            ", HITLCompleted = :hitlCompleted, HITLReviewedBy = :reviewedBy, "
            "HITLReviewedByEmail = :reviewedByEmail"
        )
        expr_values[":hitlCompleted"] = True
        expr_values[":reviewedBy"] = username or "unknown"
        expr_values[":reviewedByEmail"] = user_email or ""
        update_expr += " REMOVE HITLPendingReview"

    table.update_item(
        Key={"PK": f"doc#{object_key}", "SK": "none"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )

    logger.info(
        f"Section {section_id} marked complete. Remaining: {len(pending)}. All done: {all_completed}"
    )

    # If all sections are completed, trigger reprocessing for summarization/evaluation
    if all_completed:
        trigger_reprocessing(object_key)

    # Return document data
    return build_document_response(object_key)


def save_edited_data_to_s3(s3_uri, edited_data):
    """Save edited JSON data back to S3."""
    try:
        # Parse S3 URI: s3://bucket/key
        if not s3_uri.startswith("s3://"):
            logger.error(f"Invalid S3 URI: {s3_uri}")
            return

        parts = s3_uri[5:].split("/", 1)
        if len(parts) != 2:
            logger.error(f"Invalid S3 URI format: {s3_uri}")
            return

        bucket = parts[0]
        key = parts[1]

        # Parse edited_data if it's a string
        if isinstance(edited_data, str):
            data = json.loads(edited_data)
        else:
            data = edited_data

        # UI sends full JSON structure (with inference_result, explainability_info, etc.)
        # Save it directly - no transformation needed
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(data, indent=2),
            ContentType="application/json",
        )
        logger.info(f"Saved edited data to {s3_uri}")

    except Exception as e:
        logger.error(f"Failed to save edited data to S3: {str(e)}")
        raise


def write_correction_to_test_set_baseline(
    object_key, section_id, edited_data, username="", user_email=""
):
    """Persist a HITL correction to the owning test set's baseline (ground truth).

    A test-set review document is keyed ``{test_run_id}/{filename}`` and carries
    ``TestSetId``; the baseline it maps to is
    ``{test_set_id}/baseline/{filename}/sections/{section_id}/result.json``. Writing
    there, and not only to the document's own output, is what makes a review
    reusable as versionable ground truth. Best-effort: a document outside a test
    set, or a failed write, must not fail the review.
    """
    if not TEST_SET_BUCKET:
        return
    try:
        table = dynamodb.Table(TRACKING_TABLE_NAME)
        doc = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get(
            "Item", {}
        )
        test_set_id = doc.get("TestSetId")
        if not test_set_id:
            return  # Not a test-set review document — nothing to do.

        # object_key is "{test_run_id}/{filename}"; the baseline is keyed by filename.
        filename = object_key.split("/", 1)[1] if "/" in object_key else object_key
        baseline_key = (
            f"{test_set_id}/baseline/{filename}/sections/{section_id}/result.json"
        )
        data = json.loads(edited_data) if isinstance(edited_data, str) else edited_data

        # Read the label being replaced before overwriting it: the diff against
        # what the reviewer saved is the only evidence of whether the model was
        # right, and the write destroys it.
        previous = _read_json(TEST_SET_BUCKET, baseline_key)

        # Marks the label human-reviewed so draft labeling leaves it alone; the
        # harvester only replaces labels tagged draft-machine.
        if isinstance(data, dict):
            data["labelSource"] = "reviewed-human"
            append_edit_history(previous, data, username, user_email)

        s3_client.put_object(
            Bucket=TEST_SET_BUCKET,
            Key=baseline_key,
            Body=json.dumps(data, indent=2),
            ContentType="application/json",
        )
        logger.info(
            f"Wrote HITL correction to test-set baseline "
            f"s3://{TEST_SET_BUCKET}/{baseline_key}"
        )

        record_curve_observations(test_set_id, previous, data)
        was_reviewed = (
            isinstance(previous, dict)
            and previous.get("labelSource") == "reviewed-human"
        )
        if not was_reviewed:
            count_document_if_fully_reviewed(
                table, test_set_id, filename, section_id, doc.get("Sections") or []
            )
    except Exception as e:  # noqa: BLE001 — best-effort; must not break the review
        logger.error(
            f"Failed to write correction to test-set baseline for {object_key}: {e}"
        )


def count_document_if_fully_reviewed(
    table, test_set_id, filename, section_id, sections
):
    """Add one to the set's reviewed-document count when this was its last section.

    A document counts as reviewed only when every section is, so the other
    sections' baselines are checked (none for a single-section document). The
    count exists only once getAnnotationQueue has stored a full one, so the update
    is conditional on it. Best-effort.
    """
    others = [
        str(s.get("Id"))
        for s in sections
        if isinstance(s, dict)
        and s.get("Id") is not None
        and str(s.get("Id")) != str(section_id)
    ]
    for other in others:
        label = _read_json(
            TEST_SET_BUCKET,
            f"{test_set_id}/baseline/{filename}/sections/{other}/result.json",
        )
        if not isinstance(label, dict) or label.get("labelSource") != "reviewed-human":
            return
    try:
        table.update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="ADD QueueReviewedDocs :one SET QueueCountsAt = :at",
            ConditionExpression="attribute_exists(QueueReviewedDocs)",
            ExpressionAttributeValues={
                ":one": 1,
                ":at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        )
    except Exception as e:  # noqa: BLE001 — no stored count yet, or a transient fault
        logger.info(f"Reviewed count for test set {test_set_id} not updated: {e}")


def confirm_test_set_baseline_reviewed(
    object_key, section_id, username="", user_email=""
):
    """Tag a baseline human-reviewed when the reviewer changed nothing.

    "The labels are correct" is a verdict, not the absence of one: every field
    keeps its predicted value, and a human has asserted those values are right.
    The baseline is rewritten in place so it carries ``reviewed-human``, gains a
    revision entry, and feeds the curve observations that all say "the model was
    correct" — the high-confidence evidence review otherwise never produces.

    Best-effort, and a no-op outside a test set or when no baseline exists yet.
    """
    if not TEST_SET_BUCKET:
        return
    try:
        table = dynamodb.Table(TRACKING_TABLE_NAME)
        doc = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"}).get(
            "Item", {}
        )
        test_set_id = doc.get("TestSetId")
        if not test_set_id:
            return

        filename = object_key.split("/", 1)[1] if "/" in object_key else object_key
        baseline_key = (
            f"{test_set_id}/baseline/{filename}/sections/{section_id}/result.json"
        )
        existing = _read_json(TEST_SET_BUCKET, baseline_key)
        if not isinstance(existing, dict):
            logger.info(
                f"No baseline to confirm at s3://{TEST_SET_BUCKET}/{baseline_key}"
            )
            return
        if existing.get("labelSource") == "reviewed-human":
            return  # Already confirmed; nothing to record.

        # Reuse the correction path with the values unchanged: confirming asserts
        # the current values ARE the ground truth, so the same provenance stamp,
        # revision entry and curve observations apply.
        write_correction_to_test_set_baseline(
            object_key, section_id, existing, username, user_email
        )
    except Exception as e:  # noqa: BLE001 — must not break the review
        logger.error(f"Failed to confirm test-set baseline for {object_key}: {e}")


# Caps the trail on a repeatedly reviewed label; the newest entries are kept.
MAX_EDIT_HISTORY_ENTRIES = 50


def append_edit_history(previous, saved, username, user_email):
    """Record who changed what, in the label itself.

    The ground-truth viewer reads provenance from ``_editHistory`` inside the
    label JSON, so a review recorded only in DynamoDB (HITLReviewHistory) is
    invisible to it. The field-level diff is taken against the label being
    replaced, which is already in hand for the curve observation.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "editedBy": username or "unknown",
        "editedByEmail": user_email or "",
        "source": "annotation-review",
    }
    diffs = _field_diffs(previous, saved)
    if diffs:
        entry["baselineEdits"] = {
            "changedFields": list(diffs.keys()),
            "changeCount": len(diffs),
            "diffs": diffs,
        }

    # The trail is server-owned: seed it from the stored label, since a client that
    # does not round-trip _editHistory would otherwise erase every prior entry.
    # Taking the longer of the two also stops a client from truncating it.
    stored = (previous or {}).get("_editHistory")
    incoming = saved.get("_editHistory")
    history = stored if isinstance(stored, list) else []
    if isinstance(incoming, list) and len(incoming) > len(history):
        history = incoming
    saved["_editHistory"] = [*history, entry][-MAX_EDIT_HISTORY_ENTRIES:]


def _field_diffs(previous, saved):
    """Field path → {originalValue, newValue} for every value the review changed.

    Uses the same flattening as the curve, so "changed" means the same thing to
    the audit trail and to the calibration signal.
    """
    if not previous:
        return {}
    try:
        from idp_common.evaluation.curve_store import flatten_values

        before = flatten_values(previous.get("inference_result") or {})
        after = flatten_values(saved.get("inference_result") or {})
    except Exception as e:  # noqa: BLE001 — provenance must not fail the save
        logger.warning(f"Could not diff review changes: {e}")
        return {}

    diffs = {}
    for path in set(before) | set(after):
        original = before.get(path)
        new = after.get(path)
        if original != new:
            diffs[path] = {"originalValue": original, "newValue": new}
    return diffs


def _read_json(bucket, key):
    """Read a JSON object from S3, or None if absent/unreadable."""
    try:
        body = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()
        return json.loads(body)
    except Exception:  # noqa: BLE001 — absence is normal (first review of a doc)
        return None


def record_curve_observations(test_set_id, previous, saved):
    """Record confidence→accuracy observations from this review.

    A field the reviewer left alone was predicted correctly; a changed one was not.
    Paired with the model's claimed confidence, that is the ``(confidence, correct)``
    observation the review-effort estimator's curve is built from. Best-effort: the
    curve is an optimization and must never fail a reviewer's save.
    """
    if not previous:
        return  # No prior prediction, so there is no verdict to record.
    try:
        from idp_common.evaluation.curve_store import (
            CurveStore,
            observations_from_baseline_review,
        )

        observations = observations_from_baseline_review(previous, saved)
        if not observations:
            return

        table = dynamodb.Table(TRACKING_TABLE_NAME)
        # Key the curve by the config that produced these labels; a later config
        # change must not inherit a curve measured under different confidence
        # semantics.
        label_meta = previous.get("metadata") or {}
        config_version = label_meta.get("config_version")
        # ...and by the revision family that drafted them (#698): the harvest
        # copies the run's confidence fingerprint onto the label beside the
        # config version, so a later model swap on the same profile starts a new
        # curve instead of inheriting this one.
        fingerprint = label_meta.get("confidence_fingerprint") or None
        accepted = CurveStore(table).add_observations(
            test_set_id,
            observations,
            config_version=config_version,
            source="review",
            fingerprint=fingerprint,
        )
        logger.info(
            f"Recorded {accepted} confidence-curve observation(s) for test set "
            f"{test_set_id} from review"
        )
    except Exception as e:  # noqa: BLE001 — never break a review over the curve
        logger.warning(
            f"Could not record confidence-curve observations for {test_set_id}: {e}"
        )


def trigger_reprocessing(object_key):
    """Trigger reprocessing via SQS queue after HITL completion.

    Uses the same pattern as processChanges - sends document to queue,
    workflow runs with intelligent skip logic (OCR/Classification/Extraction/Assessment
    are skipped since data exists), only Summarization and Evaluation re-run.
    """
    try:
        # Load document from DynamoDB
        dynamodb_service = create_document_service(mode="dynamodb")
        document = dynamodb_service.get_document(object_key)

        if not document:
            logger.error(f"Document {object_key} not found for reprocessing")
            return

        # Set bucket names from environment
        document.input_bucket = os.environ.get("INPUT_BUCKET")
        document.output_bucket = os.environ.get("OUTPUT_BUCKET")

        # Reset status for reprocessing
        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None

        # Compress and send to queue (same pattern as processChanges)
        working_bucket = os.environ.get("WORKING_BUCKET")
        if working_bucket:
            sqs_message = document.serialize_document(
                working_bucket, "hitl_complete", logger
            )
        else:
            sqs_message = document.to_dict()

        queue_url = os.environ.get("QUEUE_URL")
        if queue_url:
            sqs_client.send_message(
                QueueUrl=queue_url, MessageBody=json.dumps(sqs_message, default=str)
            )
            logger.info(
                f"Queued document {object_key} for reprocessing after HITL completion"
            )
        else:
            logger.warning("QUEUE_URL not configured, skipping reprocessing trigger")

    except Exception as e:
        logger.error(f"Failed to trigger reprocessing for {object_key}: {str(e)}")


def skip_all_sections_review(
    object_key, username="", user_email="", reason=None, note=None, is_admin=False
):
    """Skip all pending section reviews and mark the document complete.

    ``reason`` and ``note`` say why nobody checked it, and are kept with the skip in
    ``HITLReviewHistory``. A caller who is not an Admin may skip only where the web
    UI offers Skip All Reviews: a review was raised and has not finished.
    """
    logger.info(
        f"Skipping all sections review for document {object_key} by admin {username}"
    )

    # Load document using document service to verify it exists
    document_service = create_document_service(mode="dynamodb")
    document = document_service.get_document(object_key)

    if not document:
        raise ValueError(f"Document {object_key} not found")

    completed = set(document.hitl_sections_completed or [])

    # Get skipped from DynamoDB (not in document model). Read consistently, so a
    # review finished a moment ago is seen by the check below.
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    doc = _read_review_row(object_key)
    existing_skipped = set(doc.get("HITLSectionsSkipped", []) or [])

    # Admins may skip any document. Anyone else only where the web UI offers the
    # button; the write's condition below holds the finished part of that at the
    # moment of the write.
    if not is_admin:
        offered = _skip_all_offered(doc)
        if offered is not True:
            logger.warning(f"Rejecting skip-all on {object_key}: {offered}")
            if offered == "finished":
                raise _review_finished_error("skip")
            raise _no_review_pending_error("skip")

    # Get all section IDs from the document
    all_section_ids = {
        section.section_id for section in document.sections if section.section_id
    }

    # Sections to skip = all sections that are not already completed
    sections_to_skip = all_section_ids - completed - existing_skipped
    all_skipped = list(sections_to_skip | existing_skipped)

    # Update review-specific fields directly in DynamoDB
    review_record = {
        "sectionId": "ALL_SKIPPED",
        "reviewedBy": username or "unknown",
        "reviewedByEmail": user_email or "",
        "reviewedAt": datetime.now(timezone.utc).isoformat(),
        "action": "skip_all",
        "skippedSections": list(sections_to_skip),
        **({"reason": reason} if reason else {}),
        **({"note": note} if note else {}),
    }
    # Appended by DynamoDB, for the same reason as in complete_section_review
    # above: a reviewer finishing a section while an admin skips the rest would
    # otherwise have their entry overwritten by whichever of the two wrote last.
    #
    # `HITLSectionsSkipped` in the same statement is still a whole-list write, and
    # it is **not** safe under the same overlap. Stating the residual precisely,
    # because the shape invites a wrong reading: `all_skipped` unions
    # `existing_skipped` in, so nothing another skip-all recorded is dropped, and
    # two skip-alls that read the same `completed` do compute the same set. What
    # diverges is `completed` itself. A reviewer finishing a section while this runs
    # means one of the two callers computed `all_section_ids - completed` from a
    # `completed` that did not yet name that section, so it lands in
    # `HITLSectionsSkipped` as well as in `HITLSectionsCompleted` -- and because the
    # union is monotonic, clicking Skip All again reproduces it rather than clearing
    # it. Measured, not assumed. What an operator sees is a section recorded both
    # reviewed and skipped, and a later `complete_section_review` reading
    # `has_skipped` and reporting `Skipped` for a document whose sections were all
    # actually reviewed.
    #
    # It is left here because the input that diverges is `completed`, which arrives
    # from the document model rather than from the read above, and every writer of
    # that attribute goes through `DocumentDynamoDBService.update_document` -- one
    # unconditional whole-document write shared by the pipeline and several
    # resolvers. Guarding one caller of it in isolation is the change that looks
    # like a fix and is not. Reported in #1111 with the other sites on that path.
    update = {
        "Key": {"PK": f"doc#{object_key}", "SK": "none"},
        "UpdateExpression": (
            "SET HITLStatus = :status, HITLSectionsPending = :pending, "
            "HITLSectionsSkipped = :skipped, "
            "HITLReviewHistory = list_append("
            "if_not_exists(HITLReviewHistory, :empty_history), :new_history), "
            "HITLCompleted = :hitlCompleted, HITLReviewedBy = :reviewedBy, "
            "HITLReviewedByEmail = :reviewedByEmail REMOVE HITLPendingReview"
        ),
        "ExpressionAttributeValues": {
            ":status": LegacyHitlStatus.REVIEW_SKIPPED.value,
            ":pending": [],
            ":skipped": all_skipped,
            ":empty_history": [],
            ":new_history": [review_record],
            ":hitlCompleted": True,
            ":reviewedBy": username or "unknown",
            ":reviewedByEmail": user_email or "",
        },
    }
    if not is_admin:
        not_finished, finished_values = _not_finished_condition()
        update["ConditionExpression"] = not_finished
        update["ExpressionAttributeValues"].update(finished_values)
    try:
        table.update_item(**update)
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning(
            f"Rejecting skip-all of a review finished during it {object_key}"
        )
        raise _review_finished_error("skip") from None

    logger.info(
        f"All sections skipped for document {object_key}. Skipped: {all_skipped}, Completed: {list(completed)}"
    )

    # Skipping all reviews resolves every pending section, so the document is now
    # fully reviewed — exactly like completing the final section via
    # complete_section_review (which calls trigger_reprocessing on all_completed).
    # Trigger the same downstream reprocessing here so the two "finish review"
    # paths behave identically: it re-runs Summarization/Evaluation with the
    # existing (unedited) data and, on workflow success, emits the Step Functions
    # "SUCCEEDED" event that drives the optional post-processing Lambda hook
    # (PostProcessingLambdaHookFunctionArn). Without this call, skipping reviews
    # would finalize the document but never run post-processing — an inconsistency
    # with the section-by-section completion path.
    trigger_reprocessing(object_key)

    return build_document_response(object_key)


def claim_review(object_key, username="", user_email=""):
    """Claim a document for review (assigns reviewer as owner)."""
    logger.info(f"Claiming review for document {object_key} by {username}")

    # Load document using document service to verify it exists
    document_service = create_document_service(mode="dynamodb")
    document = document_service.get_document(object_key)

    if not document:
        raise ValueError(f"Document {object_key} not found")

    # A finished review stays finished: claiming it would set it back to In
    # Progress and reopen its edits. Read consistently, so a review finished a
    # moment ago is refused; the write's condition below holds the same rule at the
    # moment of the write.
    #
    # Claiming also needs a review to have been raised, or it would write Review In
    # Progress onto a document nobody routed for review and list it as one. This is
    # the rule the web UI offers Start Review by, and it holds for every caller,
    # Admins included, like the finished check. The write's condition holds it too.
    row = _read_review_row(object_key)
    if _review_finished(row.get("HITLStatus")):
        logger.warning(f"Rejecting claim of finished review {object_key}")
        raise _review_finished_error("start")
    if not _review_raised(row):
        logger.warning(f"Rejecting claim of {object_key}: no review was raised")
        raise _no_review_pending_error("start")

    table = dynamodb.Table(TRACKING_TABLE_NAME)
    not_finished, finished_values = _not_finished_condition()
    raised, raised_values = _review_raised_condition()

    # Conditional, so the claim is exclusive rather than advisory, and the condition
    # is the *only* ownership test here. A read-then-check before this write cannot
    # be one: two annotators clicking Claim at the same moment both pass it and both
    # write, and the collaborative queue is exactly the feature that depends on only
    # one winning. It also cannot refuse anything this condition accepts, since it
    # answers from an eventually consistent read — a document released a moment ago
    # can still read as owned, and refusing that claim is wrong. So ownership is
    # decided once, here, and the recovery path below phrases the refusal. Updating
    # in place (rather than via the document model) also avoids re-serializing
    # metering data.
    try:
        table.update_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"},
            UpdateExpression="SET HITLStatus = :status, HITLReviewOwner = :owner, HITLReviewOwnerEmail = :email",
            ConditionExpression=(
                "(attribute_not_exists(HITLReviewOwner) OR HITLReviewOwner = :owner "
                f"OR HITLReviewOwner = :empty) AND {not_finished} AND {raised}"
            ),
            ExpressionAttributeValues={
                ":status": LegacyHitlStatus.IN_PROGRESS.value,
                ":owner": username,
                ":email": user_email,
                ":empty": "",
                **finished_values,
                **raised_values,
            },
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        # Lost the race. Re-read so the message names the actual winner, and keep the
        # "already claimed" phrasing — the UI matches on it to skip to the next
        # document instead of dead-ending. Read consistently, so a review that
        # finished in the race is reported as finished rather than as claimed by its
        # owner. A winner with no recorded name is reported as "another reviewer",
        # which keeps the phrase the UI keys on.
        current = (
            table.get_item(
                Key={"PK": f"doc#{object_key}", "SK": "none"}, ConsistentRead=True
            ).get("Item")
            or {}
        )
        if _review_finished(current.get("HITLStatus")):
            logger.warning(
                f"Rejecting claim of a review finished during it {object_key}"
            )
            raise _review_finished_error("start") from None
        if not _review_raised(current):
            logger.warning(f"Rejecting claim of {object_key}: no review is raised now")
            raise _no_review_pending_error("start") from None
        winner = current.get("HITLReviewOwner") or "another reviewer"
        logger.info(f"Claim race lost on {object_key}: already claimed by {winner}")
        raise ValueError(f"Document is already claimed by {winner}") from None

    logger.info(
        f"Review claimed for document {object_key} by {username}, HITLStatus set to InProgress"
    )
    return build_document_response(object_key)


def release_review(object_key, username="", user_email="", is_admin=False):
    """Release a document review (removes owner assignment)."""
    logger.info(f"Releasing review for document {object_key} by {username}")

    # Load document using document service to verify it exists
    document_service = create_document_service(mode="dynamodb")
    document = document_service.get_document(object_key)

    if not document:
        raise ValueError(f"Document {object_key} not found")

    table = dynamodb.Table(TRACKING_TABLE_NAME)
    # Consistent, so a release straight after a claim sees the claim's owner.
    response = table.get_item(
        Key={"PK": f"doc#{object_key}", "SK": "none"}, ConsistentRead=True
    )
    doc = response.get("Item", {})
    current_owner = doc.get("HITLReviewOwner", "")

    # Releasing gives up a claim, and writes Review Pending, which lists the
    # document for every Reviewer. With no claim there is nothing to give up, and
    # the row may be one no review was raised on or a finished review the list
    # recognises only by its status; the web UI's bulk Release sends every selected
    # row. So an unclaimed document is refused rather than put in every Reviewer's
    # queue.
    if not current_owner:
        logger.warning("Rejecting release of a document nobody has claimed")
        raise ValueError(
            "NOT_CLAIMED: nobody has claimed this review, so there is nothing to release"
        )

    # A finished review keeps its owner (the reviewer who finished it), so the
    # NOT_CLAIMED check above admits it; releasing it would write Review Pending and
    # put a finished review back in every Reviewer's queue. Refused for every caller,
    # as the web UI offers Release on no finished review.
    if _review_finished(doc.get("HITLStatus")):
        logger.warning(f"Rejecting release of finished review {object_key}")
        raise _review_finished_error("release")

    if not is_admin and current_owner and current_owner != username:
        logger.warning("Forbidden: release of a review owned by another user")
        raise PermissionError(
            "Only the review owner or an admin can release this review"
        )

    # Update Review Status and remove review owner directly in DynamoDB
    # This avoids re-serializing metering data which could cause issues
    not_finished, finished_values = _not_finished_condition()
    try:
        table.update_item(
            Key={"PK": f"doc#{object_key}", "SK": "none"},
            UpdateExpression="SET HITLStatus = :status, HITLPendingReview = :pending REMOVE HITLReviewOwner, HITLReviewOwnerEmail",
            ConditionExpression=not_finished,
            ExpressionAttributeValues={
                ":status": LegacyHitlStatus.REVIEW_PENDING.value,
                ":pending": "true",
                **finished_values,
            },
        )
    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
        logger.warning(f"Rejecting release of a review finished during it {object_key}")
        raise _review_finished_error("release") from None

    logger.info(
        f"Review released for document {object_key}, HITLStatus set to Review Pending"
    )
    return build_document_response(object_key)


def _convert_decimals(obj):
    """Recursively convert Decimal values to int/float for JSON serialization."""
    from decimal import Decimal

    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_convert_decimals(i) for i in obj]
    elif isinstance(obj, set):
        return [_convert_decimals(i) for i in obj]
    return obj


def build_document_response(object_key):
    """Build standard document response."""
    table = dynamodb.Table(TRACKING_TABLE_NAME)
    response = table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
    doc = response.get("Item", {})

    # Convert all Decimal values for JSON serialization
    doc = _convert_decimals(doc)

    result = {
        "ObjectKey": object_key,
        "ObjectStatus": doc.get("ObjectStatus", ""),
        "InitialEventTime": doc.get("InitialEventTime", ""),
        "QueuedTime": doc.get("QueuedTime", ""),
        "WorkflowStartTime": doc.get("WorkflowStartTime", ""),
        "CompletionTime": doc.get("CompletionTime", ""),
        "WorkflowExecutionArn": doc.get("WorkflowExecutionArn", ""),
        "WorkflowStatus": doc.get("WorkflowStatus", ""),
        "PageCount": doc.get("PageCount", 0),
        "Sections": doc.get("Sections", []),
        "Pages": doc.get("Pages", []),
        "Metering": doc.get("Metering", ""),
        "EvaluationReportUri": doc.get("EvaluationReportUri", ""),
        "EvaluationStatus": doc.get("EvaluationStatus", ""),
        "SummaryReportUri": doc.get("SummaryReportUri", ""),
        "HITLStatus": doc.get("HITLStatus", ""),
        "HITLTriggered": doc.get("HITLTriggered", False),
        "HITLCompleted": doc.get("HITLCompleted", False),
        "HITLReviewURL": doc.get("HITLReviewURL", ""),
        "HITLSectionsPending": doc.get("HITLSectionsPending", []),
        "HITLSectionsCompleted": doc.get("HITLSectionsCompleted", []),
        "HITLSectionsSkipped": doc.get("HITLSectionsSkipped", []),
        "HITLReviewOwner": doc.get("HITLReviewOwner", ""),
        "HITLReviewOwnerEmail": doc.get("HITLReviewOwnerEmail", ""),
        "HITLReviewedBy": doc.get("HITLReviewedBy", ""),
        "HITLReviewedByEmail": doc.get("HITLReviewedByEmail", ""),
        "HITLReviewHistory": doc.get("HITLReviewHistory", []),
        "TraceId": doc.get("TraceId", ""),
    }
    # Final safety conversion to ensure no Decimals slip through
    return _convert_decimals(result)
