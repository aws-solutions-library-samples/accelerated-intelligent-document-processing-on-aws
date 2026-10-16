# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""NotificationService.post: delivery, muting, fan-out, idempotency, dedupe, cap."""

import logging
from unittest import mock

import pytest

from idp_common.notifications import recipients as recipients_module
from idp_common.notifications.exceptions import (
    NotificationValidationError,
    UnknownNotificationTypeError,
)
from idp_common.notifications.keys import new_notification_id, partition_key
from idp_common.notifications.models import NotificationRequest

from .conftest import message_items, put_raw_message

POOL = "us-east-1_TESTPOOL"
ALICE = "alice@example.com"


def _req(**overrides):
    base = {
        "type_id": "test.sample",
        "title": "Hello",
        "source": "tests",
        "users": [ALICE],
    }
    base.update(overrides)
    return NotificationRequest(**base)


def _member(email, enabled=True):
    return {
        "Username": email,
        "Enabled": enabled,
        "Attributes": [{"Name": "email", "Value": email}],
    }


@pytest.mark.unit
class TestSingleUser:
    def test_one_unread_item_with_ttl_and_type_severity(
        self, make_service, test_type, clock
    ):
        service = make_service()
        result = service.post(_req())
        assert result.delivered == 1 and result.recipients_considered == 1
        items = message_items(service.store, ALICE)
        assert len(items) == 1
        item = items[0]
        assert item["UnreadPK"] == partition_key(ALICE)
        assert "ReadAt" not in item
        assert item["Severity"] == test_type.default_severity
        assert int(item["ExpiresAfter"]) == int(clock.reads[0].timestamp()) + 30 * 86400
        assert item["CreatedAt"].endswith("Z")
        assert result.deliveries[0].notification_id == item["SK"][2:]

    def test_explicit_severity_wins(self, make_service, test_type):
        service = make_service()
        service.post(_req(severity="error"))
        assert message_items(service.store, ALICE)[0]["Severity"] == "error"

    def test_metadata_and_link_stored(self, make_service, test_type):
        service = make_service()
        service.post(_req(metadata={"a": 1.5, "b": True}, link="/inbox"))
        item = message_items(service.store, ALICE)[0]
        assert item["MetadataJson"] == '{"a":1.5,"b":true}'
        assert item["Link"] == "/inbox"

    def test_unregistered_type_is_refused(self, make_service):
        service = make_service()
        with pytest.raises(UnknownNotificationTypeError):
            service.post(_req(type_id="not.registered"))
        assert message_items(service.store, ALICE) == []


@pytest.mark.unit
class TestMute:
    def test_muted_recipient_stores_nothing(self, make_service, test_type):
        service = make_service()
        service.update_preferences(ALICE, ["test.sample"])
        before = service.unread_count(ALICE, consistent=True)
        result = service.post(_req())
        assert result.muted == 1 and result.delivered == 0
        assert message_items(service.store, ALICE) == []
        assert service.unread_count(ALICE, consistent=True) == before

    def test_group_fanout_honours_mutes_past_one_batch_get_chunk(
        self, make_service, test_type
    ):
        members = [_member(f"m{i:03d}@x.com") for i in range(150)]
        service = make_service(
            group_lister=lambda pool, group: iter(members), user_pool_id=POOL
        )
        service.update_preferences("m010@x.com", ["test.sample"])
        service.update_preferences("m140@x.com", ["test.sample"])
        result = service.post(_req(users=[], groups=["Reviewer"]))
        assert result.recipients_considered == 150
        assert result.muted == 2
        assert result.delivered == 148
        assert message_items(service.store, "m140@x.com") == []
        assert len(message_items(service.store, "m141@x.com")) == 1

    def test_unmute_lets_new_posts_arrive(self, make_service, test_type):
        service = make_service()
        service.update_preferences(ALICE, ["test.sample"])
        service.post(_req())
        service.update_preferences(ALICE, [])
        service.post(_req())
        assert len(message_items(service.store, ALICE)) == 1


@pytest.mark.unit
class TestFanOut:
    def test_paged_lister_disabled_and_dedupe(self, make_service, test_type):
        pages = {
            "Reviewer": [
                _member("a@x.com"),
                _member("b@x.com", enabled=False),
                _member("c@x.com"),
            ],
            "Admin": [_member("a@x.com"), _member("d@x.com")],
        }
        calls = []

        def lister(pool, group):
            calls.append((pool, group))
            yield from pages[group]

        service = make_service(group_lister=lister, user_pool_id=POOL)
        result = service.post(_req(users=["C@x.com"], groups=["Reviewer", "Admin"]))
        assert calls == [(POOL, "Reviewer"), (POOL, "Admin")]
        assert result.recipients_considered == 3  # c, a, d
        assert result.delivered == 3
        assert message_items(service.store, "b@x.com") == []
        assert len(message_items(service.store, "c@x.com")) == 1

    def test_truncation(self, make_service, test_type, caplog):
        caplog.set_level(logging.WARNING)
        members = [_member(f"m{i}@x.com") for i in range(10)]
        service = make_service(
            group_lister=lambda pool, group: iter(members), user_pool_id=POOL
        )
        with mock.patch.object(recipients_module, "MAX_RECIPIENTS", 3):
            result = service.post(_req(users=[], groups=["G"]))
        assert result.truncated is True
        assert result.recipients_considered == 3
        assert any("inbox_fanout_truncated" in r.message for r in caplog.records)

    def test_groups_without_pool_raise(self, make_service, test_type):
        service = make_service()
        with pytest.raises(NotificationValidationError):
            service.post(_req(users=[], groups=["G"]))


@pytest.mark.unit
class TestIdempotency:
    def test_second_post_is_duplicate(self, make_service, test_type):
        service = make_service()
        first = service.post(_req(idempotency_key="evt-1"))
        second = service.post(_req(idempotency_key="evt-1"))
        assert first.delivered == 1
        assert second.duplicates == 1 and second.delivered == 0
        assert len(message_items(service.store, ALICE)) == 1

    def test_repost_after_delete_is_still_duplicate(self, make_service, test_type):
        service = make_service()
        first = service.post(_req(idempotency_key="evt-1"))
        service.delete(ALICE, [first.deliveries[0].notification_id])
        again = service.post(_req(idempotency_key="evt-1"))
        assert again.duplicates == 1
        assert message_items(service.store, ALICE) == []


@pytest.mark.unit
class TestDedupe:
    def test_replaces_leaving_exactly_one_unread_newest(self, make_service, test_type):
        service = make_service()
        service.post(_req(dedupe_key="lease:1", title="first"))
        second = service.post(_req(dedupe_key="lease:1", title="second"))
        items = message_items(service.store, ALICE)
        assert len(items) == 1
        assert items[0]["Title"] == "second"
        assert items[0]["SK"][2:] == second.deliveries[0].notification_id
        assert "ReadAt" not in items[0]

    def test_unread_delta(self, make_service, test_type):
        service = make_service()
        service.post(_req(dedupe_key="k"))
        assert service.unread_count(ALICE, consistent=True) == 1
        service.post(_req(dedupe_key="k"))
        assert service.unread_count(ALICE, consistent=True) == 1  # +0: old was unread
        service.mark_all_read(ALICE)
        assert service.unread_count(ALICE, consistent=True) == 0
        service.post(_req(dedupe_key="k"))
        assert service.unread_count(ALICE, consistent=True) == 1  # +1: old was read
        assert len(message_items(service.store, ALICE)) == 1

    def test_contention_retries_once(self, make_service, test_type):
        service = make_service()
        service.post(_req(dedupe_key="k"))
        real = service.store.get_pointer
        calls = {"n": 0}

        def stale_then_real(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "N#" + new_notification_id(1, "000000000000")
            return real(*args, **kwargs)

        with (
            mock.patch.object(
                service.store, "get_pointer", side_effect=stale_then_real
            ),
            mock.patch.object(
                service.store,
                "write_transactional",
                wraps=service.store.write_transactional,
            ) as writes,
        ):
            result = service.post(_req(dedupe_key="k"))
        assert result.delivered == 1
        assert calls["n"] == 2
        assert writes.call_count == 2
        assert len(message_items(service.store, ALICE)) == 1


@pytest.mark.unit
class TestCap:
    def test_read_is_evicted_first(self, make_service, test_type):
        service = make_service(max_per_user=3)
        ids = [
            service.post(_req(title=f"m{i}")).deliveries[0].notification_id
            for i in range(3)
        ]
        service.mark(ALICE, [ids[1]], read=True)
        service.post(_req(title="m3"))
        remaining = [i["SK"][2:] for i in message_items(service.store, ALICE)]
        assert len(remaining) == 3
        assert ids[1] not in remaining, "the read message should have been evicted"
        assert ids[0] in remaining

    def test_all_unread_evicts_oldest_unread_and_warns(
        self, make_service, test_type, caplog
    ):
        caplog.set_level(logging.WARNING)
        service = make_service(max_per_user=3)
        ids = [service.post(_req()).deliveries[0].notification_id for _ in range(4)]
        remaining = [i["SK"][2:] for i in message_items(service.store, ALICE)]
        assert remaining == ids[1:]
        assert any("inbox_cap_evicted_unread" in r.message for r in caplog.records)

    def test_expired_items_are_deleted_first(self, make_service, test_type, clock):
        service = make_service(max_per_user=3)
        expired_id = new_notification_id(1, "aaaaaaaaaaaa")
        put_raw_message(service.store, ALICE, expired_id, expires_after=clock.epoch - 5)
        ids = [service.post(_req()).deliveries[0].notification_id for _ in range(3)]
        remaining = [i["SK"][2:] for i in message_items(service.store, ALICE)]
        assert remaining == ids  # the expired one went; no live message was evicted

    def test_interleaved_trims_converge(self, make_service, test_type, clock):
        service = make_service(max_per_user=3)
        base = int(clock.now.timestamp() * 1000)
        for i in range(5):
            put_raw_message(
                service.store,
                ALICE,
                new_notification_id(base + i, f"{i:012x}"),
                expires_after=clock.epoch + 3600,
                read=(i == 2),
            )
        pk = partition_key(ALICE)
        service.store.trim(pk, 3, clock.epoch)
        service.store.trim(pk, 3, clock.epoch)
        assert len(message_items(service.store, ALICE)) == 3

    def test_trim_failure_keeps_delivered_and_reports(
        self, make_service, test_type, caplog
    ):
        caplog.set_level(logging.ERROR)
        service = make_service()
        with (
            mock.patch.object(service.store, "trim", side_effect=RuntimeError("x")),
            mock.patch("idp_common.notifications.service.put_metric") as metric,
        ):
            result = service.post(_req())
        assert result.delivered == 1
        assert any("inbox_cap_trim_failed" in r.message for r in caplog.records)
        metric.assert_called_once_with("NotificationCapTrimFailures", 1)
