# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which tracking rows the GSI attribute backfill marks ``HITLPendingReview``.

The backfill fills in the attribute on rows written before it existed. For every
status a review queue writes it must agree with the live writer,
``DocumentDynamoDBService``, which removes the attribute for all three; a queue
review that ended ``complete`` or ``rejected`` was read as still owed and marked
pending again (#1568's class). Rows with a finished legacy status are not marked.
Any other status on a triggered row, including the A2I flow's open spellings and a
missing status, is still marked pending, as it was before.

The queue rows are produced by the real ``DocumentDynamoDBService`` writing the
status the review-queue tracker sets (``ReviewStatus``) over a row carrying the
AppSync-era ``HITLTriggered`` flag, and the backfill worker runs its real scan
against moto, so the scan terminates on moto's own ``LastEvaluatedKey``.
"""

import importlib.util
import os
import sys
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_HANDLER_DIR = os.path.join(
    os.path.dirname(__file__), "../../../../../src/lambda/backfill_gsi_attributes"
)
TABLE = "backfill-hitl-pending"
OBJECT_KEY = "packet.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}
# Handler-local modules that other handler directories also ship under these names.
_SIBLINGS = ("log_sanitizer", "review_statuses")

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
}


def _load_worker():
    """Import the worker by path with its handler directory importable, then put
    back whatever copies of its sibling modules were loaded before."""
    previous = {name: sys.modules.pop(name, None) for name in _SIBLINGS}
    sys.path.insert(0, _HANDLER_DIR)
    try:
        spec = importlib.util.spec_from_file_location(
            "backfill_worker_hitl", os.path.join(_HANDLER_DIR, "index.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(_HANDLER_DIR)
        for name, mod in previous.items():
            sys.modules.pop(name, None)
            if mod is not None:
                sys.modules[name] = mod
    return module


class _Context:
    log_stream_name = "test"

    def get_remaining_time_in_millis(self):
        return 900_000


@pytest.fixture
def table():
    with patch.dict(os.environ, _ENV), mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName=TABLE,
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


def _legacy_row(table, **attributes):
    """A row from before HITLPendingReview existed: the AppSync-era writer set
    HITLTriggered, and nothing has written the pending marker yet."""
    table.put_item(
        Item={
            **DOC_KEY,
            "ObjectKey": OBJECT_KEY,
            "ItemType": "document",
            "ConfidenceAlertCount": 0,
            "HITLTriggered": True,
            **attributes,
        }
    )


def _backfill(table):
    worker = _load_worker()
    stats = worker.lambda_handler(
        {"tableName": TABLE, "segment": 0, "totalSegments": 1}, _Context()
    )
    assert stats["complete"] is True
    assert stats["processed"] == 1
    return table.get_item(Key=DOC_KEY)["Item"]


def _queue_statuses():
    from idp_common.review_queues.statuses import ReviewStatus

    values = [s.value for s in ReviewStatus]
    assert values  # an empty derivation would collect as a skip, not a failure
    return values


def _legacy_statuses(pending):
    """The declared legacy "still owed" or "finished" set. ``ReviewInProgress`` is
    in neither (no writer stores it) and is left to the open-review case below."""
    from idp_common.review_queues.statuses import LEGACY_DONE, LEGACY_PENDING

    values = sorted(s.value for s in (LEGACY_PENDING if pending else LEGACY_DONE))
    assert values
    return values


@pytest.mark.unit
@pytest.mark.parametrize("status", _queue_statuses())
def test_a_queue_status_is_never_marked_pending(table, status):
    """Every status the review-queue tracker sets, written by the live writer over a
    legacy row: complete and rejected are finished, in_review is worked from its
    queue item. The writer removes the marker for all three; the backfill must not
    restore it."""
    from idp_common.dynamodb.service import DocumentDynamoDBService
    from idp_common.models import Document

    _legacy_row(table, HITLStatus="PendingReview")
    service = DocumentDynamoDBService(table_name=TABLE)
    service.update_document(Document(input_key=OBJECT_KEY, hitl_status=status))
    if status != "in_review":
        # after_completion's write, as the tracker makes it on a finished cycle.
        service.update_review_attributes(
            OBJECT_KEY,
            {"HITLReviewedBy": "annotator@example.com", "ReviewDisposedAt": "t"},
        )
    seeded = table.get_item(Key=DOC_KEY)["Item"]
    assert seeded["HITLStatus"] == status
    assert "HITLPendingReview" not in seeded
    assert "HITLCompleted" not in seeded  # no queue writer records it

    assert "HITLPendingReview" not in _backfill(table)


@pytest.mark.unit
@pytest.mark.parametrize("status", _legacy_statuses(pending=False) + ["COMPLETED"])
def test_a_finished_legacy_review_is_not_marked_pending(table, status):
    """``COMPLETED`` is the earlier A2I flow's finished spelling."""
    _legacy_row(table, HITLStatus=status)
    assert "HITLPendingReview" not in _backfill(table)


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    _legacy_statuses(pending=True)
    # A legacy spelling no writer stores, the A2I flow's open spellings, and a
    # triggered row with no status yet: all read as still owed, as before.
    + ["ReviewInProgress", "IN_PROGRESS", "TRIGGERED", None],
)
def test_an_open_legacy_review_is_marked_pending(table, status):
    _legacy_row(table, **({} if status is None else {"HITLStatus": status}))
    assert _backfill(table)["HITLPendingReview"] == "true"


@pytest.mark.unit
def test_a_recorded_completion_is_not_marked_pending(table):
    _legacy_row(table, HITLStatus="PendingReview", HITLCompleted=True)
    assert "HITLPendingReview" not in _backfill(table)
