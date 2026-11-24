# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""API shapes for the review-queue operations.

Views mirror the GraphQL types in ``schema.graphql`` (camelCase aliases; dump
with ``model_dump(by_alias=True, mode="json")``). Argument models bound every
input; a validation failure becomes a ``ValueError`` whose message names the
argument but never echoes its value (``summarize_validation_error``).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pydantic.alias_generators import to_camel

from idp_common.notifications.api_models import summarize_validation_error
from idp_common.review_queues.queue_defs import QUEUE_REF_PATTERN
from idp_common.run_plan.planned import PlannedRun

__all__ = ["summarize_validation_error"]

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_NOTE = 1000
MAX_DRAFT_BYTES = 2_000_000
MAX_DRAFT_DEPTH = 64
CYCLE_ID_PATTERN = r"^[0-9a-f]{21}$"


def clamp_limit(limit: Any) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("BAD_ARGUMENT: limit must be an integer")
    return max(1, min(MAX_LIMIT, limit))


class _View(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def dump(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


class ReviewReasonView(_View):
    kind: str
    section_id: str
    field: Optional[str] = None
    label: Optional[str] = None
    score: float
    threshold: float


class ReviewSendInfoView(_View):
    """Why a document was sent to review by hand (Send to review)."""

    reason: str
    note: Optional[str] = None
    requested_by: str
    requested_at: str


class ReviewPolicyFailureView(_View):
    """A policy rule the document failed, with the explanation stored for it."""

    #: ``<policy type>.<rule key>``.
    rule_id: str = ""
    policy: str = ""
    #: The rule's key: the id without its policy type.
    name: str = ""
    reason: str = ""
    #: The page numbers the explanation cites.
    pages: list[int] = Field(default_factory=list)


class ReviewRoutedByView(_View):
    """The routing rule that placed an item in its queue."""

    table_id: str = ""
    #: Empty when the table's default outcome applied.
    rule_id: str = ""
    rule_label: str = ""
    placed_queue: str = ""
    #: Empty, or why the item is in the Default queue instead of the queue the
    #: rule named: ``queue_missing``, ``queue_disabled``, ``evaluation_error``.
    fallback_reason: str = ""
    #: The input values the decision read, as a JSON object.
    inputs: Optional[str] = None
    #: What the reviewer reads: the routing rule's reason with the document's
    #: values. ``None`` on an item placed before routing rules had a reason.
    reason: Optional[str] = None
    #: ``admin`` (the routing rule's own reason) or ``fallback`` (generated).
    reason_source: Optional[str] = None
    #: The first policy rules the document failed, whichever routing rule
    #: placed it, and how many it failed in all.
    policy_failures: list[ReviewPolicyFailureView] = Field(default_factory=list)
    policy_failure_count: int = 0


class RoutingTestResult(_View):
    """What a routing table decides for a document (``testRoutingTable``).

    The result of an evaluation that places nothing.
    """

    #: ``hold`` when the outcome is a queue, ``continue`` when it is no review.
    decision: str
    #: The rule that matched; ``None`` when the table's default outcome applied.
    matched_rule_id: Optional[str] = None
    rule_label: Optional[str] = None
    #: ``queue`` or ``no_review``.
    outcome_kind: str
    #: The queue the outcome names.
    outcome_queue: Optional[str] = None
    #: The queue a run would place the document in: the outcome's queue, or the
    #: Default queue when that one is missing or disabled.
    placed_queue: Optional[str] = None
    #: Empty, or why the placed queue is the Default queue: ``queue_missing``,
    #: ``queue_disabled``, ``evaluation_error``.
    fallback_reason: str = ""
    #: The input values the decision read, as a JSON object.
    inputs: Optional[str] = None
    #: References that could not be resolved for this document: a class the
    #: configuration does not have, a section result that could not be read.
    stale_references: list[str] = Field(default_factory=list)
    #: What the reviewer would read: the routing rule's reason with this
    #: document's values. ``None`` when the outcome is no review.
    reason: Optional[str] = None
    #: ``admin`` (the routing rule's own reason) or ``fallback`` (generated,
    #: because the routing rule has none or it could not be rendered).
    reason_source: Optional[str] = None


class ReviewItemView(_View):
    object_key: str
    cycle_id: str
    profile: str
    queue: str
    #: The queue's name; ``None`` when its definition is gone or was not read.
    queue_name: Optional[str] = None
    priority: int
    state: str
    review_status: str
    enqueued_at: str
    reasons: list[ReviewReasonView] = Field(default_factory=list)
    reason_count: int = 0
    sections: list[str] = Field(default_factory=list)
    owner_email: Optional[str] = None
    claimed_at: Optional[str] = None
    lease_expires_at: Optional[str] = None
    is_mine: bool = False
    has_draft: bool = False
    draft_updated_at: Optional[str] = None
    draft_updated_by: Optional[str] = None
    escalated: bool = False
    escalation_note: Optional[str] = None
    disposition: Optional[str] = None
    disposed_by: Optional[str] = None
    disposed_at: Optional[str] = None
    reject_reason: Optional[str] = None
    version: int = 0
    sent_to_review: Optional[ReviewSendInfoView] = None
    #: ``None`` for an item placed before routing rules existed, or by hand.
    routed_by: Optional[ReviewRoutedByView] = None
    #: ``pipeline`` or ``bda``: the mode of the run that placed the document, so
    #: the steps a send-back can start at. ``None`` when it was not recorded.
    processing_mode: Optional[str] = None


class ReviewItemPage(_View):
    items: list[ReviewItemView] = Field(default_factory=list)
    next_token: Optional[str] = None


class ReviewQueueSummary(_View):
    profile: str
    #: The queue id: what items, links and the queue arguments use.
    name: str
    #: What people see ("Loan Review" for the queue id "loan_review").
    display_name: str = ""
    description: Optional[str] = None
    priority: int
    depth: int = 0
    unclaimed: int = 0
    claimed: int = 0
    claimed_by_me: int = 0
    #: Assigned items whose Annotator has saved work (a draft); the rest of
    #: ``claimed`` is assigned but not started.
    in_progress: int = 0
    #: Distinct Annotators holding the queue's assigned items.
    annotators: int = 0
    oldest_enqueued_at: Optional[str] = None
    is_default_queue: bool = False
    #: When off, no documents are added to the queue.
    enabled: Optional[bool] = None
    claim_cap: int = 10
    checklist: list[str] = Field(default_factory=list)
    defined_in_config: bool = True


class MyReviewQueues(_View):
    queues: list[ReviewQueueSummary] = Field(default_factory=list)
    my_claim_count: int = 0
    items: ReviewItemPage = Field(default_factory=ReviewItemPage)
    #: Whether the Default queue exists and is enabled (Send to review needs it).
    default_queue_enabled: bool = False
    #: The bulk Skip All Reviews inline limit in force.
    bulk_skip_sync_limit: int = 50
    #: Review Routing (``hitl.enabled``) of the active profile: with it off, no
    #: document is held, whatever the routing rules say. ``None`` when the
    #: profile could not be read.
    review_routing_enabled: Optional[bool] = None


class ReviewSectionDetail(_View):
    section_id: str
    document_class: Optional[str] = None
    result_uri: Optional[str] = None
    draft_uri: Optional[str] = None
    draft_revision: Optional[int] = None
    draft_updated_by: Optional[str] = None
    draft_updated_at: Optional[str] = None
    page_ids: list[str] = Field(default_factory=list)
    reasons: list[ReviewReasonView] = Field(default_factory=list)


class ReviewHistoryEntryView(_View):
    at: str
    event: str
    actor: Optional[str] = None
    details: Optional[str] = None  # AWSJSON


class ReviewItemDetail(_View):
    item: ReviewItemView
    #: The item's queue as people see it: its name, or a readable form of its id
    #: when the queue is no longer defined.
    queue_name: Optional[str] = None
    sections: list[ReviewSectionDetail] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    reject_reasons: list[str] = Field(default_factory=list)
    skip_reasons: list[str] = Field(default_factory=list)
    claim_cap: int = 10
    my_claim_count: int = 0
    history: list[ReviewHistoryEntryView] = Field(default_factory=list)
    machine_output_manifest_uri: Optional[str] = None
    can_claim: bool = False
    claim_blocked_reason: Optional[str] = None


class ClaimNextResult(_View):
    item: Optional[ReviewItemView] = None
    reason: Optional[str] = None


class ReviewDraftView(_View):
    section_id: str
    revision: int
    draft_uri: str
    updated_at: str
    lease_expires_at: Optional[str] = None
    changed_field_count: int = 0


class ReviewCompletionPlanView(_View):
    """What finishing a review runs, for each of the two ways to finish it."""

    #: The run that follows Complete.
    complete: PlannedRun
    #: The run that follows Send back.
    send_back: PlannedRun


class ReviewerLoad(_View):
    email: str
    claimed: int


class ReviewQueueHealth(_View):
    summary: ReviewQueueSummary
    reviewers: list[ReviewerLoad] = Field(default_factory=list)


class ReviewQueuesOverview(_View):
    queues: list[ReviewQueueHealth] = Field(default_factory=list)


# ------------------------------------------------------------------ arguments


class _Args(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, alias_generator=to_camel
    )


class NoArgs(_Args):
    """An operation that takes no arguments (any argument is refused)."""


class ItemCycleArgs(_Args):
    object_key: str = Field(min_length=1, max_length=1024)
    cycle_id: str = Field(pattern=CYCLE_ID_PATTERN)


ItemArgs = ItemCycleArgs


class ItemKeyArgs(_Args):
    object_key: str = Field(min_length=1, max_length=1024)


class ClaimNextArgs(_Args):
    # Queues are system-wide, so a queue is named by queueName alone. queueProfile
    # is still accepted, and ignored, for callers written when queues belonged to a
    # configuration profile.
    queue_profile: Optional[str] = Field(default=None, min_length=1, max_length=256)
    queue_name: Optional[str] = Field(default=None, pattern=QUEUE_REF_PATTERN)


class ListQueueItemsArgs(ClaimNextArgs):
    claimable: bool = False
    limit: Optional[int] = None
    next_token: Optional[str] = Field(default=None, max_length=4096)


class ListMyWorkArgs(_Args):
    limit: Optional[int] = None
    next_token: Optional[str] = Field(default=None, max_length=4096)


class MyQueuesArgs(_Args):
    item_limit: Optional[int] = None


class SaveDraftArgs(ItemArgs):
    section_id: str = Field(min_length=1, max_length=64)
    data: Any
    base_revision: Optional[int] = Field(default=None, ge=0)

    @field_validator("data", mode="before")
    @classmethod
    def parse_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            if len(v.encode("utf-8")) > MAX_DRAFT_BYTES * 2:
                raise ValueError("draft data is too large")
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError("data must be JSON") from exc
        return v


class CompleteArgs(ItemArgs):
    checklist: Optional[list[bool]] = Field(default=None, max_length=20)
    note: Optional[str] = Field(default=None, max_length=MAX_NOTE)


class ReleaseArgs(ItemArgs):
    reason: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=MAX_NOTE)


class RejectArgs(ItemArgs):
    reason: str = Field(min_length=1, max_length=200)
    note: Optional[str] = Field(default=None, max_length=MAX_NOTE)


class RemoveArgs(ItemArgs):
    note: Optional[str] = Field(default=None, max_length=MAX_NOTE)


class ReassignArgs(ItemArgs):
    assignee_email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+$"
    )


class MoveArgs(ItemArgs):
    target_queue: str = Field(pattern=QUEUE_REF_PATTERN)


class SendBackArgs(ItemArgs):
    """Send a document in review back to be processed again.

    The call names no step: where the run starts is the reprocess service's
    decision.
    """

    note: Optional[str] = Field(default=None, max_length=MAX_NOTE)


class TestRoutingArgs(ItemKeyArgs):
    """Evaluate a routing table against a document, placing nothing."""

    # Not a test class: the name is the operation's (``testRoutingTable``).
    __test__ = False

    #: A routing section to try in place of the configuration's own: a JSON
    #: object, or its text. Left out, the configuration is tested as it is.
    routing: Optional[Any] = None


class _QueueDefinitionArgs(_Args):
    """A queue definition as the API takes it (``ReviewQueueDefinitionInput``).

    Every field but ``id`` may be left out. The service fills what is missing:
    with the defaults for a new queue, and for an update with the stored
    ``enabled`` and ``skipReasons``.
    """

    id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    priority: Optional[int] = None
    checklist: Optional[list[str]] = None
    reject_reasons: Optional[list[str]] = None
    skip_reasons: Optional[list[str]] = None
    time_limit_minutes: Optional[int] = None
    eligible_annotators: Optional[list[str]] = None
    enabled: Optional[bool] = None
    #: The version an update was built from. A create ignores it.
    version: Optional[StrictInt] = Field(default=None, ge=0)
    #: Sent by clients written when a queue had a threshold of its own. Accepted
    #: and ignored: queues have none.
    confidence_threshold: Optional[Any] = None


class CreateQueueArgs(_QueueDefinitionArgs):
    """``createReviewQueue``'s input."""


class UpdateQueueArgs(_QueueDefinitionArgs):
    """``updateReviewQueue``'s input."""


class ListQueuesArgs(_Args):
    limit: Optional[int] = None
    next_token: Optional[str] = Field(default=None, max_length=4096)


def draft_depth(value: Any, depth: int = 0) -> int:
    if depth > MAX_DRAFT_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([draft_depth(v, depth + 1) for v in value.values()] or [depth + 1])
    if isinstance(value, list):
        return max([draft_depth(v, depth + 1) for v in value] or [depth + 1])
    return depth
