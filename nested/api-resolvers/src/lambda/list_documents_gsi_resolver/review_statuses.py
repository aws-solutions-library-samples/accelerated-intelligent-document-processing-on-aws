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
from typing import Literal, Optional, Union


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
