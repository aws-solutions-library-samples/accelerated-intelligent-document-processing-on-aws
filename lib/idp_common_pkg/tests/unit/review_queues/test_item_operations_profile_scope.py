# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every review-queue item operation is held to the caller's configuration scope.

An Annotator or a Reviewer whose ``allowedConfigVersions`` does not admit an item's
profile can neither see it in any listing nor read, claim, save, skip, complete or
reject it — the queue they reach it through does not matter, and neither does already
holding it (a scope narrowed after the claim still refuses). An item in scope is
admitted, so these are not refusals of everything.
"""

import time
from unittest.mock import MagicMock

import pytest

from idp_common.review_queues.directory import QueueDirectory
from idp_common.review_queues.exceptions import ReviewAccessDenied
from idp_common.review_queues.models import (
    CallerContext,
    QueueRef,
    ReviewItemRecord,
    UserInfo,
)
from idp_common.review_queues.queue_defs import (
    DEFAULT_REJECT_REASONS,
    DEFAULT_SKIP_REASONS,
    QueueDefinition,
)
from idp_common.review_queues.service import ReviewQueueService
from idp_common.review_queues.store import ReviewStore, item_to_dynamo

ANN = "ann@example.com"
REV = "rev@example.com"
CYCLE = "0" * 21
IN_SCOPE = "profile-a"
OUT_OF_SCOPE = "profile-b"


def _caller(email, group):
    return CallerContext(
        reviewer_key=email,
        email=email,
        groups=frozenset({group}),
        allowed_versions=[IN_SCOPE],
    )


WORKERS = {
    "Annotator": _caller(ANN, "Annotator"),
    "Reviewer": _caller(REV, "Reviewer"),
}


@pytest.fixture
def service(review_table, monkeypatch):
    monkeypatch.setenv("HITL_TABLE_NAME", review_table)
    directory = QueueDirectory()
    directory.queue_defs.put(
        QueueDefinition(id="claims", eligible_annotators=[ANN]), create=True
    )
    directory.invalidate()

    def lookup(email):
        group = {ANN: "Annotator", REV: "Reviewer"}.get(email)
        return UserInfo(
            email=email, exists=group is not None, groups=frozenset({group} - {None})
        )

    return ReviewQueueService(
        ReviewStore(review_table),
        directory,
        notifier=MagicMock(),
        s3=MagicMock(),
        user_lookup=lookup,
    )


_clock = [int(time.time() * 1000)]


def _seed(service, doc_key, profile, owner=None):
    _clock[0] += 1000
    record = ReviewItemRecord(
        doc_key=doc_key,
        cycle_id=CYCLE,
        profile=profile,
        queue="claims",
        state="CLAIMED" if owner else "OPEN",
        owner_key=owner,
        enqueued_at="2026-10-16T00:00:00.000Z",
        enqueued_at_ms=_clock[0],
        sections=["1"],
        version=1,
    )
    service.store.client.put_item(item_to_dynamo(record))


#: Each operation a queue worker can call on one item, as (name, call).
OPERATIONS = [
    ("get_item_detail", lambda s, c, k: s.get_item_detail(c, k)),
    ("claim", lambda s, c, k: s.claim(c, k, CYCLE)),
    ("renew", lambda s, c, k: s.renew(c, k, CYCLE)),
    ("release", lambda s, c, k: s.release(c, k, CYCLE, DEFAULT_SKIP_REASONS[0])),
    ("save_draft", lambda s, c, k: s.save_draft(c, k, CYCLE, "1", {"a": 1}, None)),
    ("complete", lambda s, c, k: s.complete(c, k, CYCLE)),
    ("reject", lambda s, c, k: s.reject(c, k, CYCLE, DEFAULT_REJECT_REASONS[0])),
]


@pytest.mark.unit
@pytest.mark.parametrize("role", sorted(WORKERS))
class TestOutOfScopeItems:
    @pytest.mark.parametrize("name,call", OPERATIONS, ids=[o[0] for o in OPERATIONS])
    def test_an_open_item_outside_scope_is_refused(self, service, role, name, call):
        _seed(service, "out.pdf", OUT_OF_SCOPE)
        with pytest.raises(ReviewAccessDenied) as exc:
            call(service, WORKERS[role], "out.pdf")
        assert exc.value.code == "OUT_OF_SCOPE"
        assert service.store.get_item("out.pdf").state == "OPEN"

    @pytest.mark.parametrize("name,call", OPERATIONS, ids=[o[0] for o in OPERATIONS])
    def test_holding_an_item_does_not_outlast_a_narrowed_scope(
        self, service, role, name, call
    ):
        caller = WORKERS[role]
        _seed(service, "held.pdf", OUT_OF_SCOPE, owner=caller.reviewer_key)
        with pytest.raises(ReviewAccessDenied) as exc:
            call(service, caller, "held.pdf")
        assert exc.value.code == "OUT_OF_SCOPE"

    def test_no_listing_shows_it_and_claim_next_skips_it(self, service, role):
        caller = WORKERS[role]
        _seed(service, "out.pdf", OUT_OF_SCOPE)
        _seed(service, "held.pdf", OUT_OF_SCOPE, owner=caller.reviewer_key)
        one = service.list_queue_items(
            caller, QueueRef(queue="claims"), claimable=False, limit=50, cursor=None
        )
        every = service.list_queue_items(
            caller, None, claimable=False, limit=50, cursor=None
        )
        mine = service.list_my_work(caller, limit=50, cursor=None)
        assert [i.object_key for i in one.items + every.items + mine.items] == []
        assert service.claim_next(caller).item is None

    def test_an_item_in_scope_is_admitted(self, service, role):
        caller = WORKERS[role]
        _seed(service, "in.pdf", IN_SCOPE)
        listed = service.list_queue_items(
            caller, QueueRef(queue="claims"), claimable=True, limit=50, cursor=None
        )
        assert [i.object_key for i in listed.items] == ["in.pdf"]
        assert service.claim(caller, "in.pdf", CYCLE).state == "CLAIMED"
