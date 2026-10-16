# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One routing function for both processing modes (FR-6, FR-7).

ProcessResults (Pipeline and BDA) call :func:`route_for_review` after their own
work. With the feature off (``review_mode`` = ``"legacy"``) it does no I/O and
the handlers keep today's marking through :func:`legacy_section_triggers` and
:func:`bda_segment_triggers`, which reproduce the code they replace. In queue mode
it either recognises the finalize run of a disposed item, or (a fresh run)
builds reasons and writes a HOLDING item that the tracker activates when phase 1
succeeds.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from . import keys
from .finalize import apply_corrections
from .models import ReviewDecision, ReviewItemRecord
from .reasons import MAX_REASONS, build_reasons
from .statuses import LIVE_ITEM_STATES, QueueItemState

logger = logging.getLogger(__name__)

#: When the sweeper first looks at a HOLDING item (phase 1 should end well before).
HOLDING_SWEEP_MS = 15 * 60_000
#: Provisional TTL of a HOLDING item; activation sets the real one (D69).
HOLDING_TTL_SECONDS = 2 * 86_400

_unavailable_warned = threading.Event()


TEST_STUDIO_SOURCE = "test-studio"


def is_test_studio_document(document: Any) -> bool:
    """True for a document submitted by a Test Studio run (never queue-routed)."""
    return getattr(document, "submission_source", None) == TEST_STUDIO_SOURCE or bool(
        getattr(document, "test_set_id", None)
    )


def review_mode(
    config: Any, store: Any, document: Any = None
) -> Literal["legacy", "queues"]:
    """``"queues"`` only when HITL and review queues are on and a store exists. Pure.

    A Test Studio document always reads ``"legacy"``: it keeps the legacy HITL
    marking and is reviewed as test-set annotation work, never queue-routed.
    """
    if document is not None and is_test_studio_document(document):
        return "legacy"
    hitl = getattr(config, "hitl", None) if config is not None else None
    if hitl is None or not hitl.enabled or not hitl.review_queues.enabled:
        return "legacy"
    return "queues" if store is not None else "legacy"


def _warn_unavailable(config: Any, store: Any) -> None:
    hitl = getattr(config, "hitl", None) if config is not None else None
    if (
        hitl is None
        or not hitl.enabled
        or not hitl.review_queues.enabled
        or store is not None
    ):
        return
    if _unavailable_warned.is_set():
        return
    _unavailable_warned.set()
    logger.warning(
        "Review queues are enabled for this profile but no review queue table is "
        "configured (headless deployment?); using the legacy HITL flow"
    )
    try:
        from idp_common.metrics import put_metric

        put_metric("ReviewQueuesUnavailable", 1)
    except Exception:  # noqa: BLE001
        logger.debug("ReviewQueuesUnavailable metric not published", exc_info=True)


def legacy_section_triggers(section: Any, *, hitl_enabled: bool) -> bool:
    """Pipeline mode's trigger: HITL on and the section has confidence alerts."""
    return bool(hitl_enabled and section.confidence_threshold_alerts)


def bda_segment_triggers(
    alerts: Any, bp_confidence: Any, threshold: float, *, hitl_enabled: bool
) -> Optional[bool]:
    """BDA mode's trigger for a custom-output segment (``None`` when HITL is off)."""
    if not hitl_enabled:
        return None
    return len(alerts) > 0 or float(bp_confidence) < threshold


def _execution_name(execution_arn: str) -> str:
    return (execution_arn or "").rsplit(":", 1)[-1]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def route_for_review(
    document: Any,
    config: Any,
    *,
    mode: Literal["pipeline", "bda"],
    execution_arn: str,
    store: Any,
    s3: Any = None,
    document_service: Any = None,
    output_bucket: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ReviewDecision:
    """Decide whether this run holds for review, finalizes one, or neither."""
    # Test Studio runs are scored on the machine's output, and their documents are
    # reviewed as test-set annotation work, so they are never held in a queue.
    if is_test_studio_document(document):
        return ReviewDecision(mode="legacy")
    if review_mode(config, store) == "legacy":
        _warn_unavailable(config, store)
        return ReviewDecision(mode="legacy")
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    doc_key = document.input_key
    out_bucket = output_bucket or getattr(document, "output_bucket", None) or ""
    item = store.get_item(doc_key)
    live = item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}

    # Finalize run of a disposed item (D15).
    if (
        item is not None
        and item.state == QueueItemState.FINALIZING.value
        and item.finalize_execution_name
        and item.finalize_execution_name == _execution_name(execution_arn)
    ):
        try:
            store.client.update_item(
                keys.item_key(doc_key),
                "SET FinalizeStartedAt = if_not_exists(FinalizeStartedAt, :t)",
                expression_attribute_values={
                    ":t": _iso(now),
                    ":tok": item.finalize_token,
                },
                condition_expression="FinalizeToken = :tok",
                return_values="NONE",
            )
        except Exception:  # noqa: BLE001 — bookkeeping only
            logger.debug("FinalizeStartedAt not recorded", exc_info=True)
        if item.disposition == "complete":
            drafts = store.list_drafts(doc_key, item.cycle_id)
            apply_corrections(document, drafts, s3, out_bucket)
            outcome: Literal["complete", "rejected"] = "complete"
        else:
            outcome = "rejected"
        logger.info("event=review_finalize_run outcome=%s", outcome)
        return ReviewDecision(
            mode="queues", outcome=outcome, cycle_id=item.cycle_id, queue=item.queue
        )

    reasons = build_reasons(document, config, mode=mode)
    if not reasons:
        if live:
            _withdraw(
                item,
                store,
                s3,
                out_bucket,
                document_service,
                "superseded_no_review",
                now,
            )
        return ReviewDecision(mode="queues", hold=False)

    # The profile names a system-wide queue; an unknown id falls back to Default.
    from .queue_defs import DEFAULT_QUEUE_ID, QueueDefStore, QueuesView

    wanted = getattr(config.hitl.review_queues, "queue", None) or DEFAULT_QUEUE_ID
    try:
        view = QueuesView(QueueDefStore(lambda: store.client).list())
    except Exception:  # noqa: BLE001 — route to Default rather than fail the run
        logger.warning("event=review_queue_defs_unavailable", exc_info=True)
        view = QueuesView([])
    queue = view.queue(wanted) or view.queue(DEFAULT_QUEUE_ID)
    queue_name = queue.id if queue else DEFAULT_QUEUE_ID
    sections = list(dict.fromkeys(r.section_id for r in reasons))
    if item is not None and live and item.phase1_execution_arn == execution_arn:
        # A retry of this execution: keep its cycle.
        cycle_id = item.cycle_id
        supersede = None
    else:
        cycle_id = keys.new_cycle_id(now_ms)
        supersede = item if live else None
    record = ReviewItemRecord(
        doc_key=doc_key,
        cycle_id=cycle_id,
        profile=getattr(document, "config_version", None) or "default",
        config_revision=getattr(document, "config_revision", None),
        queue=queue_name,
        state=QueueItemState.HOLDING.value,
        version=(supersede.version + 1) if supersede else 1,
        priority=queue.priority if queue else 50,
        enqueued_at=_iso(now),
        enqueued_at_ms=now_ms,
        holding_at=_iso(now),
        reasons=reasons[:MAX_REASONS],
        reason_count=len(reasons),
        sections=sections,
        announce_groups=list(queue.member_groups) if queue else [],
        announce_emails=list(queue.member_emails) if queue else [],
        phase1_execution_arn=execution_arn,
        sweep_at_ms=now_ms + HOLDING_SWEEP_MS,
        expires_after=int(now.timestamp()) + HOLDING_TTL_SECONDS,
    )
    history = None
    if supersede is not None:
        history = store.history_txn_item(
            doc_key,
            supersede.cycle_id,
            "superseded",
            None,
            {"by_cycle": cycle_id},
            supersede.expires_after,
            now_ms=now_ms,
            now_iso=_iso(now),
        )
    store.put_holding(record, supersede=supersede, history=history)
    if supersede is not None and out_bucket:
        from . import status_object

        try:
            status_object.delete(s3, out_bucket, doc_key)
        except Exception:  # noqa: BLE001
            logger.warning("event=review_status_delete_failed", exc_info=True)
    logger.info(
        "event=review_hold reasons=%d sections=%d superseded=%s",
        len(reasons),
        len(sections),
        supersede is not None,
    )
    return ReviewDecision(
        mode="queues",
        hold=True,
        cycle_id=cycle_id,
        queue=queue_name,
        reasons=reasons[:MAX_REASONS],
        sections=sections,
    )


def _withdraw(
    item: ReviewItemRecord,
    store: Any,
    s3: Any,
    output_bucket: str,
    document_service: Any,
    reason: str,
    now: datetime,
) -> None:
    """Withdraw a stale live item when a fresh run needs no review."""
    from .directory import QueueDirectory
    from .service import ReviewQueueService

    service = ReviewQueueService(
        store,
        QueueDirectory(),
        s3=s3,
        output_bucket=output_bucket,
        document_service=document_service,
        clock=lambda: now,
    )
    service.withdraw(item.doc_key, reason)
