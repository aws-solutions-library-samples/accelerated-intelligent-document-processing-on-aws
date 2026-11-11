# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Contracts of bulk Skip All Reviews and Send to review.

The per-document outcome vocabulary (:class:`BulkOutcome`) is the canonical list;
the web UI mirrors it in ``src/ui/src/components/review-bulk/bulk-outcomes.ts``
(a parity test reads this file).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .bulk_settings import DEFAULT_SYNC_LIMIT as DEFAULT_SYNC_LIMIT  # re-export
from .bulk_settings import MAX_SYNC_LIMIT as MAX_SYNC_LIMIT  # re-export
from .models import CallerContext
from .statuses import SEND_TO_REVIEW_REASONS, SKIP_ALL_REASONS

#: Most object keys one selected-mode request may carry.
MAX_SELECTED_KEYS = 1000
#: Most candidates one by-profile job processes; beyond it ``LimitReached`` is set.
MAX_JOB_DOCS = 10_000
#: Longest by-profile date range.
MAX_RANGE_DAYS = 365
#: Documents per job page (and per selected-mode key chunk).
PAGE_SIZE = 25
#: Days a job record and its results are kept (capped at the data retention).
JOB_TTL_DAYS = 30
#: Inline path: stop starting documents after this many seconds.
SYNC_ADMISSION_SECONDS = 11.0
#: Dry run budget.
DRY_RUN_BUDGET_SECONDS = 12.0
#: A queued or running job with no heartbeat for this long reads as stalled.
STALL_SECONDS = 1800
#: The one-running-job lock lease.
LOCK_LEASE_SECONDS = 1200
#: Delay of a job step deferred by the lock or by pacing.
DEFER_SECONDS = 60
#: A worker invocation stops taking pages with less than this left.
WORKER_RESERVE_SECONDS = 120
#: Longest note on a skip or a send.
MAX_NOTE = 1000

_CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class BulkOutcome(str, Enum):
    """What happened to one document."""

    SKIPPED = "SKIPPED"
    SENT = "SENT"
    NOT_PERMITTED = "NOT_PERMITTED"
    NOT_PENDING = "NOT_PENDING"
    ALREADY_FINISHED = "ALREADY_FINISHED"
    HELD_BY_OTHER = "HELD_BY_OTHER"
    TEST_STUDIO_EXCLUDED = "TEST_STUDIO_EXCLUDED"
    NOT_FINISHED_PROCESSING = "NOT_FINISHED_PROCESSING"
    ALREADY_IN_REVIEW = "ALREADY_IN_REVIEW"
    REVIEW_QUEUE_OFF = "REVIEW_QUEUE_OFF"
    RESUBMIT_FAILED = "RESUBMIT_FAILED"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


#: Outcomes a bulk skip reports (counts list every one, zeros included).
SKIP_OUTCOMES = tuple(
    o
    for o in BulkOutcome
    if o
    not in (
        BulkOutcome.SENT,
        BulkOutcome.ALREADY_IN_REVIEW,
        BulkOutcome.REVIEW_QUEUE_OFF,
    )
)
#: Outcomes Send to review reports.
SEND_OUTCOMES = (
    BulkOutcome.SENT,
    BulkOutcome.NOT_PERMITTED,
    BulkOutcome.ALREADY_IN_REVIEW,
    BulkOutcome.NOT_FINISHED_PROCESSING,
    BulkOutcome.TEST_STUDIO_EXCLUDED,
    BulkOutcome.REVIEW_QUEUE_OFF,
    BulkOutcome.FAILED,
)


class BulkMode(str, Enum):
    SELECTED = "SELECTED"
    PROFILE = "PROFILE"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    #: Stopped at the requester's or an Admin's request, before its next page.
    CANCELLED = "CANCELLED"


class JobKind(str, Enum):
    SYNC = "SYNC"
    ASYNC = "ASYNC"


class JobType(str, Enum):
    SKIP_ALL_REVIEWS = "SKIP_ALL_REVIEWS"


FINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)
#: A job that is still waiting or working, and so can be cancelled.
ACTIVE_JOB_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


class ReviewCaller(BaseModel):
    """The verified caller of a bulk operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_key: str
    username: str = ""
    email: str = ""
    groups: frozenset[str] = frozenset()
    is_admin: bool = False
    #: ``None`` = unscoped; otherwise the allowed profile patterns.
    allowed_versions: Optional[list[str]] = None
    #: The verified Cognito ``sub`` (empty when the transport verifies none). A
    #: background job re-checks the requester's scope with it, so a grant is bound
    #: to the account that made the request, not to another one sharing its email.
    sub: str = ""

    def to_context(self) -> CallerContext:
        return CallerContext(
            reviewer_key=self.reviewer_key,
            email=self.email,
            groups=self.groups,
            is_admin=self.is_admin,
            allowed_versions=self.allowed_versions,
        )


def _clean_note(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("note must be a string")
    value = value.strip()
    if len(value) > MAX_NOTE:
        raise ValueError(f"note must be at most {MAX_NOTE} characters")
    return value or None


def _clean_keys(value: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for key in value:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("object keys must be non-empty strings")
        seen.setdefault(key, None)
    return list(seen)


class BulkSkipRequest(BaseModel):
    """A bulk Skip All Reviews request (``bulkSkipAllReviews``)."""

    model_config = ConfigDict(extra="forbid")

    mode: BulkMode
    object_keys: list[str] = Field(default_factory=list)
    profile: Optional[str] = None
    submitted_from: Optional[datetime] = None
    submitted_to: Optional[datetime] = None
    reason: str
    note: Optional[str] = None
    resubmit: bool = True
    dry_run: bool = False
    client_request_id: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if value not in SKIP_ALL_REASONS:
            raise ValueError("reason must be one of: " + ", ".join(SKIP_ALL_REASONS))
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value):
        return _clean_note(value)

    @field_validator("object_keys")
    @classmethod
    def _keys(cls, value: list[str]) -> list[str]:
        return _clean_keys(value)

    @field_validator("client_request_id")
    @classmethod
    def _crid(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _CLIENT_REQUEST_ID.match(value):
            raise ValueError("clientRequestId must be 1-64 letters, digits or '-'")
        return value

    @model_validator(mode="after")
    def _mode(self) -> "BulkSkipRequest":
        if self.mode is BulkMode.SELECTED:
            if not self.object_keys:
                raise ValueError("objectKeys is required in SELECTED mode")
            if len(self.object_keys) > MAX_SELECTED_KEYS:
                raise ValueError(f"at most {MAX_SELECTED_KEYS} objectKeys per request")
            if self.profile or self.submitted_from or self.submitted_to:
                raise ValueError("profile and dates apply to PROFILE mode only")
            return self
        if self.object_keys:
            raise ValueError("objectKeys apply to SELECTED mode only")
        if not (self.profile and self.profile.strip()):
            raise ValueError("profile is required in PROFILE mode")
        start, end = self.submitted_from, self.submitted_to
        if start is None or end is None:
            raise ValueError("submittedFrom and submittedTo are required")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("dates must carry a time zone (UTC)")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if start >= end:
            raise ValueError("submittedFrom must be before submittedTo")
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            raise ValueError(f"the date range is at most {MAX_RANGE_DAYS} days")
        self.submitted_from, self.submitted_to = start, end
        return self


class SendToReviewRequest(BaseModel):
    """A Send to review request (``sendToReview``)."""

    model_config = ConfigDict(extra="forbid")

    object_keys: list[str]
    reason: str
    note: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        if value not in SEND_TO_REVIEW_REASONS:
            raise ValueError(
                "reason must be one of: " + ", ".join(SEND_TO_REVIEW_REASONS)
            )
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value):
        return _clean_note(value)

    @field_validator("object_keys")
    @classmethod
    def _keys(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("objectKeys is required")
        return _clean_keys(value)


class DocumentOutcome(BaseModel):
    object_key: str
    outcome: BulkOutcome
    kind: Literal["queue", "legacy", "none"] = "none"
    #: e.g. ``finalize_retry_pending``, ``resubmit_skipped``.
    detail: Optional[str] = None


class OutcomeCount(BaseModel):
    outcome: BulkOutcome
    count: int


def outcome_counts(
    results: list[DocumentOutcome], outcomes: tuple[BulkOutcome, ...]
) -> list[OutcomeCount]:
    """One count per outcome in ``outcomes`` (zeros included), in enum order."""
    tally = {o: 0 for o in outcomes}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1
    return [OutcomeCount(outcome=o, count=n) for o, n in tally.items()]


class BulkSkipResult(BaseModel):
    dry_run: bool
    run_mode: Literal["SYNC", "ASYNC"]
    sync_limit: int
    approximate: bool = False
    eligible_count: int = 0
    limit_reached: bool = False
    counts: list[OutcomeCount] = Field(default_factory=list)
    results: list[DocumentOutcome] = Field(default_factory=list)
    job_id: Optional[str] = None


class SendToReviewResult(BaseModel):
    counts: list[OutcomeCount]
    results: list[DocumentOutcome]


class BulkJobView(BaseModel):
    job_id: str
    job_type: JobType = JobType.SKIP_ALL_REVIEWS
    kind: JobKind
    status: JobStatus
    mode: BulkMode
    profile: Optional[str] = None
    submitted_from: Optional[str] = None
    submitted_to: Optional[str] = None
    reason: str
    note: Optional[str] = None
    resubmit: bool = True
    requested_by: str = ""
    requester_key: str = ""
    total: int = 0
    processed: int = 0
    counts: list[OutcomeCount] = Field(default_factory=list)
    limit_reached: bool = False
    stalled: bool = False
    error: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class BulkJobResultsPage(BaseModel):
    job: BulkJobView
    results: list[DocumentOutcome]
    next_token: Optional[str] = None


class BulkJobPage(BaseModel):
    jobs: list[BulkJobView]
    next_token: Optional[str] = None


def utc_iso(value: datetime) -> str:
    """ISO-8601 UTC with a ``Z`` suffix and millisecond precision."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
