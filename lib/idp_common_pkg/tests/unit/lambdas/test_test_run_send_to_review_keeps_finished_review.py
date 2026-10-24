# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sending a test run to review must not reopen a finished review (class of #1568).

``sendTestRunToReview`` (``nested/api-resolvers/src/lambda/test_runner``) walks a
run's documents and flips each one with confidence alerts to ``PendingReview``,
skipping a review that has already finished. It decided "finished" from a list of
four legacy spellings (``LEGACY_DONE``), so two stored states a real writer leaves
on a test-run document read as unfinished and were reopened:

* ``complete`` and ``rejected``, the outcomes a review queue records. ProcessResults
  routes a test-run document with alerts into a review queue like any other, so
  under review queues these are the ordinary finished states;
* ``COMPLETED``, the spelling the earlier SageMaker A2I flow stored. Test runs
  predate that flow's removal and their rows are kept for the stack's retention
  period, so a retained run can still carry it.

Each case seeds the stored state with the real writer (``DocumentDynamoDBService``
over a ``moto`` tracking table; ``tracker.apply_overrides`` for the queue outcomes;
a raw ``update_item`` for the A2I spelling, which no live writer stores), runs the
real resolver function, and reads ``HITLStatus`` back from the table. ``moto``
stands in for DynamoDB; the property measured is the resolver's branch, not
expression-language behaviour.

Still reopened, and pinned so a widening is a visible decision: every pending or
in-progress spelling, a document with no status, and ``in_review`` (a queue review
still under way).
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
    "queue in_review": ("queue", "in_review"),
    "IN_PROGRESS (A2I)": ("raw", "IN_PROGRESS"),
}


def test_every_declared_status_is_classified():
    """A status added to either vocabulary fails here until it is placed."""
    declared = {s.value for s in LegacyHitlStatus} | {s.value for s in ReviewStatus}
    assert declared  # an empty derivation would collect as a skip, not a failure
    seeded = {spec[1] for spec in (*KEPT.values(), *REOPENED.values()) if spec}
    assert declared <= seeded
    assert not set(KEPT) & set(REOPENED)


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
