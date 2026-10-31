# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one place review queues post Inbox messages (FR-38).

Every function here never raises: a failed notice must not fail a claim, a
disposition or a sweep. Bodies name the document basename and the queue, never
notes or draft contents.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from idp_common.notifications import notify
from idp_common.notifications.links import document_edit_link
from idp_common.notifications.models import MAX_GROUPS, MAX_USERS

from .models import QueueRef, ReviewItemRecord

logger = logging.getLogger(__name__)

SOURCE = "review-queues"


def _basename(doc_key: str) -> str:
    return doc_key.rsplit("/", 1)[-1] or doc_key


def _metric_failure() -> None:
    try:
        from idp_common.metrics import put_metric

        put_metric("ReviewNoticeFailures", 1)
    except Exception:  # noqa: BLE001 — metrics must not fail the producer
        logger.debug("ReviewNoticeFailures metric not published", exc_info=True)


def _post(
    *,
    type_id: str,
    title: str,
    body: str,
    link: str,
    users: Sequence[str] = (),
    groups: Sequence[str] = (),
    dedupe_key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> int:
    """Post in chunks of ≤ 100 users and ≤ 10 groups; return the number of calls."""
    users = [u for u in dict.fromkeys(u.strip().lower() for u in users) if u]
    groups = [g for g in dict.fromkeys(groups) if g]
    if not users and not groups:
        return 0
    user_chunks = [
        users[i : i + MAX_USERS] for i in range(0, len(users), MAX_USERS)
    ] or [[]]
    group_chunks = [
        groups[i : i + MAX_GROUPS] for i in range(0, len(groups), MAX_GROUPS)
    ] or [[]]
    calls = 0
    pairs = [(u, []) for u in user_chunks if u] + [([], g) for g in group_chunks if g]
    for chunk_users, chunk_groups in pairs:
        calls += 1
        try:
            result = notify(
                type_id=type_id,
                title=title,
                body=body,
                source=SOURCE,
                users=chunk_users,
                groups=chunk_groups,
                link=link,
                dedupe_key=dedupe_key,
                idempotency_key=idempotency_key,
            )
            if result is None:
                logger.debug("event=review_notice_skipped type=%s", type_id)
        except Exception:  # noqa: BLE001 — never fail the producer
            logger.error("event=review_notice_failed type=%s", type_id, exc_info=True)
            _metric_failure()
    return calls


def queue_items_waiting(
    store: Any, ref: QueueRef, members: tuple[Sequence[str], Sequence[str]], now_ms: int
) -> bool:
    """Tell a queue's members that items wait (debounced per queue, 10 minutes)."""
    groups, emails = members
    if not groups and not emails:
        return False
    try:
        if not store.try_announce(ref, now_ms):
            return False
    except Exception:  # noqa: BLE001
        logger.error("event=review_announce_failed", exc_info=True)
        _metric_failure()
        return False
    _post(
        type_id="review.queue_items_waiting",
        title=f"{ref.queue}: documents waiting",
        body=f"Documents are waiting for review in **{ref.queue}** ({ref.profile}).",
        link="/review-queues",
        users=emails,
        groups=groups,
        dedupe_key=f"review-queue:{ref.profile}#{ref.queue}",
    )
    return True


def item_reassigned(item: ReviewItemRecord, owner_email: str) -> None:
    _post(
        type_id="review.item_reassigned",
        title=_basename(item.doc_key),
        body=f"An Admin assigned **{_basename(item.doc_key)}** in queue **{item.queue}** to you.",
        link=document_edit_link(item.doc_key),
        users=[owner_email],
        idempotency_key=f"review-reassign:{item.cycle_id}#{item.version}",
    )


def claim_released(
    item: ReviewItemRecord, former_owner_email: str, reason: str
) -> None:
    reasons = {
        "force_release": "An Admin released your claim.",
        "move": "An Admin moved the item to another queue.",
        "reprocessed": "The document was reprocessed, so its review was withdrawn.",
        "reassign": "An Admin assigned the item to another reviewer.",
    }
    if reason == "take_over":
        # The draft is keyed by item and cycle, so the Admin carries on from it.
        body = "An Admin took over the item to finish it. Your draft stays with it."
    elif reason == "removed":
        # The draft is not applied: the document completes as processed.
        body = (
            "An Admin removed the document from review; it completes with its "
            "machine results."
        )
    else:
        body = f"{reasons.get(reason, 'Your claim ended.')} Your draft is kept."
    _post(
        type_id="review.claim_released",
        title=f"{_basename(item.doc_key)}: claim released",
        body=body,
        link=document_edit_link(item.doc_key),
        users=[former_owner_email],
        idempotency_key=f"review-release:{item.cycle_id}#{item.version}",
    )


def claim_expiring(item: ReviewItemRecord) -> None:
    if not item.owner_email:
        return
    _post(
        type_id="review.claim_expiring",
        title=f"{_basename(item.doc_key)}: claim expiring",
        body="Your claim expires soon. Keep working on the item to renew it.",
        link=document_edit_link(item.doc_key),
        users=[item.owner_email],
        dedupe_key=f"review-lease:{item.cycle_id}:{item.owner_key}",
        idempotency_key=f"review-lease-warn:{item.cycle_id}:{item.lease_expires_at_ms}",
    )


def claim_expired(item: ReviewItemRecord) -> None:
    if not item.owner_email:
        return
    _post(
        type_id="review.claim_expired",
        title=f"{_basename(item.doc_key)}: claim expired",
        body="Your claim expired and the item is back in its queue. Your draft is kept.",
        link=document_edit_link(item.doc_key),
        users=[item.owner_email],
        dedupe_key=f"review-lease:{item.cycle_id}:{item.owner_key}",
        idempotency_key=f"review-lease-expired:{item.cycle_id}:{item.lease_expires_at_ms}",
    )


def task_reminder(item: ReviewItemRecord) -> None:
    """An assigned item is still open after its queue's time limit."""
    if not item.owner_email:
        return
    _post(
        type_id="review.task_reminder",
        title=f"{_basename(item.doc_key)}: still to complete",
        body=(
            f"**{_basename(item.doc_key)}** in queue **{item.queue}** was assigned to "
            "you and is still open. Annotate and complete it from My Work › Tasks."
        ),
        link=document_edit_link(item.doc_key),
        users=[item.owner_email],
        dedupe_key=f"review-task:{item.cycle_id}:{item.owner_key}",
        idempotency_key=f"review-task-reminder:{item.cycle_id}:{item.claimed_at_ms}",
    )
