# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reviewers work Production review queues, as Annotators do.

An Annotator works the queues that list them; a Reviewer works every Production queue,
listed or not (listing one changes nothing), and is never admitted to a queue that is
not defined. A Reviewer claims and lists items like an Annotator, within their
configuration-profile scope, and is never assigned them: only Annotators are assigned
work, so an assignment to a Reviewer is refused.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import (
    ReviewAccessDenied,
    ReviewValidationError,
)
from idp_common.review_queues.models import (
    CallerContext,
    QueueRef,
    ReviewItemRecord,
    UserInfo,
)
from idp_common.review_queues.queue_defs import QueueDefinition
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

ANN = "ann@example.com"
REV = "rev@example.com"
CYCLE = "0" * 21


def _caller(email, group, allowed=None):
    return CallerContext(
        reviewer_key=email,
        email=email,
        groups=frozenset({group}),
        allowed_versions=allowed,
    )


ANNOTATOR = _caller(ANN, "Annotator")
REVIEWER = _caller(REV, "Reviewer")
VIEWER = _caller("view@example.com", "Viewer")
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
        QueueDefinition(id="listed", eligible_annotators=[ANN]), create=True
    )
    d.queue_defs.put(QueueDefinition(id="unlisted"), create=True)
    d.invalidate()
    return d


def _users(**groups):
    def lookup(email):
        g = groups.get(email.split("@")[0])
        return UserInfo(email=email, exists=g is not None, groups=frozenset(g or ()))

    return lookup


@pytest.fixture
def service(directory, review_table):
    return ReviewQueueService(
        ReviewStore(review_table),
        directory,
        notifier=MagicMock(),
        s3=MagicMock(),
        user_lookup=_users(rev={"Reviewer"}, ann={"Annotator"}, view={"Viewer"}),
    )


_clock = [int(time.time() * 1000)]


def _seed(service, doc_key, queue, profile="default"):
    _clock[0] += 1000
    record = ReviewItemRecord(
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
    service.store.client.put_item(item_to_dynamo(record))


@pytest.mark.unit
class TestWhoWorksAQueue:
    def test_a_reviewer_works_every_queue_listed_or_not(self, directory):
        assert directory.is_member(REVIEWER, "", "listed")
        assert directory.is_member(REVIEWER, "", "unlisted")
        assert {r.queue for r, _, _ in directory.queues_for(REVIEWER)} == {
            "listed",
            "unlisted",
            "default",
        }

    def test_a_reviewer_is_not_admitted_to_an_undefined_queue(self, directory):
        assert not directory.is_member(REVIEWER, "", "no-such-queue")

    def test_an_annotator_still_works_only_the_queues_that_list_them(self, directory):
        assert directory.is_member(ANNOTATOR, "", "listed")
        assert not directory.is_member(ANNOTATOR, "", "unlisted")

    def test_a_viewer_works_no_queue(self, directory):
        assert not directory.is_member(VIEWER, "", "listed")
        assert directory.queues_for(VIEWER) == []


@pytest.mark.unit
class TestAReviewerWorksItems:
    def test_lists_and_claims_an_item_in_a_queue_that_does_not_list_them(self, service):
        _seed(service, "a.pdf", "unlisted")
        page = service.list_queue_items(
            REVIEWER, QueueRef(queue="unlisted"), claimable=True, limit=10, cursor=None
        )
        assert [i.object_key for i in page.items] == ["a.pdf"]
        assert service.claim(REVIEWER, "a.pdf", CYCLE).state == "CLAIMED"
        assert [
            i.object_key
            for i in service.list_my_work(REVIEWER, limit=10, cursor=None).items
        ] == ["a.pdf"]

    def test_claims_the_next_item_of_any_queue(self, service):
        _seed(service, "b.pdf", "unlisted")
        result = service.claim_next(REVIEWER)
        assert result.item is not None and result.item.object_key == "b.pdf"

    def test_profile_scope_still_applies(self, service):
        _seed(service, "c.pdf", "unlisted", profile="profile-b")
        scoped = _caller(REV, "Reviewer", allowed=["profile-a"])
        with pytest.raises(ReviewAccessDenied) as exc:
            service.claim(scoped, "c.pdf", CYCLE)
        assert exc.value.code == "OUT_OF_SCOPE"

    def test_a_viewer_is_refused(self, service):
        _seed(service, "d.pdf", "unlisted")
        with pytest.raises(ReviewAccessDenied) as exc:
            service.claim(VIEWER, "d.pdf", CYCLE)
        assert exc.value.code == "NOT_A_MEMBER"


@pytest.mark.unit
class TestAssigningWork:
    def test_an_admin_cannot_assign_an_item_to_a_reviewer_who_claims_their_own(
        self, service
    ):
        _seed(service, "e.pdf", "unlisted")
        with pytest.raises(ReviewValidationError) as exc:
            service.reassign(ADMIN, "e.pdf", CYCLE, REV)
        assert exc.value.code == "ASSIGNEE_NOT_ELIGIBLE"
        assert service.store.get_item("e.pdf").state == "OPEN"

    def test_an_admin_still_assigns_to_a_listed_annotator(self, service):
        _seed(service, "f.pdf", "listed")
        assert service.reassign(ADMIN, "f.pdf", CYCLE, ANN).state == "CLAIMED"
