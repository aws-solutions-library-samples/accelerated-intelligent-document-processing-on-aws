# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``review_skip.skip_document`` end to end on moto (HITLTable + tracking table)."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest

from idp_common.review_queues.bulk_models import BulkOutcome, ReviewCaller
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ResubmitError
from idp_common.review_queues.models import ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.review_skip import SkipDeps, skip_document
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.utils.execution_names import resubmit_token

from ._table import REGION

REASON = "Duplicate document"
CYCLE = "0" * 21
ADMIN = ReviewCaller(
    reviewer_key="admin@x", email="admin@x", groups=frozenset({"Admin"}), is_admin=True
)
REV = ReviewCaller(
    reviewer_key="rev@x", username="rev", email="rev@x", groups=frozenset({"Reviewer"})
)
SCOPED = ReviewCaller(
    reviewer_key="sc@x",
    username="sc",
    email="sc@x",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["claims"],
)


@pytest.fixture
def deps(review_table):
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
    store.queue_defs.put(QueueDefinition(id="claims", priority=70), create=True)
    service = ReviewQueueService(
        store,
        QueueDirectory(store.queue_defs),
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
    )
    document = SimpleNamespace(
        hitl_sections_completed=[], sections=[SimpleNamespace(section_id="1")]
    )
    return SkipDeps(
        tracking_table=ddb.Table("tracking"),
        get_document=lambda _k: document,
        review_service=service,
        store=store,
        resubmitter=MagicMock(),
    )


def _row(deps, key, **attrs):
    deps.tracking_table.put_item(
        Item={"PK": f"doc#{key}", "SK": "none", "ConfigVersion": "claims", **attrs}
    )


def _item(deps, key, state="OPEN", owner=None):
    now = int(time.time() * 1000)
    fields = dict(
        doc_key=key,
        cycle_id=CYCLE,
        profile="claims",
        queue="claims",
        state=state,
        enqueued_at="2026-01-01T00:00:00.000Z",
        enqueued_at_ms=now,
        sections=["1"],
        version=1,
    )
    if owner:
        fields.update(
            owner_key=owner.reviewer_key,
            owner_email=owner.email,
            claimed_at_ms=now,
            lease_expires_at_ms=now + 3_600_000,
        )
    deps.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))


def _status(deps, key):
    return deps.tracking_table.get_item(Key={"PK": f"doc#{key}", "SK": "none"})[
        "Item"
    ].get("HITLStatus")


@pytest.mark.unit
class TestSkipDocument:
    def test_a_queue_item_is_skipped_through_the_finalize_path(self, deps):
        _row(deps, "q.pdf", HITLStatus="in_review")
        _item(deps, "q.pdf")
        result = skip_document(deps, REV, "q.pdf", reason=REASON, job_id="brj-1")
        assert (result.outcome, result.kind) == (BulkOutcome.SKIPPED, "queue")
        stored = deps.store.get_item("q.pdf")
        assert (stored.state, stored.disposition) == ("FINALIZING", "skipped")
        deps.resubmitter.resubmit.assert_not_called()

    def test_a_legacy_review_is_skipped_and_resubmitted_with_the_job_token(self, deps):
        _row(deps, "l.pdf", HITLStatus="Review Pending")
        result = skip_document(deps, REV, "l.pdf", reason=REASON, job_id="brj-1")
        assert (result.outcome, result.kind) == (BulkOutcome.SKIPPED, "legacy")
        assert _status(deps, "l.pdf") == "Review Skipped"
        deps.resubmitter.resubmit.assert_called_once_with(
            "l.pdf", idempotency_token=resubmit_token("brj-1", "l.pdf")
        )

    def test_unticked_resubmit_sends_nothing(self, deps):
        _row(deps, "n.pdf", HITLStatus="Review Pending")
        result = skip_document(deps, REV, "n.pdf", reason=REASON, resubmit=False)
        assert (result.outcome, result.detail) == (
            BulkOutcome.SKIPPED,
            "resubmit_skipped",
        )
        deps.resubmitter.resubmit.assert_not_called()

    def test_a_failed_resubmit_is_reported_with_the_skip_kept(self, deps):
        deps.resubmitter.resubmit.side_effect = ResubmitError("sqs down")
        _row(deps, "f.pdf", HITLStatus="Review Pending")
        result = skip_document(deps, REV, "f.pdf", reason=REASON)
        assert result.outcome is BulkOutcome.RESUBMIT_FAILED
        assert _status(deps, "f.pdf") == "Review Skipped"

    def test_a_retried_step_repeats_only_the_resubmit(self, deps):
        _row(deps, "r.pdf", HITLStatus="Review Pending")
        skip_document(deps, REV, "r.pdf", reason=REASON, job_id="brj-9")
        again = skip_document(deps, REV, "r.pdf", reason=REASON, job_id="brj-9")
        assert again.outcome is BulkOutcome.SKIPPED
        row = deps.tracking_table.get_item(Key={"PK": "doc#r.pdf", "SK": "none"})[
            "Item"
        ]
        assert len(row["HITLReviewHistory"]) == 1
        tokens = {
            c.kwargs["idempotency_token"]
            for c in deps.resubmitter.resubmit.call_args_list
        }
        assert tokens == {resubmit_token("brj-9", "r.pdf")}

    def test_out_of_scope_reads_like_an_unknown_key(self, deps):
        _row(deps, "o.pdf", HITLStatus="Review Pending", ConfigVersion="other")
        assert (
            skip_document(deps, SCOPED, "o.pdf", reason=REASON).outcome
            == skip_document(deps, SCOPED, "missing.pdf", reason=REASON).outcome
            == BulkOutcome.NOT_PERMITTED
        )
        assert _status(deps, "o.pdf") == "Review Pending"

    def test_another_reviewers_claim_is_held(self, deps):
        _row(deps, "h.pdf", HITLStatus="in_review")
        _item(deps, "h.pdf", state="CLAIMED", owner=ADMIN)
        assert (
            skip_document(deps, REV, "h.pdf", reason=REASON).outcome
            is BulkOutcome.HELD_BY_OTHER
        )

    def test_a_fault_is_failed_not_raised(self, deps):
        deps.store = MagicMock()
        deps.store.get_item.side_effect = RuntimeError("boom")
        _row(deps, "x.pdf", HITLStatus="Review Pending")
        assert skip_document(deps, ADMIN, "x.pdf", reason=REASON).outcome is (
            BulkOutcome.FAILED
        )
