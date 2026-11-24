# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Changing a document's page grouping while it is in a review queue.

Regrouping is part of review: the item's holder (the Annotator it is assigned to, or
an Admin who claimed it) may correct the document's split while working it. Nobody
else may, so a regroup never lands on work someone else holds (``regroup_refusal``).

Section ids are kept across a regroup, so the holder's drafts stay with their
sections. The draft of a section the regroup removed is deleted
(``discard_drafts``): a later regroup may give that id to a new section, and a new
section starts with no corrections.

Completing the review decides where the run after it starts
(``idp_common.run_plan.restart``). A section the regroup added, or whose pages it
changed, is extracted again, and the corrections saved for it are then set on its
new result (``apply_review``). A BDA run cannot extract a section again, so the
resolver refuses a regroup of a document that is processed in BDA mode.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

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


def discard_drafts(store: Any, doc_key: str, section_ids: Iterable[str]) -> int:
    """Delete the drafts of the sections a regroup removed; return how many deletes.

    The drafts are those of the item's current review cycle. A section with no
    draft counts too: deleting a record that is not there is not an error.

    Called after the regroup is saved, so it never raises. An item that cannot be
    read, or a delete that fails, is logged at WARNING and the rest goes on.
    """
    wanted = sorted({str(section_id) for section_id in section_ids})
    if store is None or not wanted:
        return 0
    try:
        item = store.get_item(doc_key)
    except Exception:  # noqa: BLE001 — the regroup is saved; see the docstring
        logger.warning("event=review_draft_discard_failed step=item", exc_info=True)
        return 0
    if item is None:
        return 0
    cycle_id = item.cycle_id
    deleted = 0
    for section_id in wanted:
        try:
            store.delete_draft(doc_key, cycle_id, section_id)
        except Exception:  # noqa: BLE001 — the regroup is saved; see the docstring
            logger.warning(
                "event=review_draft_discard_failed section=%s",
                section_id,
                exc_info=True,
            )
            continue
        deleted += 1
    if deleted:
        logger.info("event=review_drafts_discarded sections=%d", deleted)
    return deleted
