# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who works a queue, and an Annotator taking work from it (My Work › Queues).

A queue is worked by the Annotators listed on it (an empty list means none; Admins
work every queue). A listed Annotator claims an item, which assigns it to them with
no expiry, and Skip releases it back to the queue.
"""

import time
from unittest.mock import MagicMock

import boto3
import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewAccessDenied
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo
from idp_common.review_queues.testset_docs import TestSetAssignmentError as Refused
from idp_common.review_queues.testset_docs import TestSetDocStore

ANN = "ann@example.com"
OTHER = "other@example.com"


def _annotator(email=ANN):
    return CallerContext(
        reviewer_key=email, email=email, groups=frozenset({"Annotator"})
    )


ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)


@pytest.fixture
def directory(review_table, monkeypatch):
    monkeypatch.setenv("HITL_TABLE_NAME", review_table)
    d = QueueDirectory()
    d.queue_defs.put(
        QueueDefinition(id="listed", eligible_annotators=[ANN], time_limit_minutes=30),
        create=True,
    )
    d.queue_defs.put(QueueDefinition(id="unlisted"), create=True)
    d.invalidate()
    return d


@pytest.mark.unit
class TestMembership:
    def test_an_empty_list_means_no_annotators(self):
        assert not QueueDefinition(id="q").allows(ANN)
        assert QueueDefinition(id="q", eligible_annotators=[ANN]).allows(ANN.upper())

    def test_an_annotator_works_only_the_queues_that_list_them(self, directory):
        assert directory.is_member(_annotator(), "", "listed")
        assert not directory.is_member(_annotator(), "", "unlisted")
        assert not directory.is_member(_annotator(OTHER), "", "listed")
        assert [r.queue for r, _, _ in directory.queues_for(_annotator())] == ["listed"]

    def test_an_admin_works_every_queue(self, directory):
        ids = {r.queue for r, _, _ in directory.queues_for(ADMIN)}
        # Default is seeded whenever it is missing.
        assert ids == {"listed", "unlisted", "default"}


def _service(directory, review_table):
    store = ReviewStore(review_table)
    return ReviewQueueService(store, directory, notifier=MagicMock(), s3=MagicMock())


def _seed_open(service, doc_key, queue):
    now_ms = int(time.time() * 1000)
    record = ReviewItemRecord(
        doc_key=doc_key,
        cycle_id="0" * 21,
        profile="default",
        queue=queue,
        state="OPEN",
        enqueued_at="2026-10-16T00:00:00Z",
        enqueued_at_ms=now_ms,
        sections=["1"],
    )
    service.store.client.put_item(item_to_dynamo(record))
    return record


@pytest.mark.unit
class TestProductionClaims:
    def test_a_listed_annotator_claims_an_item_and_it_is_theirs(
        self, directory, review_table
    ):
        service = _service(directory, review_table)
        _seed_open(service, "a.pdf", "listed")
        before = int(time.time() * 1000)

        claimed = service.claim(_annotator(), "a.pdf", "0" * 21)

        assert claimed.state == "CLAIMED"
        record = service.store.get_item("a.pdf")
        assert record is not None
        assert record.owner_email == ANN
        # Like an Admin assignment: no practical expiry, a reminder at the time limit.
        assert record.lease_expires_at_ms is not None and record.sweep_at_ms is not None
        assert record.lease_expires_at_ms > before + 365 * 24 * 3600 * 1000
        assert before + 29 * 60_000 < record.sweep_at_ms < before + 31 * 60_000
        # It is in the Annotator's own work (My Work › Tasks).
        mine = service.list_my_work(_annotator(), limit=10, cursor=None)
        assert [i.object_key for i in mine.items] == ["a.pdf"]

    def test_an_unlisted_annotator_cannot_claim(self, directory, review_table):
        service = _service(directory, review_table)
        _seed_open(service, "b.pdf", "unlisted")
        with pytest.raises(ReviewAccessDenied) as exc:
            service.claim(_annotator(), "b.pdf", "0" * 21)
        assert exc.value.code == "NOT_A_MEMBER"

    def test_skip_releases_the_item_back_to_its_queue(self, directory, review_table):
        service = _service(directory, review_table)
        _seed_open(service, "c.pdf", "listed")
        service.claim(_annotator(), "c.pdf", "0" * 21)

        released = service.release(_annotator(), "c.pdf", "0" * 21)

        assert released.state == "OPEN"
        assert service.store.get_item("c.pdf").owner_key is None
        assert service.list_my_work(_annotator(), limit=10, cursor=None).items == []

    def test_the_holder_keeps_their_item_after_being_taken_off_the_list(
        self, directory, review_table
    ):
        service = _service(directory, review_table)
        _seed_open(service, "d.pdf", "listed")
        service.claim(_annotator(), "d.pdf", "0" * 21)
        directory.queue_defs.put(
            QueueDefinition(id="listed", time_limit_minutes=30), create=False
        )
        directory.invalidate()

        # Still openable and releasable by the holder; nobody else unlisted gets in.
        service.get_item_detail(_annotator(), "d.pdf")
        assert service.release(_annotator(), "d.pdf", "0" * 21).state == "OPEN"
        with pytest.raises(ReviewAccessDenied):
            service.get_item_detail(_annotator(), "d.pdf")


@pytest.fixture
def test_set_store(review_table):
    store = TestSetDocStore(boto3.resource("dynamodb").Table(review_table))
    store.upsert_many(
        "set1",
        [
            {
                "objectKey": k,
                "inputKey": f"set1/input/{k}",
                "labelSource": "draft-machine",
            }
            for k in ("free.pdf", "held.pdf", "done.pdf")
        ],
    )
    store.assign("set1", "held.pdf", OTHER, assigned_by="admin", time_limit=5)
    store.mark_reviewed("set1", "done.pdf")
    return store


@pytest.mark.unit
class TestTestSetClaims:
    def test_claiming_a_free_document_assigns_it(self, test_set_store):
        doc = test_set_store.assign(
            "set1",
            "free.pdf",
            ANN,
            assigned_by=ANN,
            time_limit=5,
            only_if_unassigned=True,
        )
        assert doc["assignedTo"] == ANN
        assert [d["objectKey"] for d in test_set_store.assigned_to(ANN)] == ["free.pdf"]

    def test_a_held_document_cannot_be_claimed(self, test_set_store):
        with pytest.raises(Refused, match="ALREADY_CLAIMED"):
            test_set_store.assign(
                "set1",
                "held.pdf",
                ANN,
                assigned_by=ANN,
                time_limit=5,
                only_if_unassigned=True,
            )
        assert test_set_store.get("set1", "held.pdf")["assignedTo"] == OTHER

    def test_a_reviewed_document_cannot_be_claimed(self, test_set_store):
        with pytest.raises(Refused, match="ALREADY_REVIEWED"):
            test_set_store.assign(
                "set1",
                "done.pdf",
                ANN,
                assigned_by=ANN,
                time_limit=5,
                only_if_unassigned=True,
            )

    def test_an_admin_assignment_still_overrides(self, test_set_store):
        doc = test_set_store.assign(
            "set1", "held.pdf", ANN, assigned_by="admin", time_limit=5
        )
        assert doc["assignedTo"] == ANN
