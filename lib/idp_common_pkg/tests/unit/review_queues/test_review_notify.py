# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue Inbox notices: who receives them, how they are chunked, and that a
failed notice never fails the claim, disposition or sweep that sent it."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from idp_common.notifications.models import MAX_GROUPS, MAX_USERS
from idp_common.review_queues import notify as review_notify
from idp_common.review_queues.models import QueueRef, ReviewItemRecord

NOTIFY = "idp_common.review_queues.notify.notify"


def _item(**overrides):
    fields = dict(
        doc_key="batch/2026/invoice 7.pdf",
        cycle_id="c1",
        profile="profile-a",
        queue="Escalation",
        state="open",
        version=3,
        enqueued_at="2026-10-18T00:00:00.000Z",
        enqueued_at_ms=1,
        owner_email="ann@example.com",
        owner_key="RT#ann@example.com",
        lease_expires_at_ms=500,
        claimed_at_ms=400,
    )
    fields.update(overrides)
    return ReviewItemRecord(**fields)


@pytest.mark.unit
class TestPost:
    def test_dedupes_and_lowercases_users_and_drops_empty_entries(self):
        with patch(NOTIFY) as sent:
            calls = review_notify._post(
                type_id="t",
                title="x",
                body="y",
                link="/l",
                users=[" Ann@Example.com", "ann@example.com", ""],
                groups=["Admin", "Admin", ""],
            )
        assert calls == 2
        assert sent.call_args_list[0].kwargs["users"] == ["ann@example.com"]
        assert sent.call_args_list[1].kwargs["groups"] == ["Admin"]
        assert all(
            c.kwargs["source"] == review_notify.SOURCE for c in sent.call_args_list
        )

    def test_chunks_recipients_to_the_service_limits(self):
        users = [f"u{i}@example.com" for i in range(MAX_USERS + 1)]
        groups = [f"g{i}" for i in range(MAX_GROUPS + 1)]
        with patch(NOTIFY) as sent:
            calls = review_notify._post(
                type_id="t", title="x", body="y", link="/l", users=users, groups=groups
            )
        assert calls == 4
        sizes = [
            (len(c.kwargs["users"]), len(c.kwargs["groups"]))
            for c in sent.call_args_list
        ]
        assert sizes == [(MAX_USERS, 0), (1, 0), (0, MAX_GROUPS), (0, 1)]

    def test_no_recipients_sends_nothing(self):
        with patch(NOTIFY) as sent:
            assert review_notify._post(type_id="t", title="x", body="y", link="/l") == 0
        sent.assert_not_called()

    def test_a_failing_notice_is_counted_and_never_raised(self):
        with (
            patch(NOTIFY, side_effect=RuntimeError("inbox down")),
            patch("idp_common.metrics.put_metric") as metric,
        ):
            calls = review_notify._post(
                type_id="t", title="x", body="y", link="/l", users=["a@example.com"]
            )
        assert calls == 1
        metric.assert_called_once_with("ReviewNoticeFailures", 1)


@pytest.mark.unit
class TestNotices:
    def test_item_links_match_the_ui_route(self):
        assert (
            review_notify.review_item_link("a/b c.pdf")
            == "/review-queues/items/a%2Fb%20c.pdf"
        )

    def test_queue_items_waiting_is_debounced_by_the_store(self):
        ref = QueueRef(profile="profile-a", queue="default")
        members = (["Annotator"], ["ann@example.com"])
        store = SimpleNamespace(try_announce=lambda ref, now_ms: False)
        with patch(NOTIFY) as sent:
            assert review_notify.queue_items_waiting(store, ref, members, 0) is False
            sent.assert_not_called()
            store.try_announce = lambda ref, now_ms: True
            assert review_notify.queue_items_waiting(store, ref, members, 0) is True
        assert sent.call_args.kwargs["dedupe_key"] == "review-queue:profile-a#default"

    def test_queue_items_waiting_survives_a_failing_debounce(self):
        def boom(ref, now_ms):
            raise RuntimeError("ddb")

        store = SimpleNamespace(try_announce=boom)
        ref = QueueRef(profile="p", queue="q")
        with patch(NOTIFY) as sent, patch("idp_common.metrics.put_metric"):
            assert (
                review_notify.queue_items_waiting(store, ref, ([], ["a@x.com"]), 0)
                is False
            )
        sent.assert_not_called()
        # Nobody to tell: nothing is even attempted.
        assert review_notify.queue_items_waiting(store, ref, ([], []), 0) is False

    @pytest.mark.parametrize(
        ("send", "type_id", "key"),
        [
            (
                lambda i: review_notify.item_reassigned(i, "bob@example.com"),
                "review.item_reassigned",
                "review-reassign:c1#3",
            ),
            (
                lambda i: review_notify.claim_released(i, "bob@example.com", "move"),
                "review.claim_released",
                "review-release:c1#3",
            ),
            (
                review_notify.claim_expiring,
                "review.claim_expiring",
                "review-lease-warn:c1:500",
            ),
            (
                review_notify.claim_expired,
                "review.claim_expired",
                "review-lease-expired:c1:500",
            ),
            (
                review_notify.task_reminder,
                "review.task_reminder",
                "review-task-reminder:c1:400",
            ),
        ],
    )
    def test_each_notice_names_the_document_and_is_idempotent(self, send, type_id, key):
        with patch(NOTIFY) as sent:
            send(_item())
        kwargs = sent.call_args.kwargs
        assert kwargs["type_id"] == type_id
        assert kwargs["idempotency_key"] == key
        assert kwargs["title"].startswith("invoice 7.pdf")
        assert kwargs["link"] == review_notify.review_item_link(
            "batch/2026/invoice 7.pdf"
        )

    def test_owner_notices_need_an_owner(self):
        with patch(NOTIFY) as sent:
            for send in (
                review_notify.claim_expiring,
                review_notify.claim_expired,
                review_notify.task_reminder,
            ):
                send(_item(owner_email=None))
        sent.assert_not_called()

    def test_escalation_goes_to_the_queue_members_only_when_there_are_any(self):
        with patch(NOTIFY) as sent:
            review_notify.item_escalated(_item(), ([], []))
            sent.assert_not_called()
            review_notify.item_escalated(_item(), (["Admin"], []))
        assert sent.call_args.kwargs["groups"] == ["Admin"]
        assert "escalated" in sent.call_args.kwargs["title"]

    def test_an_unknown_release_reason_still_says_the_draft_is_kept(self):
        with patch(NOTIFY) as sent:
            review_notify.claim_released(_item(), "bob@example.com", "something-new")
        assert sent.call_args.kwargs["body"] == "Your claim ended. Your draft is kept."
