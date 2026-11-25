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
from .statuses import ReviewStatus

#: The top-level key of a section result that holds its review record, written
#: when a completed review is applied (``apply_review``).
REVIEW_KEY = "review"


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
    kind: Literal[
        "low_confidence",
        "blueprint_confidence",
        "missing_required",
        "extraction_failed",
    ]
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


class ManualRequest(_Record):
    """Why and by whom a document was sent to review by hand (Send to review)."""

    reason: str
    note: Optional[str] = None
    requested_by: str
    requested_at: str
    #: The tracking row's ``HITLStatus`` before the send (restored on abandon).
    prior_hitl_status: Optional[str] = None
    #: The tracking row's ``HITLCompleted`` before the send (restored on abandon).
    prior_hitl_completed: Optional[bool] = None


class PolicyFailure(_Record):
    """A policy rule the document failed, as the reviewer is shown it.

    Stored as the map ``{RuleId, Policy, Name, Reason, Pages}``.
    """

    #: ``<policy type>.<rule key>``.
    rule_id: str = ""
    policy: str = ""
    #: The rule's key: the id without its policy type.
    name: str = ""
    #: The explanation policy rule validation stored for the rule, shortened.
    reason: str = ""
    #: The page numbers the explanation cites.
    pages: list[int] = Field(default_factory=list)


class RoutedBy(_Record):
    """The routing rule that placed a document in its queue, and what it read.

    ``inputs``, ``reason`` and ``policy_failures`` are bounded when the record is
    built (``routing_step``), not by a validator, so an item that was stored
    always loads.
    """

    table_id: str = ""
    #: The matched rule; empty when the table's default outcome applied.
    rule_id: str = ""
    rule_label: str = ""
    profile: str = ""
    config_revision: Optional[int] = None
    #: The queue the outcome named, and the queue the document was placed in.
    #: They differ when the named queue is missing or disabled.
    outcome_queue: str = ""
    placed_queue: str = ""
    #: ``""``, ``queue_missing``, ``queue_disabled`` or ``evaluation_error``.
    fallback_reason: str = ""
    #: The input values the decision read, by expression text.
    inputs: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: str = ""
    #: What the reviewer reads: the reason with the document's values filled in.
    #: Empty on an item written before routing rules had a reason.
    reason: str = ""
    #: The reason as the Admin wrote it, placeholders unfilled; empty when the
    #: routing rule has none.
    reason_template: str = ""
    #: ``admin`` (the Admin's text), ``fallback`` (generated), or ``""`` on an
    #: item written before routing rules had a reason.
    reason_source: str = ""
    #: The policy rules the document failed, whichever routing rule placed it:
    #: the first of them, and how many there are in all.
    policy_failures: list[PolicyFailure] = Field(default_factory=list)
    policy_failure_count: int = 0


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
    # Manual send and bulk skip
    #: ``"manual"`` for an item created by Send to review (no phase-1 run).
    origin: Optional[Literal["manual"]] = None
    manual_request: Optional[ManualRequest] = None
    #: The bulk Skip All Reviews job that skipped the item, if any.
    skip_job_id: Optional[str] = None
    # Routing
    #: The rule that placed the document; ``None`` on an item written before
    #: routing rules existed.
    routed_by: Optional[RoutedBy] = None
    #: 1 for a first placement; one more for each placement that follows a
    #: completed review of the same document without a new upload in between.
    placement_count: int = 1
    #: The processing mode of the run that placed the document (``pipeline`` or
    #: ``bda``): the mode a send-back of this review runs in, so the steps it can
    #: start at. ``None`` on an item placed by hand (Send to review) or written
    #: before the mode was recorded.
    processing_mode: Optional[str] = None
    #: The usage of the runs before this placement, when it follows a completed
    #: review: the tracker adds it to the baseline at activation.
    carried_metering_json: Optional[str] = None
    #: The document's sections when it was placed: section id to a digest of the
    #: section's pages (``run_plan.restart.layout_of``). A section the reviewer
    #: regrouped is told apart against it when the review is completed. Empty on
    #: an item written before the layout was recorded, and for a document with
    #: more sections than a layout records.
    placed_layout: dict[str, str] = Field(default_factory=dict)
    #: Where the run after this review starts, decided when the review is
    #: completed: ``apply_review_changes`` or ``extraction``. ``None`` until
    #: then, and on an item completed before the start was recorded.
    restart_step: Optional[str] = None
    #: The sections that run extracts again, for an extraction start. Empty means
    #: every processed section.
    restart_sections: list[str] = Field(default_factory=list)
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
    kind: Literal[
        "low_confidence",
        "blueprint_confidence",
        "missing_required",
        "extraction_failed",
    ]
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


class StatusPolicyFailure(BaseModel):
    """A policy rule the document failed, as a consumer sees it."""

    rule_id: str = ""
    policy: str = ""
    name: str = ""
    reason: str = ""
    pages: list[int] = Field(default_factory=list)


class StatusRoutedBy(BaseModel):
    """The routing rule that placed the document, as a consumer sees it."""

    table_id: str = ""
    rule_id: str = ""
    rule_label: str = ""
    placed_queue: str = ""
    fallback_reason: str = ""
    #: What the reviewer reads, and where it came from: ``admin``, ``fallback``,
    #: or ``""`` for a placement made before routing rules had a reason.
    reason: str = ""
    reason_source: str = ""
    #: The first failed policy rules, and how many the document failed in all.
    policy_failures: list[StatusPolicyFailure] = Field(default_factory=list)
    policy_failure_count: int = 0


class StatusObject(BaseModel):
    """``<docKey>/review/status.json`` (schema ``status_object.schema.json``)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"] = "1.1"
    document_key: str
    status: ReviewStatus
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
    routed_by: Optional[StatusRoutedBy] = None
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
    #: The routing rule's reason as written, placeholders unfilled: reporting is
    #: read across profiles, so it gets no document values. ``None`` when the
    #: routing rule has no reason, or no routing rule placed the document.
    routing_reason: Optional[str] = None


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
