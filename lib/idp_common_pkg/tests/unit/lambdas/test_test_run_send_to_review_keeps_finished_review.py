# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sending a test run to review must not reopen a finished review (class of #1568).

``sendTestRunToReview`` (``nested/api-resolvers/src/lambda/test_runner``) walks a
run's documents and flips each one with confidence alerts to ``PendingReview``,
skipping a review that has already finished. It decided "finished" from a list of
four legacy spellings (``LEGACY_DONE``), so ``COMPLETED`` -- the spelling the
earlier SageMaker A2I flow stored -- read as unfinished and the review was reopened.
Test runs predate that flow's removal and their rows are kept for the stack's
retention period, so a retained run can still carry it.

The resolver now applies the document lists' finished rule, which also counts a
review queue's ``complete`` and ``rejected`` as finished. Current Test Studio
documents are never queue-routed (``routing.review_mode`` reads them as legacy), so
those two are reachable only on a test-run row copied before the copier stamped
Test Studio provenance; they are pinned here so the rule stays the same as the
lists'.

Each case seeds the stored state with the real writer (``DocumentDynamoDBService``
over a ``moto`` tracking table; ``tracker.apply_overrides`` for the queue outcomes;
a raw ``update_item`` for the A2I spelling, which no live writer stores), runs the
real resolver function, and reads ``HITLStatus`` back from the table. ``moto``
stands in for DynamoDB. The resolver's write is conditional on the status it read,
and the tests that depend on that condition measure moto's evaluation of it, not
DynamoDB's.

Still reopened, and pinned so a widening is a visible decision: every pending or
in-progress legacy spelling, and a document with no status.

Never touched: ``in_review``, a queue review still under way. Such a row is
reachable only on a test-run document copied before the copier stamped Test Studio
provenance and then reprocessed with the Default queue on. Writing the legacy
``PendingReview`` (and ``TestSetId``) over it left the document in both flows, so
the resolver skips it, counts it in ``skippedCount`` and leaves the row as the
queue wrote it -- skipped rather than refused, because one queue-managed document
must not fail the rest of the run. The write is also conditional on the status the
resolver read, so a review a queue opens between the read and the write is not
overwritten either.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Section, Status
from idp_common.review_queues import tracker
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.statuses import (
    LegacyHitlStatus,
    QueueItemState,
    ReviewStatus,
)
from tests.unit.lambdas._lambda_loader import REPO_ROOT, load_lambda

test_runner_index = load_lambda(
    "test_runner_send_to_review_index",
    "nested/api-resolvers/src/lambda/test_runner",
    patches={"boto3.client": lambda *a, **k: None},
)

REGION = "us-east-1"
TRACKING = "send-to-review-tracking-table"
RUN_ID = "set-a-20261024-120000"
FILE = "inv.pdf"
OBJECT_KEY = f"{RUN_ID}/{FILE}"
PENDING = LegacyHitlStatus.PENDING_REVIEW.value
ALERT = {
    "attribute_name": "InvoiceTotal",
    "confidence": 0.62,
    "confidence_threshold": 0.8,
}

#: Stored state (how it was written) -> whether ``sendTestRunToReview`` keeps it.
#: Written out rather than derived, so the test does not share the code's reading.
KEPT = {
    "Completed": ("legacy", LegacyHitlStatus.COMPLETED.value),
    "Review Completed": ("legacy", LegacyHitlStatus.REVIEW_COMPLETED.value),
    "Skipped": ("legacy", LegacyHitlStatus.SKIPPED.value),
    "Review Skipped": ("legacy", LegacyHitlStatus.REVIEW_SKIPPED.value),
    "queue complete": ("queue", "complete"),
    "queue rejected": ("queue", "rejected"),
    "COMPLETED (A2I)": ("raw", "COMPLETED"),
}
REOPENED = {
    "none": None,
    "PendingReview": ("legacy", LegacyHitlStatus.PENDING_REVIEW.value),
    "Review Pending": ("legacy", LegacyHitlStatus.REVIEW_PENDING.value),
    "InProgress": ("legacy", LegacyHitlStatus.IN_PROGRESS.value),
    "ReviewInProgress": ("legacy", LegacyHitlStatus.REVIEW_IN_PROGRESS.value),
    "IN_PROGRESS (A2I)": ("raw", "IN_PROGRESS"),
    "empty string": ("raw", ""),
}
#: Queue-managed and still under review: left exactly as the queue wrote it.
QUEUE_MANAGED = {
    "queue in_review": ("queue", "in_review"),
}


def test_every_declared_status_is_classified():
    """A status added to either vocabulary fails here until it is placed."""
    declared = {s.value for s in LegacyHitlStatus} | {s.value for s in ReviewStatus}
    assert declared  # an empty derivation would collect as a skip, not a failure
    groups = (KEPT, REOPENED, QUEUE_MANAGED)
    seeded = {spec[1] for group in groups for spec in group.values() if spec}
    assert declared <= seeded
    names = [name for group in groups for name in group]
    assert len(names) == len(set(names))


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("TRACKING_TABLE", TRACKING)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.create_table(
            TableName=TRACKING,
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
        # The run row as the file copier leaves it: ``Files`` lists the run's
        # documents by name under the run's prefix.
        resource.Table(TRACKING).put_item(
            Item={
                "PK": f"testrun#{RUN_ID}",
                "SK": "metadata",
                "TestSetId": "set-a",
                "Files": [FILE],
            }
        )
        with patch.object(test_runner_index, "dynamodb", resource):
            yield SimpleNamespace(
                service=DocumentDynamoDBService(
                    dynamodb_client=DynamoDBClient(table_name=TRACKING, region=REGION)
                )
            )


def _seed(service, spec):
    """Write the test-run document the way its real writer does."""
    doc = Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.COMPLETED,
        num_pages=1,
        sections=[
            Section(
                section_id="1",
                classification="invoice",
                page_ids=["1"],
                confidence_threshold_alerts=[dict(ALERT)],
            )
        ],
    )
    # ProcessResults stores the document-wide count the same way.
    doc.confidence_alert_count = sum(
        len(section.confidence_threshold_alerts or []) for section in doc.sections
    )
    if spec is not None and spec[0] == "legacy":
        doc.hitl_status = spec[1]
    elif spec is not None and spec[0] == "queue":
        outcome = spec[1]
        held = outcome == "in_review"
        item = ReviewItemRecord(
            doc_key=OBJECT_KEY,
            cycle_id="c1",
            profile="default",
            queue="default",
            state=(
                QueueItemState.HOLDING.value
                if held
                else QueueItemState.FINALIZING.value
            ),
            enqueued_at="2026-10-24T00:00:00Z",
            enqueued_at_ms=0,
            disposition=None if held else outcome,
        )
        ctx = (
            tracker.TrackerReviewContext(kind="hold", item=item)
            if held
            else tracker.TrackerReviewContext(
                kind="finalize", item=item, token="t", outcome=outcome
            )
        )
        doc, _ = tracker.apply_overrides(ctx, doc)
    service.update_document(doc)
    if spec is not None and spec[0] == "raw":
        # No live writer stores this spelling any more; retained rows still do.
        boto3.client("dynamodb", region_name=REGION).update_item(
            TableName=TRACKING,
            Key={"PK": {"S": f"doc#{OBJECT_KEY}"}, "SK": {"S": "none"}},
            UpdateExpression="SET HITLStatus = :s",
            ExpressionAttributeValues={":s": {"S": spec[1]}},
        )
    stored = _stored()
    # The seeded row is what the case says it is, and it does carry alerts, so a
    # kept case is not explained by the alert-count skip.
    assert stored.get("HITLStatus") == (None if spec is None else spec[1])
    assert int(stored["ConfidenceAlertCount"]) > 0


def _stored():
    return (
        boto3.resource("dynamodb", region_name=REGION)
        .Table(TRACKING)
        .get_item(Key={"PK": f"doc#{OBJECT_KEY}", "SK": "none"})["Item"]
    )


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(KEPT))
def test_a_finished_review_is_not_reopened(aws, case):
    spec = KEPT[case]
    _seed(aws.service, spec)
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (0, 1)
    assert _stored().get("HITLStatus") == spec[1]


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(REOPENED))
def test_a_review_still_owed_is_sent_to_review(aws, case):
    _seed(aws.service, REOPENED[case])
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (1, 0)
    row = _stored()
    assert row.get("HITLStatus") == PENDING
    assert row.get("HITLTriggered") is True
    assert row.get("TestSetId") == "set-a"


@pytest.mark.unit
def test_runs_against_the_resolver_in_this_checkout():
    expected = REPO_ROOT / "nested/api-resolvers/src/lambda/test_runner/index.py"
    assert os.path.samefile(str(test_runner_index.__file__), expected)


@pytest.mark.unit
def test_a_document_without_alerts_is_not_sent_to_review(aws):
    _seed(aws.service, None)
    boto3.resource("dynamodb", region_name=REGION).Table(TRACKING).update_item(
        Key={"PK": f"doc#{OBJECT_KEY}", "SK": "none"},
        UpdateExpression="SET ConfidenceAlertCount = :z",
        ExpressionAttributeValues={":z": 0},
    )
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (0, 1)
    assert _stored().get("HITLStatus") is None


@pytest.mark.unit
def test_a_listed_document_with_no_row_is_skipped(aws):
    """A file the run lists but the pipeline never tracked is counted as skipped."""
    _seed(aws.service, None)
    boto3.resource("dynamodb", region_name=REGION).Table(TRACKING).update_item(
        Key={"PK": f"testrun#{RUN_ID}", "SK": "metadata"},
        UpdateExpression="SET Files = :f",
        ExpressionAttributeValues={":f": ["never-tracked.pdf", FILE]},
    )
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (1, 1)
    assert _stored().get("HITLStatus") == PENDING


@pytest.mark.unit
@pytest.mark.parametrize("case", sorted(QUEUE_MANAGED))
def test_a_queue_review_in_progress_is_left_to_the_queue(aws, case, caplog):
    spec = QUEUE_MANAGED[case]
    _seed(aws.service, spec)
    before = _stored()
    with caplog.at_level("INFO", logger=test_runner_index.logger.name):
        result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (0, 1)
    assert "queue_managed=1 changed_during_send=0" in caplog.text
    # Not one attribute changed: no PendingReview, no HITLTriggered, no TestSetId
    # (which would make routing read the document as Test Studio work).
    assert _stored() == before


@pytest.mark.unit
def test_one_queue_managed_document_does_not_stop_the_rest_of_the_run(aws):
    """The run's other documents are still sent to review (skip, not refuse)."""
    _seed(aws.service, ("queue", "in_review"))
    other = f"{RUN_ID}/other.pdf"
    table = boto3.resource("dynamodb", region_name=REGION).Table(TRACKING)
    owed = dict(_stored(), PK=f"doc#{other}", HITLStatus=REOPENED["PendingReview"][1])
    table.put_item(Item=owed)
    table.update_item(
        Key={"PK": f"testrun#{RUN_ID}", "SK": "metadata"},
        UpdateExpression="SET Files = :f",
        ExpressionAttributeValues={":f": [FILE, "other.pdf"]},
    )
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (1, 1)
    assert _stored().get("HITLStatus") == "in_review"
    row = table.get_item(Key={"PK": f"doc#{other}", "SK": "none"})["Item"]
    assert (row.get("HITLStatus"), row.get("HITLTriggered")) == (PENDING, True)


class _QueueOpensAfterTheRead:
    """A resource whose table lets a queue open a review after the resolver reads
    the document row and before it writes, so the read-time check alone cannot
    see it."""

    def __init__(self, resource, service):
        self._resource, self._service = resource, service
        self.opened = 0

    def Table(self, name):  # noqa: N802 - mirrors boto3's resource API
        real = self._resource.Table(name)
        outer = self

        class _Table:
            def __getattr__(self, attr):
                return getattr(real, attr)

            def get_item(self, **kwargs):
                response = real.get_item(**kwargs)
                if kwargs["Key"]["PK"].startswith("doc#") and not outer.opened:
                    outer.opened += 1
                    _seed(outer._service, ("queue", "in_review"))
                return response

        return _Table()


@pytest.mark.unit
def test_a_queue_review_opened_after_the_read_is_not_overwritten(aws, caplog):
    _seed(aws.service, REOPENED["PendingReview"])
    racing = _QueueOpensAfterTheRead(
        boto3.resource("dynamodb", region_name=REGION), aws.service
    )
    with (
        patch.object(test_runner_index, "dynamodb", racing),
        caplog.at_level("INFO", logger=test_runner_index.logger.name),
    ):
        result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert "queue_managed=0 changed_during_send=1" in caplog.text
    assert racing.opened == 1
    assert (result["queuedCount"], result["skippedCount"]) == (0, 1)
    assert _stored().get("HITLStatus") == "in_review"
    assert "TestSetId" not in _stored()


@pytest.mark.unit
def test_a_failed_write_that_is_not_the_condition_still_fails_the_call(aws):
    """Only the status-changed refusal is a skip; any other error propagates."""
    from botocore.exceptions import ClientError

    _seed(aws.service, REOPENED["PendingReview"])
    real = boto3.resource("dynamodb", region_name=REGION)

    class _Throttled:
        def Table(self, name):  # noqa: N802 - mirrors boto3's resource API
            table = real.Table(name)

            class _T:
                def __getattr__(self, attr):
                    return getattr(table, attr)

                def update_item(self, **kwargs):
                    raise ClientError(
                        {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                        "UpdateItem",
                    )

            return _T()

    with patch.object(test_runner_index, "dynamodb", _Throttled()):
        with pytest.raises(ClientError):
            test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})


@pytest.mark.unit
def test_a_status_stored_as_null_is_still_sent_to_review(aws):
    """No current writer stores a NULL ``HITLStatus``; boto3 reads one as ``None``
    like an absent attribute, so the condition must accept both."""
    _seed(aws.service, None)
    boto3.client("dynamodb", region_name=REGION).update_item(
        TableName=TRACKING,
        Key={"PK": {"S": f"doc#{OBJECT_KEY}"}, "SK": {"S": "none"}},
        UpdateExpression="SET HITLStatus = :n",
        ExpressionAttributeValues={":n": {"NULL": True}},
    )
    result = test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    assert (result["queuedCount"], result["skippedCount"]) == (1, 0)
    assert _stored().get("HITLStatus") == PENDING


@pytest.mark.unit
def test_the_document_read_is_strongly_consistent(aws):
    """moto is always consistent, so this is asserted on the call: an eventually
    consistent read could fail the conditional write and skip an owed review."""
    _seed(aws.service, REOPENED["PendingReview"])
    real = boto3.resource("dynamodb", region_name=REGION)
    reads = []

    class _Recording:
        def Table(self, name):  # noqa: N802 - mirrors boto3's resource API
            table = real.Table(name)

            class _T:
                def __getattr__(self, attr):
                    return getattr(table, attr)

                def get_item(self, **kwargs):
                    reads.append(kwargs)
                    return table.get_item(**kwargs)

            return _T()

    with patch.object(test_runner_index, "dynamodb", _Recording()):
        test_runner_index.send_test_run_to_review({"testRunId": RUN_ID})
    doc_reads = [r for r in reads if r["Key"]["PK"].startswith("doc#")]
    assert doc_reads
    assert all(r.get("ConsistentRead") is True for r in doc_reads)
