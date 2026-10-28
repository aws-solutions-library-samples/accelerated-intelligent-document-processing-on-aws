# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guard on direct writes of a document's section results.

A document in a review queue is edited only through its queue item: the item's
finalize run writes the reviewer's corrections over the section results, so a
direct write made meanwhile is either lost or, worse, overwrites the review's
input. ``processChanges`` and ``saveSectionReviewEdits`` already refuse such a
document; :func:`assert_section_result_writable` is the same refusal for an upload
of ``<document>/sections/<id>/result.json`` to the Output bucket
(``uploadDocument``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from .statuses import normalize_hitl_status

logger = logging.getLogger(__name__)

#: The refusal, in the ``CODE: message`` shape the UI reads (``reviewErrorCode``).
QUEUE_MANAGED_MESSAGE = (
    "QUEUE_MANAGED: this document is in a review queue; work it from Review Queues"
)

_SECTION_RESULT = re.compile(
    r"(?P<document>.+)/sections/(?P<section>[^/]+)/result\.json"
)


def section_result_document(key: str) -> Optional[str]:
    """The document a section-result key belongs to, or None for any other key."""
    match = _SECTION_RESULT.fullmatch(key or "")
    return match.group("document") if match else None


def assert_section_result_writable(
    tracking_table: Any, *, bucket: str, key: str, output_bucket: str
) -> None:
    """Refuse a section-result write for a document that is in a review queue.

    A no-op for any other bucket or key. Reads the document's tracking row
    (``{"PK": "doc#<document>", "SK": "none"}``); a read error propagates, so no
    write is allowed on an unknown state.

    Raises:
        ValueError: ``QUEUE_MANAGED_MESSAGE`` when the document is in review.
    """
    if bucket != output_bucket:
        return
    document = section_result_document(key)
    if document is None:
        return
    row = (
        tracking_table.get_item(
            Key={"PK": f"doc#{document}", "SK": "none"},
            ProjectionExpression="HITLStatus",
        ).get("Item")
        or {}
    )
    if normalize_hitl_status(row.get("HITLStatus")).in_review:
        logger.warning(
            "Rejecting a section-result upload for a document in a review queue"
        )
        raise ValueError(QUEUE_MANAGED_MESSAGE)
