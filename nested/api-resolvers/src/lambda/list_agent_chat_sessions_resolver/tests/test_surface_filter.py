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


class TestAFaultDoesNotRelayBotocoresMessage:
    def test_a_dynamodb_error_is_not_echoed_to_the_caller(self):
        from unittest.mock import patch

        from botocore.exceptions import ClientError

        table = MagicMock()
        table.query.side_effect = ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": (
                        "User: arn:aws:sts::123456789012:assumed-role/"
                        "SomeStack-Role/abc is not authorized to perform: "
                        "dynamodb:Query on resource: arn:aws:dynamodb:us-west-2:"
                        "123456789012:table/SomeStack-ChatSessionsTable"
                    ),
                }
            },
            "Query",
        )
        with patch.object(index.dynamodb, "Table", return_value=table):
            with pytest.raises(Exception) as excinfo:
                index.handler(
                    {"arguments": {}, "identity": {"username": "u@example.com"}}, None
                )

        message = str(excinfo.value)
        assert "assumed-role" not in message
        assert "arn:aws" not in message


class TestTheClaimsObjectIsNotLogged:
    """`sanitize_event_for_logging` redacts `identity` and `claims` at the top of the
    handler; dumping the identity object two lines later put back exactly what that
    call took out.

    Scoped to the CLAIMS BLOB, not to the principal. Logging which user a request was
    resolved to is ordinary operational logging and is left alone — the finding is the
    token's claim set reaching the log, not the identifier.
    """

    def test_the_claim_set_does_not_reach_the_log(self, caplog):
        import logging
        from unittest.mock import patch

        table = MagicMock()
        table.query.return_value = {"Items": []}
        claims = {
            "email": "u@example.com",
            "cognito:groups": ["Admin"],
            "sub": "11111111-2222-3333-4444-555555555555",
        }
        with caplog.at_level(logging.DEBUG):
            with patch.object(index.dynamodb, "Table", return_value=table):
                index.handler(
                    {
                        "arguments": {},
                        "identity": {"username": "u@example.com", "claims": claims},
                    },
                    None,
                )

        logged = caplog.text
        for leaked in ("cognito:groups", "11111111-2222-3333-4444-555555555555"):
            assert leaked not in logged, f"{leaked} reached the log"

    def test_the_event_itself_is_still_redacted(self, caplog):
        """The control: the sanitiser that makes the above true must still run."""
        import logging
        from unittest.mock import patch

        table = MagicMock()
        table.query.return_value = {"Items": []}
        with caplog.at_level(logging.INFO):
            with patch.object(index.dynamodb, "Table", return_value=table):
                index.handler(
                    {
                        "arguments": {},
                        "identity": {"claims": {"cognito:groups": ["Admin"]}},
                    },
                    None,
                )

        assert "REDACTED" in caplog.text
