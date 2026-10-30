# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Profile scope, applied to each review item by its own profile.

Queues are system-wide, so one queue holds items of several configuration profiles.
A caller with ``allowed_versions`` sees and works only the items whose own profile
their scope admits — in every listing (one queue, all their queues, My Queues, their
own work, Claim next) as in the per-item operations — and an item with no profile is
admitted to no scoped caller. Admins and unscoped callers see every item. A listing
that leaves items out still pages forward: every item is listed once, and the paging
ends.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewAccessDenied
from idp_common.review_queues.models import CallerContext, QueueRef, ReviewItemRecord
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

ANN = "ann@example.com"
CYCLE = "0" * 21


def _caller(allowed=None, *, admin=False):
    return CallerContext(
        reviewer_key="admin@example.com" if admin else ANN,
        email="admin@example.com" if admin else ANN,
        groups=frozenset({"Admin"} if admin else {"Annotator"}),
        is_admin=admin,
        allowed_versions=allowed,
    )


SCOPED = _caller(["profile-a"])
UNSCOPED = _caller()
ADMIN = _caller(admin=True)


@pytest.fixture
def service(review_table, monkeypatch):
    monkeypatch.setenv("HITL_TABLE_NAME", review_table)
    directory = QueueDirectory()
    directory.queue_defs.put(
        QueueDefinition(id="first", eligible_annotators=[ANN], priority=60), create=True
    )
    directory.queue_defs.put(
        QueueDefinition(id="second", eligible_annotators=[ANN], priority=40),
        create=True,
    )
    directory.invalidate()
    return ReviewQueueService(
        ReviewStore(review_table), directory, notifier=MagicMock(), s3=MagicMock()
    )


_clock = [int(time.time() * 1000)]


def _seed(service, doc_key, profile, *, queue="first", owner=None):
    """An item, enqueued after every item seeded before it."""
    _clock[0] += 1000
    fields = dict(
        doc_key=doc_key,
        cycle_id=CYCLE,
        profile=profile,
        queue=queue,
        state="OPEN",
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=_clock[0],
        sections=["1"],
        version=1,
    )
    if owner is not None:
        fields.update(
            state="CLAIMED",
            owner_key=owner.reviewer_key,
            owner_email=owner.email,
            claimed_at_ms=_clock[0],
            lease_expires_at_ms=_clock[0] + 3_600_000,
        )
    service.store.client.put_item(item_to_dynamo(ReviewItemRecord(**fields)))


def _keys(page):
    return [i.object_key for i in page.items]


def _seed_mixed(service):
    _seed(service, "a1.pdf", "profile-a")
    _seed(service, "b1.pdf", "profile-b")
    _seed(service, "none.pdf", "")
    _seed(service, "a2.pdf", "profile-a", queue="second")
    _seed(service, "b2.pdf", "profile-b", queue="second")


@pytest.mark.unit
class TestListingsApplyEachItemsProfile:
    def test_one_queue_lists_only_the_items_in_scope(self, service):
        _seed_mixed(service)
        page = service.list_queue_items(
            SCOPED, QueueRef(queue="first"), claimable=False, limit=10, cursor=None
        )
        assert _keys(page) == ["a1.pdf"]

    def test_all_the_callers_queues_list_only_the_items_in_scope(self, service):
        _seed_mixed(service)
        page = service.list_queue_items(
            SCOPED, None, claimable=True, limit=10, cursor=None
        )
        assert _keys(page) == ["a1.pdf", "a2.pdf"]

    def test_my_queues_lists_only_the_items_in_scope(self, service):
        _seed_mixed(service)
        assert _keys(service.get_my_queues(SCOPED).items) == ["a1.pdf", "a2.pdf"]

    def test_my_work_lists_only_the_callers_items_in_scope_now(self, service):
        # Held before the caller's scope narrowed: still theirs, no longer listed.
        _seed(service, "mine-a.pdf", "profile-a", owner=SCOPED)
        _seed(service, "mine-b.pdf", "profile-b", owner=SCOPED)
        assert _keys(service.list_my_work(SCOPED, limit=10, cursor=None)) == [
            "mine-a.pdf"
        ]

    @pytest.mark.parametrize("caller", [UNSCOPED, ADMIN], ids=["unscoped", "admin"])
    def test_an_unscoped_caller_and_an_admin_see_every_item(self, service, caller):
        _seed_mixed(service)
        page = service.list_queue_items(
            caller, None, claimable=True, limit=10, cursor=None
        )
        assert sorted(_keys(page)) == [
            "a1.pdf",
            "a2.pdf",
            "b1.pdf",
            "b2.pdf",
            "none.pdf",
        ]


@pytest.mark.unit
class TestClaimNextTakesOnlyItemsInScope:
    def test_it_claims_the_oldest_item_in_scope_past_older_ones_outside_it(
        self, service
    ):
        for n in range(12):  # more than its attempts, all older than the one in scope
            _seed(service, f"b{n}.pdf", "profile-b")
        _seed(service, "a.pdf", "profile-a")
        result = service.claim_next(SCOPED, QueueRef(queue="first"))
        assert result.item is not None and result.item.object_key == "a.pdf"

    def test_it_reads_a_bounded_number_of_pages_past_items_outside_scope(
        self, service, monkeypatch
    ):
        from idp_common.review_queues import service as service_module

        for n in range(80):
            _seed(service, f"b{n:02}.pdf", "profile-b")
        reads = []
        real = service.store.query_queue
        monkeypatch.setattr(
            service.store,
            "query_queue",
            lambda *a, **k: reads.append(1) or real(*a, **k),
        )
        result = service.claim_next(SCOPED, QueueRef(queue="first"))
        assert result.item is None and result.reason == "EMPTY"
        assert len(reads) == service_module.CLAIM_NEXT_MAX_PAGES

    def test_it_finds_nothing_when_no_item_is_in_scope(self, service):
        _seed(service, "b.pdf", "profile-b")
        result = service.claim_next(SCOPED, QueueRef(queue="first"))
        assert result.item is None and result.reason == "EMPTY"
        assert service.store.get_item("b.pdf").state == "OPEN"


@pytest.mark.unit
class TestThePerItemCheck:
    @pytest.mark.parametrize(
        "doc_key", ["b1.pdf", "none.pdf"], ids=["other-profile", "no-profile"]
    )
    def test_refuses_an_item_outside_scope_or_without_a_profile(self, service, doc_key):
        _seed_mixed(service)
        with pytest.raises(ReviewAccessDenied) as exc:
            service.claim(SCOPED, doc_key, CYCLE)
        assert exc.value.code == "OUT_OF_SCOPE"

    def test_admits_an_item_in_scope(self, service):
        _seed(service, "a.pdf", "profile-a")
        assert service.claim(SCOPED, "a.pdf", CYCLE).state == "CLAIMED"


def _all_pages(fetch, *, bound=50):
    """Every page until the token runs out; fails if the paging does not end."""
    keys, cursor = [], None
    for _ in range(bound):
        page = fetch(cursor)
        keys.extend(_keys(page))
        cursor = page.next_token
        if cursor is None:
            return keys
    pytest.fail("the paging did not end")


@pytest.mark.unit
class TestPagingPastItemsLeftOut:
    def _seed_interleaved(self, service):
        # Long runs of items outside scope between the ones in scope, in both queues.
        expected = []
        for n in range(4):
            for m in range(3):
                _seed(service, f"first-b{n}{m}.pdf", "profile-b")
                _seed(service, f"second-b{n}{m}.pdf", "profile-b", queue="second")
            _seed(service, f"first-a{n}.pdf", "profile-a")
            _seed(service, f"second-a{n}.pdf", "profile-a", queue="second")
            expected += [f"first-a{n}.pdf", f"second-a{n}.pdf"]
        return expected

    def test_all_queues_list_every_item_in_scope_once_and_end(self, service):
        expected = self._seed_interleaved(service)
        listed = _all_pages(
            lambda c: service.list_queue_items(
                SCOPED, None, claimable=True, limit=2, cursor=c
            )
        )
        assert sorted(listed) == sorted(expected)
        assert len(listed) == len(set(listed))

    def test_one_queue_lists_every_item_in_scope_once_and_ends(self, service):
        expected = [
            k for k in self._seed_interleaved(service) if k.startswith("first-")
        ]
        listed = _all_pages(
            lambda c: service.list_queue_items(
                SCOPED, QueueRef(queue="first"), claimable=True, limit=2, cursor=c
            )
        )
        assert listed == expected

    def test_an_unscoped_caller_pages_through_every_item_in_order(self, service):
        self._seed_interleaved(service)
        listed = _all_pages(
            lambda c: service.list_queue_items(
                UNSCOPED, None, claimable=True, limit=3, cursor=c
            )
        )
        assert len(listed) == 32 and len(set(listed)) == 32
