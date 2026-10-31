# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The consumer-facing review status object, ``<docKey>/review/status.json``.

The contract (canonical in ``README.md``): only routed documents have one;
``in_review`` is written before the item is claimable; ``complete`` / ``rejected``
is written after the tracking status, reporting rows and final manifest; deletion
means the review was withdrawn. Events are at-least-once and unordered, so
consumers read the object and use ``sequence``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from .models import (
    ReviewItemRecord,
    RunRef,
    StatusObject,
    StatusReason,
    StatusTimestamps,
)
from .statuses import ReviewStatus

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
STATUS_SEGMENT = "review/status.json"


def status_key(doc_key: str) -> str:
    return f"{doc_key}/{STATUS_SEGMENT}"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_status(
    item: ReviewItemRecord,
    status: ReviewStatus,
    *,
    sequence: int,
    run_id: Optional[str],
    machine_ref: Optional[RunRef],
    final_ref: Optional[RunRef],
    now: datetime,
) -> StatusObject:
    final = status in (ReviewStatus.COMPLETE, ReviewStatus.REJECTED)
    return StatusObject(
        document_key=item.doc_key,
        status=status.value,
        sequence=sequence,
        cycle_id=item.cycle_id,
        profile=item.profile,
        queue=item.queue,
        run_id=run_id,
        machine_output=machine_ref,
        final_output=final_ref if status == ReviewStatus.COMPLETE else None,
        reasons=[StatusReason(**r.model_dump()) for r in item.reasons],
        reason_count=item.reason_count,
        sections_in_review=list(item.sections),
        escalated=item.escalated,
        disposition=item.disposition if final else None,
        reviewer=item.disposed_by if final else None,
        reject_reason=item.reject_reason if status == ReviewStatus.REJECTED else None,
        reject_note=item.reject_note if status == ReviewStatus.REJECTED else None,
        timestamps=StatusTimestamps(
            enqueued_at=item.enqueued_at,
            disposed_at=item.disposed_at if final else None,
            finalized_at=_iso(now) if final else None,
            updated_at=_iso(now),
        ),
    )


def publish(
    store: Any,
    s3: Any,
    bucket: str,
    item: ReviewItemRecord,
    status: ReviewStatus,
    *,
    run_id: Optional[str],
    machine_ref: Optional[RunRef],
    final_ref: Optional[RunRef],
    now: datetime,
) -> StatusObject:
    """Take the next ``StatusSequence`` on the item, then write the object.

    The sequence is taken under the item's ``Version`` (the item is written
    whole elsewhere, so an unversioned counter would be overwritten); a race
    re-reads the item and retries. The object is written after the sequence is
    taken, so its ``sequence`` is always the latest.
    """
    from idp_common.dynamodb.client import DynamoDBTransactionCanceledError

    current = item
    for attempt in range(5):
        sequence = current.status_sequence + 1
        try:
            store.transition(
                current.doc_key,
                expected_version=current.version,
                set_={"StatusSequence": sequence},
            )
            break
        except DynamoDBTransactionCanceledError:
            fresh = store.get_item(current.doc_key)
            if fresh is None or fresh.cycle_id != item.cycle_id or attempt == 4:
                raise
            current = fresh
    item = current
    obj = build_status(
        item,
        status,
        sequence=sequence,
        run_id=run_id,
        machine_ref=machine_ref,
        final_ref=final_ref,
        now=now,
    )
    s3.write_content(
        obj.model_dump(mode="json"),
        bucket,
        status_key(item.doc_key),
        "application/json",
        metadata={
            "review-status": status.value,
            "review-cycle-id": item.cycle_id,
            "review-schema-version": SCHEMA_VERSION,
        },
    )
    logger.info(
        "event=review_status_published status=%s sequence=%d", status.value, sequence
    )
    return obj


def delete(s3: Any, bucket: str, doc_key: str) -> None:
    """Withdraw: remove the status object (absence means withdrawn)."""
    s3.delete_content(bucket, status_key(doc_key))
