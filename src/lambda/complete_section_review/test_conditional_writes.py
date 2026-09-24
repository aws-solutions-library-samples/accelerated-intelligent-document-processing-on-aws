# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concurrency tests for the HITL review-history append (issue #1111).

Several annotators reviewing different sections of one document at the same time
is the *designed* workflow, not an edge case, so a review record has to survive
another reviewer's overlapping write. Reading ``HITLReviewHistory``, appending in
Python and writing the whole array back is a lost update: both writes succeed and
the loser's entry is gone with nothing reporting it.

Two things make these tests measure the defect rather than a keyword argument.

The table is a **real** ``moto`` table, so ``list_append`` and ``if_not_exists``
are evaluated by DynamoDB's own expression engine rather than by a double written
to agree with the code under test. A ``MagicMock`` table -- which is what the rest
of this directory's suite uses -- accepts any ``UpdateExpression`` at all and
stores nothing, so it cannot tell the fixed write from the broken one.

The interleaving is **forced, not raced**. ``_CompetingWriteTable`` commits the
other reviewer's append inside the first ``get_item`` call, immediately after the
snapshot the handler will go on to use has been taken. There is no thread, no
sleep and no dependence on the scheduler: the handler always reads a state that is
stale by exactly one entry by the time it writes, which is the worst case for this
defect rather than a sample of the space.
"""

import importlib
import os
import sys
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
TABLE_NAME = "test-tracking"
DOC_KEY = "doc/key.pdf"
ITEM_KEY = {"PK": f"doc#{DOC_KEY}", "SK": "none"}
OTHER_REVIEWER = "other-reviewer"


def _section(section_id: str, extraction_result_uri: Optional[str] = None):
    return MagicMock(section_id=section_id, extraction_result_uri=extraction_result_uri)


def _document(sections, pending=None, completed=None):
    document = MagicMock()
    document.sections = sections
    document.hitl_sections_pending = pending or []
    document.hitl_sections_completed = completed or []
    document.hitl_status = None
    return document


@pytest.fixture
def mod(monkeypatch):
    """Import the resolver hermetically, exactly as the sibling suite does."""
    for name in ("idp_common", "idp_common.docs_service", "idp_common.models"):
        sys.modules.setdefault(name, MagicMock())
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("TRACKING_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("OUTPUT_BUCKET", "test-output")
    sys.modules.pop("index", None)
    return importlib.import_module("index")


class _CompetingWriteTable:
    """A real table with one competing write forced between read and write.

    ``get_item`` reads through to the real table and *then* lets the other
    reviewer commit, so the caller holds a state that is one entry stale by the
    time it writes. Only the first read is intercepted, so a retry loop -- if one
    is ever added to this handler -- reads the settled state instead of looping.
    """

    def __init__(self, table: Any, competitor: Optional[Callable[[Any], None]]):
        self._table = table
        self._competitor = competitor
        self.reads = 0
        self.writes = 0

    def get_item(self, **kwargs) -> Dict[str, Any]:
        response = self._table.get_item(**kwargs)
        self.reads += 1
        if self.reads == 1 and self._competitor is not None:
            self._competitor(self._table)
        return response

    def update_item(self, **kwargs):
        self.writes += 1
        return self._table.update_item(**kwargs)


def _append_other_reviewers_entry(table: Any) -> None:
    """The competing writer.

    Deliberately performs the atomic append rather than the broken whole-array
    write: the competitor stands in for another copy of this same Lambda, so
    modelling it as already fixed is what isolates the handler's own write as the
    only thing under test. Were the competitor broken too, a green result would
    not say which of the two writes was correct.
    """
    table.update_item(
        Key=ITEM_KEY,
        UpdateExpression=(
            "SET HITLReviewHistory = list_append("
            "if_not_exists(HITLReviewHistory, :empty), :entry)"
        ),
        ExpressionAttributeValues={
            ":empty": [],
            ":entry": [{"sectionId": "sec-other", "reviewedBy": OTHER_REVIEWER}],
        },
    )


def _reviewers(history: List[Dict[str, Any]]) -> set:
    return {entry.get("reviewedBy") for entry in history}


class _Harness:
    """A real moto tracking table plus the wrapper the handler is handed."""

    def __init__(
        self,
        seed: Optional[Dict[str, Any]] = None,
        competitor: Optional[Callable[[Any], None]] = None,
    ):
        self.resource = boto3.resource("dynamodb", region_name="us-east-1")
        self.resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        self.real = self.resource.Table(TABLE_NAME)
        self.real.put_item(Item={**ITEM_KEY, **(seed or {})})
        self.wrapper = _CompetingWriteTable(self.real, competitor)
        self.facade = MagicMock()
        self.facade.Table.return_value = self.wrapper
        self.facade.meta = self.resource.meta

    def history(self) -> List[Dict[str, Any]]:
        item = self.real.get_item(Key=ITEM_KEY).get("Item", {})
        return list(item.get("HITLReviewHistory", []))

    def item(self) -> Dict[str, Any]:
        return dict(self.real.get_item(Key=ITEM_KEY).get("Item", {}))


@pytest.mark.unit
class TestCompleteSectionReviewHistoryAppend:
    """``complete_section_review``'s append to ``HITLReviewHistory``."""

    @staticmethod
    def _run(mod, harness: _Harness) -> None:
        service = MagicMock()
        service.get_document.return_value = _document(
            sections=[_section("sec-1", "s3://bucket/sec1.json"), _section("sec-2")],
            pending=["sec-1", "sec-2"],
            completed=[],
        )
        with (
            patch.object(mod, "create_document_service", return_value=service),
            patch.object(mod, "dynamodb", harness.facade),
            patch.object(mod, "trigger_reprocessing"),
            patch.object(mod, "confirm_test_set_baseline_reviewed"),
            patch.object(mod, "build_document_response", return_value={}),
        ):
            mod.complete_section_review("doc/key.pdf", "sec-1", username="rev-1")

    @mock_aws
    def test_an_overlapping_reviewers_entry_is_not_overwritten(self, mod):
        harness = _Harness(competitor=_append_other_reviewers_entry)
        self._run(mod, harness)
        # Both reviewers' records are stored. The broken write stored only
        # `rev-1`, because it wrote back the array it read before the other
        # reviewer committed.
        assert _reviewers(harness.history()) == {OTHER_REVIEWER, "rev-1"}
        # The overlap is asserted, not assumed: without it this test would pass
        # against the defect.
        assert harness.wrapper.reads >= 1

    @mock_aws
    def test_an_existing_history_is_preserved_through_the_overlap(self, mod):
        harness = _Harness(
            seed={"HITLReviewHistory": [{"sectionId": "sec-0", "reviewedBy": "rev-0"}]},
            competitor=_append_other_reviewers_entry,
        )
        self._run(mod, harness)
        history = harness.history()
        assert _reviewers(history) == {"rev-0", OTHER_REVIEWER, "rev-1"}
        # Pins the operand order of `list_append`, which nothing else here does:
        # swapping it prepends instead of appending, leaving the stored array in
        # reverse chronological order with every content assertion still green.
        assert history[0]["reviewedBy"] == "rev-0"
        assert history[-1]["reviewedBy"] == "rev-1"

    @mock_aws
    def test_the_first_entry_lands_when_the_attribute_is_absent(self, mod):
        # `if_not_exists` is what makes `list_append` work on a document nobody
        # has reviewed yet. Without it DynamoDB rejects the whole update.
        harness = _Harness()
        self._run(mod, harness)
        history = harness.history()
        assert len(history) == 1
        assert history[0]["sectionId"] == "sec-1"
        assert history[0]["reviewedBy"] == "rev-1"

    @mock_aws
    def test_finishing_the_last_section_still_sets_the_completion_flag(self, mod):
        # The append shares one `UpdateExpression` with `HITLCompleted` and the
        # `REMOVE`, so a malformed append would take those with it.
        harness = _Harness(seed={"HITLPendingReview": "true"})
        service = MagicMock()
        service.get_document.return_value = _document(
            sections=[_section("sec-1", "s3://bucket/sec1.json")],
            pending=["sec-1"],
            completed=[],
        )
        with (
            patch.object(mod, "create_document_service", return_value=service),
            patch.object(mod, "dynamodb", harness.facade),
            patch.object(mod, "trigger_reprocessing") as reprocess,
            patch.object(mod, "confirm_test_set_baseline_reviewed"),
            patch.object(mod, "build_document_response", return_value={}),
        ):
            mod.complete_section_review("doc/key.pdf", "sec-1", username="rev-1")
        item = harness.item()
        assert item["HITLCompleted"] is True
        assert "HITLPendingReview" not in item
        reprocess.assert_called_once_with("doc/key.pdf")


@pytest.mark.unit
class TestSkipAllHistoryAppend:
    """``skip_all_sections_review``'s append to the same attribute."""

    @staticmethod
    def _run(mod, harness: _Harness) -> None:
        service = MagicMock()
        service.get_document.return_value = _document(
            sections=[_section("sec-1"), _section("sec-2")],
            completed=[],
        )
        with (
            patch.object(mod, "create_document_service", return_value=service),
            patch.object(mod, "dynamodb", harness.facade),
            patch.object(mod, "trigger_reprocessing"),
            patch.object(mod, "build_document_response", return_value={}),
        ):
            mod.skip_all_sections_review("doc/key.pdf", username="admin")

    @mock_aws
    def test_a_reviewers_entry_is_not_overwritten_by_skip_all(self, mod):
        harness = _Harness(competitor=_append_other_reviewers_entry)
        self._run(mod, harness)
        assert _reviewers(harness.history()) == {OTHER_REVIEWER, "admin"}

    @mock_aws
    def test_the_rest_of_the_skip_all_write_is_unchanged(self, mod):
        # `HITLSectionsSkipped` is still a whole-list write, and it is a residual
        # rather than a safe value: what diverges between two callers is the
        # `completed` set, which arrives from the document model and not from the
        # read above, so every writer of it goes through the one unconditional
        # whole-document write this PR does not touch. The call site states the
        # residual in full. What is pinned here is only that the clauses sharing
        # the `UpdateExpression` with the append still land, so a later edit to it
        # cannot drop one unnoticed.
        harness = _Harness(
            seed={"HITLPendingReview": "true", "HITLSectionsSkipped": ["sec-0"]}
        )
        self._run(mod, harness)
        item = harness.item()
        assert item["HITLStatus"] == "Review Skipped"
        assert item["HITLCompleted"] is True
        assert item["HITLSectionsPending"] == []
        assert sorted(item["HITLSectionsSkipped"]) == ["sec-0", "sec-1", "sec-2"]
        assert item["HITLReviewedBy"] == "admin"
        assert "HITLPendingReview" not in item

    @mock_aws
    def test_the_first_entry_lands_when_the_attribute_is_absent(self, mod):
        harness = _Harness()
        self._run(mod, harness)
        history = harness.history()
        assert len(history) == 1
        assert history[0]["action"] == "skip_all"
