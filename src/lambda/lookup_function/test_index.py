# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the document status lookup function.

What is pinned here is the failure explanation: a document refused at ingest
(for example, outside every tenant's prefixes under strict tenant isolation) is
``FAILED`` with its reason only on the tracking record's ``ConfigAssignmentError``.
The lookup must return that reason as ``error``, the key the SDK and CLI read, or
those callers report the refusal as "Unknown error".
"""

import os
import sys
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
