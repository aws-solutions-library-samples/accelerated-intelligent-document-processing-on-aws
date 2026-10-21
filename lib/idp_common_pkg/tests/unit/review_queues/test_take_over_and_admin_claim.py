# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An Admin working production review items (moto HITLTable).

An Admin opening an unassigned item claims it (the UI does this on the first edit
or on Complete) and can then save drafts and complete it. An item assigned to
someone else is taken over with ``take_over``: the Admin becomes the assignee,
both claim counters move, the former assignee is told, and the draft — keyed by
item and cycle — carries over.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewAccessDenied, ReviewConflict
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
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
def service(review_table):
    store = ReviewStore(review_table, region=REGION)
    directory = QueueDirectory(store.queue_defs)
    # The Admin is deliberately not on the queue's list: Admins work every queue.
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
    )


def _seed(service, doc_key, *, state="OPEN", owner=None, queue="claims"):
    now_ms = int(time.time() * 1000)
    fields = dict(
        doc_key=doc_key,
        cycle_id=CYCLE,
        profile="default",
        queue=queue,
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
    if state == "FINALIZING":
        fields["sweep_at_ms"] = now_ms + 600_000
    service.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))


def _history_events(service, doc_key):
    return [h.event for h in service.store.list_history(doc_key, CYCLE)]


@pytest.mark.unit
class TestAdminOnAnOpenItem:
    def test_claim_then_draft_then_complete_reaches_finalizing(self, service):
        _seed(service, "a.pdf")
        claimed = service.claim(ADMIN, "a.pdf", CYCLE)
        assert claimed.state == "CLAIMED" and claimed.owner_email == ADMIN.email
        assert service.store.counter(ADMIN.reviewer_key) == 1

        draft = service.save_draft(ADMIN, "a.pdf", CYCLE, "1", {"total": 5}, None)
        assert draft.revision == 1

        view = service.complete(ADMIN, "a.pdf", CYCLE)
        stored = service.store.get_item("a.pdf")
        assert (stored.state, stored.disposition) == ("FINALIZING", "complete")
        assert stored.disposed_by == ADMIN.email and view.disposition == "complete"
        service.finalize_sender.send.assert_called_once()
        assert service.store.counter(ADMIN.reviewer_key) == 0
        assert _history_events(service, "a.pdf") == [
            "claimed",
            "draft_saved",
            "completed",
        ]

    def test_without_a_claim_a_draft_save_is_refused(self, service):
        # Why the UI claims first: the service only takes drafts from the assignee.
        _seed(service, "b.pdf")
        with pytest.raises(ReviewConflict) as exc:
            service.save_draft(ADMIN, "b.pdf", CYCLE, "1", {"total": 5}, None)
        assert exc.value.code == "INVALID_STATE"


@pytest.mark.unit
class TestTakeOver:
    def test_the_admin_becomes_the_assignee_and_the_draft_carries_over(self, service):
        _seed(service, "c.pdf", state="CLAIMED", owner=ANN)
        service.save_draft(ANN, "c.pdf", CYCLE, "1", {"total": 1}, None)

        view = service.take_over(ADMIN, "c.pdf", CYCLE)

        stored = service.store.get_item("c.pdf")
        assert (stored.state, stored.owner_key, stored.owner_email) == (
            "CLAIMED",
            ADMIN.reviewer_key,
            ADMIN.email,
        )
        assert view.is_mine is True and view.owner_email == ADMIN.email
        # The seeded claim was never counted, so the former owner reads -1.
        assert service.store.counter(ANN.reviewer_key) == -1
        assert service.store.counter(ADMIN.reviewer_key) == 1
        assert _history_events(service, "c.pdf")[-1] == "taken_over"
        entry = service.store.list_history("c.pdf", CYCLE)[-1]
        assert entry.actor == ADMIN.email
        assert entry.details == {"former_owner": ANN.email}
        service.notify.claim_released.assert_called_once()
        _item, email, reason = service.notify.claim_released.call_args.args
        assert (email, reason) == (ANN.email, "take_over")
        service.notify.item_reassigned.assert_not_called()

        draft = service.store.get_draft("c.pdf", CYCLE, "1")
        assert draft is not None and draft.revision == 1
        assert stored.has_draft is True
        # The Admin carries on from the Annotator's revision ...
        saved = service.save_draft(ADMIN, "c.pdf", CYCLE, "1", {"total": 2}, 1)
        assert saved.revision == 2
        # ... and the Annotator no longer can.
        with pytest.raises(ReviewAccessDenied):
            service.save_draft(ANN, "c.pdf", CYCLE, "1", {"total": 3}, 2)

    def test_annotators_cannot_take_over(self, service):
        _seed(service, "d.pdf", state="CLAIMED", owner=ADMIN)
        with pytest.raises(ReviewAccessDenied) as exc:
            service.take_over(ANN, "d.pdf", CYCLE)
        assert exc.value.code == "ROLE"
        assert service.store.get_item("d.pdf").owner_key == ADMIN.reviewer_key

    def test_an_open_item_is_claimed_not_taken_over(self, service):
        _seed(service, "e.pdf")
        with pytest.raises(ReviewConflict) as exc:
            service.take_over(ADMIN, "e.pdf", CYCLE)
        assert exc.value.code == "NOT_ASSIGNED"
        assert service.store.get_item("e.pdf").state == "OPEN"

    @pytest.mark.parametrize("state", ["FINALIZING", "HOLDING"])
    def test_an_item_out_of_review_cannot_change_hands(self, service, state):
        _seed(service, "f.pdf", state=state, owner=ANN)
        with pytest.raises(ReviewConflict) as exc:
            service.take_over(ADMIN, "f.pdf", CYCLE)
        assert exc.value.code == "INVALID_STATE"
        service.notify.claim_released.assert_not_called()

    def test_an_item_already_yours_is_refused(self, service):
        _seed(service, "g.pdf", state="CLAIMED", owner=ADMIN)
        with pytest.raises(ReviewConflict) as exc:
            service.take_over(ADMIN, "g.pdf", CYCLE)
        assert exc.value.code == "ALREADY_YOURS"
        assert service.store.counter(ADMIN.reviewer_key) == 0

    def test_a_stale_cycle_is_refused(self, service):
        _seed(service, "h.pdf", state="CLAIMED", owner=ANN)
        with pytest.raises(ReviewConflict) as exc:
            service.take_over(ADMIN, "h.pdf", "1" * 21)
        assert exc.value.code == "STALE_CYCLE"


@pytest.mark.unit
class TestItemDetailQueueName:
    """The annotation screen's breadcrumb names the item's queue as people see it."""

    def test_the_queue_is_named_by_its_definition(self, service):
        service.store.queue_defs.put(
            QueueDefinition(id="named", name="High-value claims"), create=True
        )
        _seed(service, "n.pdf", queue="named")
        assert service.get_item_detail(ADMIN, "n.pdf").queue_name == "High-value claims"

    def test_an_unnamed_queue_reads_as_its_id(self, service):
        _seed(service, "m.pdf")
        assert service.get_item_detail(ADMIN, "m.pdf").queue_name == "Claims"

    def test_an_undefined_queue_reads_as_its_id(self, service):
        _seed(service, "u.pdf", queue="old_claims")
        assert service.get_item_detail(ADMIN, "u.pdf").queue_name == "Old Claims"
