# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Send to review (``send_to_review.SendToReviewService``) on moto."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest

from idp_common.review_queues import tracker
from idp_common.review_queues.bulk_models import (
    BulkOutcome,
    ReviewCaller,
    SendToReviewRequest,
)
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewValidationError
from idp_common.review_queues.models import CallerContext
from idp_common.review_queues.queue_defs import DEFAULT_QUEUE_ID, QueueDefinition
from idp_common.review_queues.send_to_review import SENT_EVENT, SendToReviewService
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore

from ._table import REGION

USAGE = {"OCR/textract/analyze_document": {"pages": 2}}
REV = ReviewCaller(
    reviewer_key="rev@x", username="rev", email="rev@x", groups=frozenset({"Reviewer"})
)
SCOPED = ReviewCaller(
    reviewer_key="sc@x",
    email="sc@x",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["claims"],
)


def _document(key):
    return SimpleNamespace(
        input_key=key,
        config_version="claims",
        config_revision=3,
        sections=[SimpleNamespace(section_id="1"), SimpleNamespace(section_id="2")],
        metering=dict(USAGE),
        metering_baseline={},
    )


@pytest.fixture
def env(review_table):
    ddb = boto3.resource("dynamodb", region_name=REGION)
    ddb.create_table(
        TableName="tracking",
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
    store = ReviewStore(review_table, region=REGION)
    store.queue_defs.put(QueueDefinition(id=DEFAULT_QUEUE_ID), create=True)
    s3 = MagicMock()
    notifier = MagicMock()
    sender = SendToReviewService(
        store,
        ddb.Table("tracking"),
        _document,
        s3,
        "out",
        notifier=notifier,
        retention_days=30,
        sync_limit=lambda: 5,
    )
    return SimpleNamespace(
        store=store, table=ddb.Table("tracking"), s3=s3, sender=sender
    )


def _row(env, key, **attrs):
    env.table.put_item(
        Item={
            "PK": f"doc#{key}",
            "SK": "none",
            "ConfigVersion": "claims",
            "ObjectStatus": "COMPLETED",
            **attrs,
        }
    )


def _tracking(env, key):
    return env.table.get_item(Key={"PK": f"doc#{key}", "SK": "none"})["Item"]


def _send(env, *keys, caller=REV):
    return env.sender.send(
        caller,
        SendToReviewRequest(object_keys=list(keys), reason="Spot check", note="n"),
    )


@pytest.mark.unit
class TestSend:
    def test_a_finished_document_becomes_an_open_item(self, env):
        _row(env, "a.pdf", HITLStatus="Review Skipped", HITLCompleted=True)
        result = _send(env, "a.pdf")
        assert result.results[0].outcome is BulkOutcome.SENT
        item = env.store.get_item("a.pdf")
        assert (item.state, item.origin, item.queue) == (
            "OPEN",
            "manual",
            DEFAULT_QUEUE_ID,
        )
        assert item.sections == ["1", "2"] and item.sweep_at_ms is None
        assert json.loads(item.phase1_metering_json) == USAGE
        assert item.manual_request.reason == "Spot check"
        row = _tracking(env, "a.pdf")
        assert row["HITLStatus"] == "in_review" and "HITLCompleted" not in row
        assert row["ReviewCycleId"] == item.cycle_id
        assert [h.event for h in env.store.list_history("a.pdf", item.cycle_id)] == [
            SENT_EVENT
        ]
        env.s3.write_content.assert_called()

    def test_the_view_carries_the_send_reason(self, env):
        from idp_common.review_queues.service import item_view

        _row(env, "v.pdf")
        _send(env, "v.pdf")
        view = item_view(env.store.get_item("v.pdf")).dump()
        assert view["sentToReview"]["reason"] == "Spot check"

    def test_the_default_queue_off_refuses_every_key(self, env):
        env.store.queue_defs.put(
            QueueDefinition(id=DEFAULT_QUEUE_ID, enabled=False), create=False
        )
        _row(env, "off.pdf")
        result = _send(env, "off.pdf", "other.pdf")
        assert {r.outcome for r in result.results} == {BulkOutcome.REVIEW_QUEUE_OFF}

    @pytest.mark.parametrize(
        "attrs,outcome",
        [
            ({"ObjectStatus": "RUNNING"}, BulkOutcome.NOT_FINISHED_PROCESSING),
            ({"TestSetId": "ts"}, BulkOutcome.TEST_STUDIO_EXCLUDED),
            ({"HITLStatus": "Review Pending"}, BulkOutcome.ALREADY_IN_REVIEW),
            ({"HITLStatus": "in_review"}, BulkOutcome.ALREADY_IN_REVIEW),
        ],
    )
    def test_refusals(self, env, attrs, outcome):
        _row(env, "r.pdf", **attrs)
        assert _send(env, "r.pdf").results[0].outcome is outcome
        assert env.store.get_item("r.pdf") is None

    def test_out_of_scope_reads_like_an_unknown_key(self, env):
        _row(env, "o.pdf", ConfigVersion="other")
        outcomes = {
            r.outcome for r in _send(env, "o.pdf", "missing.pdf", caller=SCOPED).results
        }
        assert outcomes == {BulkOutcome.NOT_PERMITTED}

    def test_a_live_item_is_already_in_review(self, env):
        _row(env, "l.pdf")
        _send(env, "l.pdf")
        assert _send(env, "l.pdf").results[0].outcome is BulkOutcome.ALREADY_IN_REVIEW

    def test_more_keys_than_the_limit_is_a_bad_argument(self, env):
        with pytest.raises(ReviewValidationError):
            _send(env, *[f"{i}.pdf" for i in range(6)])

    def test_losing_the_tracking_race_deletes_the_item(self, env):
        _row(env, "race.pdf")
        real = env.sender._mark_tracking

        def _race(record):
            env.table.update_item(
                Key={"PK": "doc#race.pdf", "SK": "none"},
                UpdateExpression="SET HITLStatus = :s",
                ExpressionAttributeValues={":s": "Review Pending"},
            )
            return real(record)

        env.sender._mark_tracking = _race
        assert (
            _send(env, "race.pdf").results[0].outcome is BulkOutcome.ALREADY_IN_REVIEW
        )
        assert env.store.get_item("race.pdf") is None

    def test_a_publish_failure_is_abandoned_and_the_row_restored(self, env):
        _row(env, "p.pdf", HITLStatus="Review Completed", HITLCompleted=True)
        env.s3.write_content.side_effect = RuntimeError("s3 down")
        assert _send(env, "p.pdf").results[0].outcome is BulkOutcome.FAILED
        assert env.store.get_item("p.pdf") is None
        row = _tracking(env, "p.pdf")
        assert row["HITLStatus"] == "Review Completed" and row["HITLCompleted"] is True
        assert "ReviewCycleId" not in row

    def test_a_second_send_after_the_review_finished(self, env):
        _row(env, "twice.pdf")
        _send(env, "twice.pdf")
        item = env.store.get_item("twice.pdf")
        item.state = "COMPLETE"
        env.store.replace_item(
            item.model_copy(update={"version": item.version + 1}),
            expected_version=item.version,
        )
        env.table.update_item(
            Key={"PK": "doc#twice.pdf", "SK": "none"},
            UpdateExpression="SET HITLStatus = :s",
            ExpressionAttributeValues={":s": "complete"},
        )
        assert _send(env, "twice.pdf").results[0].outcome is BulkOutcome.SENT
        again = env.store.get_item("twice.pdf")
        assert again.cycle_id != item.cycle_id and again.state == "OPEN"


@pytest.mark.unit
class TestFullCycle:
    def test_send_claim_complete_finalize_carries_the_usage_baseline(self, env):
        _row(env, "c.pdf")
        _send(env, "c.pdf")
        service = ReviewQueueService(
            env.store,
            QueueDirectory(env.store.queue_defs),
            notifier=MagicMock(),
            finalize_sender=MagicMock(),
            s3=MagicMock(),
            output_bucket="out",
        )
        caller = CallerContext(
            reviewer_key="rev@x", email="rev@x", groups=frozenset({"Reviewer"})
        )
        item = env.store.get_item("c.pdf")
        service.claim(caller, "c.pdf", item.cycle_id)
        service.complete(caller, "c.pdf", item.cycle_id)
        done = env.store.get_item("c.pdf")
        assert (done.state, done.disposition) == ("FINALIZING", "complete")
        service.finalize_sender.send.assert_called_once()
        ctx = tracker.load_context(
            "finalize", done.finalize_token, "c.pdf", None, env.store
        )
        assert ctx is not None and ctx.outcome == "complete"
        processed = SimpleNamespace(
            metering={"Summarization/x": {"t": 1}}, metering_baseline={}
        )
        tracking_doc, _ = tracker.apply_overrides(ctx, processed)
        assert tracking_doc.metering_baseline == USAGE


@pytest.mark.unit
class TestAbandon:
    def test_abandon_restores_a_row_that_had_no_status(self, env):
        _row(env, "x.pdf")
        _send(env, "x.pdf")
        env.sender.abandon(env.store.get_item("x.pdf"))
        row = _tracking(env, "x.pdf")
        assert "HITLStatus" not in row and "ReviewQueue" not in row
        assert env.store.get_item("x.pdf") is None
