# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CHAT_SESSIONS_TABLE", "test-sessions")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import index  # noqa: E402

pytestmark = pytest.mark.unit


def _run(arguments):
    captured = {}
    table = MagicMock()

    def _query(**kwargs):
        captured.update(kwargs)
        return {"Items": []}

    table.query.side_effect = _query
    with patch.object(index.dynamodb, "Table", return_value=table):
        index.handler(
            {"arguments": arguments, "identity": {"username": "u@example.com"}},
            None,
        )
    return captured


def test_no_surface_has_no_filter():
    q = _run({})
    assert "FilterExpression" not in q


def test_quick_start_surface_filters_exactly():
    q = _run({"surface": "quick_start"})
    assert q["FilterExpression"] == "surface = :surface"
    assert q["ExpressionAttributeValues"][":surface"] == "quick_start"


def test_chat_surface_includes_legacy_rows():
    # Legacy rows written before the surface attribute existed must still show
    # up in Companion history.
    q = _run({"surface": "chat"})
    assert q["FilterExpression"] == "attribute_not_exists(surface) OR surface = :surface"
    assert q["ExpressionAttributeValues"][":surface"] == "chat"


class TestThePageSizeIsClampedNotDefaulted:
    """`limit` reached DynamoDB unbounded.

    `arguments.get("limit", 20)` reads as a cap but is a default: the central
    validation spec checks only that the value is an Int, so the page size was
    whatever the caller asked for. The siblings that get this right
    (list_documents_gsi_resolver, test_set_resolver) all use a hard `min()`.
    """

    def test_an_oversized_limit_cannot_raise_the_page_size(self):
        q = _run({"limit": 10_000})

        assert q["Limit"] == index.MAX_PAGE_SIZE

    def test_a_non_positive_limit_does_not_reach_dynamodb(self):
        """DynamoDB rejects `Limit <= 0` with a ValidationException, which the
        dispatcher reports to the caller as a 500."""
        q = _run({"limit": 0})

        assert q["Limit"] >= 1

    def test_an_absent_limit_still_gets_the_default(self):
        q = _run({})

        assert q["Limit"] == index.DEFAULT_PAGE_SIZE

    def test_a_reasonable_limit_is_honoured(self):
        q = _run({"limit": 7})

        assert q["Limit"] == 7
