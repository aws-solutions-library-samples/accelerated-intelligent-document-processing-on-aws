# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Select a profile's pending production reviews from ``TypeDateIndex``.

One paginated ``Query``: ``ItemType = document`` and ``InitialEventTime BETWEEN`` the
range, filtered server-side by :func:`statuses.pending_filter_expression` (the
profile, raised and not finished), projecting only ``ObjectKey``. Queue items are
included as ``in_review`` rows; whether one can be acted on is decided per document
(:func:`review_skip.classify`). Read units are charged for every row examined, so
the range is required and bounded (``bulk_models.MAX_RANGE_DAYS``).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Iterator, Optional, Tuple

from .bulk_models import PAGE_SIZE, utc_iso
from .statuses import pending_filter_expression

#: The production document index (hash ``ItemType``, range ``InitialEventTime``).
TYPE_DATE_INDEX = "TypeDateIndex"
_PRODUCTION_ITEM_TYPE = "document"


class PendingReviewQuery:
    """Pages of object keys with a pending review for one profile and range."""

    def __init__(self, client: Any, index_name: str = TYPE_DATE_INDEX):
        self.client = client  # idp_common.dynamodb.DynamoDBClient on the tracking table
        self.index_name = index_name

    def _query_args(
        self, profile: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        filter_expression, names, values = pending_filter_expression(":pf_profile")
        return {
            "key_condition_expression": "#it = :it AND #iet BETWEEN :a AND :b",
            "expression_attribute_names": {
                "#it": "ItemType",
                "#iet": "InitialEventTime",
                "#ok": "ObjectKey",
                **names,
            },
            "expression_attribute_values": {
                ":it": _PRODUCTION_ITEM_TYPE,
                ":a": utc_iso(start),
                ":b": utc_iso(end),
                ":pf_profile": profile,
                **values,
            },
            "index_name": self.index_name,
            "filter_expression": filter_expression,
            "projection_expression": "#ok",
        }

    def pages(
        self,
        profile: str,
        start: datetime,
        end: datetime,
        *,
        cursor: Optional[dict] = None,
        page_size: int = PAGE_SIZE,
    ) -> Iterator[Tuple[list[str], Optional[dict]]]:
        """Yield ``(object_keys, cursor)`` per DynamoDB page.

        ``cursor`` is the ``LastEvaluatedKey`` to resume from (``None`` on the last
        page). ``page_size`` is the request ``Limit`` (rows examined, not matched).
        """
        for response in self.client.query_pages(
            page_size=page_size,
            exclusive_start_key=cursor,
            **self._query_args(profile, start, end),
        ):
            keys = [
                str(item["ObjectKey"])
                for item in response.get("Items") or []
                if item.get("ObjectKey")
            ]
            yield keys, response.get("LastEvaluatedKey")

    def collect(
        self,
        profile: str,
        start: datetime,
        end: datetime,
        *,
        max_docs: int,
        budget_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Tuple[list[str], bool]:
        """Up to ``max_docs`` keys within ``budget_seconds``; ``(keys, approximate)``.

        ``approximate`` is True when the budget ran out or more keys exist.
        """
        deadline = monotonic() + budget_seconds
        found: list[str] = []
        for keys, cursor in self.pages(profile, start, end, page_size=1000):
            found.extend(keys)
            if len(found) > max_docs:
                return found[:max_docs], True
            if cursor is None:
                return found, False
            if monotonic() >= deadline:
                return found, True
        return found, False
