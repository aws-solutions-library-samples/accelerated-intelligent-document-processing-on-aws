# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Admin "Remove from queue" and deleting a queue that holds items (moto HITLTable).

Remove takes an OPEN or assigned item out of review: it goes to FINALIZING with
disposition ``removed``, its finalize run completes the document with the machine
results, and the review status object is deleted rather than written. Deleting a
queue moves its items to the Default queue, and is refused while one of them is
being finalized.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues import tracker
from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewValidationError,
)
from idp_common.review_queues.models import (
    CallerContext,
    ReviewItemRecord,
    TrackerReviewContext,
)
from idp_common.review_queues.queue_defs import DEFAULT_QUEUE_ID, QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.statuses import QueueItemState
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
    # The table's region, not the environment's: another suite in the same worker
    # may have changed AWS_DEFAULT_REGION.
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
    )


def _seed(service, doc_key, *, queue="claims", state="OPEN", owner=None, **extra):
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
    if state in ("HOLDING", "FINALIZING"):
        fields["sweep_at_ms"] = now_ms + 600_000
    fields.update(extra)
    record = ReviewItemRecord(**fields)
    service.store.client.put_item(item_to_dynamo(record))
    return record


def _history_events(service, doc_key):
    return [h.event for h in service.store.list_history(doc_key, CYCLE)]


@pytest.mark.unit
class TestRemove:
    def test_an_open_item_goes_to_finalizing_as_removed(self, service):
        _seed(service, "a.pdf")
        view = service.remove(ADMIN, "a.pdf", CYCLE, note="duplicate upload")
        stored = service.store.get_item("a.pdf")
        assert (stored.state, stored.disposition) == ("FINALIZING", "removed")
        assert stored.disposed_by == ADMIN.email
        assert view.disposition == "removed"
        service.finalize_sender.send.assert_called_once()
        assert _history_events(service, "a.pdf") == ["removed"]
        [entry] = service.store.list_history("a.pdf", CYCLE)
        assert entry.details == {"note": "duplicate upload"}
        service.notify.claim_released.assert_not_called()

    def test_an_assigned_item_is_released_and_its_annotator_told(self, service):
        _seed(service, "b.pdf", state="CLAIMED", owner=ANN)
        service.remove(ADMIN, "b.pdf", CYCLE)
        stored = service.store.get_item("b.pdf")
        assert stored.state == "FINALIZING" and stored.owner_key is None
        service.notify.claim_released.assert_called_once()
        _item, email, reason = service.notify.claim_released.call_args.args
        assert (email, reason) == (ANN.email, "removed")
        # The assignee's open-claim counter is decremented.
        assert service.store.counter(ANN.reviewer_key) == -1

    @pytest.mark.parametrize("state", ["HOLDING", "FINALIZING", "COMPLETE"])
    def test_only_open_or_assigned_items(self, service, state):
        _seed(service, "c.pdf", state=state)
        with pytest.raises(ReviewConflict) as exc:
            service.remove(ADMIN, "c.pdf", CYCLE)
        assert exc.value.code == "INVALID_STATE"

    def test_admin_only(self, service):
        _seed(service, "d.pdf", state="CLAIMED", owner=ANN)
        with pytest.raises(ReviewAccessDenied) as exc:
            service.remove(ANN, "d.pdf", CYCLE)
        assert exc.value.code == "ROLE"
        assert service.store.get_item("d.pdf").state == "CLAIMED"


def _finalize_ctx(item):
    return TrackerReviewContext(
        kind="finalize", item=item, token=item.finalize_token, outcome="complete"
    )


@pytest.mark.unit
class TestRemovedFinalizeRun:
    def test_the_document_completes_and_its_status_object_is_deleted(self, service):
        _seed(service, "e.pdf")
        service.remove(ADMIN, "e.pdf", CYCLE)
        item = service.store.get_item("e.pdf")
        ctx = tracker.load_context(
            "finalize", item.finalize_token, "e.pdf", None, service.store
        )
        assert ctx is not None and ctx.outcome == "complete"
        s3 = MagicMock()
        documents = MagicMock()
        tracker.after_completion(
            ctx,
            workflow_status="SUCCEEDED",
            run_ref=None,
            document=SimpleNamespace(metering={}),
            store=service.store,
            s3=s3,
            output_bucket="out",
            notifier=MagicMock(),
            document_service=documents,
            retention_days=30,
        )
        s3.delete_content.assert_called_once_with("out", "e.pdf/review/status.json")
        s3.write_content.assert_not_called()
        (attrs,) = documents.update_review_attributes.call_args.args[1:2]
        assert "ReviewStatusUri" not in attrs
        assert (
            "ReviewStatusUri"
            in documents.update_review_attributes.call_args.kwargs["remove_attrs"]
        )
        done = service.store.get_item("e.pdf")
        assert (done.state, done.disposition) == (
            QueueItemState.COMPLETE.value,
            "removed",
        )
        assert tracker.reporting_info(ctx).disposition == "removed"

    def test_a_completed_review_still_publishes_its_status_object(self, service):
        _seed(service, "f.pdf", state="CLAIMED", owner=ANN)
        service.complete(ANN, "f.pdf", CYCLE)
        item = service.store.get_item("f.pdf")
        s3 = MagicMock()
        tracker.after_completion(
            _finalize_ctx(item),
            workflow_status="SUCCEEDED",
            run_ref=None,
            document=SimpleNamespace(metering={}),
            store=service.store,
            s3=s3,
            output_bucket="out",
            notifier=MagicMock(),
            document_service=MagicMock(),
            retention_days=30,
        )
        s3.write_content.assert_called_once()
        s3.delete_content.assert_not_called()


@pytest.mark.unit
class TestDeleteQueue:
    def test_items_move_to_default_and_the_queue_is_deleted(self, service):
        _seed(service, "open.pdf")
        _seed(service, "held.pdf", state="CLAIMED", owner=ANN)
        _seed(service, "hold.pdf", state="HOLDING")
        _seed(service, "other.pdf", queue=DEFAULT_QUEUE_ID)
        moved = service.delete_queue(ADMIN, "claims")
        assert moved == 3
        for key in ("open.pdf", "held.pdf", "hold.pdf"):
            item = service.store.get_item(key)
            assert item.queue == DEFAULT_QUEUE_ID, key
        assert service.store.get_item("held.pdf").state == "OPEN"
        assert service.store.get_item("hold.pdf").state == "HOLDING"
        _item, email, reason = service.notify.claim_released.call_args.args
        assert (email, reason) == (ANN.email, "move")
        assert service.store.queue_defs.get("claims") is None

    def test_refused_while_an_item_is_finalizing(self, service):
        _seed(service, "open.pdf")
        _seed(service, "fin.pdf", state="FINALIZING", finalize_token="t")
        with pytest.raises(ReviewConflict) as exc:
            service.delete_queue(ADMIN, "claims")
        assert exc.value.code == "QUEUE_FINALIZING"
        assert service.store.get_item("open.pdf").queue == "claims"
        assert service.store.queue_defs.get("claims") is not None

    def test_default_is_protected_and_admin_only(self, service):
        with pytest.raises(ReviewValidationError) as exc:
            service.delete_queue(ADMIN, "Default")
        assert exc.value.code == "PROTECTED_QUEUE"
        with pytest.raises(ReviewAccessDenied):
            service.delete_queue(ANN, "claims")


@pytest.mark.unit
class TestActivation:
    def _hold(self, service, queue):
        item = _seed(
            service,
            "g.pdf",
            queue=queue,
            state="HOLDING",
            phase1_execution_arn="arn:exec",
        )
        tracker.after_completion(
            TrackerReviewContext(kind="hold", item=item),
            workflow_status="SUCCEEDED",
            run_ref=None,
            document=SimpleNamespace(metering={}),
            store=service.store,
            s3=MagicMock(),
            output_bucket="out",
            notifier=MagicMock(),
            document_service=MagicMock(),
            retention_days=30,
        )
        return service.store.get_item("g.pdf")

    def test_an_item_whose_queue_was_deleted_opens_in_default(self, service):
        active = self._hold(service, "gone")
        assert (active.state, active.queue) == ("OPEN", DEFAULT_QUEUE_ID)
        assert (
            active.priority == service.store.queue_defs.get(DEFAULT_QUEUE_ID).priority
        )

    def test_an_item_whose_queue_exists_stays_there(self, service):
        active = self._hold(service, "claims")
        assert (active.state, active.queue) == ("OPEN", "claims")
