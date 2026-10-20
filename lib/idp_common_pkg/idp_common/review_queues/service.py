# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The review-queue service: listing, claims, leases, drafts and dispositions.

Every caller-facing method takes a :class:`CallerContext` (built from verified
claims), checks scope and queue membership unless the caller is an Admin,
re-reads the item with a consistent read and writes the whole item back under an
optimistic ``Version`` condition (D29). A lease is checked inside the write
condition (``LeaseExpiresAtMs > :now``), never only in Python, so an expired
lease can't complete an item however close the race. Refusals raise the domain
exceptions of :mod:`.exceptions`; the API resolver maps them to exactly
``PermissionError`` / ``ValueError`` / ``ResourceNotFound``.
"""

from __future__ import annotations

import heapq
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from idp_common.dynamodb.client import DynamoDBTransactionCanceledError

from . import keys, status_object
from . import notify as notify_module
from .annotators import cognito_user_lookup  # noqa: F401 — re-exported
from .api_models import (
    MAX_DRAFT_BYTES,
    MAX_DRAFT_DEPTH,
    ClaimNextResult,
    MyReviewQueues,
    ReviewDraftView,
    ReviewerLoad,
    ReviewHistoryEntryView,
    ReviewItemDetail,
    ReviewItemPage,
    ReviewItemView,
    ReviewQueueHealth,
    ReviewQueuesOverview,
    ReviewQueueSummary,
    ReviewReasonView,
    ReviewSectionDetail,
    draft_depth,
)
from .directory import QueueDirectory
from .exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
    ReviewValidationError,
)
from .finalize import FinalizeSender, finalize_execution_name, finalize_token
from .models import (
    CallerContext,
    DraftRecord,
    QueueRef,
    ReviewItemRecord,
    UserInfo,
)
from .queue_defs import DEFAULT_QUEUE_ID, display_name
from .statuses import (
    LIVE_ITEM_STATES,
    TRACKING_REVIEW_ATTRIBUTES,
    QueueItemState,
    review_status_for_state,
)
from .store import MAX_HISTORY_PER_CYCLE, ReviewStore, create_review_store

logger = logging.getLogger(__name__)

OPEN = QueueItemState.OPEN.value
CLAIMED = QueueItemState.CLAIMED.value
FINALIZING = QueueItemState.FINALIZING.value
HOLDING = QueueItemState.HOLDING.value
WITHDRAWN = QueueItemState.WITHDRAWN.value

#: Finalize recheck delay after a disposition (the sweeper resends if lost).
FINALIZE_SWEEP_MS = 10 * 60_000
#: Counter repair only lowers a counter idle this long (D23).
COUNTER_IDLE_SECONDS = 60
MAX_CHANGED_FIELDS = 200
CLAIM_NEXT_ATTEMPTS = 10
CLAIM_NEXT_TIER_PEEK = 5
#: Draft objects live in the output bucket under the document's review segment.
DRAFT_KEY_FORMAT = "{doc}/review/drafts/{cycle}/{section}/{revision:06d}.json"

DISPOSITION_METRIC = "ReviewDispositions"
#: History event and metric ``Kind`` of each disposition.
_HISTORY_EVENTS = {
    "complete": "completed",
    "rejected": "rejected",
    "removed": "removed",
}
_METRIC_KINDS = {"complete": "complete", "rejected": "reject", "removed": "removed"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def iso_from_ms(ms: Optional[int]) -> Optional[str]:
    if ms is None:
        return None
    return iso(datetime.fromtimestamp(ms / 1000, tz=timezone.utc))


def warning_lead_ms(lease_minutes: int) -> int:
    """``min(10 min, lease / 2)``: when the expiring notice goes out."""
    return min(10 * 60_000, lease_minutes * 60_000 // 2)


def _put_metric(
    name: str, value: float = 1, dimensions: Optional[list[dict]] = None
) -> None:
    try:
        from idp_common.metrics import put_metric

        put_metric(name, value, dimensions=dimensions)
    except Exception:  # noqa: BLE001 — a metric must not fail the operation
        logger.debug("metric %s not published", name, exc_info=True)


def _reason_views(
    record: ReviewItemRecord, section_id: Optional[str] = None
) -> list[ReviewReasonView]:
    return [
        ReviewReasonView(
            kind=r.kind,
            section_id=r.section_id,
            field=r.field,
            label=r.label,
            score=r.score,
            threshold=r.threshold,
        )
        for r in record.reasons
        if section_id is None or r.section_id == section_id
    ]


def item_view(
    record: ReviewItemRecord, caller: Optional[CallerContext] = None
) -> ReviewItemView:
    status = review_status_for_state(record.state)
    return ReviewItemView(
        object_key=record.doc_key,
        cycle_id=record.cycle_id,
        profile=record.profile,
        queue=record.queue,
        priority=record.priority,
        state=record.state,
        review_status=status.value if status else "withdrawn",
        enqueued_at=record.enqueued_at,
        reasons=_reason_views(record),
        reason_count=record.reason_count,
        sections=list(record.sections),
        owner_email=record.owner_email if record.state == CLAIMED else None,
        claimed_at=record.claimed_at if record.state == CLAIMED else None,
        lease_expires_at=iso_from_ms(record.lease_expires_at_ms)
        if record.state == CLAIMED
        else None,
        is_mine=bool(
            caller
            and record.state == CLAIMED
            and record.owner_key == caller.reviewer_key
        ),
        has_draft=record.has_draft,
        draft_updated_at=record.draft_updated_at,
        draft_updated_by=record.draft_updated_by,
        escalated=record.escalated,
        escalation_note=record.escalation_note,
        disposition=record.disposition,
        disposed_by=record.disposed_by,
        disposed_at=record.disposed_at,
        reject_reason=record.reject_reason,
        version=record.version,
    )


def _leaf_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_leaf_paths(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            out.update(_leaf_paths(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = value
    return out


def changed_fields(base: Any, new: Any) -> list[str]:
    """Leaf paths under ``inference_result`` whose value differs (D71)."""
    a = _leaf_paths(
        (base or {}).get("inference_result", {}) if isinstance(base, dict) else {}
    )
    b = _leaf_paths(
        (new or {}).get("inference_result", {}) if isinstance(new, dict) else {}
    )
    return sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))


class ReviewQueueService:
    """Operations behind the review-queue API, the tracker and the sweeper."""

    def __init__(
        self,
        store: ReviewStore,
        directory: QueueDirectory,
        *,
        notifier: Any = notify_module,
        finalize_sender: Optional[FinalizeSender] = None,
        clock: Callable[[], datetime] = utc_now,
        s3: Any = None,
        document_loader: Optional[Callable[[str], Any]] = None,
        user_lookup: Optional[Callable[[str], Optional[UserInfo]]] = None,
        output_bucket: str = "",
        document_service: Any = None,
    ):
        if s3 is None:
            import idp_common.s3 as s3_module

            s3 = s3_module
        self.store = store
        self.directory = directory
        self.notify = notifier
        self.finalize_sender = finalize_sender
        self.clock = clock
        self.s3 = s3
        self.document_loader = document_loader
        self.user_lookup = user_lookup
        self.output_bucket = output_bucket
        self.document_service = document_service

    # --------------------------------------------------------------- helpers

    def _now(self) -> tuple[datetime, int, str]:
        now = self.clock()
        return now, _ms(now), iso(now)

    def _load(self, doc_key: str) -> ReviewItemRecord:
        record = self.store.get_item(doc_key)
        if record is None:
            raise ReviewItemNotFound("ITEM_NOT_FOUND", "the review item doesn't exist")
        return record

    def _check_cycle(self, record: ReviewItemRecord, cycle_id: str) -> None:
        if record.cycle_id != cycle_id:
            raise ReviewConflict("STALE_CYCLE", "the item has a newer review cycle")

    def _check_access(self, caller: CallerContext, record: ReviewItemRecord) -> None:
        if caller.is_admin:
            return
        if not self.directory.in_scope(caller, record.profile):
            logger.warning("Rejecting review-queue access: OUT_OF_SCOPE")
            raise ReviewAccessDenied(
                "OUT_OF_SCOPE", "the item's profile is outside your scope"
            )
        # The holder keeps their own item even when the queue no longer lists them
        # (an assignment made before they were taken off the list).
        is_holder = record.state == CLAIMED and record.owner_key == caller.reviewer_key
        if not is_holder and not self.directory.is_member(
            caller, record.profile, record.queue
        ):
            logger.warning("Rejecting review-queue access: NOT_A_MEMBER")
            raise ReviewAccessDenied(
                "NOT_A_MEMBER", "you are not a member of this queue"
            )

    def _require_admin(self, caller: CallerContext) -> None:
        if not caller.is_admin:
            logger.warning("Rejecting review-queue supervisor operation: ROLE")
            raise ReviewAccessDenied("ROLE", "only Admins can do this")

    def _rq(self, profile: str):
        """The system-wide queue definitions (``QueuesView``); never ``None``."""
        return self.directory.queues_config(profile)

    def _history(
        self,
        record: ReviewItemRecord,
        event: str,
        actor: Optional[str],
        details: Optional[Mapping[str, Any]],
        now_ms: int,
        now_iso: str,
    ) -> list[dict[str, Any]]:
        """A history transact item (or none when the cycle's history is full).

        Bounded per cycle: the item's ``HistoryCount`` travels with the item's
        Version condition, so two writers can't both take the last slot.
        """
        if record.history_count >= MAX_HISTORY_PER_CYCLE:
            record.history_truncated = True
            return []
        record.history_count += 1
        return [
            self.store.history_txn_item(
                record.doc_key,
                record.cycle_id,
                event,
                actor,
                dict(details or {}),
                record.expires_after,
                now_ms=now_ms,
                now_iso=now_iso,
            )
        ]

    def _write(
        self,
        old: ReviewItemRecord,
        new: ReviewItemRecord,
        *,
        condition: Optional[str] = None,
        values: Optional[Mapping[str, Any]] = None,
        extra_txn: Sequence[dict[str, Any]] = (),
    ) -> ReviewItemRecord:
        new.version = old.version + 1
        self.store.replace_item(
            new,
            expected_version=old.version,
            condition=condition,
            values=values,
            names={"#st": "State"},
            extra_txn=extra_txn,
        )
        return new

    def _release_fields(self, record: ReviewItemRecord) -> None:
        record.owner_key = None
        record.owner_email = None
        record.claimed_at = None
        record.claimed_at_ms = None
        record.lease_expires_at_ms = None
        record.lease_warned_for_ms = None
        record.sweep_at_ms = None

    def _claim_fields(
        self,
        record: ReviewItemRecord,
        owner: CallerContext,
        lease_minutes: int,
        now_ms: int,
        now_iso: str,
    ) -> None:
        record.state = CLAIMED
        record.owner_key = owner.reviewer_key
        record.owner_email = owner.email
        record.claimed_at = now_iso
        record.claimed_at_ms = now_ms
        record.lease_minutes = lease_minutes
        record.lease_expires_at_ms = now_ms + lease_minutes * 60_000
        record.lease_warned_for_ms = None
        record.sweep_at_ms = record.lease_expires_at_ms - warning_lead_ms(lease_minutes)

    def _cap(self, record: ReviewItemRecord) -> int:
        return int(self._rq(record.profile).max_claims_per_reviewer)

    # ------------------------------------------------------------- listing

    def get_my_queues(
        self, caller: CallerContext, item_limit: int = 25
    ) -> MyReviewQueues:
        queues = self.directory.queues_for(caller)
        owned = self.store.all_owned(caller.reviewer_key)
        mine_by_queue: dict[str, int] = {}
        for r in owned:
            mine_by_queue[r.ref.partition] = mine_by_queue.get(r.ref.partition, 0) + 1

        def summarise(entry) -> ReviewQueueSummary:
            ref, definition, rq = entry
            client = self.store.thread_client()
            depth = self.store.count_queue(ref, open_only=False, client=client)
            unclaimed = self.store.count_queue(ref, open_only=True, client=client)
            oldest, _ = self.store.query_queue(
                ref, open_only=False, limit=1, client=client
            )
            return ReviewQueueSummary(
                profile=ref.profile,
                name=definition.id,
                display_name=definition.name,
                description=definition.description or None,
                priority=definition.priority,
                depth=depth,
                unclaimed=unclaimed,
                claimed=max(0, depth - unclaimed),
                claimed_by_me=mine_by_queue.get(ref.partition, 0),
                oldest_enqueued_at=oldest[0].enqueued_at if oldest else None,
                is_default_queue=definition.id.lower() == rq.default_queue.lower(),
                confidence_threshold=definition.confidence_threshold,
                enabled=definition.enabled,
                claim_cap=rq.max_claims_per_reviewer,
                checklist=list(definition.checklist),
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            summaries = list(pool.map(summarise, queues))
        summaries.sort(key=lambda s: (-s.priority, s.profile, s.name))
        profiles = sorted({ref.profile for ref, _, _ in queues})
        return MyReviewQueues(
            queues=summaries,
            my_claim_count=len(owned),
            enabled_profiles=profiles,
            items=self.list_queue_items(
                caller, None, claimable=False, limit=item_limit, cursor=None
            ),
        )

    def _caller_refs(
        self, caller: CallerContext, ref: Optional[QueueRef]
    ) -> list[tuple[QueueRef, int]]:
        if ref is not None:
            # An undefined queue is a bad argument for everyone (Admin or not),
            # decided before membership so the answer can't probe membership.
            rq_defined = self.directory.queues_config(ref.profile)
            if rq_defined is None or rq_defined.queue(ref.queue) is None:
                raise ReviewValidationError("UNKNOWN_QUEUE", "the queue isn't defined")
            if not caller.is_admin:
                if not self.directory.in_scope(caller, ref.profile):
                    raise ReviewAccessDenied(
                        "OUT_OF_SCOPE", "the profile is outside your scope"
                    )
                if not self.directory.is_member(caller, ref.profile, ref.queue):
                    raise ReviewAccessDenied(
                        "NOT_A_MEMBER", "you are not a member of this queue"
                    )
            rq = self._rq(ref.profile)
            definition = rq.queue(ref.queue)
            return [(ref, definition.priority if definition else 50)]
        refs = [(r, d.priority) for r, d, _ in self.directory.queues_for(caller)]
        if caller.is_admin:
            # Admins also see queues that are no longer in configuration (D35).
            known = {r.partition for r, _ in refs}
            for row in self.store.list_registry():
                partition = str(row.get("SK", ""))
                if partition and partition not in known:
                    refs.append((QueueRef.from_partition(partition), 0))
        return refs

    def list_queue_items(
        self,
        caller: CallerContext,
        ref: Optional[QueueRef],
        *,
        claimable: bool,
        limit: int,
        cursor: Optional[str],
    ) -> ReviewItemPage:
        """One queue, or a k-way merge of all the caller's queues (FR-9 order)."""
        refs = self._caller_refs(caller, ref)
        if ref is not None:
            start = keys.decode_cursor(cursor, multi=False) if cursor else None
            items, lek = self.store.query_queue(
                ref, open_only=claimable, limit=limit, start_key=start
            )
            return ReviewItemPage(
                items=[item_view(i, caller) for i in items],
                next_token=keys.encode_single_cursor(lek) if lek else None,
            )
        positions: dict[str, Optional[dict]] = {}
        if cursor:
            positions = keys.decode_cursor(cursor, multi=True)
        priority_of = {r.partition: p for r, p in refs}
        candidates: list[tuple[int, int, str, int, ReviewItemRecord]] = []
        fetched: dict[str, tuple[list[ReviewItemRecord], Optional[dict]]] = {}
        for r, _ in refs:
            if r.partition in positions and positions[r.partition] is None:
                continue  # exhausted
            start = positions.get(r.partition)
            items, lek = self.store.query_queue(
                r, open_only=claimable, limit=limit, start_key=start
            )
            fetched[r.partition] = (items, lek)
            for idx, item in enumerate(items):
                candidates.append(
                    (
                        -priority_of[r.partition],
                        item.enqueued_at_ms,
                        r.partition,
                        idx,
                        item,
                    )
                )
        taken = heapq.nsmallest(limit, candidates, key=lambda c: c[:4])
        last_taken: dict[str, int] = {}
        for _, _, partition, idx, _ in taken:
            last_taken[partition] = max(last_taken.get(partition, -1), idx)
        new_positions: dict[str, Optional[dict]] = dict(positions)
        more = False
        for partition, (items, lek) in fetched.items():
            taken_idx = last_taken.get(partition)
            if taken_idx is None:
                # Nothing taken: keep the previous position.
                if items:
                    more = True
                else:
                    new_positions[partition] = None
                continue
            if taken_idx == len(items) - 1 and not lek:
                new_positions[partition] = None
                continue
            last = items[taken_idx]
            index_key = {
                "PK": keys.doc_pk(last.doc_key),
                "SK": keys.ITEM_SK,
                ("OpenPK" if claimable else "QueuePK"): partition,
                "QueueSK": keys.queue_sk(last.enqueued_at_ms, last.doc_key),
            }
            new_positions[partition] = index_key
            more = True
        token = None
        if more and any(v is not None for v in new_positions.values()):
            for r, _ in refs:
                new_positions.setdefault(r.partition, None)
            token = keys.encode_multi_cursor(new_positions)
        return ReviewItemPage(
            items=[item_view(c[4], caller) for c in taken], next_token=token
        )

    def list_my_work(
        self, caller: CallerContext, *, limit: int, cursor: Optional[str]
    ) -> ReviewItemPage:
        start = keys.decode_cursor(cursor, multi=False) if cursor else None
        items, lek = self.store.query_owner(
            caller.reviewer_key, limit=limit, start_key=start
        )
        if not cursor:
            self.repair_counter(caller.reviewer_key)
        return ReviewItemPage(
            items=[item_view(i, caller) for i in items],
            next_token=keys.encode_single_cursor(lek) if lek else None,
        )

    def get_item_detail(
        self, caller: CallerContext, object_key: str
    ) -> ReviewItemDetail:
        record = self._load(object_key)
        self._check_access(caller, record)
        document = self.document_loader(object_key) if self.document_loader else None
        if self.document_loader is not None and document is None:
            self.withdraw(object_key, "document_missing")
            raise ReviewItemNotFound(
                "DOCUMENT_NOT_FOUND", "the document no longer exists"
            )
        rq = self._rq(record.profile)
        definition = rq.queue(record.queue)
        drafts = {
            d.section_id: d for d in self.store.list_drafts(object_key, record.cycle_id)
        }
        sections: list[ReviewSectionDetail] = []
        doc_sections = {
            str(s.section_id): s for s in (getattr(document, "sections", None) or [])
        }
        for section_id in record.sections:
            s = doc_sections.get(section_id)
            d = drafts.get(section_id)
            sections.append(
                ReviewSectionDetail(
                    section_id=section_id,
                    document_class=getattr(s, "classification", None),
                    result_uri=getattr(s, "extraction_result_uri", None),
                    draft_uri=f"s3://{self.output_bucket}/{d.s3_key}" if d else None,
                    draft_revision=d.revision if d else None,
                    draft_updated_by=d.updated_by if d else None,
                    draft_updated_at=d.updated_at if d else None,
                    page_ids=[str(p) for p in (getattr(s, "page_ids", None) or [])],
                    reasons=_reason_views(record, section_id),
                )
            )
        history = [
            ReviewHistoryEntryView(
                at=h.at,
                event=h.event,
                actor=h.actor,
                details=json.dumps(h.details) if h.details else None,
            )
            for h in self.store.list_history(object_key, record.cycle_id)
        ]
        my_count = self.store.counter(caller.reviewer_key)
        can_claim, blocked = self._claimability(
            caller, record, my_count, rq.max_claims_per_reviewer
        )
        return ReviewItemDetail(
            item=item_view(record, caller),
            sections=sections,
            checklist=list(definition.checklist) if definition else [],
            reject_reasons=list(
                definition.reject_reasons if definition else rq.reject_reasons
            ),
            claim_cap=rq.max_claims_per_reviewer,
            my_claim_count=my_count,
            history=history,
            machine_output_manifest_uri=record.machine_manifest_uri,
            can_claim=can_claim,
            claim_blocked_reason=blocked,
        )

    def _claimability(
        self, caller: CallerContext, record: ReviewItemRecord, my_count: int, cap: int
    ) -> tuple[bool, Optional[str]]:
        _, now_ms, _ = self._now()
        if record.state == CLAIMED and (record.lease_expires_at_ms or 0) > now_ms:
            if record.owner_key == caller.reviewer_key:
                return False, None
            return False, "ALREADY_CLAIMED"
        if record.state not in (OPEN, CLAIMED):
            return False, "INVALID_STATE"
        if not caller.is_admin and not self.directory.is_member(
            caller, record.profile, record.queue
        ):
            return False, "NOT_A_MEMBER"
        if my_count >= cap:
            return False, "CAP_REACHED"
        return True, None

    # -------------------------------------------------------------- claims

    def claim(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemView:
        return item_view(self._claim(caller, object_key, cycle_id), caller)

    def _claim(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: Optional[str],
        *,
        retry_cap: bool = True,
    ) -> ReviewItemRecord:
        old = self._load(object_key)
        self._check_access(caller, old)
        if cycle_id is not None:
            self._check_cycle(old, cycle_id)
        now, now_ms, now_iso = self._now()
        lease_valid = old.state == CLAIMED and (old.lease_expires_at_ms or 0) > now_ms
        if old.state == CLAIMED and lease_valid:
            raise ReviewConflict("ALREADY_CLAIMED", f"claimed by {old.owner_email}")
        if old.state not in (OPEN, CLAIMED):
            raise ReviewConflict("INVALID_STATE", f"the item is {old.state}")
        rq = self._rq(old.profile)
        new = old.model_copy(deep=True)
        reclaim = old.state == CLAIMED and old.owner_key
        self._claim_fields(new, caller, rq.lease_minutes, now_ms, now_iso)
        # A claim is the Annotator assigning the item to themselves: like an Admin
        # assignment it never expires, and the queue's time limit sends a reminder.
        definition = rq.queue(old.queue)
        time_limit = definition.time_limit_minutes if definition else 60
        new.sweep_at_ms = now_ms + time_limit * 60_000
        extra = [
            self.store.counter_txn_item(
                caller.reviewer_key, 1, cap=rq.max_claims_per_reviewer, now_iso=now_iso
            )
        ]
        if reclaim:
            extra.append(
                self.store.counter_txn_item(old.owner_key, -1, now_iso=now_iso)
            )
        event = "reclaimed_expired" if reclaim else "claimed"
        extra.extend(self._history(new, event, caller.email, {}, now_ms, now_iso))
        if old.state == OPEN:
            condition = "#st = :open"
            values: dict[str, Any] = {":open": OPEN}
        else:
            condition = "#st = :claimed AND LeaseExpiresAtMs <= :now"
            values = {":claimed": CLAIMED, ":now": now_ms}
        try:
            self._write(old, new, condition=condition, values=values, extra_txn=extra)
        except DynamoDBTransactionCanceledError as exc:
            if exc.reason_code(1) == "ConditionalCheckFailed":
                if retry_cap and self.repair_counter(caller.reviewer_key):
                    return self._claim(caller, object_key, cycle_id, retry_cap=False)
                raise ReviewConflict(
                    "CAP_REACHED",
                    f"you already hold {rq.max_claims_per_reviewer} claims",
                ) from None
            current = self.store.get_item(object_key)
            if current and current.state == CLAIMED:
                raise ReviewConflict(
                    "ALREADY_CLAIMED", f"claimed by {current.owner_email}"
                ) from None
            raise ReviewConflict(
                "INVALID_STATE", "the item changed; reload it"
            ) from None
        _put_metric("ReviewClaims")
        logger.info("event=review_claimed reclaim=%s", bool(reclaim))
        return new

    def claim_next(
        self, caller: CallerContext, ref: Optional[QueueRef] = None
    ) -> ClaimNextResult:
        refs = self._caller_refs(caller, ref)
        tiers: dict[int, list[QueueRef]] = {}
        for r, priority in refs:
            tiers.setdefault(priority, []).append(r)
        attempts = 0
        saw_open = False
        cap_hits = 0
        for priority in sorted(tiers, reverse=True):
            candidates: list[ReviewItemRecord] = []
            for r in tiers[priority]:
                items, _ = self.store.query_queue(
                    r, open_only=True, limit=CLAIM_NEXT_TIER_PEEK
                )
                candidates.extend(items)
            candidates.sort(key=lambda i: (i.enqueued_at_ms, i.doc_key))
            for candidate in candidates:
                saw_open = True
                if attempts >= CLAIM_NEXT_ATTEMPTS:
                    return ClaimNextResult(item=None, reason="CONTENDED")
                attempts += 1
                try:
                    record = self._claim(caller, candidate.doc_key, candidate.cycle_id)
                    return ClaimNextResult(item=item_view(record, caller))
                except ReviewConflict as exc:
                    if exc.code == "CAP_REACHED":
                        cap_hits += 1
                    continue
                except ReviewAccessDenied:
                    continue
        if cap_hits and cap_hits == attempts:
            return ClaimNextResult(item=None, reason="CAP_REACHED")
        return ClaimNextResult(item=None, reason="CONTENDED" if saw_open else "EMPTY")

    def _owned_live(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemRecord:
        record = self._load(object_key)
        self._check_access(caller, record)
        self._check_cycle(record, cycle_id)
        if record.state != CLAIMED:
            raise ReviewConflict("INVALID_STATE", f"the item is {record.state}")
        if record.owner_key != caller.reviewer_key:
            logger.warning("Rejecting review-queue operation: NOT_LEASE_HOLDER")
            raise ReviewAccessDenied(
                "NOT_LEASE_HOLDER", "you don't hold this item's claim"
            )
        return record

    def _lease_condition(
        self, owner_key: str, now_ms: int
    ) -> tuple[str, dict[str, Any]]:
        return (
            "#st = :claimed AND OwnerKey = :me AND LeaseExpiresAtMs > :now",
            {":claimed": CLAIMED, ":me": owner_key, ":now": now_ms},
        )

    def _raise_lease_failure(self, object_key: str, owner_key: str) -> None:
        current = self.store.get_item(object_key)
        _, now_ms, _ = self._now()
        if current is None:
            raise ReviewItemNotFound("ITEM_NOT_FOUND", "the review item doesn't exist")
        if current.state != CLAIMED:
            raise ReviewConflict("INVALID_STATE", f"the item is {current.state}")
        if current.owner_key != owner_key:
            raise ReviewAccessDenied(
                "NOT_LEASE_HOLDER", "you don't hold this item's claim"
            )
        if (current.lease_expires_at_ms or 0) <= now_ms:
            raise ReviewConflict(
                "LEASE_EXPIRED", "your claim expired; claim the item again"
            )
        raise ReviewConflict("INVALID_STATE", "the item changed; reload it")

    def renew(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemView:
        old = self._owned_live(caller, object_key, cycle_id)
        _, now_ms, _ = self._now()
        new = old.model_copy(deep=True)
        minutes = old.lease_minutes or self._rq(old.profile).lease_minutes
        new.lease_expires_at_ms = now_ms + minutes * 60_000
        new.lease_warned_for_ms = None
        new.sweep_at_ms = new.lease_expires_at_ms - warning_lead_ms(minutes)
        condition, values = self._lease_condition(caller.reviewer_key, now_ms)
        try:
            self._write(old, new, condition=condition, values=values)
        except DynamoDBTransactionCanceledError:
            self._raise_lease_failure(object_key, caller.reviewer_key)
        return item_view(new, caller)

    def release(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemView:
        old = self._owned_live(caller, object_key, cycle_id)
        _, now_ms, now_iso = self._now()
        new = old.model_copy(deep=True)
        new.state = OPEN
        self._release_fields(new)
        extra = [self.store.counter_txn_item(caller.reviewer_key, -1, now_iso=now_iso)]
        extra.extend(self._history(new, "released", caller.email, {}, now_ms, now_iso))
        try:
            self._write(
                old,
                new,
                condition="#st = :claimed AND OwnerKey = :me",
                values={":claimed": CLAIMED, ":me": caller.reviewer_key},
                extra_txn=extra,
            )
        except DynamoDBTransactionCanceledError:
            self._raise_lease_failure(object_key, caller.reviewer_key)
        return item_view(new, caller)

    # -------------------------------------------------------------- drafts

    def _read_json_uri(self, uri: Optional[str]) -> Any:
        if not uri:
            return None
        try:
            return self.s3.get_json_content(uri)
        except Exception:  # noqa: BLE001 — the diff base is best effort
            logger.warning("event=review_draft_base_unreadable", exc_info=True)
            return None

    def save_draft(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: str,
        section_id: str,
        data: Any,
        base_revision: Optional[int],
    ) -> ReviewDraftView:
        old = self._owned_live(caller, object_key, cycle_id)
        if section_id not in old.sections:
            raise ReviewValidationError("BAD_ARGUMENT", "the section isn't in review")
        if not isinstance(data, dict):
            raise ReviewValidationError(
                "BAD_ARGUMENT", "draft data must be a JSON object"
            )
        body = json.dumps(data, separators=(",", ":"))
        if len(body.encode("utf-8")) > MAX_DRAFT_BYTES:
            raise ReviewValidationError(
                "DRAFT_TOO_LARGE", "the draft exceeds 2,000,000 bytes"
            )
        if draft_depth(data) > MAX_DRAFT_DEPTH:
            raise ReviewValidationError(
                "BAD_ARGUMENT", "the draft is nested too deeply"
            )
        current = self.store.get_draft(object_key, cycle_id, section_id)
        current_rev = current.revision if current else 0
        if base_revision is not None and base_revision != current_rev:
            raise ReviewConflict("STALE_DRAFT", "a newer draft was saved; reload it")
        now, now_ms, now_iso = self._now()
        revision = current_rev + 1
        s3_key = DRAFT_KEY_FORMAT.format(
            doc=object_key, cycle=cycle_id, section=section_id, revision=revision
        )
        if current is not None:
            base = self._read_json_uri(f"s3://{self.output_bucket}/{current.s3_key}")
        else:
            section = None
            document = (
                self.document_loader(object_key) if self.document_loader else None
            )
            for s in getattr(document, "sections", None) or []:
                if str(s.section_id) == section_id:
                    section = s
            base = self._read_json_uri(getattr(section, "extraction_result_uri", None))
        diff = changed_fields(base, data)
        self.s3.write_content(body, self.output_bucket, s3_key, "application/json")
        authors = sorted(set((current.authors if current else [])) | {caller.email})
        draft = DraftRecord(
            doc_key=object_key,
            cycle_id=cycle_id,
            section_id=section_id,
            S3Key=s3_key,
            revision=revision,
            updated_by=caller.email,
            updated_at=now_iso,
            authors=authors,
            changed_fields=diff[:MAX_CHANGED_FIELDS],
            changed_fields_truncated=len(diff) > MAX_CHANGED_FIELDS,
            size_bytes=len(body.encode("utf-8")),
            logged_for_claim_ms=current.logged_for_claim_ms if current else None,
            expires_after=old.expires_after,
        )
        new = old.model_copy(deep=True)
        minutes = old.lease_minutes or self._rq(old.profile).lease_minutes
        new.lease_expires_at_ms = now_ms + minutes * 60_000
        new.lease_warned_for_ms = None
        new.sweep_at_ms = new.lease_expires_at_ms - warning_lead_ms(minutes)
        new.has_draft = True
        new.draft_updated_at = now_iso
        new.draft_updated_by = caller.email
        extra: list[dict[str, Any]] = []
        if draft.logged_for_claim_ms != old.claimed_at_ms:
            draft.logged_for_claim_ms = old.claimed_at_ms
            extra.extend(
                self._history(
                    new,
                    "draft_saved",
                    caller.email,
                    {"section": section_id},
                    now_ms,
                    now_iso,
                )
            )
        extra.insert(
            0,
            self.store.draft_txn_item(
                draft, expected_revision=current.revision if current else None
            ),
        )
        condition, values = self._lease_condition(caller.reviewer_key, now_ms)
        try:
            self._write(old, new, condition=condition, values=values, extra_txn=extra)
        except DynamoDBTransactionCanceledError as exc:
            if exc.reason_code(1) == "ConditionalCheckFailed":
                raise ReviewConflict(
                    "STALE_DRAFT", "a newer draft was saved; reload it"
                ) from None
            self._raise_lease_failure(object_key, caller.reviewer_key)
        return ReviewDraftView(
            section_id=section_id,
            revision=revision,
            draft_uri=f"s3://{self.output_bucket}/{s3_key}",
            updated_at=now_iso,
            lease_expires_at=iso_from_ms(new.lease_expires_at_ms),
            changed_field_count=len(diff),
        )

    # -------------------------------------------------------- dispositions

    def _disposable(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemRecord:
        """The item a disposition applies to: the caller's claim, or any claim (Admin)."""
        record = self._load(object_key)
        self._check_access(caller, record)
        self._check_cycle(record, cycle_id)
        if record.state != CLAIMED:
            raise ReviewConflict("INVALID_STATE", f"the item is {record.state}")
        if record.owner_key != caller.reviewer_key and not caller.is_admin:
            logger.warning("Rejecting review-queue disposition: NOT_LEASE_HOLDER")
            raise ReviewAccessDenied(
                "NOT_LEASE_HOLDER", "you don't hold this item's claim"
            )
        return record

    def _disposition_condition(
        self, caller: CallerContext, record: ReviewItemRecord, now_ms: int
    ):
        if record.owner_key == caller.reviewer_key:
            return self._lease_condition(caller.reviewer_key, now_ms)
        # Admin completing someone else's claim: the claim must still be held.
        return "#st = :claimed AND OwnerKey = :owner", {
            ":claimed": CLAIMED,
            ":owner": record.owner_key,
        }

    def _finalize(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: str,
        *,
        disposition: str,
        apply: Callable[[ReviewItemRecord], None],
        supervised: bool = False,
        note: Optional[str] = None,
    ) -> ReviewItemView:
        """Move the item to FINALIZING and send its finalize message.

        ``supervised`` is an Admin acting on an OPEN or CLAIMED item they need not
        hold (Remove from queue); the write is conditioned on the state read, and
        a claimant is told their claim ended.
        """
        if supervised:
            old = self._supervised(caller, object_key, cycle_id)
            if old.state not in (OPEN, CLAIMED):
                raise ReviewConflict("INVALID_STATE", f"the item is {old.state}")
        else:
            old = self._disposable(caller, object_key, cycle_id)
        _, now_ms, now_iso = self._now()
        new = old.model_copy(deep=True)
        apply(new)
        new.state = FINALIZING
        new.disposition = disposition
        new.disposed_by = caller.email
        new.disposed_at = now_iso
        new.finalize_attempt = 1
        new.finalize_token = finalize_token(object_key, old.cycle_id, 1)
        new.finalize_execution_name = finalize_execution_name(
            object_key, new.finalize_token
        )
        new.finalize_requested_at = now_iso
        new.finalize_resend_count = 0
        owner = old.owner_key
        self._release_fields(new)
        new.sweep_at_ms = now_ms + FINALIZE_SWEEP_MS
        extra = (
            [self.store.counter_txn_item(owner, -1, now_iso=now_iso)] if owner else []
        )
        extra.extend(
            self._history(
                new,
                _HISTORY_EVENTS[disposition],
                caller.email,
                {"note": note} if note else {},
                now_ms,
                now_iso,
            )
        )
        if supervised:
            condition, values = "#st = :st", {":st": old.state}
        else:
            condition, values = self._disposition_condition(caller, old, now_ms)
        try:
            self._write(old, new, condition=condition, values=values, extra_txn=extra)
        except DynamoDBTransactionCanceledError:
            if supervised:
                raise ReviewConflict(
                    "INVALID_STATE", "the item changed; reload it"
                ) from None
            self._raise_lease_failure(object_key, owner or caller.reviewer_key)
        self._send_finalize(new)
        _put_metric(
            DISPOSITION_METRIC,
            dimensions=[{"Name": "Kind", "Value": _METRIC_KINDS[disposition]}],
        )
        if supervised and old.state == CLAIMED and old.owner_email:
            self.notify.claim_released(new, old.owner_email, disposition)
        logger.info("event=review_disposed disposition=%s", disposition)
        return item_view(new, caller)

    def _send_finalize(self, record: ReviewItemRecord) -> None:
        """Send the finalize message; a failure is logged and healed by the sweeper."""
        if self.finalize_sender is None:
            logger.error("event=review_finalize_send_failed reason=no_sender")
            _put_metric("ReviewFinalizeSendFailures")
            return
        try:
            self.finalize_sender.send(record)
        except Exception:  # noqa: BLE001 — recorded; the sweeper resends (FR-11)
            logger.error("event=review_finalize_send_failed", exc_info=True)
            _put_metric("ReviewFinalizeSendFailures")
            return
        try:
            _, _, now_iso = self._now()
            self.store.client.update_item(
                keys.item_key(record.doc_key),
                "SET FinalizeSentAt = :t",
                expression_attribute_values={
                    ":t": now_iso,
                    ":tok": record.finalize_token,
                },
                condition_expression="FinalizeToken = :tok",
                return_values="NONE",
            )
        except Exception:  # noqa: BLE001 — best effort bookkeeping
            logger.debug("FinalizeSentAt not recorded", exc_info=True)

    def complete(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: str,
        checklist: Optional[Sequence[bool]] = None,
        note: Optional[str] = None,
    ) -> ReviewItemView:
        record = self._load(object_key)
        definition = self._rq(record.profile).queue(record.queue)
        template = list(definition.checklist) if definition else []
        if checklist is not None and len(checklist) != len(template):
            raise ReviewValidationError(
                "BAD_ARGUMENT", "the checklist doesn't match the queue's template"
            )

        def apply(r: ReviewItemRecord) -> None:
            r.checklist = list(checklist) if checklist is not None else None
            r.checklist_template = template
            r.complete_note = note

        return self._finalize(
            caller, object_key, cycle_id, disposition="complete", apply=apply
        )

    def reject(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: str,
        reason: str,
        note: Optional[str] = None,
    ) -> ReviewItemView:
        if not reason or not reason.strip():
            raise ReviewValidationError("REASON_REQUIRED", "choose a reject reason")
        record = self._load(object_key)
        rq = self._rq(record.profile)
        definition = rq.queue(record.queue)
        allowed = definition.reject_reasons if definition else rq.reject_reasons
        if reason not in allowed:
            raise ReviewValidationError(
                "BAD_ARGUMENT", "the reason isn't in this queue's list"
            )

        def apply(r: ReviewItemRecord) -> None:
            r.reject_reason = reason
            r.reject_note = note

        return self._finalize(
            caller, object_key, cycle_id, disposition="rejected", apply=apply
        )

    def remove(
        self,
        caller: CallerContext,
        object_key: str,
        cycle_id: str,
        note: Optional[str] = None,
    ) -> ReviewItemView:
        """Admin: take an OPEN or CLAIMED item out of review.

        The document completes with its machine results (no draft is applied),
        its status object is deleted, and a claimant is told their claim ended.
        """

        def apply(r: ReviewItemRecord) -> None:
            r.complete_note = note

        return self._finalize(
            caller,
            object_key,
            cycle_id,
            disposition="removed",
            apply=apply,
            supervised=True,
            note=note,
        )

    def _safe_registry(
        self, ref: QueueRef, groups: Sequence[str], emails: Sequence[str], now_iso: str
    ) -> None:
        try:
            self.store.put_registry(
                ref, member_groups=groups, member_emails=emails, now_iso=now_iso
            )
        except Exception:  # noqa: BLE001 — the sweeper refreshes the registry
            logger.warning("event=review_registry_put_failed", exc_info=True)

    # ---------------------------------------------------------- supervisor

    def _supervised(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemRecord:
        self._require_admin(caller)
        record = self._load(object_key)
        self._check_cycle(record, cycle_id)
        return record

    def force_release(
        self, caller: CallerContext, object_key: str, cycle_id: str
    ) -> ReviewItemView:
        old = self._supervised(caller, object_key, cycle_id)
        if old.state != CLAIMED:
            raise ReviewConflict("INVALID_STATE", f"the item is {old.state}")
        _, now_ms, now_iso = self._now()
        new = old.model_copy(deep=True)
        former = old.owner_email
        new.state = OPEN
        self._release_fields(new)
        extra = (
            [self.store.counter_txn_item(old.owner_key, -1, now_iso=now_iso)]
            if old.owner_key
            else []
        )
        extra.extend(
            self._history(
                new,
                "force_released",
                caller.email,
                {"former_owner": former},
                now_ms,
                now_iso,
            )
        )
        try:
            self._write(
                old,
                new,
                condition="#st = :claimed",
                values={":claimed": CLAIMED},
                extra_txn=extra,
            )
        except DynamoDBTransactionCanceledError:
            raise ReviewConflict(
                "INVALID_STATE", "the item changed; reload it"
            ) from None
        if former:
            self.notify.claim_released(new, former, "force_release")
        return item_view(new, caller)

    def reassign(
        self, caller: CallerContext, object_key: str, cycle_id: str, assignee_email: str
    ) -> ReviewItemView:
        old = self._supervised(caller, object_key, cycle_id)
        if old.state not in (OPEN, CLAIMED):
            raise ReviewConflict("INVALID_STATE", f"the item is {old.state}")
        from idp_common.notifications.keys import normalize_recipient_key

        email = assignee_email.strip().lower()
        info = self.user_lookup(email) if self.user_lookup else None
        groups = set(info.groups) if info else set()
        # HITL work is assigned to Annotators only (Admins assign; nobody self-claims).
        if info is None or not info.exists or "Annotator" not in groups:
            raise ReviewValidationError(
                "ASSIGNEE_NOT_ELIGIBLE", "the user isn't an Annotator"
            )
        assignee = CallerContext(
            reviewer_key=normalize_recipient_key(email),
            email=email,
            groups=frozenset(groups),
            is_admin="Admin" in groups,
        )
        if not self.directory.is_member(
            assignee.model_copy(update={"is_admin": False}), old.profile, old.queue
        ):
            raise ReviewValidationError(
                "ASSIGNEE_NOT_ELIGIBLE", "the Annotator isn't eligible for this queue"
            )
        if old.state == CLAIMED and old.owner_key == assignee.reviewer_key:
            raise ReviewValidationError(
                "BAD_ARGUMENT", "the user already holds the claim"
            )
        _, now_ms, now_iso = self._now()
        rq = self._rq(old.profile)
        new = old.model_copy(deep=True)
        former_key, former_email = (
            (old.owner_key, old.owner_email) if old.state == CLAIMED else (None, None)
        )
        self._claim_fields(new, assignee, rq.lease_minutes, now_ms, now_iso)
        # An assignment never expires; the sweep at the queue's time limit only
        # sends the Annotator a reminder.
        definition = rq.queue(old.queue)
        time_limit = definition.time_limit_minutes if definition else 60
        new.sweep_at_ms = now_ms + time_limit * 60_000
        extra = [
            self.store.counter_txn_item(
                assignee.reviewer_key,
                1,
                cap=rq.max_claims_per_reviewer,
                now_iso=now_iso,
            )
        ]
        if former_key:
            extra.append(self.store.counter_txn_item(former_key, -1, now_iso=now_iso))
        extra.extend(
            self._history(
                new, "reassigned", caller.email, {"to": email}, now_ms, now_iso
            )
        )
        try:
            self._write(
                old,
                new,
                condition="#st = :st",
                values={":st": old.state},
                extra_txn=extra,
            )
        except DynamoDBTransactionCanceledError as exc:
            if exc.reason_code(1) == "ConditionalCheckFailed":
                raise ReviewConflict(
                    "CAP_REACHED", "the reviewer is at their claim cap"
                ) from None
            raise ReviewConflict(
                "INVALID_STATE", "the item changed; reload it"
            ) from None
        self.notify.item_reassigned(new, email)
        if former_email:
            self.notify.claim_released(new, former_email, "reassign")
        return item_view(new, caller)

    def move(
        self, caller: CallerContext, object_key: str, cycle_id: str, target_queue: str
    ) -> ReviewItemView:
        old = self._supervised(caller, object_key, cycle_id)
        if old.state not in (OPEN, CLAIMED):
            raise ReviewConflict("INVALID_STATE", f"the item is {old.state}")
        target = self._rq(old.profile).queue(target_queue)
        if target is None:
            raise ReviewValidationError(
                "UNKNOWN_QUEUE", "the queue isn't defined in the item's profile"
            )
        if target.id.lower() == old.queue.lower():
            raise ReviewValidationError(
                "BAD_ARGUMENT", "the item is already in that queue"
            )
        _, now_ms, now_iso = self._now()
        new = old.model_copy(deep=True)
        former_key, former_email = (
            (old.owner_key, old.owner_email) if old.state == CLAIMED else (None, None)
        )
        new.state = OPEN
        self._release_fields(new)
        new.queue = target.id
        new.priority = target.priority
        extra = (
            [self.store.counter_txn_item(former_key, -1, now_iso=now_iso)]
            if former_key
            else []
        )
        extra.extend(
            self._history(
                new,
                "moved",
                caller.email,
                {"from": old.queue, "to": target.id},
                now_ms,
                now_iso,
            )
        )
        try:
            self._write(
                old,
                new,
                condition="#st = :st",
                values={":st": old.state},
                extra_txn=extra,
            )
        except DynamoDBTransactionCanceledError:
            raise ReviewConflict(
                "INVALID_STATE", "the item changed; reload it"
            ) from None
        groups, emails = self.directory.members(new.profile, new.queue)
        self._safe_registry(new.ref, groups, emails, now_iso)
        if former_email:
            self.notify.claim_released(new, former_email, "move")
        return item_view(new, caller)

    def delete_queue(self, caller: CallerContext, queue_id: str) -> int:
        """Admin: delete a queue, moving its items to Default; return how many.

        OPEN and CLAIMED items move with :meth:`move` (unassigned; an assignee is
        told), and HOLDING items are re-pointed at Default before they activate.
        Refused while an item is FINALIZING, because its finalize run still
        reads the queue (``QUEUE_FINALIZING``).
        """
        self._require_admin(caller)
        queue_id = queue_id.strip().lower()
        if queue_id == DEFAULT_QUEUE_ID:
            raise ReviewValidationError(
                "PROTECTED_QUEUE", "the Default queue can't be deleted"
            )
        if self.store.in_flight(queue_id, (FINALIZING,)):
            raise ReviewConflict(
                "QUEUE_FINALIZING",
                "an item in this queue is being finalized; try again in a minute",
            )
        ref = QueueRef(queue=queue_id)
        live: list[ReviewItemRecord] = []
        start: Optional[dict[str, Any]] = None
        while True:
            page, start = self.store.query_queue(
                ref, open_only=False, limit=100, start_key=start
            )
            live.extend(page)
            if not start:
                break
        moved = 0
        for item in live:
            try:
                self.move(caller, item.doc_key, item.cycle_id, DEFAULT_QUEUE_ID)
                moved += 1
            except (ReviewConflict, ReviewItemNotFound, ReviewValidationError) as exc:
                # Changed since the listing (disposed, withdrawn, already moved):
                # it no longer needs moving.
                logger.info("event=review_queue_delete_item_skipped code=%s", exc.code)
        default = self._rq("").queue(DEFAULT_QUEUE_ID)
        _, _, now_iso = self._now()
        for item in self.store.in_flight(queue_id, (HOLDING,)):
            try:
                self.store.transition(
                    item.doc_key,
                    expected_version=item.version,
                    set_={
                        "Queue": DEFAULT_QUEUE_ID,
                        "Priority": default.priority if default else item.priority,
                    },
                    condition="#st = :holding",
                    values={":holding": HOLDING},
                )
                moved += 1
            except DynamoDBTransactionCanceledError:
                # Activated or withdrawn meanwhile: activation re-points it.
                logger.info("event=review_queue_delete_hold_changed")
        self.directory.queue_defs.delete(queue_id)
        logger.info("event=review_queue_deleted moved=%d", moved)
        return moved

    def overview(self, caller: CallerContext) -> ReviewQueuesOverview:
        """Queue health for Admins, including queues no longer in configuration."""
        self._require_admin(caller)
        configured = {
            r.partition: (r, d, rq) for r, d, rq in self.directory.queues_for(caller)
        }
        registry = {str(row.get("SK")): row for row in self.store.list_registry()}
        partitions = list(dict.fromkeys([*configured, *registry]))

        def health(partition: str) -> ReviewQueueHealth:
            client = self.store.thread_client()
            ref = QueueRef.from_partition(partition)
            depth = self.store.count_queue(ref, open_only=False, client=client)
            unclaimed = self.store.count_queue(ref, open_only=True, client=client)
            oldest, _ = self.store.query_queue(
                ref, open_only=False, limit=1, client=client
            )
            loads: dict[str, int] = {}
            in_progress = 0
            start = None
            while True:
                page, start = self.store.query_queue(
                    ref, open_only=False, limit=100, start_key=start, client=client
                )
                for item in page:
                    if item.state == CLAIMED and item.owner_email:
                        loads[item.owner_email] = loads.get(item.owner_email, 0) + 1
                        if item.has_draft:
                            in_progress += 1
                if not start:
                    break
            entry = configured.get(partition)
            if entry:
                _, definition, rq = entry
                summary = ReviewQueueSummary(
                    profile=ref.profile,
                    name=definition.id,
                    display_name=definition.name,
                    description=definition.description or None,
                    priority=definition.priority,
                    is_default_queue=definition.id.lower() == rq.default_queue.lower(),
                    confidence_threshold=definition.confidence_threshold,
                    enabled=definition.enabled,
                    claim_cap=rq.max_claims_per_reviewer,
                    checklist=list(definition.checklist),
                    defined_in_config=True,
                )
            else:
                summary = ReviewQueueSummary(
                    profile=ref.profile,
                    name=ref.queue,
                    display_name=display_name(ref.queue),
                    priority=0,
                    defined_in_config=False,
                )
            summary.depth = depth
            summary.unclaimed = unclaimed
            summary.claimed = max(0, depth - unclaimed)
            summary.in_progress = in_progress
            summary.annotators = len(loads)
            summary.oldest_enqueued_at = oldest[0].enqueued_at if oldest else None
            reviewers = [
                ReviewerLoad(email=e, claimed=n) for e, n in sorted(loads.items())
            ]
            return ReviewQueueHealth(summary=summary, reviewers=reviewers)

        with ThreadPoolExecutor(max_workers=8) as pool:
            queues = list(pool.map(health, partitions))
        queues.sort(
            key=lambda h: (
                not h.summary.defined_in_config,
                -h.summary.priority,
                h.summary.profile,
                h.summary.name,
            )
        )
        return ReviewQueuesOverview(queues=queues)

    # -------------------------------------------------------------- system

    def withdraw(self, doc_key: str, reason: str) -> bool:
        """Withdraw a live item (abort, reprocess, supersede, missing document)."""
        old = self.store.get_item(doc_key)
        if old is None or old.state not in {s.value for s in LIVE_ITEM_STATES}:
            return False
        _, now_ms, now_iso = self._now()
        new = old.model_copy(deep=True)
        former_key, former_email = (
            (old.owner_key, old.owner_email) if old.state == CLAIMED else (None, None)
        )
        new.state = WITHDRAWN
        self._release_fields(new)
        new.withdrawn_reason = reason
        new.withdrawn_at = now_iso
        extra = (
            [self.store.counter_txn_item(former_key, -1, now_iso=now_iso)]
            if former_key
            else []
        )
        extra.extend(
            self._history(new, "withdrawn", None, {"reason": reason}, now_ms, now_iso)
        )
        try:
            self._write(old, new, extra_txn=extra)
        except DynamoDBTransactionCanceledError:
            # Raced with another writer: try once more from a fresh read.
            return self._withdraw_retry(doc_key, reason)
        self._clear_outputs(doc_key)
        if reason == "reprocessed" and former_email:
            self.notify.claim_released(new, former_email, "reprocessed")
        logger.info("event=review_withdrawn reason=%s", reason)
        return True

    def _withdraw_retry(self, doc_key: str, reason: str) -> bool:
        current = self.store.get_item(doc_key)
        if current is None or current.state not in {s.value for s in LIVE_ITEM_STATES}:
            return False
        return self.withdraw(doc_key, reason)

    def _clear_outputs(self, doc_key: str) -> None:
        if self.output_bucket:
            try:
                status_object.delete(self.s3, self.output_bucket, doc_key)
            except Exception:  # noqa: BLE001
                logger.warning("event=review_status_delete_failed", exc_info=True)
        if self.document_service is not None and hasattr(
            self.document_service, "update_review_attributes"
        ):
            try:
                self.document_service.update_review_attributes(
                    doc_key, {}, remove_attrs=TRACKING_REVIEW_ATTRIBUTES
                )
            except Exception:  # noqa: BLE001 — a missing tracking row is fine
                logger.warning("event=review_tracking_clear_failed", exc_info=True)

    def purge_document(self, doc_key: str) -> int:
        _, _, now_iso = self._now()
        return self.store.purge_document(doc_key, now_iso=now_iso)

    def repair_counter(self, reviewer_key: str) -> bool:
        now, _, now_iso = self._now()
        actual = self.store.count_owner(reviewer_key)
        idle = iso(now - timedelta(seconds=COUNTER_IDLE_SECONDS))
        return self.store.repair_counter(
            reviewer_key, actual, idle_before_iso=idle, now_iso=now_iso
        )


# ------------------------------------------------------------------ factory


def create_finalize_sender(document_service: Any = None) -> Optional[FinalizeSender]:
    queue_url = os.environ.get("QUEUE_URL", "")
    if not queue_url:
        return None
    import boto3

    if document_service is None:
        from idp_common.docs_service import create_document_service

        document_service = create_document_service(mode="dynamodb")
    return FinalizeSender(
        boto3.client("sqs"),
        queue_url,
        os.environ.get("WORKING_BUCKET"),
        document_service,
        input_bucket=os.environ.get("INPUT_BUCKET"),
        output_bucket=os.environ.get("OUTPUT_BUCKET"),
    )


def create_review_queue_service() -> Optional[ReviewQueueService]:
    """The service from the environment, or ``None`` when no table is configured."""
    store = create_review_store()
    if store is None:
        return None
    from idp_common.docs_service import create_document_service

    documents = create_document_service(mode="dynamodb")
    pool = os.environ.get("USER_POOL_ID", "")
    return ReviewQueueService(
        store,
        QueueDirectory(),
        finalize_sender=create_finalize_sender(documents),
        document_loader=documents.get_document,
        user_lookup=cognito_user_lookup(pool) if pool else None,
        output_bucket=os.environ.get("OUTPUT_BUCKET", ""),
        document_service=documents,
    )
