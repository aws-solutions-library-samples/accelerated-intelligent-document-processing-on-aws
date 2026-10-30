# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A queue item's assignee is written to the document's tracking row (moto HITLTable).

The document page reads its Review Owner from ``HITLReviewOwnerEmail`` on the tracking
row, while a review queue keeps its assignee on the queue item. Every write that
changes who holds an item therefore mirrors it: a claim or take over writes the new
holder; a skip clears it; a finish keeps the last holder; a withdrawal clears it with
the other review attributes. The mirror is best-effort and never undoes the write.
When the finalize run ends, Review Completed By is written by email as well.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues import tracker
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import (
    REVIEW_OWNER_ATTRIBUTES,
    REVIEW_OWNER_EMAIL_ATTRIBUTE,
    ReviewQueueService,
)
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)
ANN = CallerContext(
    reviewer_key="ann@example.com",
    email="ann@example.com",
    groups=frozenset({"Annotator"}),
)
CYCLE = "0" * 21


@pytest.fixture
def documents():
    return MagicMock()


@pytest.fixture
def service(review_table, documents):
    store = ReviewStore(review_table, region=REGION)
    directory = QueueDirectory(store.queue_defs)
    store.queue_defs.put(
        QueueDefinition(id="claims", priority=70, eligible_annotators=[ANN.email]),
        create=True,
    )
    return ReviewQueueService(
        store,
        directory,
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
        document_service=documents,
    )


def _seed(service, doc_key, *, state="OPEN", owner=None):
    now_ms = int(time.time() * 1000)
    fields = dict(
        doc_key=doc_key,
        cycle_id=CYCLE,
        profile="default",
        queue="claims",
        state=state,
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=now_ms,
        sections=["1"],
        version=1,
    )
    if owner is not None:
        fields.update(
            owner_key=owner.reviewer_key,
            owner_email=owner.email,
            claimed_at_ms=now_ms,
            lease_expires_at_ms=now_ms + 3_600_000,
        )
    service.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))


def _owner_writes(documents, doc_key):
    """The owner writes made for one document: ("set", email) or ("clear", attrs)."""
    out = []
    for call in documents.update_review_attributes.call_args_list:
        key, attrs = call.args[0], call.args[1]
        if key != doc_key:
            continue
        removed = tuple(call.kwargs.get("remove_attrs", ()))
        if REVIEW_OWNER_EMAIL_ATTRIBUTE in attrs:
            out.append(("set", attrs[REVIEW_OWNER_EMAIL_ATTRIBUTE]))
        elif set(REVIEW_OWNER_ATTRIBUTES) <= set(removed):
            out.append(("clear", removed))
    return out


@pytest.mark.unit
class TestReviewOwnerMirror:
    def test_a_claim_writes_the_holder(self, service, documents):
        _seed(service, "a.pdf")
        service.claim(ANN, "a.pdf", CYCLE)
        assert _owner_writes(documents, "a.pdf") == [("set", ANN.email)]

    def test_a_take_over_writes_the_new_holder(self, service, documents):
        _seed(service, "b.pdf", state="CLAIMED", owner=ANN)
        service.take_over(ADMIN, "b.pdf", CYCLE)
        assert _owner_writes(documents, "b.pdf") == [("set", ADMIN.email)]

    def test_a_skip_clears_the_holder(self, service, documents):
        _seed(service, "c.pdf", state="CLAIMED", owner=ANN)
        service.release(ANN, "c.pdf", CYCLE, "Out of time")
        [(kind, removed)] = _owner_writes(documents, "c.pdf")
        assert kind == "clear" and set(REVIEW_OWNER_ATTRIBUTES) <= set(removed)

    def test_a_finish_keeps_the_last_holder(self, service, documents):
        _seed(service, "d.pdf", state="CLAIMED", owner=ANN)
        service.complete(ANN, "d.pdf", CYCLE)
        assert service.store.get_item("d.pdf").state == "FINALIZING"
        assert _owner_writes(documents, "d.pdf") == []

    def test_a_withdrawal_clears_it_with_the_review_attributes(
        self, service, documents
    ):
        _seed(service, "e.pdf", state="CLAIMED", owner=ANN)
        assert service.withdraw("e.pdf", "reprocessed") is True
        [(kind, removed)] = _owner_writes(documents, "e.pdf")
        assert kind == "clear"
        assert "HITLStatus" in removed

    def test_a_tracking_row_that_cannot_be_written_does_not_undo_the_claim(
        self, service, documents
    ):
        documents.update_review_attributes.side_effect = RuntimeError("no row")
        _seed(service, "f.pdf")
        service.claim(ANN, "f.pdf", CYCLE)
        stored = service.store.get_item("f.pdf")
        assert (stored.state, stored.owner_email) == ("CLAIMED", ANN.email)

    def test_a_finished_review_records_who_completed_it_by_email(self, service):
        _seed(service, "g.pdf", state="CLAIMED", owner=ANN)
        service.complete(ANN, "g.pdf", CYCLE)
        item = service.store.get_item("g.pdf")
        ctx = tracker.load_context(
            "finalize", item.finalize_token, "g.pdf", None, service.store
        )
        documents = MagicMock()
        tracker.after_completion(
            ctx,
            workflow_status="SUCCEEDED",
            run_ref=None,
            document=SimpleNamespace(metering={}),
            store=service.store,
            s3=MagicMock(),
            output_bucket="out",
            notifier=MagicMock(),
            document_service=documents,
            retention_days=30,
        )
        (attrs,) = documents.update_review_attributes.call_args.args[1:2]
        assert attrs["HITLReviewedBy"] == ANN.email
        assert attrs["HITLReviewedByEmail"] == ANN.email
