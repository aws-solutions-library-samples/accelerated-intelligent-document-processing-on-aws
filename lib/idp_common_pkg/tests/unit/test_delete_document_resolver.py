# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the web UI's `deleteDocument` resolver Lambda.

The resolver returns **one boolean** for a whole selection, and the UI sends the whole
multi-select in one call — so that boolean is the only thing a user sees about a delete
that did not finish. It used to answer `deleted_count > 0 or failed_count == 0`, which is
`True` whenever *any* document in the selection was removed: nine deleted and one
tracking row orphaned read exactly like ten deleted (#1238). These tests assert the
counts-to-boolean mapping directly, because that expression is where the truth this
module works to produce was being discarded.

The resolver is loaded from its Lambda source by path, the way the other resolver tests
here do it, with `boto3` patched so no client is constructed and no credential is used.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def resolver(monkeypatch):
    """The resolver module, with its module-level boto3 resources mocked out."""
    monkeypatch.setenv("TRACKING_TABLE_NAME", "tracking")
    monkeypatch.setenv("INPUT_BUCKET", "in")
    monkeypatch.setenv("OUTPUT_BUCKET", "out")
    path = os.path.join(
        os.path.dirname(__file__),
        "../../../../nested/api-resolvers/src/lambda/delete_document_resolver/index.py",
    )
    with patch("boto3.resource"), patch("boto3.client"):
        spec = importlib.util.spec_from_file_location("delete_document_resolver", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["delete_document_resolver"] = module
        spec.loader.exec_module(module)
    yield module
    sys.modules.pop("delete_document_resolver", None)


def _event(keys: list[str]) -> dict:
    return {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {"objectKeys": keys},
    }


def _results(*successes: bool) -> list:
    return [
        {
            "success": ok,
            "object_key": f"k{i}",
            "errors": [] if ok else ["ThrottlingException"],
        }
        for i, ok in enumerate(successes)
    ]


@pytest.mark.unit
class TestTheReportedOutcome:
    @pytest.mark.parametrize(
        "successes, expected",
        [
            ((True,), True),
            ((False,), False),
            ((True, True, True), True),
            ((True, True, False), False),
            ((False, True, True), False),
            ((False, False, False), False),
        ],
        ids=[
            "one-ok",
            "one-failed",
            "all-ok",
            "last-failed",
            "first-failed",
            "all-failed",
        ],
    )
    def test_any_document_that_failed_makes_the_answer_false(
        self, resolver, successes, expected
    ):
        keys = [f"k{i}" for i in range(len(successes))]
        with patch.object(
            resolver, "delete_single_document", side_effect=_results(*successes)
        ):
            assert resolver.handler(_event(keys), MagicMock()) is expected

    def test_a_partial_failure_is_not_reported_as_a_complete_delete(self, resolver):
        """The measured defect, spelled out separately from the table above.

        `deleted_count > 0 or failed_count == 0` answers `True` here — two documents were
        removed — while one document's tracking-list row is still in the list. Multi-select
        is the ordinary UI path, so this is the shape a user meets.
        """
        with patch.object(
            resolver,
            "delete_single_document",
            side_effect=_results(True, True, False),
        ) as deleter:
            answer = resolver.handler(_event(["a", "b", "c"]), MagicMock())

        assert deleter.call_count == 3, "a failure must not abandon the rest"
        assert answer is False

    def test_a_caller_outside_the_two_groups_is_refused(self, resolver):
        # Defence in depth, unchanged by this change and worth keeping pinned: the
        # boolean above is only reachable by an Admin or an Author.
        event = {
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
            "arguments": {"objectKeys": ["a"]},
        }
        with pytest.raises(PermissionError):
            resolver.handler(event, MagicMock())
