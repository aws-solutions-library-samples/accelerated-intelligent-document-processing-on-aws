# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Send to review: put finished production documents into the Default queue.

A sent document gets a queue item with ``origin="manual"`` and no phase-1 run.
From then on it is worked like any routed item; its finalize run applies the
reviewer's edits. There is no cross-table transaction, so the write order makes
the tracking row the commit point (plan D23):

1. Put a HOLDING item (``put_manual_holding``) with a ``sent_to_review`` history
   entry. Fails when the document already has a live item.
2. Conditionally update the tracking row: finished, production, not in a review.
3. Publish the ``in_review`` status object.
4. HOLDING → OPEN (claimable), with the document's cumulative usage as the
   phase-1 baseline the finalize run carries.
5. Registry entry and "items waiting" announcement (best-effort).

A failure after step 2 undoes the send (:meth:`SendToReviewService.abandon`); a
crash leaves a HOLDING item the sweeper abandons in the same way.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import keys, status_object
from . import notify as notify_module
from .bulk_models import (
    SEND_OUTCOMES,
    BulkOutcome,
    DocumentOutcome,
    ReviewCaller,
    SendToReviewRequest,
    SendToReviewResult,
    outcome_counts,
)
from .exceptions import ReviewValidationError
from .legacy_skip import tracking_key
from .models import ManualRequest, ReviewItemRecord
from .review_skip import in_scope, is_test_studio
from .statuses import (
    FINISHED_HITL_STATUSES,
    LIVE_ITEM_STATES,
    QueueItemState,
    ReviewStatus,
    is_legacy_review_pending,
)

logger = logging.getLogger(__name__)

#: History event of a manual send.
SENT_EVENT = "sent_to_review"
#: The sweeper's first look at a manual HOLDING item (abandoned if still HOLDING).
MANUAL_HOLDING_SWEEP_MS = 10 * 60_000
#: Provisional TTL of a manual HOLDING item; activation sets the real one.
MANUAL_HOLDING_TTL_SECONDS = 2 * 24 * 3600
#: Metric of every Send to review outcome (dimension ``Outcome``).
OUTCOME_METRIC = "SendToReviewOutcomes"
_COMPLETED = "COMPLETED"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _put_metric(outcome: BulkOutcome) -> None:
    try:
        from idp_common.metrics import put_metric

        put_metric(
            OUTCOME_METRIC, 1, dimensions=[{"Name": "Outcome", "Value": outcome.value}]
        )
    except Exception:  # noqa: BLE001 — a metric must not fail the operation
        logger.debug("metric %s not published", OUTCOME_METRIC, exc_info=True)


def _condition_failed(exc: BaseException) -> bool:
    response = getattr(exc, "response", None) or {}
    return (response.get("Error") or {}).get(
        "Code"
    ) == "ConditionalCheckFailedException"


class SendToReviewService:
    """Sends finished production documents to the Default review queue."""

    def __init__(
        self,
        store: Any,
        tracking_table: Any,
        get_document: Callable[[str], Any],
        s3: Any,
        output_bucket: str,
        *,
        notifier: Any = notify_module,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        retention_days: int = 365,
        sync_limit: Optional[Callable[[], int]] = None,
    ):
        self.store = store
        self.tracking_table = tracking_table
        self.get_document = get_document
        self.s3 = s3
        self.output_bucket = output_bucket
        self.notify = notifier
        self.clock = clock
        self.retention_days = retention_days
        if sync_limit is None:
            from .bulk_settings import effective_sync_limit

            sync_limit = effective_sync_limit
        self.sync_limit = sync_limit

    # ------------------------------------------------------------------ API

    def send(
        self, caller: ReviewCaller, request: SendToReviewRequest
    ) -> SendToReviewResult:
        limit = self.sync_limit()
        if len(request.object_keys) > limit:
            raise ReviewValidationError(
                "BAD_ARGUMENT", f"send at most {limit} documents per request"
            )
        from .routing import default_queue

        default = default_queue(self.store)
        results: list[DocumentOutcome] = []
        for key in request.object_keys:
            if default is None or not default.enabled:
                result = DocumentOutcome(
                    object_key=key, outcome=BulkOutcome.REVIEW_QUEUE_OFF
                )
            else:
                try:
                    result = self._send_one(caller, key, request, default)
                except Exception:  # noqa: BLE001 — reported per document
                    logger.error("event=send_to_review_failed", exc_info=True)
                    result = DocumentOutcome(object_key=key, outcome=BulkOutcome.FAILED)
            _put_metric(result.outcome)
            results.append(result)
        return SendToReviewResult(
            counts=outcome_counts(results, SEND_OUTCOMES), results=results
        )

    # ------------------------------------------------------------- one doc

    def _classify(
        self,
        caller: ReviewCaller,
        row: Optional[dict],
        item: Optional[ReviewItemRecord],
    ) -> Optional[BulkOutcome]:
        if not row or not in_scope(caller, row):
            return BulkOutcome.NOT_PERMITTED
        if is_test_studio(row):
            return BulkOutcome.TEST_STUDIO_EXCLUDED
        if row.get("ObjectStatus") != _COMPLETED:
            return BulkOutcome.NOT_FINISHED_PROCESSING
        if item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}:
            return BulkOutcome.ALREADY_IN_REVIEW
        if is_legacy_review_pending(row):
            return BulkOutcome.ALREADY_IN_REVIEW
        status = row.get("HITLStatus")
        if status == ReviewStatus.IN_REVIEW.value:
            return BulkOutcome.ALREADY_IN_REVIEW
        return None

    def _read_row(self, key: str) -> Optional[dict]:
        return self.tracking_table.get_item(
            Key=tracking_key(key), ConsistentRead=True
        ).get("Item")

    def _send_one(
        self,
        caller: ReviewCaller,
        key: str,
        request: SendToReviewRequest,
        default: Any,
    ) -> DocumentOutcome:
        row = self._read_row(key)
        old = self.store.get_item(key) if row else None
        outcome = self._classify(caller, row, old)
        if outcome is not None:
            return DocumentOutcome(object_key=key, outcome=outcome)
        assert row is not None
        document = self.get_document(key)
        if document is None:
            return DocumentOutcome(object_key=key, outcome=BulkOutcome.NOT_PERMITTED)

        now = self.clock()
        now_ms = int(now.timestamp() * 1000)
        now_iso = _iso(now)
        record = ReviewItemRecord(
            doc_key=key,
            cycle_id=keys.new_cycle_id(now_ms),
            profile=getattr(document, "config_version", None)
            or row.get("ConfigVersion")
            or "default",
            config_revision=getattr(document, "config_revision", None),
            queue=default.id,
            state=QueueItemState.HOLDING.value,
            version=(old.version + 1) if old else 1,
            priority=default.priority,
            enqueued_at=now_iso,
            enqueued_at_ms=now_ms,
            holding_at=now_iso,
            sections=[
                str(s.section_id)
                for s in (getattr(document, "sections", None) or [])
                if getattr(s, "section_id", None)
            ],
            announce_groups=list(default.member_groups),
            announce_emails=list(default.member_emails),
            sweep_at_ms=now_ms + MANUAL_HOLDING_SWEEP_MS,
            expires_after=int(now.timestamp()) + MANUAL_HOLDING_TTL_SECONDS,
            origin="manual",
            manual_request=ManualRequest(
                reason=request.reason,
                note=request.note,
                requested_by=caller.email or caller.reviewer_key,
                requested_at=now_iso,
                prior_hitl_status=row.get("HITLStatus"),
                prior_hitl_completed=row.get("HITLCompleted"),
            ),
            history_count=1,
        )
        history = self.store.history_txn_item(
            key,
            record.cycle_id,
            SENT_EVENT,
            caller.email or caller.reviewer_key,
            {
                "reason": request.reason,
                **({"note": request.note} if request.note else {}),
                "source": "ui",
            },
            record.expires_after,
            now_ms=now_ms,
            now_iso=now_iso,
        )
        from idp_common.dynamodb.client import DynamoDBTransactionCanceledError

        try:
            self.store.put_manual_holding(record, replaces=old, history=history)
        except DynamoDBTransactionCanceledError:
            return DocumentOutcome(
                object_key=key, outcome=BulkOutcome.ALREADY_IN_REVIEW
            )

        if not self._mark_tracking(record):
            self._delete_item(record)
            row = self._read_row(key)
            outcome = self._classify(caller, row, None) or BulkOutcome.ALREADY_IN_REVIEW
            return DocumentOutcome(object_key=key, outcome=outcome)

        try:
            status_object.publish(
                self.store,
                self.s3,
                self.output_bucket,
                record,
                ReviewStatus.IN_REVIEW,
                run_id=None,
                machine_ref=None,
                final_ref=None,
                now=now,
            )
            active = self._activate(record, document, now)
        except Exception:  # noqa: BLE001 — undone below, reported as FAILED
            logger.error("event=send_to_review_activation_failed", exc_info=True)
            self.abandon(self.store.get_item(key) or record)
            return DocumentOutcome(object_key=key, outcome=BulkOutcome.FAILED)

        try:
            self.store.put_registry(
                active.ref,
                member_groups=active.announce_groups,
                member_emails=active.announce_emails,
                now_iso=now_iso,
            )
        except Exception:  # noqa: BLE001 — the sweeper refreshes the registry
            logger.warning("event=review_registry_put_failed", exc_info=True)
        try:
            self.notify.queue_items_waiting(
                self.store,
                active.ref,
                (active.announce_groups, active.announce_emails),
                now_ms,
            )
        except Exception:  # noqa: BLE001 — an announcement must not fail the send
            logger.warning("event=review_announce_failed", exc_info=True)
        logger.info("event=sent_to_review queue=%s", active.queue)
        return DocumentOutcome(object_key=key, outcome=BulkOutcome.SENT, kind="queue")

    def _mark_tracking(self, record: ReviewItemRecord) -> bool:
        """Step 2: the commit point. ``False`` when the row no longer qualifies."""
        finished = {f":sf{i}": v for i, v in enumerate(FINISHED_HITL_STATUSES)}
        try:
            self.tracking_table.update_item(
                Key=tracking_key(record.doc_key),
                UpdateExpression=(
                    "SET HITLStatus = :inrev, ReviewQueue = :q, ReviewCycleId = :c, "
                    "ReviewStatusUri = :uri REMOVE HITLReviewedBy, HITLReviewedByEmail, "
                    "ReviewDisposedAt, ReviewRejectReason, HITLPendingReview, "
                    "HITLCompleted"
                ),
                ConditionExpression=(
                    "attribute_exists(PK) AND ObjectStatus = :done AND "
                    "attribute_not_exists(TestSetId) AND "
                    "(attribute_not_exists(HITLStatus) OR HITLStatus IN "
                    f"({', '.join(finished)}, :na, :empty))"
                ),
                ExpressionAttributeValues={
                    ":inrev": ReviewStatus.IN_REVIEW.value,
                    ":q": record.queue,
                    ":c": record.cycle_id,
                    ":uri": f"s3://{self.output_bucket}/"
                    f"{status_object.status_key(record.doc_key)}",
                    ":done": _COMPLETED,
                    ":na": "N/A",
                    ":empty": "",
                    **finished,
                },
            )
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        return True

    def _activate(
        self, record: ReviewItemRecord, document: Any, now: datetime
    ) -> ReviewItemRecord:
        """Step 4: HOLDING → OPEN."""
        from idp_common.dynamodb.ttl import calculate_ttl
        from idp_common.utils import cumulative_metering

        from .tracker import MAX_METERING_JSON

        fresh = self.store.get_item(record.doc_key)
        if fresh is None or fresh.cycle_id != record.cycle_id:
            raise RuntimeError("the manual item changed before activation")
        active = fresh.model_copy(deep=True)
        active.state = QueueItemState.OPEN.value
        active.sweep_at_ms = None
        active.activated_at = _iso(now)
        metering_json = json.dumps(cumulative_metering(document), default=str)
        active.phase1_metering_json = (
            metering_json if len(metering_json) <= MAX_METERING_JSON else None
        )
        active.expires_after = calculate_ttl(self.retention_days, now=now)
        active.version = fresh.version + 1
        self.store.replace_item(
            active,
            expected_version=fresh.version,
            condition="#st = :holding AND CycleId = :c",
            values={":holding": QueueItemState.HOLDING.value, ":c": record.cycle_id},
            names={"#st": "State"},
        )
        return active

    def _delete_item(self, record: ReviewItemRecord) -> None:
        try:
            self.store.delete_item_if_cycle(record.doc_key, record.cycle_id)
        except Exception:  # noqa: BLE001 — logged; the sweeper abandons it later
            logger.warning("event=send_to_review_item_delete_failed", exc_info=True)

    def abandon(self, item: ReviewItemRecord) -> None:
        """Undo a send that never became claimable: restore the tracking row's
        review status, delete the status object, delete the item. Best-effort."""
        prior = item.manual_request
        sets = []
        values: dict[str, Any] = {":c": item.cycle_id}
        if prior is not None and prior.prior_hitl_status:
            sets.append("HITLStatus = :prior")
            values[":prior"] = prior.prior_hitl_status
        if prior is not None and prior.prior_hitl_completed is not None:
            sets.append("HITLCompleted = :pc")
            values[":pc"] = prior.prior_hitl_completed
        removes = ["ReviewQueue", "ReviewCycleId", "ReviewStatusUri"]
        if prior is None or not prior.prior_hitl_status:
            removes.append("HITLStatus")
        expression = (
            (f"SET {', '.join(sets)} " if sets else "") + "REMOVE " + ", ".join(removes)
        )
        try:
            self.tracking_table.update_item(
                Key=tracking_key(item.doc_key),
                UpdateExpression=expression,
                ConditionExpression="ReviewCycleId = :c",
                ExpressionAttributeValues=values,
            )
        except Exception as e:
            if not _condition_failed(e):
                logger.warning("event=send_to_review_revert_failed", exc_info=True)
        try:
            status_object.delete(self.s3, self.output_bucket, item.doc_key)
        except Exception:  # noqa: BLE001
            logger.warning("event=review_status_delete_failed", exc_info=True)
        self._delete_item(item)
        logger.info("event=send_to_review_abandoned")


def create_send_to_review_service(
    store: Any, tracking_table: Any, document_service: Any, s3: Any = None
) -> SendToReviewService:
    """From the environment (``OUTPUT_BUCKET``, ``DATA_RETENTION_IN_DAYS``)."""
    import os

    if s3 is None:
        import idp_common.s3 as s3

    return SendToReviewService(
        store,
        tracking_table,
        document_service.get_document,
        s3,
        os.environ.get("OUTPUT_BUCKET", ""),
        retention_days=int(os.environ.get("DATA_RETENTION_IN_DAYS", "365") or 365),
    )
