# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who may change a document's page grouping while it is in a review queue.

Regrouping is part of review: the item's holder (the Annotator it is assigned to, or
an Admin who claimed it) may correct the document's split while working it. Nobody
else may, so a regroup never lands on work someone else holds. Section ids are kept
across a regroup, so the holder's drafts stay with their sections; a draft for a
section the regroup removed is skipped by the finalize run (``apply_corrections``).
The finalize run reprocesses the tracked document, so the new grouping is what it
finishes with.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .statuses import QueueItemState

logger = logging.getLogger(__name__)


def regroup_refusal(store: Any, doc_key: str, reviewer_key: str) -> Optional[str]:
    """Why the caller may not regroup this in-review document, or ``None`` if they may.

    ``reviewer_key`` is the caller's key from verified claims
    (``recipient_key_from_claims``), the same key a claim records as the owner.
    """
    if store is None:
        return "REVIEW_UNAVAILABLE: this stack has no review queues"
    item = store.get_item(doc_key)
    if item is None:
        return "NO_REVIEW_ITEM: this document has no review item"
    if item.state == QueueItemState.FINALIZING.value:
        return "FINALIZING: this review was submitted and its corrections are being applied"
    if item.state != QueueItemState.CLAIMED.value:
        return "NOT_ASSIGNED: claim or be assigned this item before changing its page grouping"
    if item.owner_key != reviewer_key:
        logger.warning("Rejecting regroup of an in-review document: NOT_LEASE_HOLDER")
        return "NOT_LEASE_HOLDER: this item is assigned to someone else"
    return None
