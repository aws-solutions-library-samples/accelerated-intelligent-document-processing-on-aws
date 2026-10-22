# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why an item left a reviewer's hands is recorded with it (moto HITLTable).

Skip gives an assigned item back to its queue unfinished. It now takes a reason from
the queue's skip list, plus an optional note, and both go into the item's history,
which is what the next reviewer and the Admin read. Reject already required a reason
but kept it only on the item record; its history entry now carries it as well.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewValidationError
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import (
    DEFAULT_SKIP_REASONS,
    QueueDefinition,
)
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

ANN = CallerContext(
    reviewer_key="ann@example.com",
    email="ann@example.com",
    groups=frozenset({"Annotator"}),
)
CYCLE = "0" * 21


@pytest.fixture
def service(review_table):
    store = ReviewStore(review_table, region=REGION)
    directory = QueueDirectory(store.queue_defs)
    store.queue_defs.put(
        QueueDefinition(id="claims", eligible_annotators=[ANN.email]), create=True
    )
    store.queue_defs.put(
        QueueDefinition(
            id="tax",
            eligible_annotators=[ANN.email],
            skip_reasons=["Needs a tax specialist"],
        ),
        create=True,
    )
    return ReviewQueueService(
        store,
        directory,
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
    )


def _assigned(service, doc_key, queue="claims"):
    now_ms = int(time.time() * 1000)
    record = ReviewItemRecord(
        doc_key=doc_key,
        cycle_id=CYCLE,
        profile="default",
        queue=queue,
        state="CLAIMED",
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=now_ms,
        sections=["1"],
        version=1,
        owner_key=ANN.reviewer_key,
        owner_email=ANN.email,
        claimed_at_ms=now_ms,
        lease_expires_at_ms=now_ms + 3_600_000,
    )
    service.store.client.put_item(item_to_dynamo(record))


def _history(service, doc_key):
    return [(h.event, h.details) for h in service.store.list_history(doc_key, CYCLE)]


@pytest.mark.unit
class TestSkipReason:
    def test_skip_records_its_reason_and_note_in_the_item_history(self, service):
        _assigned(service, "a.pdf")
        view = service.release(ANN, "a.pdf", CYCLE, "Out of time", "back tomorrow")
        assert view.state == "OPEN"
        assert _history(service, "a.pdf") == [
            ("released", {"reason": "Out of time", "note": "back tomorrow"})
        ]

    def test_a_skip_without_a_note_records_only_the_reason(self, service):
        _assigned(service, "b.pdf")
        service.release(ANN, "b.pdf", CYCLE, "Needs expert review")
        assert _history(service, "b.pdf") == [
            ("released", {"reason": "Needs expert review"})
        ]

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_a_skip_needs_a_reason(self, service, reason):
        _assigned(service, "c.pdf")
        with pytest.raises(ReviewValidationError) as exc:
            service.release(ANN, "c.pdf", CYCLE, reason)
        assert exc.value.code == "REASON_REQUIRED"
        assert service.store.get_item("c.pdf").state == "CLAIMED"

    def test_a_reason_outside_the_queue_list_is_refused(self, service):
        _assigned(service, "d.pdf")
        with pytest.raises(ReviewValidationError) as exc:
            service.release(ANN, "d.pdf", CYCLE, "Lunch")
        assert exc.value.code == "BAD_ARGUMENT"
        assert service.store.get_item("d.pdf").state == "CLAIMED"
        assert _history(service, "d.pdf") == []

    def test_each_queue_checks_against_its_own_list(self, service):
        _assigned(service, "e.pdf", queue="tax")
        with pytest.raises(ReviewValidationError):
            service.release(ANN, "e.pdf", CYCLE, DEFAULT_SKIP_REASONS[0])
        service.release(ANN, "e.pdf", CYCLE, "Needs a tax specialist")
        assert service.store.get_item("e.pdf").state == "OPEN"

    def test_the_item_detail_offers_the_queue_skip_list(self, service):
        _assigned(service, "f.pdf", queue="tax")
        detail = service.get_item_detail(ANN, "f.pdf")
        assert detail.skip_reasons == ["Needs a tax specialist"]


@pytest.mark.unit
class TestRejectReasonInHistory:
    def test_reject_records_its_reason_and_note_in_the_item_history(self, service):
        _assigned(service, "g.pdf")
        service.reject(ANN, "g.pdf", CYCLE, "Duplicate document", "same as g2.pdf")
        assert _history(service, "g.pdf") == [
            ("rejected", {"reason": "Duplicate document", "note": "same as g2.pdf"})
        ]


@pytest.mark.unit
class TestQueueDefinitionSkipReasons:
    def test_a_definition_stored_before_skip_reasons_reads_the_defaults(self):
        item = QueueDefinition(id="old").to_item()
        del item["SkipReasons"]
        assert QueueDefinition.from_item(item).skip_reasons == list(
            DEFAULT_SKIP_REASONS
        )

    def test_skip_reasons_round_trip(self):
        definition = QueueDefinition(id="q", skip_reasons=["A", "B"])
        assert QueueDefinition.from_item(definition.to_item()).skip_reasons == [
            "A",
            "B",
        ]
