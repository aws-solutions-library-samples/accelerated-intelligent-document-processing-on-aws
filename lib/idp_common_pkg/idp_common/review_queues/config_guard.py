# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin-only changes to ``hitl.review_queues`` (FR-3, FR-4).

The block decides whether a profile's low-confidence documents wait for review and
which system-wide queue they enter, so changing it is an access decision.
:class:`ReviewQueuesAdminGuard` is the ``write_guard`` the configuration resolver
passes to ``ConfigurationManager``: it refuses a non-Admin save that changes the
block and records Admin changes so the resolver can warn about a queue id that no
queue in HITL › Queue Management has.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from idp_common.config.models import HITLReviewQueuesConfig, IDPConfig

logger = logging.getLogger(__name__)

#: At most this many added emails are looked up per save.
MAX_EMAIL_LOOKUPS = 50

ReviewQueuesChange = Tuple[str, dict, dict]


def _review_queues_dump(config: Optional[IDPConfig]) -> dict:
    rq = config.hitl.review_queues if config is not None else HITLReviewQueuesConfig()
    return rq.model_dump(mode="python")


class ReviewQueuesAdminGuard:
    """A ``WriteGuard`` that lets only Admins change ``hitl.review_queues``."""

    def __init__(self, *, is_admin: bool, caller_email: str):
        self.is_admin = is_admin
        self.caller_email = caller_email
        #: ``(profile, before, after)`` for every Admin save that changed the block.
        self.changes: List[ReviewQueuesChange] = []

    def __call__(
        self, before: Optional[IDPConfig], after: IDPConfig, version: str
    ) -> None:
        before_rq = _review_queues_dump(before)
        after_rq = _review_queues_dump(after)
        if before_rq == after_rq:
            return
        if not self.is_admin:
            logger.warning(
                "Rejecting configuration write to %s: hitl.review_queues is Admin-only",
                version,
            )
            raise PermissionError(
                "Unauthorized: only Admins may change hitl.review_queues"
            )
        self.changes.append((version, before_rq, after_rq))


def unknown_queue_ids(
    changes: Sequence[ReviewQueuesChange],
    queue_ids: Callable[[], Iterable[str]],
) -> List[str]:
    """Queue ids chosen by ``changes`` that no system-wide queue has.

    ``queue_ids()`` returns the defined queue ids. Never raises: a failed read logs
    a WARNING and returns no warnings (routing falls back to the Default queue).
    """
    chosen: List[str] = []
    for _version, _before, after in changes:
        queue = str(after.get("queue") or "").strip().lower()
        if queue and queue not in chosen:
            chosen.append(queue)
    if not chosen:
        return []
    try:
        known = {str(q).lower() for q in queue_ids()}
    except Exception as exc:  # noqa: BLE001 - a warning must never fail a save
        logger.warning(
            "Queue definitions unreadable (%s); not checking the queue id",
            type(exc).__name__,
        )
        return []
    return [q for q in chosen if q not in known]


def warnings_for(unknown: Sequence[str]) -> List[str]:
    """User-facing warnings for :func:`unknown_queue_ids`."""
    return [
        f"Queue {queue!r} does not exist in HITL › Queue Management; this profile's "
        "documents go to the Default queue until it is created."
        for queue in unknown
    ]
