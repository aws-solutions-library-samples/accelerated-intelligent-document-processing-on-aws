# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One routing function for both processing modes (FR-6, FR-7).

ProcessResults (Pipeline and BDA) call :func:`route_for_review` after their own
work. Queue mode is on when HITL is enabled in the document's configuration, the
stack has a review table and its Default queue is enabled (Review Studio › Review Management); otherwise ``review_mode`` is ``"legacy"`` and the handlers keep the
HITL marking through :func:`legacy_section_triggers` and
:func:`bda_segment_triggers`. In queue mode it either recognises the finalize run
of a disposed item, or (a fresh run) builds reasons from the sections'
confidence alerts (each field's own threshold, otherwise
``hitl.confidence_threshold``; in BDA mode also the blueprint confidence against
``hitl.confidence_threshold``), missing required fields and failed extractions
(see ``reasons``), and writes a HOLDING item in the Default queue
that the tracker activates when phase 1 succeeds.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from idp_common.config.feature_flags import review_routing_enabled
from idp_common.run_plan.restart import layout_of
from idp_common.run_plan.steps import EXTRACTION
from idp_common.utils import parse_blueprint_confidence
from idp_common.utils.transient_errors import reraise_if_transient

from . import keys
from .finalize import apply_corrections
from .models import ReviewDecision, ReviewItemRecord, ReviewReason, RoutedBy
from .queue_defs import DEFAULT_QUEUE_ID, QueueDefinition, QueueDefStore
from .reasons import MAX_REASONS, build_reasons, failed_extraction_issue
from .statuses import LIVE_ITEM_STATES, MACHINE_RESULT_DISPOSITIONS, QueueItemState

logger = logging.getLogger(__name__)

#: When the sweeper first looks at a HOLDING item (phase 1 should end well before).
HOLDING_SWEEP_MS = 15 * 60_000
#: Provisional TTL of a HOLDING item; activation sets the real one (D69).
HOLDING_TTL_SECONDS = 2 * 86_400


TEST_STUDIO_SOURCE = "test-studio"


def is_test_studio_document(document: Any) -> bool:
    """True for a document submitted by a Test Studio run (never queue-routed)."""
    return getattr(document, "submission_source", None) == TEST_STUDIO_SOURCE or bool(
        getattr(document, "test_set_id", None)
    )


def queue_definitions(store: Any) -> QueueDefStore:
    """The store's queue definitions (cached briefly per process)."""
    defs = getattr(store, "queue_defs", None)
    return (
        defs if isinstance(defs, QueueDefStore) else QueueDefStore(lambda: store.client)
    )


def default_queue(store: Any, *, strict: bool = False) -> Optional[QueueDefinition]:
    """The Default queue's definition, or ``None`` when it can't be read.

    With ``strict``, a read that failed for a transient cause (throttling, a
    timeout) raises ``TransientError`` instead, so the step that asked is
    retried: a queue that could not be read for a moment is not a queue that is
    off. Any other failure is logged and gives ``None``, with or without it.
    """
    try:
        return queue_definitions(store).get(DEFAULT_QUEUE_ID)
    except Exception as error:  # noqa: BLE001 — routing falls back to the legacy flow
        if strict:
            reraise_if_transient(error, where="the Default review queue definition")
        logger.warning("event=review_queue_defs_unavailable", exc_info=True)
        return None


def _finalizing(store: Any, document: Any) -> bool:
    """True when the document's item waits for its finalize run."""
    doc_key = getattr(document, "input_key", None)
    if not doc_key:
        return False
    try:
        item = store.get_item(doc_key)
    except Exception:  # noqa: BLE001
        logger.warning("event=review_item_unavailable", exc_info=True)
        return False
    return item is not None and item.state == QueueItemState.FINALIZING.value


def review_mode(
    config: Any, store: Any, document: Any = None
) -> Literal["legacy", "queues"]:
    """``"queues"`` when HITL is enabled, a store exists and Default is enabled.

    A Test Studio document always reads ``"legacy"``: it keeps the legacy HITL
    marking and is reviewed as test-set annotation work, never queue-routed. With
    HITL off or Default disabled, a document whose item is FINALIZING still reads
    ``"queues"`` so its finalize run completes after the switch was turned off.
    """
    if document is not None and is_test_studio_document(document):
        return "legacy"
    if store is None:
        logger.debug("event=review_mode_legacy reason=no_store")
        return "legacy"
    default = default_queue(store)
    hitl = review_routing_enabled(config)
    if hitl and default is not None and default.enabled:
        return "queues"
    if document is not None and _finalizing(store, document):
        return "queues"
    logger.debug(
        "event=review_mode_legacy reason=%s",
        "hitl_disabled" if not hitl else "default_queue_disabled",
    )
    return "legacy"


def legacy_section_triggers(section: Any, *, hitl_enabled: bool) -> bool:
    """Pipeline mode's trigger: HITL on, and the section has confidence alerts or
    an error-severity ``extraction_failed`` issue (no successful result, #2038).

    The failed-extraction half is the one queue mode reads as an
    ``extraction_failed`` reason (``build_reasons``), so both modes hold the same
    documents for it.
    """
    if not hitl_enabled:
        return False
    return bool(
        section.confidence_threshold_alerts
        or failed_extraction_issue(section) is not None
    )


def bda_segment_triggers(
    alerts: Any, bp_confidence: Any, threshold: float, *, hitl_enabled: bool
) -> Optional[bool]:
    """BDA mode's trigger for a custom-output segment (``None`` when HITL is off).

    The blueprint confidence is read by ``parse_blueprint_confidence``, the reader
    review-queue mode uses too. An unscored one (null, non-numeric, non-finite, a
    bool, out of range, or a string ``float()`` rejects, such as ``"95%"``)
    routes the segment to review at every threshold, including 0.0, as queue
    mode holds it with a ``blueprint_confidence`` reason scored 0.0.
    ``float(None)``
    used to raise TypeError, which escaped ``process_segments`` into the BDA
    handler's catch-all and abandoned the rest of the document's segments, and a
    NaN compared directly never triggered, since ``nan < t`` is False.
    """
    if not hitl_enabled:
        return None
    confidence = parse_blueprint_confidence(bp_confidence)
    return len(alerts) > 0 or confidence is None or confidence < threshold


def _execution_name(execution_arn: str) -> str:
    return (execution_arn or "").rsplit(":", 1)[-1]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _is_finalize_run(item: Optional[ReviewItemRecord], execution_arn: str) -> bool:
    """True when ``execution_arn`` is the finalize run of ``item`` (D15)."""
    return bool(
        item is not None
        and item.state == QueueItemState.FINALIZING.value
        and item.finalize_execution_name
        and item.finalize_execution_name == _execution_name(execution_arn)
    )


def _finalize_of(
    item: Optional[ReviewItemRecord],
    document: Any,
    store: Any,
    execution_arn: str,
    *,
    apply: bool,
    s3: Any,
    output_bucket: str,
    now: datetime,
) -> Optional[tuple[ReviewItemRecord, Literal["complete", "rejected"]]]:
    """The finalize run of ``item``: the item and how its review ended.

    With ``apply``, the drafts of a completed review are copied onto the section
    results, unless the run started at extraction (``restart_step``): its
    results are new, and the step that applies the review merges the reviewer's
    corrections into them. Copying a draft here would write over them.
    """
    if item is None or not _is_finalize_run(item, execution_arn):
        return None
    doc_key = document.input_key
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
    outcome: Literal["complete", "rejected"]
    if item.disposition == "complete":
        if apply and item.restart_step != EXTRACTION:
            drafts = store.list_drafts(doc_key, item.cycle_id)
            apply_corrections(document, drafts, s3, output_bucket)
        outcome = "complete"
    elif item.disposition in MACHINE_RESULT_DISPOSITIONS:
        # Removed from review by an Admin, or skipped (Skip All Reviews):
        # completes with the machine results.
        outcome = "complete"
    else:
        outcome = "rejected"
    logger.info("event=review_finalize_run outcome=%s", outcome)
    return item, outcome


def recognize_finalize(
    document: Any,
    store: Any,
    execution_arn: str,
    *,
    apply: bool,
    s3: Any = None,
    output_bucket: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[tuple[ReviewItemRecord, Literal["complete", "rejected"]]]:
    """Recognise the finalize run of a disposed item (D15).

    Returns the item and how its review ended (``complete`` or ``rejected``), or
    ``None`` when this execution is not the finalize run of the document's item.
    It records when the finalize run started. With ``apply``, the reviewer's
    corrections of a completed review are written to the section results; a run
    that applies them in a step of its own passes ``apply=False``. A run that
    started at extraction has none written, whatever ``apply`` says: its results
    are new, and that step merges the corrections into them.
    """
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    return _finalize_of(
        store.get_item(document.input_key),
        document,
        store,
        execution_arn,
        apply=apply,
        s3=s3,
        output_bucket=output_bucket or getattr(document, "output_bucket", None) or "",
        now=now or datetime.now(timezone.utc),
    )


def place_for_review(
    document: Any,
    config: Any,
    *,
    mode: Literal["pipeline", "bda"],
    execution_arn: str,
    store: Any,
    item: Optional[ReviewItemRecord],
    queue: QueueDefinition,
    reasons: list[ReviewReason],
    routed_by: Optional[RoutedBy] = None,
    placement_count: int = 1,
    carried_metering_json: Optional[str] = None,
    previous_event: str = "superseded",
    s3: Any,
    output_bucket: str,
    now: datetime,
) -> ReviewDecision:
    """Write the HOLDING item that places the document in ``queue``.

    ``item`` is the document's current item, if it has one. A live item of
    another execution is replaced, and its cycle's history gets
    ``previous_event``: ``superseded``, or ``rerouted`` when a rule sends a
    document whose review was just completed to a queue again. A retry of this
    execution keeps its cycle.

    ``reasons`` may be empty: a routing rule can place a document that has no
    confidence alert. The sections in review are then every processed section,
    so the reviewer can work on any of them. With ``routed_by``, the new cycle's
    history gets a ``routed`` entry.

    The tracker activates the item when the run succeeds.
    """
    now_ms = int(now.timestamp() * 1000)
    doc_key = document.input_key
    live = item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}
    queue_name = queue.id
    sections = list(dict.fromkeys(r.section_id for r in reasons))
    if not sections:
        sections = [
            str(section.section_id)
            for section in list(getattr(document, "sections", None) or [])
            if not getattr(section, "excluded", False)
        ]
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
        priority=queue.priority,
        enqueued_at=_iso(now),
        enqueued_at_ms=now_ms,
        holding_at=_iso(now),
        reasons=reasons[:MAX_REASONS],
        reason_count=len(reasons),
        sections=sections,
        announce_groups=list(queue.member_groups),
        announce_emails=list(queue.member_emails),
        phase1_execution_arn=execution_arn,
        sweep_at_ms=now_ms + HOLDING_SWEEP_MS,
        expires_after=int(now.timestamp()) + HOLDING_TTL_SECONDS,
        routed_by=routed_by,
        placement_count=placement_count,
        processing_mode=mode,
        carried_metering_json=carried_metering_json,
        placed_layout=layout_of(document),
    )
    history = None
    if supersede is not None:
        details: dict[str, Any] = {"by_cycle": cycle_id}
        if previous_event == "rerouted":
            details["queue"] = queue_name
            details["rule_id"] = routed_by.rule_id if routed_by else ""
        history = store.history_txn_item(
            doc_key,
            supersede.cycle_id,
            previous_event,
            None,
            details,
            supersede.expires_after,
            now_ms=now_ms,
            now_iso=_iso(now),
        )
    store.put_holding(record, supersede=supersede, history=history)
    if routed_by is not None:
        try:
            store.put_history(
                doc_key,
                cycle_id,
                "routed",
                None,
                {
                    "queue": queue_name,
                    "rule_id": routed_by.rule_id,
                    "rule_label": routed_by.rule_label,
                    "fallback_reason": routed_by.fallback_reason,
                    # Not ``reason``: that key of a history entry is a reason a
                    # person chose (a reject or a skip reason).
                    "routing_reason": routed_by.reason,
                    "reason_source": routed_by.reason_source,
                },
                record.expires_after,
                now_ms=now_ms,
                now_iso=_iso(now),
            )
        except Exception:  # noqa: BLE001 — the item records the rule; history is extra
            logger.warning("event=review_history_put_failed", exc_info=True)
    if supersede is not None and output_bucket:
        from . import status_object

        try:
            status_object.delete(s3, output_bucket, doc_key)
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
    """Decide whether this run holds for review, finalizes one, or neither.

    This is the decision a state machine definition without a routing step
    relies on: the assembly step makes it. It recognises a finalize run
    (``recognize_finalize``) and otherwise holds a document that has a reason in
    the Default queue (``place_for_review``). A run with a routing step decides
    in ``routing_step.decide``.
    """
    # Test Studio runs are scored on the machine's output, and their documents are
    # reviewed as test-set annotation work, so they are never held in a queue.
    if is_test_studio_document(document):
        return ReviewDecision(mode="legacy")
    if review_mode(config, store, document) == "legacy":
        return ReviewDecision(mode="legacy")
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    now = now or datetime.now(timezone.utc)
    doc_key = document.input_key
    out_bucket = output_bucket or getattr(document, "output_bucket", None) or ""
    item = store.get_item(doc_key)
    live = item is not None and item.state in {s.value for s in LIVE_ITEM_STATES}

    # Finalize run of a disposed item (D15).
    finalized = _finalize_of(
        item,
        document,
        store,
        execution_arn,
        apply=True,
        s3=s3,
        output_bucket=out_bucket,
        now=now,
    )
    if finalized is not None:
        finalized_item, outcome = finalized
        return ReviewDecision(
            mode="queues",
            outcome=outcome,
            cycle_id=finalized_item.cycle_id,
            queue=finalized_item.queue,
        )

    # A production document that raised confidence alerts goes to the Default
    # queue, when HITL is enabled in its configuration.
    queue = default_queue(store)
    if queue is None or not queue.enabled or not review_routing_enabled(config):
        # Default was turned off (or is unreadable), or HITL is off and this run
        # is in queue mode only because an earlier item is FINALIZING: nothing new
        # is queued.
        return ReviewDecision(mode="queues", hold=False)
    reasons = build_reasons(document, config, mode=mode)
    if not reasons:
        logger.info("event=review_no_reasons")
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
    return place_for_review(
        document,
        config,
        mode=mode,
        execution_arn=execution_arn,
        store=store,
        item=item,
        queue=queue,
        reasons=reasons,
        s3=s3,
        output_bucket=out_bucket,
        now=now,
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
