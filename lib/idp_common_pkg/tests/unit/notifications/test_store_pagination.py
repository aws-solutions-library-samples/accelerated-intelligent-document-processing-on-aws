# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""List pagination touches only live ``N#`` items and never ends on an empty page."""

import pytest

from idp_common.notifications.keys import (
    PREFS_SK,
    dedupe_sk,
    idempotency_sk,
    new_notification_id,
    partition_key,
)

from .conftest import put_raw_message

RK = "pager@example.com"


@pytest.fixture
def seeded(make_service, clock):
    service = make_service()
    store = service.store
    pk = partition_key(RK)
    base_ms = int(clock.now.timestamp() * 1000)
    future = clock.epoch + 86_400
    live_ids = []
    for i in range(7):
        msg_id = new_notification_id(base_ms + i, f"{i:012x}")
        # Two of the seven are expired (TTL passed, not yet deleted).
        expired = i in (1, 4)
        put_raw_message(
            store, RK, msg_id, expires_after=clock.epoch - 10 if expired else future
        )
        if not expired:
            live_ids.append(msg_id)
    for sk in (dedupe_sk("k"), idempotency_sk("k"), PREFS_SK):
        store.client.put_item({"PK": pk, "SK": sk, "ExpiresAfter": future})
    return service, list(reversed(live_ids))  # newest first


@pytest.mark.unit
class TestPagination:
    def test_store_list_page_limit_and_has_more(self, seeded, clock):
        service, newest_first = seeded
        items, has_more = service.store.list_page(
            partition_key(RK),
            read=None,
            types=None,
            severities=None,
            limit=2,
            after_id=None,
            now=clock.epoch,
        )
        assert [i["SK"][2:] for i in items] == newest_first[:2]
        assert has_more is True

    def test_cursor_walk_has_no_empty_trailing_page(self, seeded):
        service, newest_first = seeded
        first = service.list_messages(RK, limit=2)
        assert [m.id for m in first.items] == newest_first[0:2]
        assert first.next_token
        second = service.list_messages(RK, limit=2, next_token=first.next_token)
        assert [m.id for m in second.items] == newest_first[2:4]
        assert second.next_token
        third = service.list_messages(RK, limit=2, next_token=second.next_token)
        assert [m.id for m in third.items] == newest_first[4:5]
        assert third.next_token is None, "the last page must not promise another"

    def test_exact_multiple_ends_without_a_token(self, seeded):
        service, newest_first = seeded
        page = service.list_messages(RK, limit=5)
        assert [m.id for m in page.items] == newest_first
        assert page.next_token is None

    def test_only_live_message_items_appear(self, seeded):
        service, newest_first = seeded
        page = service.list_messages(RK, limit=100)
        assert [m.id for m in page.items] == newest_first
        assert all(len(m.id) == 25 for m in page.items)
        assert page.unread_count == 5
