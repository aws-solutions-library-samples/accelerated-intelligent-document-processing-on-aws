# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sending a document in review back to be processed again (moto HITLTable).

Two halves. ``authorize_send_back`` says who may: the holder of the item, and an
Admin for an item that is open or claimed. ``withdraw`` then ends the review, with
the history entry a send-back leaves and a guard against a review that was
replaced in the meantime. The run itself is the reprocess service's.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewConflict,
    ReviewItemNotFound,
)
from idp_common.review_queues.models import CallerContext, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

from ._table import REGION

pytestmark = pytest.mark.unit

ADMIN = CallerContext(
    reviewer_key="admin@example.com",
    email="admin@example.com",
    groups=frozenset({"Admin"}),
    is_admin=True,
)
REVIEWER = CallerContext(
    reviewer_key="rev@example.com",
    email="rev@example.com",
    groups=frozenset({"Reviewer"}),
)
OTHER_REVIEWER = CallerContext(
    reviewer_key="other@example.com",
    email="other@example.com",
    groups=frozenset({"Reviewer"}),
)
SCOPED_REVIEWER = CallerContext(
    reviewer_key="scoped@example.com",
    email="scoped@example.com",
    groups=frozenset({"Reviewer"}),
    allowed_versions=["another-profile"],
)
CYCLE = "0" * 21
OTHER_CYCLE = "1" * 21
KEY = "claims/a.pdf"


@pytest.fixture
def service(review_table):
    store = ReviewStore(review_table, region=REGION)
    store.queue_defs.put(QueueDefinition(id="claims", priority=70), create=True)
    return ReviewQueueService(
        store,
        QueueDirectory(store.queue_defs),
        notifier=MagicMock(),
        finalize_sender=MagicMock(),
        s3=MagicMock(),
        output_bucket="out",
        document_service=MagicMock(),
    )


def _seed(service, *, state="CLAIMED", owner=REVIEWER, cycle_id=CYCLE, **extra):
    now_ms = int(time.time() * 1000)
    fields = dict(
        doc_key=KEY,
        cycle_id=cycle_id,
        profile="default",
        queue="claims",
        state=state,
        enqueued_at="2026-11-16T00:00:00.000Z",
        enqueued_at_ms=now_ms,
        sections=["1"],
        version=1,
    )
    if state == "CLAIMED" and owner is not None:
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


def _history(service, cycle_id=CYCLE):
    return [
        (h.event, h.actor, h.details) for h in service.store.list_history(KEY, cycle_id)
    ]


class TestWhoMaySendBack:
    def test_the_holder_may(self, service):
        _seed(service)

        record = service.authorize_send_back(REVIEWER, KEY, CYCLE)

        assert (record.doc_key, record.cycle_id, record.state) == (
            KEY,
            CYCLE,
            "CLAIMED",
        )

    def test_another_reviewer_may_not(self, service):
        _seed(service)

        with pytest.raises(ReviewAccessDenied) as refused:
            service.authorize_send_back(OTHER_REVIEWER, KEY, CYCLE)

        assert refused.value.code == "NOT_LEASE_HOLDER"

    def test_a_reviewer_may_not_send_back_an_item_nobody_holds(self, service):
        _seed(service, state="OPEN")

        with pytest.raises(ReviewConflict) as refused:
            service.authorize_send_back(REVIEWER, KEY, CYCLE)

        assert refused.value.code == "INVALID_STATE"

    @pytest.mark.parametrize("state", ["OPEN", "CLAIMED"])
    def test_an_admin_may_on_an_open_or_a_claimed_item(self, service, state):
        _seed(service, state=state)

        assert service.authorize_send_back(ADMIN, KEY, CYCLE).state == state

    @pytest.mark.parametrize(
        "state", ["HOLDING", "FINALIZING", "COMPLETE", "REJECTED", "WITHDRAWN"]
    )
    @pytest.mark.parametrize("caller", [ADMIN, REVIEWER], ids=["admin", "reviewer"])
    def test_a_review_that_is_not_open_or_claimed_is_refused(
        self, service, state, caller
    ):
        _seed(service, state=state)

        with pytest.raises(ReviewConflict) as refused:
            service.authorize_send_back(caller, KEY, CYCLE)

        assert refused.value.code == "INVALID_STATE"

    def test_an_item_outside_the_callers_scope_is_refused(self, service):
        _seed(service, owner=SCOPED_REVIEWER)

        with pytest.raises(ReviewAccessDenied) as refused:
            service.authorize_send_back(SCOPED_REVIEWER, KEY, CYCLE)

        assert refused.value.code == "OUT_OF_SCOPE"

    def test_an_older_cycle_is_refused(self, service):
        _seed(service, cycle_id=OTHER_CYCLE)

        with pytest.raises(ReviewConflict) as refused:
            service.authorize_send_back(REVIEWER, KEY, CYCLE)

        assert refused.value.code == "STALE_CYCLE"

    def test_a_document_with_no_item_is_not_found(self, service):
        with pytest.raises(ReviewItemNotFound):
            service.authorize_send_back(ADMIN, KEY, CYCLE)

    def test_authorising_changes_nothing(self, service):
        before = _seed(service)

        service.authorize_send_back(REVIEWER, KEY, CYCLE)

        after = service.store.get_item(KEY)
        assert (after.state, after.version, after.owner_key) == (
            before.state,
            before.version,
            before.owner_key,
        )
        assert _history(service) == []


class TestWithdrawForASendBack:
    def test_the_history_entry_names_the_event_the_actor_and_the_details(self, service):
        _seed(service)

        withdrawn = service.withdraw(
            KEY,
            "sent_back",
            actor=REVIEWER.email,
            expected_cycle=CYCLE,
            history_event="sent_back",
            details={"start_step": "classification", "note": "wrong class on page 2"},
        )

        stored = service.store.get_item(KEY)
        assert withdrawn is True
        assert (stored.state, stored.withdrawn_reason) == ("WITHDRAWN", "sent_back")
        assert stored.owner_key is None
        assert _history(service) == [
            (
                "sent_back",
                REVIEWER.email,
                {
                    "reason": "sent_back",
                    "start_step": "classification",
                    "note": "wrong class on page 2",
                },
            )
        ]
        # The holder's open-claim count goes down with the claim.
        assert service.store.counter(REVIEWER.reviewer_key) == -1

    def test_a_different_live_cycle_raises_and_changes_nothing(self, service):
        before = _seed(service, cycle_id=OTHER_CYCLE)

        with pytest.raises(ReviewConflict) as refused:
            service.withdraw(
                KEY, "sent_back", expected_cycle=CYCLE, history_event="sent_back"
            )

        after = service.store.get_item(KEY)
        assert refused.value.code == "CYCLE_CHANGED"
        assert (after.state, after.version, after.cycle_id) == (
            "CLAIMED",
            before.version,
            OTHER_CYCLE,
        )
        assert _history(service, OTHER_CYCLE) == []
        service.document_service.update_review_attributes.assert_not_called()

    def test_the_expected_cycle_is_not_checked_when_nothing_is_live(self, service):
        _seed(service, state="COMPLETE", cycle_id=OTHER_CYCLE)

        assert service.withdraw(KEY, "sent_back", expected_cycle=CYCLE) is False

    def test_a_plain_withdraw_is_as_before(self, service):
        """Existing callers pass a document and a reason, nothing else."""
        _seed(service)

        assert service.withdraw(KEY, "reprocessed") is True

        assert _history(service) == [("withdrawn", None, {"reason": "reprocessed"})]
        # A reprocess tells the reviewer whose claim it released.
        service.notify.claim_released.assert_called_once()

    def test_a_send_back_by_its_holder_notifies_nobody(self, service):
        _seed(service)

        service.withdraw(
            KEY, "sent_back", actor=REVIEWER.email, history_event="sent_back"
        )

        service.notify.claim_released.assert_not_called()

    def test_the_review_attributes_are_cleared_from_the_tracking_row(self, service):
        _seed(service)

        service.withdraw(KEY, "sent_back", history_event="sent_back")

        call = service.document_service.update_review_attributes.call_args
        assert call.args[:2] == (KEY, {})
        assert "HITLStatus" in call.kwargs["remove_attrs"]
