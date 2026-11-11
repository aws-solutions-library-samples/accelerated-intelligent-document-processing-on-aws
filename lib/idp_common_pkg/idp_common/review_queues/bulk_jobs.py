# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Bulk Skip All Reviews: the job store, the inline runner and the job worker step.

Rows live in ``HITLTable`` (see :mod:`.keys`): a job's ``META`` row is the single
source of truth for its progress (``NextPage``, ``Cursor``, the wake tokens), its
results are conditional puts keyed by document (so a retried page neither skips
nor counts a document twice), and a lock row lets one job run at a time per stack.

A request of at most ``hitl.bulk_skip_sync_limit`` documents runs inline
(:meth:`BulkSkipService.submit`) on its own clock, inside the API's time budget;
documents it could not start are reported ``NOT_ATTEMPTED``. Inline runs are
recorded as ``SYNC`` jobs so they are listed under Home › Jobs. Larger requests
become ``ASYNC`` jobs, advanced one step per worker invocation
(:meth:`BulkSkipService.run_job_step`), paced against the document queue.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence, Tuple

from . import keys
from .bulk_models import (
    ACTIVE_JOB_STATUSES,
    DEFER_SECONDS,
    DRY_RUN_BUDGET_SECONDS,
    FINAL_JOB_STATUSES,
    JOB_TTL_DAYS,
    LOCK_LEASE_SECONDS,
    MAX_JOB_DOCS,
    PAGE_SIZE,
    SKIP_OUTCOMES,
    STALL_SECONDS,
    SYNC_ADMISSION_SECONDS,
    WORKER_RESERVE_SECONDS,
    BulkJobPage,
    BulkJobResultsPage,
    BulkJobView,
    BulkMode,
    BulkOutcome,
    BulkSkipRequest,
    BulkSkipResult,
    DocumentOutcome,
    JobKind,
    JobStatus,
    JobType,
    OutcomeCount,
    ReviewCaller,
    outcome_counts,
    utc_iso,
)
from .exceptions import ReviewValidationError
from .review_skip import SkipDeps, classify, skip_document

logger = logging.getLogger(__name__)

#: Dry-run classification sample of a by-profile request.
DRY_RUN_SAMPLE = 200
_RECORD_TYPE = "BULK_JOB"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _condition_failed(exc: BaseException) -> bool:
    from idp_common.dynamodb.client import CONDITIONAL_CHECK_FAILED

    return getattr(exc, "error_code", None) == CONDITIONAL_CHECK_FAILED


def encode_token(last_key: Optional[dict]) -> Optional[str]:
    if not last_key:
        return None
    raw = json.dumps(last_key, default=str, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    try:
        value = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise ReviewValidationError("BAD_CURSOR", "the page token is invalid") from None
    if not isinstance(value, dict):
        raise ReviewValidationError("BAD_CURSOR", "the page token is invalid")
    return value


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------- store


class BulkJobStore:
    """Job, chunk, result, index and lock rows in ``HITLTable``."""

    def __init__(self, client: Any, clock: Callable[[], datetime] = _now):
        self.client = client  # DynamoDBClient on HITLTable
        self.clock = clock

    # ---- create / read

    def create(
        self, meta: dict[str, Any], chunks: Sequence[Sequence[str]] = ()
    ) -> bool:
        """Write a new job; ``False`` when a job with that id already exists."""
        try:
            self.client.put_item(meta, "attribute_not_exists(PK)")
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        expires = meta.get("ExpiresAfter")
        for seq, chunk in enumerate(chunks):
            self.client.put_item(
                {
                    "PK": meta["PK"],
                    "SK": keys.bulk_chunk_sk(seq),
                    "ObjectKeys": list(chunk),
                    **({"ExpiresAfter": expires} if expires else {}),
                }
            )
        sk = keys.bulk_index_sk(meta["CreatedAt"], meta["JobId"])
        for pk in (
            keys.bulk_user_index_pk(meta["RequesterKey"]),
            keys.BULK_ALL_INDEX_PK,
        ):
            self.client.put_item(
                {
                    "PK": pk,
                    "SK": sk,
                    "JobId": meta["JobId"],
                    "RequesterKey": meta["RequesterKey"],
                    **({"ExpiresAfter": expires} if expires else {}),
                }
            )
        return True

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        if not keys.BULK_JOB_ID_RE.match(job_id or ""):
            return None
        return self.client.get_item(keys.bulk_job_key(job_id), consistent_read=True)

    def chunk(self, job_id: str, seq: int) -> Optional[list[str]]:
        item = self.client.get_item(
            {"PK": keys.bulk_job_pk(job_id), "SK": keys.bulk_chunk_sk(seq)},
            consistent_read=True,
        )
        return None if item is None else [str(k) for k in item.get("ObjectKeys") or []]

    def has_result(self, job_id: str, object_key: str) -> bool:
        return (
            self.client.get_item(
                {"PK": keys.bulk_job_pk(job_id), "SK": keys.bulk_result_sk(object_key)},
                consistent_read=True,
                projection_expression="SK",
            )
            is not None
        )

    # ---- progress

    def update(
        self,
        job_id: str,
        sets: dict[str, Any],
        *,
        removes: Sequence[str] = (),
        condition: Optional[str] = None,
        values: Optional[dict[str, Any]] = None,
        names: Optional[dict[str, str]] = None,
    ) -> bool:
        """SET/REMOVE attributes of META; ``False`` when ``condition`` failed."""
        attr_names = dict(names or {})
        attr_values = dict(values or {})
        parts = []
        for i, (name, value) in enumerate(sets.items()):
            attr_names[f"#u{i}"] = name
            attr_values[f":u{i}"] = value
            parts.append(f"#u{i} = :u{i}")
        expression = "SET " + ", ".join(parts) if parts else ""
        if removes:
            for i, name in enumerate(removes):
                attr_names[f"#d{i}"] = name
            expression += " REMOVE " + ", ".join(f"#d{i}" for i in range(len(removes)))
        try:
            self.client.update_item(
                keys.bulk_job_key(job_id),
                expression.strip(),
                expression_attribute_names=attr_names or None,
                expression_attribute_values=attr_values or None,
                return_values="NONE",
                condition_expression=(
                    "attribute_exists(PK)"
                    + (f" AND ({condition})" if condition else "")
                ),
            )
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        return True

    def record_result(
        self,
        job_id: str,
        result: DocumentOutcome,
        page: int,
        *,
        expires_after: Optional[int] = None,
    ) -> bool:
        """Put one document's result (once) and count it; ``False`` if already there."""
        meta_pk = keys.bulk_job_pk(job_id)
        item = {
            "PK": meta_pk,
            "SK": keys.bulk_result_sk(result.object_key),
            "ObjectKey": result.object_key,
            "Outcome": result.outcome.value,
            "Kind": result.kind,
            "At": utc_iso(self.clock()),
            "Page": page,
        }
        if result.detail:
            item["Detail"] = result.detail
        if expires_after is None:
            meta = self.get(job_id)
            expires_after = (meta or {}).get("ExpiresAfter")
        if expires_after:
            item["ExpiresAfter"] = expires_after
        try:
            self.client.put_item(item, "attribute_not_exists(SK)")
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        self.client.update_item(
            keys.bulk_job_key(job_id),
            "ADD #p :one, #c.#o :one",
            expression_attribute_names={
                "#p": "Processed",
                "#c": "Counts",
                "#o": result.outcome.value,
            },
            expression_attribute_values={":one": 1},
            return_values="NONE",
        )
        return True

    def add_total(self, job_id: str, n: int) -> None:
        if n:
            self.client.update_item(
                keys.bulk_job_key(job_id),
                "ADD #t :n",
                expression_attribute_names={"#t": "Total"},
                expression_attribute_values={":n": n},
                return_values="NONE",
            )

    # ---- results and listings

    def results(
        self,
        job_id: str,
        *,
        outcome: Optional[str] = None,
        limit: int = 50,
        next_token: Optional[str] = None,
    ) -> Tuple[list[DocumentOutcome], Optional[str]]:
        names: dict[str, str] = {}
        values: dict[str, Any] = {":pk": keys.bulk_job_pk(job_id), ":r": "R#"}
        filter_expression = None
        if outcome:
            filter_expression = "#o = :o"
            names["#o"] = "Outcome"
            values[":o"] = outcome
        response = self.client.query(
            "PK = :pk AND begins_with(SK, :r)",
            names or None,
            values,
            max(1, min(limit, 500)),
            decode_token(next_token),
            filter_expression=filter_expression,
        )
        out = [
            DocumentOutcome(
                object_key=str(item["ObjectKey"]),
                outcome=BulkOutcome(item["Outcome"]),
                kind=item.get("Kind") or "none",
                detail=item.get("Detail"),
            )
            for item in response.get("Items") or []
        ]
        return out, encode_token(response.get("LastEvaluatedKey"))

    def list_for(
        self, caller: ReviewCaller, *, limit: int = 25, next_token: Optional[str] = None
    ) -> Tuple[list[dict[str, Any]], Optional[str]]:
        """The caller's jobs, newest first (an Admin's lists every job)."""
        pk = (
            keys.BULK_ALL_INDEX_PK
            if caller.is_admin
            else keys.bulk_user_index_pk(caller.reviewer_key)
        )
        response = self.client.query(
            "PK = :pk",
            None,
            {":pk": pk},
            max(1, min(limit, 100)),
            decode_token(next_token),
            scan_index_forward=False,
        )
        ids = [str(i["JobId"]) for i in response.get("Items") or [] if i.get("JobId")]
        found = {}
        if ids:
            for item in self.client.batch_get_items(
                [keys.bulk_job_key(j) for j in ids]
            ):
                found[str(item.get("JobId"))] = item
        return [found[j] for j in ids if j in found], encode_token(
            response.get("LastEvaluatedKey")
        )

    def delete(self, meta: dict[str, Any]) -> bool:
        """Delete a finished job's list entries and META; ``False`` when it is no
        longer finished (or already gone). Its chunk and result rows are unreachable
        without META and expire with it (``ExpiresAfter``), so a job of any size is
        deleted in a bounded number of writes."""
        job_id = str(meta["JobId"])
        finals = sorted(s.value for s in FINAL_JOB_STATUSES)
        values = {f":f{i}": v for i, v in enumerate(finals)}
        try:
            self.client.delete_item(
                keys.bulk_job_key(job_id),
                condition_expression="#s IN (" + ", ".join(values) + ")",
                expression_attribute_names={"#s": "Status"},
                expression_attribute_values=values,
            )
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        sk = keys.bulk_index_sk(str(meta["CreatedAt"]), job_id)
        for pk in (
            keys.bulk_user_index_pk(str(meta["RequesterKey"])),
            keys.BULK_ALL_INDEX_PK,
        ):
            self.client.delete_item({"PK": pk, "SK": sk})
        return True

    # ---- one-running-job lock

    def acquire_lock(self, job_id: str, now_ms: int) -> bool:
        try:
            self.client.put_item(
                {
                    **keys.BULK_LOCK_KEY,
                    "JobId": job_id,
                    "LeaseUntilMs": now_ms + LOCK_LEASE_SECONDS * 1000,
                },
                "attribute_not_exists(PK) OR JobId = :j OR LeaseUntilMs < :now",
                expression_attribute_values={":j": job_id, ":now": now_ms},
            )
        except Exception as e:
            if _condition_failed(e):
                return False
            raise
        return True

    def release_lock(self, job_id: str) -> None:
        try:
            self.client.delete_item(
                dict(keys.BULK_LOCK_KEY),
                condition_expression="JobId = :j",
                expression_attribute_values={":j": job_id},
            )
        except Exception as e:
            if not _condition_failed(e):
                logger.warning("event=bulk_lock_release_failed", exc_info=True)


def _while_active() -> dict[str, Any]:
    """``BulkJobStore.update`` arguments that apply only while the job is active."""
    active = sorted(s.value for s in ACTIVE_JOB_STATUSES)
    values = {f":a{i}": v for i, v in enumerate(active)}
    return {
        "condition": "#s IN (" + ", ".join(values) + ")",
        "names": {"#s": "Status"},
        "values": values,
    }


# ------------------------------------------------------------------- views


def _counts(meta: dict[str, Any]) -> list[OutcomeCount]:
    stored = meta.get("Counts") or {}
    return [
        OutcomeCount(outcome=o, count=_int(stored.get(o.value))) for o in SKIP_OUTCOMES
    ]


def job_view(meta: dict[str, Any], now: Optional[datetime] = None) -> BulkJobView:
    now = now or _now()
    status = JobStatus(meta.get("Status") or JobStatus.QUEUED.value)
    stalled = False
    if status not in FINAL_JOB_STATUSES:
        beat = meta.get("HeartbeatAt") or meta.get("CreatedAt")
        try:
            last = datetime.fromisoformat(str(beat).replace("Z", "+00:00"))
            stalled = (now - last).total_seconds() > STALL_SECONDS
        except (TypeError, ValueError):
            stalled = False
    return BulkJobView(
        job_id=str(meta["JobId"]),
        job_type=JobType(meta.get("JobType") or JobType.SKIP_ALL_REVIEWS.value),
        kind=JobKind(meta.get("Kind") or JobKind.ASYNC.value),
        status=status,
        mode=BulkMode(meta.get("Mode") or BulkMode.SELECTED.value),
        profile=meta.get("Profile"),
        submitted_from=meta.get("SubmittedFrom"),
        submitted_to=meta.get("SubmittedTo"),
        reason=str(meta.get("Reason") or ""),
        note=meta.get("Note"),
        resubmit=bool(meta.get("Resubmit", True)),
        requested_by=str(meta.get("RequesterEmail") or meta.get("RequesterKey") or ""),
        requester_key=str(meta.get("RequesterKey") or ""),
        total=_int(meta.get("Total")),
        processed=_int(meta.get("Processed")),
        counts=_counts(meta),
        limit_reached=bool(meta.get("LimitReached")),
        stalled=stalled,
        error=meta.get("Error"),
        created_at=str(meta.get("CreatedAt") or ""),
        started_at=meta.get("StartedAt"),
        finished_at=meta.get("FinishedAt"),
    )


# ----------------------------------------------------------------- service


class BulkSkipService:
    """Bulk Skip All Reviews: dry run, inline runs, async jobs and their steps."""

    def __init__(
        self,
        deps_factory: Callable[[], SkipDeps],
        job_store: BulkJobStore,
        selection: Any,
        *,
        tracking_client: Any,
        job_sender: Optional[Callable[[str, str, int], None]] = None,
        sync_limit: Optional[Callable[[], int]] = None,
        retention_days: int = 365,
        scope_resolver: Optional[Callable[[dict], Optional[list[str]]]] = None,
        queue_depth: Optional[Callable[[], int]] = None,
        max_concurrent_workflows: int = 100,
        notifier: Optional[Callable[[BulkJobView], None]] = None,
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
        max_workers: int = 8,
    ):
        self.deps_factory = deps_factory
        self.jobs = job_store
        self.selection = selection
        self.tracking_client = tracking_client
        self.job_sender = job_sender
        if sync_limit is None:
            from .bulk_settings import effective_sync_limit

            sync_limit = effective_sync_limit
        self.sync_limit = sync_limit
        self.retention_days = retention_days
        self.scope_resolver = scope_resolver
        self.queue_depth = queue_depth
        self.max_concurrent_workflows = max(1, int(max_concurrent_workflows))
        self.notifier = notifier
        self.clock = clock
        self.monotonic = monotonic
        self.max_workers = max_workers
        self._local = threading.local()

    # ------------------------------------------------------------ helpers

    def _deps(self) -> SkipDeps:
        deps = getattr(self._local, "deps", None)
        if deps is None:
            deps = self.deps_factory()
            self._local.deps = deps
        return deps

    @property
    def pacing_cap(self) -> int:
        return max(1, self.max_concurrent_workflows // 2)

    def _expires_after(self, now: datetime) -> int:
        days = max(1, min(JOB_TTL_DAYS, int(self.retention_days or JOB_TTL_DAYS)))
        return int((now + timedelta(days=days)).timestamp())

    def _new_meta(
        self,
        caller: ReviewCaller,
        request: BulkSkipRequest,
        job_id: str,
        *,
        kind: JobKind,
        status: JobStatus,
        total: int,
    ) -> dict[str, Any]:
        now = self.clock()
        meta: dict[str, Any] = {
            **keys.bulk_job_key(job_id),
            "RecordType": _RECORD_TYPE,
            "JobId": job_id,
            "JobType": JobType.SKIP_ALL_REVIEWS.value,
            "Kind": kind.value,
            "Status": status.value,
            "Mode": request.mode.value,
            "Reason": request.reason,
            "Resubmit": request.resubmit,
            "RequesterKey": caller.reviewer_key,
            "RequesterEmail": caller.email,
            "RequesterUsername": caller.username,
            "RequesterGroups": sorted(caller.groups),
            "Total": total,
            "Processed": 0,
            "Counts": {o.value: 0 for o in SKIP_OUTCOMES},
            "LimitReached": False,
            "NextPage": 0,
            "CreatedAt": utc_iso(now),
            "HeartbeatAt": utc_iso(now),
            "ExpiresAfter": self._expires_after(now),
            "Version": 1,
        }
        if request.note:
            meta["Note"] = request.note
        if request.profile:
            meta["Profile"] = request.profile
        if request.submitted_from and request.submitted_to:
            meta["SubmittedFrom"] = utc_iso(request.submitted_from)
            meta["SubmittedTo"] = utc_iso(request.submitted_to)
        if caller.allowed_versions is not None and not caller.is_admin:
            meta["ScopeSnapshot"] = list(caller.allowed_versions)
        return meta

    def _classify_keys(
        self, caller: ReviewCaller, object_keys: Sequence[str]
    ) -> list[Optional[BulkOutcome]]:
        """The dry-run outcome of each key (``None`` = would be skipped)."""
        from .legacy_skip import tracking_key
        from .models import ReviewItemRecord

        deps = self._deps()
        rows = {
            str(r.get("PK", ""))[4:]: r
            for r in self.tracking_client.batch_get_items(
                [tracking_key(k) for k in object_keys], consistent_read=True
            )
        }
        items = {}
        for raw in deps.store.client.batch_get_items(
            [keys.item_key(k) for k in object_keys if k in rows], consistent_read=True
        ):
            record = ReviewItemRecord.from_item(raw)
            items[record.doc_key] = record
        return [classify(rows.get(k), items.get(k), caller)[0] for k in object_keys]

    # ---------------------------------------------------------------- API

    def submit(self, caller: ReviewCaller, request: BulkSkipRequest) -> BulkSkipResult:
        """Dry run, inline run or async job, by the request and the sync limit."""
        limit = self.sync_limit()
        if request.mode is BulkMode.PROFILE and not caller.is_admin:
            from idp_common.document_scope import assert_profile_in_scope

            assert_profile_in_scope(
                caller.allowed_versions, request.profile, what="profile"
            )
        if request.dry_run:
            return self.dry_run(caller, request, limit)
        job_id = keys.new_bulk_job_id(caller.reviewer_key, request.client_request_id)
        existing = self.jobs.get(job_id) if request.client_request_id else None
        if existing is not None:
            return self._result_of(existing, limit)
        if request.mode is BulkMode.SELECTED:
            if len(request.object_keys) <= limit:
                return self.run_sync(
                    caller, request, request.object_keys, job_id, limit
                )
            return self.create_job(caller, request, job_id, limit)
        assert request.profile and request.submitted_from and request.submitted_to
        found, approximate = self.selection.collect(
            request.profile,
            request.submitted_from,
            request.submitted_to,
            max_docs=limit,
            budget_seconds=DRY_RUN_BUDGET_SECONDS,
            monotonic=self.monotonic,
        )
        if not approximate:
            return self.run_sync(caller, request, found, job_id, limit)
        return self.create_job(caller, request, job_id, limit)

    def dry_run(
        self, caller: ReviewCaller, request: BulkSkipRequest, limit: int
    ) -> BulkSkipResult:
        """Counts per outcome and whether it would run inline; writes nothing."""
        if request.mode is BulkMode.SELECTED:
            outcomes = self._classify_keys(caller, request.object_keys)
            eligible = sum(1 for o in outcomes if o is None)
            sample = [
                DocumentOutcome(object_key=k, outcome=o or BulkOutcome.SKIPPED)
                for k, o in zip(request.object_keys, outcomes)
            ]
            return BulkSkipResult(
                dry_run=True,
                run_mode="SYNC" if len(request.object_keys) <= limit else "ASYNC",
                sync_limit=limit,
                eligible_count=eligible,
                counts=outcome_counts(sample, SKIP_OUTCOMES),
            )
        assert request.profile and request.submitted_from and request.submitted_to
        found, approximate = self.selection.collect(
            request.profile,
            request.submitted_from,
            request.submitted_to,
            max_docs=MAX_JOB_DOCS,
            budget_seconds=DRY_RUN_BUDGET_SECONDS,
            monotonic=self.monotonic,
        )
        sampled = found[:DRY_RUN_SAMPLE]
        outcomes = self._classify_keys(caller, sampled) if sampled else []
        sample = [
            DocumentOutcome(object_key=k, outcome=o or BulkOutcome.SKIPPED)
            for k, o in zip(sampled, outcomes)
        ]
        exact = not approximate and len(found) <= DRY_RUN_SAMPLE
        eligible = sum(1 for o in outcomes if o is None) if exact else len(found)
        return BulkSkipResult(
            dry_run=True,
            run_mode="SYNC" if exact and len(found) <= limit else "ASYNC",
            sync_limit=limit,
            approximate=not exact,
            eligible_count=eligible,
            limit_reached=len(found) >= MAX_JOB_DOCS,
            counts=outcome_counts(sample, SKIP_OUTCOMES),
        )

    def run_sync(
        self,
        caller: ReviewCaller,
        request: BulkSkipRequest,
        object_keys: Sequence[str],
        job_id: str,
        limit: int,
    ) -> BulkSkipResult:
        """Skip ``object_keys`` inline on 8 threads; documents not started within
        the admission budget are ``NOT_ATTEMPTED``. Recorded as a SYNC job."""
        deadline = self.monotonic() + SYNC_ADMISSION_SECONDS

        def work(key: str) -> DocumentOutcome:
            if self.monotonic() >= deadline:
                return DocumentOutcome(
                    object_key=key, outcome=BulkOutcome.NOT_ATTEMPTED
                )
            return skip_document(
                self._deps(),
                caller,
                key,
                reason=request.reason,
                note=request.note,
                resubmit=request.resubmit,
                job_id=job_id,
            )

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            results = list(pool.map(work, object_keys))
        self._record_sync(caller, request, job_id, results)
        return BulkSkipResult(
            dry_run=False,
            run_mode="SYNC",
            sync_limit=limit,
            eligible_count=sum(1 for r in results if r.outcome is BulkOutcome.SKIPPED),
            counts=outcome_counts(results, SKIP_OUTCOMES),
            results=results,
            job_id=job_id,
        )

    def _record_sync(
        self,
        caller: ReviewCaller,
        request: BulkSkipRequest,
        job_id: str,
        results: list[DocumentOutcome],
    ) -> None:
        """Best effort: the run is done whatever happens to its record."""
        try:
            meta = self._new_meta(
                caller,
                request,
                job_id,
                kind=JobKind.SYNC,
                status=JobStatus.SUCCEEDED,
                total=len(results),
            )
            meta["StartedAt"] = meta["CreatedAt"]
            meta["FinishedAt"] = utc_iso(self.clock())
            if not self.jobs.create(meta):
                return
            for result in results:
                self.jobs.record_result(
                    job_id, result, 0, expires_after=meta["ExpiresAfter"]
                )
        except Exception:  # noqa: BLE001 — logged; the documents are already skipped
            logger.warning("event=bulk_sync_record_failed", exc_info=True)

    def _result_of(self, meta: dict[str, Any], limit: int) -> BulkSkipResult:
        view = job_view(meta, self.clock())
        results, _ = self.jobs.results(view.job_id, limit=500)
        return BulkSkipResult(
            dry_run=False,
            run_mode="SYNC" if view.kind is JobKind.SYNC else "ASYNC",
            sync_limit=limit,
            eligible_count=view.total,
            limit_reached=view.limit_reached,
            counts=view.counts,
            results=results if view.kind is JobKind.SYNC else [],
            job_id=view.job_id,
        )

    def create_job(
        self, caller: ReviewCaller, request: BulkSkipRequest, job_id: str, limit: int
    ) -> BulkSkipResult:
        """Create an ASYNC job and send its first wake-up."""
        if self.job_sender is None:
            raise ReviewValidationError(
                "BAD_ARGUMENT", "background jobs are not configured"
            )
        chunks = [
            request.object_keys[i : i + PAGE_SIZE]
            for i in range(0, len(request.object_keys), PAGE_SIZE)
        ]
        meta = self._new_meta(
            caller,
            request,
            job_id,
            kind=JobKind.ASYNC,
            status=JobStatus.QUEUED,
            total=len(request.object_keys),
        )
        wake = secrets.token_hex(16)
        meta["WakeToken"] = wake
        meta["Chunks"] = len(chunks)
        if not self.jobs.create(meta, chunks):
            return self._result_of(self.jobs.get(job_id) or meta, limit)
        self.job_sender(job_id, wake, 0)
        logger.info("event=bulk_job_created mode=%s", request.mode.value)
        return BulkSkipResult(
            dry_run=False,
            run_mode="ASYNC",
            sync_limit=limit,
            eligible_count=len(request.object_keys),
            counts=outcome_counts([], SKIP_OUTCOMES),
            job_id=job_id,
        )

    def get_job(
        self,
        caller: ReviewCaller,
        job_id: str,
        *,
        outcome: Optional[str] = None,
        limit: int = 100,
        next_token: Optional[str] = None,
    ) -> Optional[BulkJobResultsPage]:
        """The job and a page of results; ``None`` unless it is the caller's own
        (or the caller is an Admin), so another user's job reads as not found."""
        meta = self._owned(caller, job_id, "read")
        if meta is None:
            return None
        if outcome is not None:
            BulkOutcome(outcome)  # validates
        results, token = self.jobs.results(
            job_id, outcome=outcome, limit=limit, next_token=next_token
        )
        return BulkJobResultsPage(
            job=job_view(meta, self.clock()), results=results, next_token=token
        )

    def _owned(
        self, caller: ReviewCaller, job_id: str, action: str
    ) -> Optional[dict[str, Any]]:
        """The job's META when the caller requested it (or is an Admin); ``None``
        otherwise, so another user's job reads as not found."""
        meta = self.jobs.get(job_id)
        if meta is None:
            return None
        if not caller.is_admin and meta.get("RequesterKey") != caller.reviewer_key:
            logger.warning("Denied bulk job %s: not the requester", action)
            return None
        return meta

    def cancel_job(self, caller: ReviewCaller, job_id: str) -> Optional[BulkJobView]:
        """Cancel a queued or running job; ``None`` when it isn't the caller's.

        The job ends ``CANCELLED`` at once. The worker sees it before its next page
        and stops, so at most the page in progress (``PAGE_SIZE`` documents) is
        still skipped; documents already skipped stay skipped. The requester is
        told, as for any finished job.
        """
        meta = self._owned(caller, job_id, "cancel")
        if meta is None:
            return None
        active = sorted(s.value for s in ACTIVE_JOB_STATUSES)
        values = {f":a{i}": v for i, v in enumerate(active)}
        cancelled = self.jobs.update(
            job_id,
            {
                "Status": JobStatus.CANCELLED.value,
                "FinishedAt": utc_iso(self.clock()),
                "CancelledBy": caller.email or caller.reviewer_key,
            },
            condition="#s IN (" + ", ".join(values) + ")",
            names={"#s": "Status"},
            values=values,
        )
        if not cancelled:
            raise ReviewValidationError("JOB_FINISHED", "the job has already finished")
        logger.info("event=bulk_job_cancelled")
        self._notify(job_id)
        meta = self.jobs.get(job_id) or meta
        return job_view(meta, self.clock())

    def delete_job(self, caller: ReviewCaller, job_id: str) -> bool:
        """Delete a finished job; ``False`` when it isn't the caller's (not found)."""
        meta = self._owned(caller, job_id, "delete")
        if meta is None:
            return False
        if meta.get("Status") not in {s.value for s in FINAL_JOB_STATUSES}:
            raise ReviewValidationError(
                "JOB_ACTIVE", "cancel the job, or wait for it to finish, first"
            )
        if not self.jobs.delete(meta):
            if self.jobs.get(job_id) is None:
                return False
            raise ReviewValidationError(
                "JOB_ACTIVE", "cancel the job, or wait for it to finish, first"
            )
        logger.info("event=bulk_job_deleted")
        return True

    def list_jobs(
        self, caller: ReviewCaller, *, limit: int = 25, next_token: Optional[str] = None
    ) -> BulkJobPage:
        metas, token = self.jobs.list_for(caller, limit=limit, next_token=next_token)
        now = self.clock()
        return BulkJobPage(jobs=[job_view(m, now) for m in metas], next_token=token)

    # ------------------------------------------------------------ worker

    def schedule(self, job_id: str, delay_seconds: int) -> None:
        """Hand the job to its next wake-up: record the new token, send it, then
        promote it (a lost send is recovered by the predecessor's redelivery)."""
        if self.job_sender is None:
            raise RuntimeError("no job sender configured")
        token = secrets.token_hex(16)
        self.jobs.update(job_id, {"NextWakeToken": token})
        self.job_sender(job_id, token, max(0, min(900, int(delay_seconds))))
        self.jobs.update(job_id, {"WakeToken": token}, removes=("NextWakeToken",))

    def fail_job(self, job_id: str, error: str) -> None:
        """Mark a job FAILED (unless already final), release its lock, notify."""
        meta = self.jobs.get(job_id)
        if meta is None:
            return
        if meta.get("Status") not in {s.value for s in FINAL_JOB_STATUSES}:
            if self.jobs.update(
                job_id,
                {
                    "Status": JobStatus.FAILED.value,
                    "Error": error,
                    "FinishedAt": utc_iso(self.clock()),
                },
                **_while_active(),
            ):
                logger.warning("event=bulk_job_failed error=%s", error)
                self._notify(job_id)
        self.jobs.release_lock(job_id)

    def _finish(self, job_id: str, *, limit_reached: bool = False) -> None:
        # Conditional, so a job cancelled during its last page stays CANCELLED.
        finished = self.jobs.update(
            job_id,
            {
                "Status": JobStatus.SUCCEEDED.value,
                "FinishedAt": utc_iso(self.clock()),
                "LimitReached": limit_reached,
            },
            removes=("Cursor",),
            **_while_active(),
        )
        self.jobs.release_lock(job_id)
        if finished:
            logger.info("event=bulk_job_finished")
            self._notify(job_id)

    def _notify(self, job_id: str) -> None:
        if self.notifier is None:
            return
        meta = self.jobs.get(job_id)
        if meta is None:
            return
        try:
            self.notifier(job_view(meta, self.clock()))
        except Exception:  # noqa: BLE001 — a notice must not fail the job
            logger.warning("event=bulk_job_notify_failed", exc_info=True)

    def _caller_for(self, meta: dict[str, Any]) -> Optional[ReviewCaller]:
        """The requester, with the scope snapshot intersected with today's scope;
        ``None`` when the job's profile left the scope."""
        groups = frozenset(meta.get("RequesterGroups") or [])
        is_admin = "Admin" in groups
        allowed = meta.get("ScopeSnapshot")
        if not is_admin and self.scope_resolver is not None:
            current = self.scope_resolver(meta)
            if current is not None:
                if allowed is None:
                    allowed = list(current)
                else:
                    from idp_common.config_scope import scope_allows

                    allowed = [a for a in allowed if scope_allows(current, a)] or [
                        "\x00none"
                    ]
        caller = ReviewCaller(
            reviewer_key=str(meta.get("RequesterKey") or ""),
            username=str(meta.get("RequesterUsername") or ""),
            email=str(meta.get("RequesterEmail") or ""),
            groups=groups,
            is_admin=is_admin,
            allowed_versions=None if is_admin else allowed,
        )
        if meta.get("Mode") == BulkMode.PROFILE.value and not is_admin:
            from idp_common.config_scope import scope_allows

            if allowed is not None and not scope_allows(allowed, meta.get("Profile")):
                return None
        return caller

    def run_job_step(
        self, job_id: str, wake_token: str, remaining_seconds: Callable[[], float]
    ) -> Optional[int]:
        """Advance a job. Returns the delay of its next wake-up (``0`` = continue
        now), or ``None`` when there is nothing more to do (finished, failed, or a
        stale wake-up)."""
        meta = self.jobs.get(job_id)
        if meta is None:
            return None
        if meta.get("Status") in {s.value for s in FINAL_JOB_STATUSES}:
            if meta.get("Status") == JobStatus.CANCELLED.value:
                # Cancelled between wake-ups: the lock this job held is freed now
                # rather than at the end of its lease (release is per job id).
                self.jobs.release_lock(job_id)
            return None
        if wake_token not in {meta.get("WakeToken"), meta.get("NextWakeToken")}:
            logger.info("event=bulk_job_stale_wakeup")
            return None
        now = self.clock()
        if not self.jobs.acquire_lock(job_id, int(now.timestamp() * 1000)):
            return DEFER_SECONDS
        if meta.get("Status") == JobStatus.QUEUED.value:
            # Conditional, so a job cancelled while queued is not restarted.
            if not self.jobs.update(
                job_id,
                {"Status": JobStatus.RUNNING.value, "StartedAt": utc_iso(now)},
                condition="#s = :q",
                names={"#s": "Status"},
                values={":q": JobStatus.QUEUED.value},
            ):
                self.jobs.release_lock(job_id)
                return None
        caller = self._caller_for(meta)
        if caller is None:
            self.fail_job(job_id, "scope_revoked")
            return None
        deps = self._deps()
        resubmits = 0
        while remaining_seconds() > WORKER_RESERVE_SECONDS:
            if self.queue_depth is not None and self.queue_depth() >= self.pacing_cap:
                self._heartbeat(job_id)
                return DEFER_SECONDS
            meta = self.jobs.get(job_id) or meta
            if meta.get("Status") == JobStatus.CANCELLED.value:
                logger.info("event=bulk_job_stopped_cancelled")
                self.jobs.release_lock(job_id)
                return None
            page = _int(meta.get("NextPage"))
            done = False
            limit_reached = False
            cursor_after: Optional[dict] = None
            if meta.get("Mode") == BulkMode.SELECTED.value:
                if page >= _int(meta.get("Chunks")):
                    self._finish(job_id)
                    return None
                page_keys = self.jobs.chunk(job_id, page)
                if page_keys is None:
                    self.fail_job(job_id, "chunk_missing")
                    return None
                done = page + 1 >= _int(meta.get("Chunks"))
            else:
                cursor = json.loads(meta["Cursor"]) if meta.get("Cursor") else None
                if page > 0 and cursor is None:
                    self._finish(job_id, limit_reached=bool(meta.get("LimitReached")))
                    return None
                start = datetime.fromisoformat(
                    str(meta["SubmittedFrom"]).replace("Z", "+00:00")
                )
                end = datetime.fromisoformat(
                    str(meta["SubmittedTo"]).replace("Z", "+00:00")
                )
                page_keys, cursor_after = next(
                    iter(
                        self.selection.pages(
                            str(meta["Profile"]),
                            start,
                            end,
                            cursor=cursor,
                            page_size=PAGE_SIZE,
                        )
                    ),
                    ([], None),
                )
                seen = _int(meta.get("Total"))
                if seen + len(page_keys) >= MAX_JOB_DOCS:
                    page_keys = page_keys[: max(0, MAX_JOB_DOCS - seen)]
                    limit_reached = cursor_after is not None or (
                        seen + len(page_keys) >= MAX_JOB_DOCS
                    )
                    done = True
                done = done or cursor_after is None
                self.jobs.add_total(job_id, len(page_keys))
            for key in page_keys:
                if self.jobs.has_result(job_id, key):
                    continue
                result = skip_document(
                    deps,
                    caller,
                    key,
                    reason=str(meta.get("Reason") or ""),
                    note=meta.get("Note"),
                    resubmit=bool(meta.get("Resubmit", True)),
                    job_id=job_id,
                )
                self.jobs.record_result(
                    job_id, result, page, expires_after=meta.get("ExpiresAfter")
                )
                if (
                    result.kind == "legacy"
                    and result.outcome is BulkOutcome.SKIPPED
                    and result.detail != "resubmit_skipped"
                ) or (result.kind == "queue" and result.outcome is BulkOutcome.SKIPPED):
                    resubmits += 1
            advance = {"NextPage": page + 1, "HeartbeatAt": utc_iso(self.clock())}
            removes: tuple[str, ...] = ()
            if meta.get("Mode") == BulkMode.PROFILE.value:
                if cursor_after is not None and not done:
                    advance["Cursor"] = json.dumps(cursor_after, default=str)
                else:
                    removes = ("Cursor",)
                if limit_reached:
                    advance["LimitReached"] = True
            if not self.jobs.update(
                job_id,
                advance,
                removes=removes,
                condition="#np = :p",
                names={"#np": "NextPage"},
                values={":p": page},
            ):
                logger.info("event=bulk_job_page_raced")
                return None
            self.jobs.acquire_lock(job_id, int(self.clock().timestamp() * 1000))
            if done:
                self._finish(job_id, limit_reached=limit_reached)
                return None
            if resubmits >= self.pacing_cap:
                return DEFER_SECONDS
        return 0

    def _heartbeat(self, job_id: str) -> None:
        self.jobs.update(job_id, {"HeartbeatAt": utc_iso(self.clock())})
