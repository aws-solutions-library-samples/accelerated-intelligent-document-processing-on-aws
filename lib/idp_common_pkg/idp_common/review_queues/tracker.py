# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the WorkflowTracker does for the two runs of a review-queue document.

Write order (D17):

* **Hold** (phase 1 SUCCEEDED, wrapper ``HITL_IN_PROGRESS``): document status →
  reporting → run record (``review_phase1``) → tracking review attributes →
  ``review/status.json`` = ``in_review`` → activation (HOLDING → OPEN, only for
  this execution) → registry → debounced notice. ``in_review`` exists before the
  item is claimable, and a redelivered event can't reactivate anything.
* **Finalize** (SUCCEEDED): document status (with the phase-1 metering merged) →
  reporting (finalize-only metering) → run record (``review_final``; none for
  rejected) → tracking review attributes → status object **last** → item
  terminal (only for this token). An item an Admin removed from review completes
  like ``complete`` with the machine results, and its status object is deleted
  rather than written, so consumers read it as withdrawn rather than reviewed.

An item whose queue was deleted while it was HOLDING is activated in the Default
queue.

The tracker calls these between its own steps; :func:`after_completion` raises on
failure so the tracker can re-raise after its slot decrement.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional

from . import keys, status_object
from .finalize import is_finalize_token
from .models import ReviewReportingInfo, RunRef, TrackerReviewContext
from .queue_defs import DEFAULT_QUEUE_ID
from .statuses import QueueItemState, ReviewStatus

logger = logging.getLogger(__name__)

#: Phase-1 metering kept on the item for the finalize merge (else omitted).
MAX_METERING_JSON = 100_000

RunKind = Literal["none", "hold", "finalize"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def finalize_token_of(input_data: Any) -> Optional[str]:
    review = (input_data or {}).get("review") if isinstance(input_data, dict) else None
    token = review.get("finalizeToken") if isinstance(review, dict) else None
    return token if is_finalize_token(token) else None


def classify_run(input_data: Any, output_data: Any, workflow_status: str) -> RunKind:
    """``finalize`` (input carries a token), ``hold`` (SUCCEEDED at the review point) or ``none``."""
    from idp_common.models import Status, document_wrapper_status

    if finalize_token_of(input_data):
        return "finalize"
    if (
        workflow_status == "SUCCEEDED"
        and document_wrapper_status(output_data) == Status.HITL_IN_PROGRESS.value
    ):
        return "hold"
    return "none"


def load_context(
    kind: RunKind,
    token: Optional[str],
    object_key: str,
    execution_arn: Optional[str],
    store: Any,
) -> Optional[TrackerReviewContext]:
    """The live item this run belongs to, or ``None`` (a stale event, D70)."""
    if kind == "none" or store is None:
        return None
    item = store.get_item(object_key)
    if item is None:
        return None
    if kind == "hold":
        if (
            item.state == QueueItemState.HOLDING.value
            and item.phase1_execution_arn == execution_arn
        ):
            return TrackerReviewContext(kind="hold", item=item)
        return None
    if item.state == QueueItemState.FINALIZING.value and item.finalize_token == token:
        outcome = "rejected" if item.disposition == "rejected" else "complete"
        return TrackerReviewContext(
            kind="finalize", item=item, token=token, outcome=outcome
        )
    return None


def apply_overrides(ctx: TrackerReviewContext, processed_doc: Any) -> tuple[Any, Any]:
    """Set the review fields on the tracked document.

    Returns ``(tracking_doc, reporting_doc)``. For a finalize run the tracking
    document carries phase-1 + finalize metering, and the reporting copy only the
    finalize run's own usage, so reporting never counts phase 1 twice.
    """
    from idp_common.utils import merge_metering_data

    if ctx.kind == "hold":
        processed_doc.hitl_status = ReviewStatus.IN_REVIEW.value
        return processed_doc, processed_doc
    processed_doc.hitl_status = (
        ReviewStatus.REJECTED.value
        if ctx.outcome == "rejected"
        else ReviewStatus.COMPLETE.value
    )
    reporting_doc = copy.deepcopy(processed_doc)
    phase1: dict = {}
    if ctx.item.phase1_metering_json:
        try:
            phase1 = json.loads(ctx.item.phase1_metering_json)
        except (TypeError, ValueError):
            logger.warning("event=review_phase1_metering_unreadable")
    processed_doc.metering = merge_metering_data(phase1, processed_doc.metering or {})
    return processed_doc, reporting_doc


def reporting_info(ctx: TrackerReviewContext) -> ReviewReportingInfo:
    item = ctx.item
    if ctx.kind == "hold":
        status = ReviewStatus.IN_REVIEW.value
    else:
        status = (
            ReviewStatus.REJECTED.value
            if ctx.outcome == "rejected"
            else ReviewStatus.COMPLETE.value
        )
    return ReviewReportingInfo(
        cycle_id=item.cycle_id,
        review_status=status,
        disposition=item.disposition if ctx.kind == "finalize" else None,
        profile=item.profile,
        queue=item.queue,
        reviewer=item.disposed_by if ctx.kind == "finalize" else None,
        reviewed_at=item.disposed_at if ctx.kind == "finalize" else None,
        enqueued_at=item.enqueued_at,
        reason_count=item.reason_count,
        top_reason_kind=item.reasons[0].kind if item.reasons else None,
        reject_reason=item.reject_reason if ctx.outcome == "rejected" else None,
        escalated=item.escalated,
        machine_run_id=item.phase1_run_id,
        final_run_id=None,
    )


def _status_uri(output_bucket: str, doc_key: str) -> str:
    return f"s3://{output_bucket}/{status_object.status_key(doc_key)}"


def after_completion(
    ctx: TrackerReviewContext,
    *,
    workflow_status: str,
    run_ref: Optional[RunRef],
    document: Any,
    store: Any,
    s3: Any,
    output_bucket: str,
    notifier: Any,
    document_service: Any,
    retention_days: int,
    now: Optional[datetime] = None,
    service_factory: Any = None,
) -> None:
    """Review writes after the tracker's document, reporting and run-record writes."""
    from idp_common.dynamodb.ttl import calculate_ttl

    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    item = ctx.item
    doc_key = item.doc_key

    if ctx.kind == "hold":
        if workflow_status != "SUCCEEDED":
            return
        target = _activation_queue(store, item)
        if target is not None:
            item = item.model_copy(update=target)
        document_service.update_review_attributes(
            doc_key,
            {
                "ReviewQueue": item.queue,
                "ReviewCycleId": item.cycle_id,
                "ReviewStatusUri": _status_uri(output_bucket, doc_key),
            },
            remove_attrs=(
                "HITLReviewedBy",
                "HITLReviewedByEmail",
                "ReviewDisposedAt",
                "ReviewRejectReason",
            ),
        )
        machine_ref = run_ref
        status_object.publish(
            store,
            s3,
            output_bucket,
            item,
            ReviewStatus.IN_REVIEW,
            run_id=run_ref.run_id if run_ref else None,
            machine_ref=machine_ref,
            final_ref=None,
            now=now,
        )
        fresh = store.get_item(doc_key)
        if (
            fresh is None
            or fresh.state != QueueItemState.HOLDING.value
            or fresh.phase1_execution_arn != item.phase1_execution_arn
        ):
            logger.info("event=review_activation_skipped reason=state_changed")
            return
        active = fresh.model_copy(deep=True)
        if target is not None:
            active.queue = target["queue"]
            active.priority = target["priority"]
        active.state = QueueItemState.OPEN.value
        active.sweep_at_ms = None
        active.activated_at = _iso(now)
        active.phase1_run_id = run_ref.run_id if run_ref else None
        active.machine_manifest_uri = run_ref.manifest_uri if run_ref else None
        metering_json = json.dumps(
            getattr(document, "metering", None) or {}, default=str
        )
        if len(metering_json) > MAX_METERING_JSON:
            logger.warning(
                "event=review_phase1_metering_too_large bytes=%d", len(metering_json)
            )
            metering_json = None
        active.phase1_metering_json = metering_json
        active.expires_after = calculate_ttl(retention_days, now=now)
        active.version = fresh.version + 1
        try:
            store.replace_item(
                active,
                expected_version=fresh.version,
                condition="#st = :holding AND Phase1ExecutionArn = :arn",
                values={
                    ":holding": QueueItemState.HOLDING.value,
                    ":arn": item.phase1_execution_arn,
                },
                names={"#st": "State"},
            )
        except Exception as exc:
            from idp_common.dynamodb.client import DynamoDBTransactionCanceledError

            if isinstance(exc, DynamoDBTransactionCanceledError):
                logger.info("event=review_activation_skipped reason=condition")
                return
            raise
        try:
            store.put_registry(
                active.ref,
                member_groups=active.announce_groups,
                member_emails=active.announce_emails,
                now_iso=_iso(now),
            )
        except Exception:  # noqa: BLE001 — the sweeper refreshes the registry
            logger.warning("event=review_registry_put_failed", exc_info=True)
        notifier.queue_items_waiting(
            store, active.ref, (active.announce_groups, active.announce_emails), now_ms
        )
        logger.info("event=review_activated queue=%s", active.queue)
        return

    # Finalize run.
    if workflow_status == "ABORTED":
        _withdraw(
            store, s3, output_bucket, document_service, doc_key, now, service_factory
        )
        return
    if workflow_status != "SUCCEEDED":
        # Let the sweeper retry now (FINALIZE_FAILED after the attempt limit).
        store.client.update_item(
            keys.item_key(doc_key),
            "SET SweepAtMs = :now, SweepSK = :sk ADD #v :one",
            expression_attribute_values={
                ":one": 1,
                ":now": now_ms,
                ":sk": keys.sweep_sk(now_ms, doc_key),
                ":tok": ctx.token,
                ":fin": QueueItemState.FINALIZING.value,
            },
            expression_attribute_names={"#st": "State", "#v": "Version"},
            condition_expression="#st = :fin AND FinalizeToken = :tok",
            return_values="NONE",
        )
        logger.info("event=review_finalize_run_failed status=%s", workflow_status)
        return
    rejected = ctx.outcome == "rejected"
    removed = item.disposition == "removed"
    document_service.update_review_attributes(
        doc_key,
        {
            # `disposed_by` is the reviewer's email; the document page reads the email
            # attribute first, as the legacy flow writes both.
            "HITLReviewedBy": item.disposed_by or "",
            "HITLReviewedByEmail": item.disposed_by or "",
            "ReviewDisposedAt": item.disposed_at or _iso(now),
            **(
                {}
                if removed
                else {"ReviewStatusUri": _status_uri(output_bucket, doc_key)}
            ),
            **({"ReviewRejectReason": item.reject_reason or ""} if rejected else {}),
        },
        remove_attrs=(
            ()
            if rejected
            else ("ReviewRejectReason", "ReviewStatusUri")
            if removed
            else ("ReviewRejectReason",)
        ),
    )
    machine_ref = (
        RunRef(run_id=item.phase1_run_id, manifest_uri=item.machine_manifest_uri)
        if item.phase1_run_id
        else None
    )
    final_ref = None if rejected else run_ref
    if removed:
        status_object.delete(s3, output_bucket, doc_key)
    else:
        status_object.publish(
            store,
            s3,
            output_bucket,
            item,
            ReviewStatus.REJECTED if rejected else ReviewStatus.COMPLETE,
            run_id=run_ref.run_id if run_ref else None,
            machine_ref=machine_ref,
            final_ref=final_ref,
            now=now,
        )
    fresh = store.get_item(doc_key)
    if (
        fresh is None
        or fresh.state != QueueItemState.FINALIZING.value
        or fresh.finalize_token != ctx.token
    ):
        logger.info("event=review_terminal_skipped reason=state_changed")
        return
    done = fresh.model_copy(deep=True)
    done.state = (
        QueueItemState.REJECTED.value if rejected else QueueItemState.COMPLETE.value
    )
    done.sweep_at_ms = None
    done.final_run_id = final_ref.run_id if final_ref else None
    done.final_manifest_uri = final_ref.manifest_uri if final_ref else None
    done.finalized_at = _iso(now)
    done.version = fresh.version + 1
    store.replace_item(
        done,
        expected_version=fresh.version,
        condition="#st = :fin AND FinalizeToken = :tok",
        values={":fin": QueueItemState.FINALIZING.value, ":tok": ctx.token},
        names={"#st": "State"},
    )
    logger.info("event=review_finalized outcome=%s", ctx.outcome)


def _activation_queue(store: Any, item: Any) -> Optional[dict[str, Any]]:
    """``{"queue", "priority"}`` of Default when the item's queue no longer exists.

    ``None`` keeps the item where it is (its queue exists, or the definitions
    can't be read, in which case it is activated where it is rather than not at
    all).
    """
    from .routing import queue_definitions

    try:
        defs = queue_definitions(store)
        if defs.get(item.queue) is not None:
            return None
        default = defs.get(DEFAULT_QUEUE_ID)
    except Exception:  # noqa: BLE001
        logger.warning("event=review_queue_defs_unavailable", exc_info=True)
        return None
    logger.info("event=review_requeued_to_default reason=queue_missing")
    return {
        "queue": DEFAULT_QUEUE_ID,
        "priority": default.priority if default is not None else item.priority,
    }


def _withdraw(
    store, s3, output_bucket, document_service, doc_key, now, service_factory
) -> None:
    from .directory import QueueDirectory
    from .service import ReviewQueueService

    factory = service_factory or (
        lambda: ReviewQueueService(
            store,
            QueueDirectory(),
            s3=s3,
            output_bucket=output_bucket,
            document_service=document_service,
            clock=lambda: now,
        )
    )
    factory().withdraw(doc_key, "aborted")


def replay_event(describe_execution: Mapping[str, Any]) -> dict[str, Any]:
    """A Step Functions status-change event built from ``DescribeExecution``.

    Used by the sweeper to heal a lost tracker event: the tracker deduplicates
    redelivery (run id from ``stopDate``, the per-execution decrement marker), so
    replaying a terminal event is safe.
    """

    def epoch_ms(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return int(value.timestamp() * 1000)
        return int(value)

    return {
        "version": "0",
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {
            "executionArn": describe_execution.get("executionArn"),
            "stateMachineArn": describe_execution.get("stateMachineArn"),
            "name": describe_execution.get("name"),
            "status": describe_execution.get("status"),
            "startDate": epoch_ms(describe_execution.get("startDate")),
            "stopDate": epoch_ms(describe_execution.get("stopDate")),
            "input": describe_execution.get("input"),
            "output": describe_execution.get("output"),
        },
        "replayed": True,
    }
