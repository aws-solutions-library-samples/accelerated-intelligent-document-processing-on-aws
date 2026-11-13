# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one declaration of review and legacy HITL status values.

Vendored verbatim as ``review_statuses.py`` into the two layer-free document-list
resolvers; keep it standard-library only.

* :class:`ReviewStatus` is the consumer-facing review status, written to the
  ``review/status.json`` status object and to the tracking row's ``HITLStatus``
  for documents routed to a review queue.
* :class:`QueueItemState` is the state of a queue item in ``HITLTable``.
* :class:`LegacyHitlStatus` lists the spellings the legacy HITL flow stores in
  ``HITLStatus``. They are kept as they are (existing rows keep working); readers
  normalise every spelling through :func:`normalize_hitl_status`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Literal, Mapping, Optional, Tuple, Union


class ReviewStatus(str, Enum):
    """Consumer-facing review status of a document routed to a review queue."""

    IN_REVIEW = "in_review"
    COMPLETE = "complete"
    REJECTED = "rejected"


class QueueItemState(str, Enum):
    """State of a review queue item."""

    HOLDING = "HOLDING"
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    FINALIZING = "FINALIZING"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    FINALIZE_FAILED = "FINALIZE_FAILED"
    WITHDRAWN = "WITHDRAWN"


class LegacyHitlStatus(str, Enum):
    """``HITLStatus`` spellings written by the legacy HITL flow."""

    PENDING_REVIEW = "PendingReview"
    REVIEW_PENDING = "Review Pending"
    IN_PROGRESS = "InProgress"
    REVIEW_IN_PROGRESS = "ReviewInProgress"
    COMPLETED = "Completed"
    REVIEW_COMPLETED = "Review Completed"
    SKIPPED = "Skipped"
    REVIEW_SKIPPED = "Review Skipped"


#: Why a legacy review is finished without a person checking it (Skip All Reviews).
#: Recorded in ``HITLReviewHistory`` with the skip. The web UI offers the same list
#: (``SKIP_ALL_REASONS`` in ``src/ui/src/components/common/review-status.ts``).
SKIP_ALL_REASONS = (
    "Extracted values look correct",
    "Review not needed for this document",
    "Duplicate document",
    "Other (see note)",
)


#: Why a finished production document is sent to a review queue by hand (Send to
#: review). Recorded with the item; the web UI offers the same list
#: (``SEND_TO_REVIEW_REASONS`` in ``src/ui/src/components/common/review-status.ts``).
SEND_TO_REVIEW_REASONS = (
    "Spot check",
    "Suspected extraction error",
    "Customer or downstream query",
    "Other (see note)",
)

#: Queue dispositions that finish a document with its machine results (no draft is
#: applied): an Admin's Remove from queue, and Skip All Reviews on a queue item.
MACHINE_RESULT_DISPOSITIONS = frozenset({"removed", "skipped"})

#: The refusal, in the ``CODE: message`` shape the UI reads (``reviewErrorCode``).
QUEUE_MANAGED_MESSAGE = (
    "QUEUE_MANAGED: this document is in a review queue; work it from Review Queues"
)

#: Legacy statuses meaning "a review is still owed".
LEGACY_PENDING = frozenset(
    {
        LegacyHitlStatus.PENDING_REVIEW,
        LegacyHitlStatus.REVIEW_PENDING,
        LegacyHitlStatus.IN_PROGRESS,
    }
)

#: Legacy statuses meaning "the review is finished" (completed or skipped).
LEGACY_DONE = frozenset(
    {
        LegacyHitlStatus.COMPLETED,
        LegacyHitlStatus.REVIEW_COMPLETED,
        LegacyHitlStatus.SKIPPED,
        LegacyHitlStatus.REVIEW_SKIPPED,
    }
)

#: Queue item states of a cycle that is still under way.
LIVE_ITEM_STATES = frozenset(
    {
        QueueItemState.HOLDING,
        QueueItemState.OPEN,
        QueueItemState.CLAIMED,
        QueueItemState.FINALIZING,
    }
)

#: Queue item states that end a cycle.
TERMINAL_ITEM_STATES = frozenset(
    {
        QueueItemState.COMPLETE,
        QueueItemState.REJECTED,
        QueueItemState.FINALIZE_FAILED,
        QueueItemState.WITHDRAWN,
    }
)

#: Tracking-row attributes that carry review state for a routed document.
TRACKING_REVIEW_ATTRIBUTES = (
    "HITLStatus",
    "HITLReviewedBy",
    "ReviewQueue",
    "ReviewCycleId",
    "ReviewStatusUri",
    "ReviewDisposedAt",
    "ReviewRejectReason",
)

HitlKind = Literal[
    "none", "pending", "in_progress", "in_review", "complete", "rejected", "skipped"
]


@dataclass(frozen=True)
class HitlView:
    """A normalised reading of a stored ``HITLStatus`` value."""

    kind: HitlKind
    queue_managed: bool

    @property
    def in_review(self) -> bool:
        """True while a queue review is under way (not yet complete or rejected)."""
        return self.kind == "in_review"


_LEGACY_KINDS = {
    LegacyHitlStatus.PENDING_REVIEW.value: "pending",
    LegacyHitlStatus.REVIEW_PENDING.value: "pending",
    LegacyHitlStatus.IN_PROGRESS.value: "in_progress",
    LegacyHitlStatus.REVIEW_IN_PROGRESS.value: "in_progress",
    LegacyHitlStatus.COMPLETED.value: "complete",
    LegacyHitlStatus.REVIEW_COMPLETED.value: "complete",
    LegacyHitlStatus.SKIPPED.value: "skipped",
    LegacyHitlStatus.REVIEW_SKIPPED.value: "skipped",
}

_REVIEW_KINDS = {
    ReviewStatus.IN_REVIEW.value: "in_review",
    ReviewStatus.COMPLETE.value: "complete",
    ReviewStatus.REJECTED.value: "rejected",
}


def normalize_hitl_status(raw: Optional[str]) -> HitlView:
    """Normalise a stored ``HITLStatus`` value. The one reader normaliser.

    Matching is exact after ``strip()``. Legacy spellings are not queue-managed;
    the three :class:`ReviewStatus` values are. Anything else reads as ``none``.
    """
    if raw is None:
        return HitlView("none", False)
    value = str(raw).strip()
    if value in _REVIEW_KINDS:
        return HitlView(_REVIEW_KINDS[value], True)  # type: ignore[arg-type]
    if value in _LEGACY_KINDS:
        return HitlView(_LEGACY_KINDS[value], False)  # type: ignore[arg-type]
    return HitlView("none", False)


_STATE_TO_STATUS = {
    QueueItemState.HOLDING: ReviewStatus.IN_REVIEW,
    QueueItemState.OPEN: ReviewStatus.IN_REVIEW,
    QueueItemState.CLAIMED: ReviewStatus.IN_REVIEW,
    QueueItemState.FINALIZING: ReviewStatus.IN_REVIEW,
    QueueItemState.FINALIZE_FAILED: ReviewStatus.IN_REVIEW,
    QueueItemState.COMPLETE: ReviewStatus.COMPLETE,
    QueueItemState.REJECTED: ReviewStatus.REJECTED,
    QueueItemState.WITHDRAWN: None,
}


def review_status_for_state(
    state: Union[QueueItemState, str],
) -> Optional[ReviewStatus]:
    """Consumer-facing status for a queue item state (``None`` = withdrawn)."""
    return _STATE_TO_STATUS[QueueItemState(state)]


#: Legacy HITL statuses that make a document part of a Reviewer's working set.
#: ``Review Pending`` is what ``releaseReview`` writes: a released review goes back
#: to the queue for another Reviewer to claim. Matched exactly by both document-list
#: resolvers, so the two lists agree row for row.
REVIEWER_ACTIVE_STATUSES = (
    LegacyHitlStatus.PENDING_REVIEW.value,
    LegacyHitlStatus.REVIEW_PENDING.value,
    LegacyHitlStatus.IN_PROGRESS.value,
    LegacyHitlStatus.REVIEW_IN_PROGRESS.value,
)


def is_review_finished(hitl_status: Any) -> bool:
    """Whether a stored ``HITLStatus`` says the document's review has finished.

    A legacy review completed or skipped, or a queue review that ended complete or
    rejected. ``COMPLETED`` is the one finished spelling a real writer stored that
    the exact-match normaliser does not know (the earlier SageMaker A2I flow);
    retained rows may still carry it. The web UI's ``isHitlReviewFinished`` is the
    same rule.
    """
    return (
        normalize_hitl_status(hitl_status).kind in ("complete", "rejected", "skipped")
        or hitl_status == "COMPLETED"
    )


#: Every stored ``HITLStatus`` value :func:`is_review_finished` accepts, derived
#: from the declared vocabularies through the rule so a condition or filter
#: expression can test it. Exact values: the normaliser also accepts surrounding
#: whitespace, which no writer stores.
FINISHED_HITL_STATUSES: Tuple[str, ...] = tuple(
    sorted(
        value
        for value in {
            *(s.value for s in ReviewStatus),
            *(s.value for s in LegacyHitlStatus),
            "COMPLETED",
        }
        if is_review_finished(value)
    )
)


def is_review_raised(row: Mapping[str, Any]) -> bool:
    """Whether a review was raised on a tracking row, finished or not.

    The web UI's ``hitlTriggered`` (``map-document-attributes.ts``): ``HITLTriggered``
    is true, or ``HITLStatus`` is a non-empty string other than ``N/A``.
    """
    status = row.get("HITLStatus")
    return row.get("HITLTriggered") is True or (
        isinstance(status, str) and status not in ("", "N/A")
    )


def is_legacy_review_pending(row: Mapping[str, Any]) -> bool:
    """Whether a tracking row holds a legacy (non-queue) review still owed.

    Raised, not finished, not ``HITLCompleted``, and not a review-queue status.
    """
    status = row.get("HITLStatus")
    return (
        is_review_raised(row)
        and not is_review_finished(status)
        and row.get("HITLCompleted") is not True
        and not normalize_hitl_status(status).queue_managed
    )


def pending_filter_expression(
    profile_placeholder: str = ":pf_profile",
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """``(filter, names, values)`` selecting a profile's raised, unfinished reviews.

    The DynamoDB form of :func:`is_review_raised` and not :func:`is_review_finished`
    and not ``HITLCompleted``, for a document-index query. Queue-managed rows
    (``in_review``) are included: whether a queue item can be acted on is decided
    per document from the item itself. The caller supplies the profile value under
    ``profile_placeholder``; every other placeholder starts with ``:pf_``.
    """
    finished = {f":pf_fin{i}": value for i, value in enumerate(FINISHED_HITL_STATUSES)}
    expression = (
        f"#pf_cv = {profile_placeholder} AND "
        "(#pf_trig = :pf_true OR (attribute_type(#pf_st, :pf_type) "
        "AND #pf_st <> :pf_empty AND #pf_st <> :pf_na)) AND "
        "NOT (#pf_done = :pf_true) AND "
        f"NOT (#pf_st IN ({', '.join(finished)}))"
    )
    names = {
        "#pf_cv": "ConfigVersion",
        "#pf_trig": "HITLTriggered",
        "#pf_st": "HITLStatus",
        "#pf_done": "HITLCompleted",
    }
    values: Dict[str, Any] = {
        ":pf_true": True,
        ":pf_type": "S",
        ":pf_empty": "",
        ":pf_na": "N/A",
        **finished,
    }
    return expression, names, values
