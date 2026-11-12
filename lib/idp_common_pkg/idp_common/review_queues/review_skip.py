# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Skip All Reviews on one production document, whatever its review mode.

:func:`classify` decides, from the tracking row and the queue item, what bulk Skip
All Reviews does with a document (the dry run uses the same function, so its counts
match the run). :func:`skip_document` then finishes the review by the path that
matches its mode: a live queue item through the queue's finalize path (the
``skipped`` disposition), a legacy review through :mod:`.legacy_skip`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Optional, Tuple

from .bulk_models import BulkOutcome, DocumentOutcome, ReviewCaller
from .exceptions import (
    ResubmitError,
    ReviewAccessDenied,
    ReviewConflict,
    ReviewHeldByOther,
    ReviewItemNotFound,
)
from .models import ReviewItemRecord
from .statuses import (
    LIVE_ITEM_STATES,
    QueueItemState,
    is_legacy_review_pending,
    is_review_finished,
    normalize_hitl_status,
)

logger = logging.getLogger(__name__)

Kind = Literal["queue", "legacy", "none"]

#: Metric of every bulk per-document outcome (dimension ``Outcome``).
OUTCOME_METRIC = "BulkReviewOutcomes"
_TEST_DOCUMENT_ITEM_TYPE = "test-document"


@dataclass
class SkipDeps:
    """What one skip needs. Build one per thread (boto3 resources aren't
    thread-safe)."""

    tracking_table: Any  # Table protocol: .get_item / .update_item
    get_document: Callable[[str], Any]
    review_service: Any  # ReviewQueueService
    store: Any  # ReviewStore
    resubmitter: Any = None  # DocumentResubmitter


def _put_metric(outcome: BulkOutcome) -> None:
    try:
        from idp_common.metrics import put_metric

        put_metric(
            OUTCOME_METRIC, 1, dimensions=[{"Name": "Outcome", "Value": outcome.value}]
        )
    except Exception:  # noqa: BLE001 — a metric must not fail the operation
        logger.debug("metric %s not published", OUTCOME_METRIC, exc_info=True)


def in_scope(caller: ReviewCaller, row: Mapping[str, Any]) -> bool:
    """Whether the caller's profile scope admits the document on ``row``."""
    if caller.is_admin:
        return True
    from idp_common.config_scope import scope_allows
    from idp_common.document_scope import ABSENT, config_version_of_item

    version = config_version_of_item(row)
    if version is ABSENT:
        return False
    return scope_allows(caller.allowed_versions, version)


def is_test_studio(row: Mapping[str, Any]) -> bool:
    return bool(row.get("TestSetId")) or row.get("ItemType") == _TEST_DOCUMENT_ITEM_TYPE


def _skipped_by_job(row: Mapping[str, Any], job_id: Optional[str]) -> bool:
    if not job_id:
        return False
    history = row.get("HITLReviewHistory") or []
    if not history:
        return False
    last = history[-1] if isinstance(history[-1], Mapping) else {}
    return last.get("action") == "skip_all" and last.get("jobId") == job_id


def classify(
    row: Optional[Mapping[str, Any]],
    item: Optional[ReviewItemRecord],
    caller: ReviewCaller,
    *,
    job_id: Optional[str] = None,
) -> Tuple[Optional[BulkOutcome], Kind]:
    """``(outcome, kind)``: an outcome when nothing is to be done, else ``None``
    with the path (``queue`` or ``legacy``) that will skip it.

    A ``SKIPPED`` outcome with a ``job_id`` means this job already skipped the
    document (a retried step); a legacy one still owes its resubmit.
    """
    if not row:
        return BulkOutcome.NOT_PERMITTED, "none"
    if not in_scope(caller, row):
        return BulkOutcome.NOT_PERMITTED, "none"
    if is_test_studio(row):
        return BulkOutcome.TEST_STUDIO_EXCLUDED, "none"
    if item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}:
        state = item.state
        if state == QueueItemState.HOLDING.value:
            return BulkOutcome.NOT_FINISHED_PROCESSING, "queue"
        if state == QueueItemState.FINALIZING.value:
            if job_id and item.skip_job_id == job_id:
                return BulkOutcome.SKIPPED, "queue"
            return BulkOutcome.ALREADY_FINISHED, "queue"
        if state == QueueItemState.CLAIMED.value and not caller.is_admin:
            if item.owner_key != caller.reviewer_key:
                return BulkOutcome.HELD_BY_OTHER, "queue"
        return None, "queue"
    status = row.get("HITLStatus")
    if normalize_hitl_status(status).queue_managed and not is_review_finished(status):
        return BulkOutcome.NOT_PENDING, "none"
    if is_review_finished(status) or row.get("HITLCompleted") is True:
        if _skipped_by_job(row, job_id):
            return BulkOutcome.SKIPPED, "legacy"
        return BulkOutcome.ALREADY_FINISHED, "legacy"
    if is_legacy_review_pending(row):
        owner = str(row.get("HITLReviewOwner") or "")
        if (
            owner
            and not caller.is_admin
            and owner
            not in {
                caller.username,
                caller.email,
            }
            - {""}
        ):
            return BulkOutcome.HELD_BY_OTHER, "legacy"
        return None, "legacy"
    return BulkOutcome.NOT_PENDING, "none"


def read_row(table: Any, object_key: str) -> Optional[dict[str, Any]]:
    """The tracking row, read consistently (``None`` when it doesn't exist)."""
    from .legacy_skip import tracking_key

    return table.get_item(Key=tracking_key(object_key), ConsistentRead=True).get("Item")


def _resubmit(deps: SkipDeps, object_key: str, job_id: Optional[str]) -> Optional[str]:
    """Resubmit; ``None`` when sent, else the failure detail."""
    from idp_common.utils.execution_names import resubmit_token

    if deps.resubmitter is None:
        return "resubmitter_not_configured"
    try:
        deps.resubmitter.resubmit(
            object_key,
            idempotency_token=resubmit_token(job_id, object_key) if job_id else None,
        )
    except ResubmitError as e:
        logger.error("event=bulk_resubmit_failed error=%s", type(e).__name__)
        return "resubmit_failed"
    return None


def skip_document(
    deps: SkipDeps,
    caller: ReviewCaller,
    object_key: str,
    *,
    reason: str,
    note: Optional[str] = None,
    resubmit: bool = True,
    job_id: Optional[str] = None,
    source: str = "ui",
) -> DocumentOutcome:
    """Skip All Reviews on one document; never raises (a fault is ``FAILED``)."""
    try:
        result = _skip(
            deps,
            caller,
            object_key,
            reason=reason,
            note=note,
            resubmit=resubmit,
            job_id=job_id,
            source=source,
        )
    except Exception:  # noqa: BLE001 — reported per document, never the batch
        logger.error("event=bulk_skip_failed", exc_info=True)
        result = DocumentOutcome(object_key=object_key, outcome=BulkOutcome.FAILED)
    _put_metric(result.outcome)
    return result


def _skip(
    deps: SkipDeps,
    caller: ReviewCaller,
    object_key: str,
    *,
    reason: str,
    note: Optional[str],
    resubmit: bool,
    job_id: Optional[str],
    source: str,
) -> DocumentOutcome:
    from .legacy_skip import OwnerRule, skip_legacy_review

    row = read_row(deps.tracking_table, object_key)
    item = deps.store.get_item(object_key) if row else None
    outcome, kind = classify(row, item, caller, job_id=job_id)
    if outcome is BulkOutcome.SKIPPED and kind == "legacy":
        # This job skipped it already (a retried step): repeat the resubmit only.
        detail = _resubmit(deps, object_key, job_id) if resubmit else "resubmit_skipped"
        if detail == "resubmit_failed":
            return DocumentOutcome(
                object_key=object_key, outcome=BulkOutcome.RESUBMIT_FAILED, kind=kind
            )
        return DocumentOutcome(
            object_key=object_key, outcome=BulkOutcome.SKIPPED, kind=kind, detail=detail
        )
    if outcome is not None:
        return DocumentOutcome(object_key=object_key, outcome=outcome, kind=kind)

    if kind == "queue":
        assert item is not None
        try:
            _view, sent = deps.review_service.skip_all_reviews(
                caller.to_context(),
                object_key,
                item.cycle_id,
                reason=reason,
                note=note,
                source=source,
                job_id=job_id,
            )
        except ReviewHeldByOther:
            return DocumentOutcome(
                object_key=object_key, outcome=BulkOutcome.HELD_BY_OTHER, kind=kind
            )
        except ReviewAccessDenied:
            return DocumentOutcome(
                object_key=object_key, outcome=BulkOutcome.NOT_PERMITTED, kind=kind
            )
        except (ReviewConflict, ReviewItemNotFound):
            return DocumentOutcome(
                object_key=object_key, outcome=BulkOutcome.ALREADY_FINISHED, kind=kind
            )
        return DocumentOutcome(
            object_key=object_key,
            outcome=BulkOutcome.SKIPPED,
            kind=kind,
            detail=None if sent else "finalize_retry_pending",
        )

    owner_rule = (
        None
        if caller.is_admin
        else OwnerRule(username=caller.username, email=caller.email)
    )
    try:
        skip_legacy_review(
            deps.tracking_table,
            deps.get_document,
            object_key,
            username=caller.username or caller.email,
            user_email=caller.email,
            reason=reason,
            note=note,
            require_offered=True,
            require_raised=True,
            owner_rule=owner_rule,
            source=source,
            job_id=job_id,
            resubmitted=resubmit,
        )
    except ReviewHeldByOther:
        return DocumentOutcome(
            object_key=object_key, outcome=BulkOutcome.HELD_BY_OTHER, kind=kind
        )
    except ValueError as e:
        text = str(e)
        if text.startswith("REVIEW_FINISHED"):
            outcome = BulkOutcome.ALREADY_FINISHED
        elif text.startswith("NO_REVIEW_PENDING"):
            outcome = BulkOutcome.NOT_PENDING
        else:
            outcome = BulkOutcome.NOT_PERMITTED  # e.g. "Document … not found"
        return DocumentOutcome(object_key=object_key, outcome=outcome, kind=kind)
    if not resubmit:
        return DocumentOutcome(
            object_key=object_key,
            outcome=BulkOutcome.SKIPPED,
            kind=kind,
            detail="resubmit_skipped",
        )
    detail = _resubmit(deps, object_key, job_id)
    if detail is not None:
        return DocumentOutcome(
            object_key=object_key,
            outcome=BulkOutcome.RESUBMIT_FAILED,
            kind=kind,
            detail=detail,
        )
    return DocumentOutcome(
        object_key=object_key, outcome=BulkOutcome.SKIPPED, kind=kind
    )
