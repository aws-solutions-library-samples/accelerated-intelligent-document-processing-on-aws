# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Internal records of ``idp_common.review_queues``.

``ReviewItemRecord`` mirrors the stored queue item: its fields are the snake_case
form of the DynamoDB attribute names (``LeaseExpiresAtMs`` ↔
``lease_expires_at_ms``). GSI key attributes are not modelled; the store derives
them from the state. API shapes live in :mod:`.api_models`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from .keys import queue_partition


def _pascal(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in name.split("_"))


def from_dynamo(value: Any) -> Any:
    """``Decimal`` → ``int``/``float``, recursively; sets become sorted lists."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [from_dynamo(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(from_dynamo(v) for v in value)
    return value


def to_dynamo(value: Any) -> Any:
    """``float`` → ``Decimal``, recursively (the resource API rejects floats)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    return value


class CallerContext(BaseModel):
    """Who is calling, from verified claims only."""

    model_config = ConfigDict(frozen=True)

    reviewer_key: str
    email: str
    groups: frozenset[str] = frozenset()
    is_admin: bool = False
    #: ``None`` = unscoped; a list = the profiles the caller may see.
    allowed_versions: Optional[list[str]] = None


class QueueRef(BaseModel):
    """A system-wide queue. ``profile`` is the item's profile where one is known;
    it is display context only and never part of the queue's key."""

    model_config = ConfigDict(frozen=True)

    queue: str
    profile: str = ""

    @property
    def partition(self) -> str:
        return queue_partition(self.queue)

    @classmethod
    def from_partition(cls, partition: str) -> "QueueRef":
        prefix, _, queue = partition.partition("#")
        if prefix != "Q" or not queue:
            raise ValueError(f"not a queue partition: {partition!r}")
        return cls(queue=queue)


class ReviewReason(BaseModel):
    kind: Literal["low_confidence", "blueprint_confidence"]
    section_id: str
    field: Optional[str] = None
    label: Optional[str] = None
    score: float
    threshold: float


class UserInfo(BaseModel):
    email: str
    exists: bool
    groups: frozenset[str] = frozenset()


class _Record(BaseModel):
    model_config = ConfigDict(
        alias_generator=_pascal, populate_by_name=True, extra="ignore"
    )

    @classmethod
    def from_item(cls, item: Mapping[str, Any]):
        return cls.model_validate(from_dynamo(dict(item)))

    def to_attributes(self) -> dict[str, Any]:
        """Stored attributes (PascalCase), ``None`` omitted, floats as Decimal."""
        return to_dynamo(self.model_dump(by_alias=True, exclude_none=True))


class ReviewItemRecord(_Record):
    """The queue item of a document's current review cycle."""

    # Identity and placement
    doc_key: str
    cycle_id: str
    profile: str
    config_revision: Optional[int] = None
    queue: str
    state: str
    version: int = 0
    priority: int = 50
    enqueued_at: str
    enqueued_at_ms: int
    holding_at: Optional[str] = None
    activated_at: Optional[str] = None
    # Reasons
    reasons: list[ReviewReason] = Field(default_factory=list)
    reason_count: int = 0
    sections: list[str] = Field(default_factory=list)
    announce_groups: list[str] = Field(default_factory=list)
    announce_emails: list[str] = Field(default_factory=list)
    # Phase 1
    phase1_execution_arn: Optional[str] = None
    phase1_run_id: Optional[str] = None
    machine_manifest_uri: Optional[str] = None
    phase1_metering_json: Optional[str] = None
    # Claim and lease
    owner_key: Optional[str] = None
    owner_email: Optional[str] = None
    claimed_at: Optional[str] = None
    claimed_at_ms: Optional[int] = None
    lease_expires_at_ms: Optional[int] = None
    lease_minutes: Optional[int] = None
    lease_warned_for_ms: Optional[int] = None
    #: When the sweeper next looks at the item (drives ``SweepSK``).
    sweep_at_ms: Optional[int] = None
    # Draft and escalation
    has_draft: bool = False
    draft_updated_at: Optional[str] = None
    draft_updated_by: Optional[str] = None
    escalated: bool = False
    escalated_at: Optional[str] = None
    escalated_by: Optional[str] = None
    escalation_note: Optional[str] = None
    escalated_from_queue: Optional[str] = None
    # Disposition
    disposition: Optional[str] = None
    disposed_by: Optional[str] = None
    disposed_at: Optional[str] = None
    checklist: Optional[list[bool]] = None
    checklist_template: Optional[list[str]] = None
    complete_note: Optional[str] = None
    reject_reason: Optional[str] = None
    reject_note: Optional[str] = None
    # Finalize
    finalize_token: Optional[str] = None
    finalize_execution_name: Optional[str] = None
    finalize_attempt: int = 0
    finalize_requested_at: Optional[str] = None
    finalize_sent_at: Optional[str] = None
    finalize_resend_count: int = 0
    finalize_started_at: Optional[str] = None
    final_run_id: Optional[str] = None
    final_manifest_uri: Optional[str] = None
    finalized_at: Optional[str] = None
    # Other
    withdrawn_reason: Optional[str] = None
    withdrawn_at: Optional[str] = None
    status_sequence: int = 0
    history_count: int = 0
    history_truncated: bool = False
    expires_after: Optional[int] = None

    @property
    def ref(self) -> QueueRef:
        return QueueRef(profile=self.profile, queue=self.queue)


class DraftRecord(_Record):
    doc_key: str = ""
    cycle_id: str = ""
    section_id: str
    s3_key: str = Field(alias="S3Key")
    revision: int
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    changed_fields_truncated: bool = False
    size_bytes: int = 0
    logged_for_claim_ms: Optional[int] = None
    expires_after: Optional[int] = None


class HistoryEntry(_Record):
    at: str
    event: str
    actor: Optional[str] = None
    details: dict[str, Any] = Field(default_factory=dict)


class ReviewDecision(BaseModel):
    """What ProcessResults should do with a document (see :mod:`.routing`)."""

    mode: Literal["legacy", "queues"]
    hold: bool = False
    outcome: Optional[Literal["complete", "rejected"]] = None
    cycle_id: Optional[str] = None
    queue: Optional[str] = None
    reasons: list[ReviewReason] = Field(default_factory=list)
    sections: list[str] = Field(default_factory=list)


class RunRef(BaseModel):
    run_id: str
    manifest_uri: Optional[str] = None


class StatusReason(BaseModel):
    kind: Literal["low_confidence", "blueprint_confidence"]
    section_id: str
    field: Optional[str] = None
    label: Optional[str] = None
    score: float
    threshold: float


class StatusTimestamps(BaseModel):
    enqueued_at: str
    disposed_at: Optional[str] = None
    finalized_at: Optional[str] = None
    updated_at: str


class StatusObject(BaseModel):
    """``<docKey>/review/status.json`` (schema ``status_object.schema.json``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    document_key: str
    status: Literal["in_review", "complete", "rejected"]
    sequence: int
    cycle_id: str
    profile: str
    queue: str
    run_id: Optional[str] = None
    machine_output: Optional[RunRef] = None
    final_output: Optional[RunRef] = None
    reasons: list[StatusReason] = Field(default_factory=list)
    reason_count: int = 0
    sections_in_review: list[str] = Field(default_factory=list)
    escalated: bool = False
    disposition: Optional[Literal["complete", "rejected"]] = None
    reviewer: Optional[str] = None
    reject_reason: Optional[str] = None
    reject_note: Optional[str] = None
    timestamps: StatusTimestamps


class TrackerReviewContext(BaseModel):
    kind: Literal["hold", "finalize"]
    item: ReviewItemRecord
    token: Optional[str] = None
    outcome: Optional[Literal["complete", "rejected"]] = None


class ReviewReportingInfo(BaseModel):
    cycle_id: str
    review_status: str
    disposition: Optional[str] = None
    profile: str
    queue: str
    reviewer: Optional[str] = None
    reviewed_at: Optional[str] = None
    enqueued_at: Optional[str] = None
    reason_count: int = 0
    top_reason_kind: Optional[str] = None
    reject_reason: Optional[str] = None
    escalated: bool = False
    machine_run_id: Optional[str] = None
    final_run_id: Optional[str] = None


class SweepReport(BaseModel):
    visited: int = 0
    expired: int = 0
    warned: int = 0
    holds_healed: int = 0
    holds_reaped: int = 0
    finalize_resent: int = 0
    finalize_retried: int = 0
    finalize_failed: int = 0
    finalize_healed: int = 0
    withdrawn: int = 0
    backlog: int = 0
    registry_refreshed: int = 0
