# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the document status lookup function.

What is pinned here is the failure explanation: a document refused at ingest
(for example, outside every tenant's prefixes under strict tenant isolation) is
``FAILED`` with its reason only on the tracking record's ``ConfigAssignmentError``.
The lookup must return that reason as ``error``, the key the SDK and CLI read, or
those callers report the refusal as "Unknown error".
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import index  # noqa: E402

REFUSAL = (
    "Refused: this object is outside every tenant's prefixes, and this deployment "
    "runs in strict tenant isolation."
)


@pytest.fixture
def tracking_item():
    """Patch the tracking table so ``get_item`` returns the item under test."""
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "test-tracking"}),
        patch.object(index.boto3, "resource", return_value=resource),
    ):

        def set_item(item):
            table.get_item.return_value = {"Item": item} if item is not None else {}

        yield set_item


def _refused_item():
    return {
        "PK": "doc#batch/a.pdf",
        "SK": "none",
        "ObjectStatus": "FAILED",
        "InitialEventTime": "2026-11-13T14:44:38Z",
        "QueuedTime": "2026-11-13T14:44:41.331118+00:00",
        "ConfigSource": "rejected",
        "ConfigAssignmentError": REFUSAL,
    }


@pytest.mark.unit
@pytest.mark.parametrize("status_only", [True, False])
def test_a_refused_document_returns_its_refusal_reason(tracking_item, status_only):
    tracking_item(_refused_item())

    result = index.get_document_status("batch/a.pdf", status_only=status_only)

    assert result["status"] == "FAILED"
    assert result["error"] == REFUSAL
    assert result["failed_step"] == "QueueSender"


@pytest.mark.unit
def test_the_batch_handler_carries_the_reason_per_document(tracking_item):
    tracking_item(_refused_item())

    response = index.handler(
        {"object_keys": ["batch/a.pdf"], "status_only": True}, None
    )

    (doc,) = response["results"]
    assert doc["error"] == REFUSAL
    assert doc["failed_step"] == "QueueSender"


@pytest.mark.unit
def test_a_document_without_a_refusal_carries_no_error_key(tracking_item):
    """No reason recorded means no `error`: callers keep their own placeholder."""
    item = _refused_item()
    item["ObjectStatus"] = "COMPLETED"
    del item["ConfigAssignmentError"]
    tracking_item(item)

    result = index.get_document_status("batch/a.pdf", status_only=True)

    assert "error" not in result
    assert "failed_step" not in result


@pytest.mark.unit
def test_an_unknown_document_is_not_found(tracking_item):
    tracking_item(None)

    assert index.get_document_status("missing.pdf", status_only=True) == {
        "object_key": "missing.pdf",
        "status": "NOT_FOUND",
    }


@pytest.mark.unit
def test_durations_skip_stages_not_reached_without_logging_an_error(caplog):
    """A refused document has no workflow start; that is not an error.

    Every caller passes all four keys, with None for a stage not reached, so a
    key-presence check sent None into ``fromisoformat`` and the resulting
    exception discarded every duration and logged a traceback on each poll.
    """
    timestamps = {
        "InitialEventTime": "2026-11-13T14:44:38+00:00",
        "QueuedTime": "2026-11-13T14:44:41+00:00",
        "WorkflowStartTime": None,
        "CompletionTime": None,
    }

    assert index.calculate_durations(timestamps) == {}
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.unit
def test_durations_are_computed_for_the_stages_that_were_reached():
    timestamps = {
        "InitialEventTime": "2026-11-13T14:00:00+00:00",
        "QueuedTime": "2026-11-13T14:00:01+00:00",
        "WorkflowStartTime": "2026-11-13T14:00:03+00:00",
        "CompletionTime": None,
    }

    assert index.calculate_durations(timestamps) == {"queue": 2000}


def _completed_item(**extra):
    item = {
        "PK": "doc#batch/b.pdf",
        "SK": "none",
        "ObjectStatus": "COMPLETED",
        "InitialEventTime": "2026-11-13T14:44:38Z",
    }
    item.update(extra)
    return item


@pytest.mark.unit
@pytest.mark.parametrize("status_only", [True, False])
def test_caller_attributes_are_returned_json_serialisable(tracking_item, status_only):
    """#1851: DynamoDB hands back numbers as Decimal, which the Lambda response
    serialiser cannot encode, so they are converted on the way out."""
    import json
    from decimal import Decimal

    tracking_item(
        _completed_item(
            DocumentAttributes={
                "account_id": "ACME-0042",
                "amount": Decimal("12.5"),
                "count": Decimal("3"),
                "flags": [True, Decimal("1")],
            }
        )
    )

    result = index.get_document_status("batch/b.pdf", status_only=status_only)

    assert result["attributes"] == {
        "account_id": "ACME-0042",
        "amount": 12.5,
        "count": 3,
        "flags": [True, 1],
    }
    json.dumps(result)  # would raise on a Decimal
    # And the right native type: an int stays an int, a fraction a float.
    assert type(result["attributes"]["count"]) is int
    assert type(result["attributes"]["amount"]) is float


@pytest.mark.unit
def test_a_document_without_attributes_has_no_attributes_key(tracking_item):
    tracking_item(_completed_item())
    result = index.get_document_status("batch/b.pdf", status_only=True)
    assert "attributes" not in result


@pytest.mark.unit
@pytest.mark.parametrize("stored", ["not-a-map", ["a"], {}])
def test_a_malformed_or_empty_stored_set_is_not_returned(tracking_item, stored):
    tracking_item(_completed_item(DocumentAttributes=stored))
    assert "attributes" not in index.get_document_status(
        "batch/b.pdf", status_only=True
    )


@pytest.mark.unit
class TestReviewInfo:
    """The review block of a document the review queues routed."""

    def _item(self, **attributes):
        return {
            "PK": "doc#batch/a.pdf",
            "SK": "none",
            "HITLStatus": "in_review",
            "ReviewQueue": "high-value",
            "ReviewCycleId": "0" * 21,
            **attributes,
        }

    def test_it_names_the_queue_and_the_rule_that_placed_the_document(self):
        routed_by = {
            "tableId": "review-routing",
            "ruleId": "high-value",
            "ruleLabel": "Invoices over 10,000",
        }

        review = index.review_info(
            self._item(ReviewQueueName="High value", ReviewRoutedBy=routed_by)
        )

        assert review["queue"] == "high-value"
        assert review["queueName"] == "High value"
        assert review["routedBy"] == routed_by

    def test_the_reason_and_the_failed_policy_rules_pass_through(self):
        """The tracker writes them into the map, so the SDK and the CLI have them
        with the status and make no second call."""
        stored = {
            "tableId": "review-routing",
            "ruleId": "high-value",
            "ruleLabel": "Invoices over 10,000",
            "reason": "Total 12000 is over 10000",
            "policyFailures": [
                {
                    "ruleId": "lending.max_ltv",
                    "policy": "lending",
                    "name": "max_ltv",
                    "reason": "LTV is 92%, over the 80% limit.",
                    # DynamoDB returns every number as a Decimal.
                    "pages": [Decimal("3"), Decimal("4")],
                }
            ],
            "policyFailureCount": Decimal("14"),
        }

        routed_by = index.review_info(self._item(ReviewRoutedBy=stored))["routedBy"]

        assert routed_by == {
            "tableId": "review-routing",
            "ruleId": "high-value",
            "ruleLabel": "Invoices over 10,000",
            "reason": "Total 12000 is over 10000",
            "policyFailures": [
                {
                    "ruleId": "lending.max_ltv",
                    "policy": "lending",
                    "name": "max_ltv",
                    "reason": "LTV is 92%, over the 80% limit.",
                    "pages": [3, 4],
                }
            ],
            "policyFailureCount": 14,
        }
        # Whole numbers, as JSON carries them to the caller, and nothing a JSON
        # encoder refuses.
        assert type(routed_by["policyFailureCount"]) is int
        assert [type(p) for p in routed_by["policyFailures"][0]["pages"]] == [int, int]
        assert json.loads(json.dumps(routed_by)) == routed_by

    def test_the_whole_status_of_a_routed_document_is_json(self, tracking_item):
        """The handler's answer is serialised as it is returned."""
        tracking_item(
            self._item(
                ObjectStatus="HITL_IN_PROGRESS",
                ReviewRoutedBy={
                    "ruleId": "r1",
                    "policyFailures": [{"ruleId": "p.a", "pages": [Decimal("2")]}],
                    "policyFailureCount": Decimal("1"),
                },
            )
        )

        status = index.get_document_status("batch/a.pdf", status_only=True)

        assert json.loads(json.dumps(status))["review"]["routedBy"] == {
            "ruleId": "r1",
            "policyFailures": [{"ruleId": "p.a", "pages": [2]}],
            "policyFailureCount": 1,
        }

    def test_a_document_no_rule_placed_has_neither(self):
        review = index.review_info(self._item())

        assert review["queueName"] is None
        assert review["routedBy"] is None
        assert review["status"] == "in_review"

    def test_a_document_that_was_never_routed_has_no_review_block(self):
        assert index.review_info({"PK": "doc#batch/a.pdf", "SK": "none"}) is None
