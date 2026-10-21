# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB TTL helper shared by every table that expires items."""

from datetime import datetime, timedelta, timezone


def calculate_ttl(days: int | float, *, now: datetime | None = None) -> int:
    """Epoch seconds ``days`` after ``now`` (default: the current UTC time).

    DynamoDB TTL attributes are a Number holding epoch seconds. A naive ``now``
    is treated as UTC.
    """
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return int((base + timedelta(days=days)).timestamp())
