# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading and mutating a recipient's own Inbox."""

from unittest import mock

import pytest

from idp_common.notifications.exceptions import (
    NotificationNotFoundError,
    UnknownNotificationTypeError,
)
from idp_common.notifications.keys import new_notification_id, partition_key
from idp_common.notifications.models import NotificationRequest, NotificationType

from .conftest import message_items, put_raw_message

ALICE = "alice@example.com"
BOB = "bob@example.com"
OTHER_TYPE = NotificationType(id="test.other", display_name="Other", description="o")


def _post(service, rk=ALICE, **overrides):
    base = {"type_id": "test.sample", "title": "t", "source": "tests", "users": [rk]}
    base.update(overrides)
    return service.post(NotificationRequest(**base)).deliveries[0].notification_id


@pytest.fixture
def other_type():
    from idp_common.notifications.catalogue import register_type, unregister_type

    register_type(OTHER_TYPE)
    yield OTHER_TYPE
    unregister_type(OTHER_TYPE.id)


@pytest.mark.unit
class TestList:
    def test_newest_first(self, make_service, test_type):
        service = make_service()
        ids = [_post(service) for _ in range(3)]
        page = service.list_messages(ALICE)
        assert [m.id for m in page.items] == list(reversed(ids))
        assert page.unread_count == 3

    def test_filters(self, make_service, test_type, other_type):
        service = make_service()
        a = _post(service, severity="error")
        b = _post(service, type_id="test.other")
        c = _post(service, severity="warning")
        service.mark(ALICE, [c], read=True)

        def ids(**kw):
            return {m.id for m in service.list_messages(ALICE, **kw).items}

        assert ids(read=False) == {a, b}
        assert ids(read=True) == {c}
        assert ids(types=["test.other"]) == {b}
        assert ids(severities=["error", "warning"]) == {a, c}
        assert ids(read=False, types=["test.sample"], severities=["error"]) == {a}

    def test_list_unread_count_is_total_not_filtered(self, make_service, test_type):
        service = make_service()
        _post(service)
        _post(service)
        page = service.list_messages(ALICE, read=True)
        assert page.items == [] and page.unread_count == 2

    def test_expired_excluded_from_list_and_both_counts(
        self, make_service, test_type, clock
    ):
        service = make_service()
        live = _post(service)
        put_raw_message(
            service.store,
            ALICE,
            new_notification_id(1, "bbbbbbbbbbbb"),
            expires_after=clock.epoch - 1,
        )
        pk = partition_key(ALICE)
        assert [m.id for m in service.list_messages(ALICE).items] == [live]
        assert service.store.unread_count_index(pk, clock.epoch) == 1
        assert service.store.unread_count_consistent(pk, clock.epoch) == 1


@pytest.mark.unit
class TestMark:
    def test_mark_read_is_idempotent_and_keeps_first_read_at(
        self, make_service, test_type
    ):
        service = make_service()
        msg = _post(service)
        first = service.mark(ALICE, [msg], read=True)
        read_at = message_items(service.store, ALICE)[0]["ReadAt"]
        second = service.mark(ALICE, [msg], read=True)
        assert first.updated == 1 and second.updated == 1
        assert first.unread_count == 0
        item = message_items(service.store, ALICE)[0]
        assert item["ReadAt"] == read_at
        assert "UnreadPK" not in item

    def test_mark_unread_restores_unread_pk(self, make_service, test_type):
        service = make_service()
        msg = _post(service)
        service.mark(ALICE, [msg], read=True)
        result = service.mark(ALICE, [msg], read=False)
        item = message_items(service.store, ALICE)[0]
        assert item["UnreadPK"] == partition_key(ALICE)
        assert "ReadAt" not in item
        assert result.unread_count == 1
        assert service.unread_count(ALICE) == 1

    def test_other_users_id_is_not_found(self, make_service, test_type):
        service = make_service()
        bobs = _post(service, rk=BOB)
        with pytest.raises(NotificationNotFoundError):
            service.mark(ALICE, [bobs], read=True)
        assert "ReadAt" not in message_items(service.store, BOB)[0]

    def test_mixed_ids_report_not_found(self, make_service, test_type):
        service = make_service()
        mine = _post(service)
        bobs = _post(service, rk=BOB)
        result = service.mark(ALICE, [mine, bobs], read=True)
        assert result.updated == 1
        assert result.not_found_ids == [bobs]

    def test_mark_all_read(self, make_service, test_type):
        service = make_service()
        for _ in range(3):
            _post(service)
        result = service.mark_all_read(ALICE)
        assert result.updated == 3 and result.unread_count == 0
        assert service.mark_all_read(ALICE).updated == 0

    def test_mutation_count_is_consistent(self, make_service, test_type):
        service = make_service()
        msg = _post(service)
        with mock.patch.object(
            service.store.client,
            "query_pages",
            wraps=service.store.client.query_pages,
        ) as spy:
            service.mark(ALICE, [msg], read=True)
        assert spy.call_args.kwargs["consistent_read"] is True
        assert spy.call_args.kwargs.get("index_name") is None


@pytest.mark.unit
class TestDelete:
    def test_delete(self, make_service, test_type):
        service = make_service()
        keep, drop = _post(service), _post(service)
        result = service.delete(ALICE, [drop])
        assert result.updated == 1 and result.not_found_ids == []
        assert [i["SK"][2:] for i in message_items(service.store, ALICE)] == [keep]

    def test_delete_other_users_id_is_not_found(self, make_service, test_type):
        service = make_service()
        bobs = _post(service, rk=BOB)
        with pytest.raises(NotificationNotFoundError):
            service.delete(ALICE, [bobs])
        assert len(message_items(service.store, BOB)) == 1

    def test_delete_mixed(self, make_service, test_type):
        service = make_service()
        mine, bobs = _post(service), _post(service, rk=BOB)
        result = service.delete(ALICE, [mine, bobs])
        assert result.updated == 1 and result.not_found_ids == [bobs]

    def test_clear_read_removes_only_read(self, make_service, test_type):
        service = make_service()
        read_one, unread_one = _post(service), _post(service)
        service.mark(ALICE, [read_one], read=True)
        result = service.clear_read(ALICE)
        assert result.updated == 1 and result.unread_count == 1
        assert [i["SK"][2:] for i in message_items(service.store, ALICE)] == [
            unread_one
        ]


@pytest.mark.unit
class TestPreferences:
    def test_empty_catalogue(self, make_service):
        prefs = make_service().get_preferences(ALICE)
        assert [t for t in prefs.types if t.id.startswith("test.")] == []
        assert prefs.muted_types == [] and prefs.updated_at is None

    def test_get_update_get(self, make_service, test_type):
        service = make_service()
        assert service.get_preferences(ALICE).types[0].muted is False
        prefs = service.update_preferences(ALICE, ["test.sample", "test.sample"])
        assert prefs.muted_types == ["test.sample"]
        row = next(t for t in prefs.types if t.id == "test.sample")
        assert row.muted is True and row.display_name == "Sample"
        assert prefs.updated_at

    def test_unknown_id_is_rejected(self, make_service, test_type):
        with pytest.raises(UnknownNotificationTypeError):
            make_service().update_preferences(ALICE, ["no.such"])

    def test_dump_uses_camel_case(self, make_service, test_type):
        dumped = (
            make_service().get_preferences(ALICE).model_dump(by_alias=True, mode="json")
        )
        assert set(dumped) == {"types", "mutedTypes", "updatedAt"}
        assert set(dumped["types"][0]) == {
            "id",
            "displayName",
            "description",
            "defaultSeverity",
            "muted",
        }
