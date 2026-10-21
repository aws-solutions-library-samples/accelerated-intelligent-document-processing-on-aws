# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the notifications_resolver Lambda (the Inbox API).

Runs the real handler over the real NotificationService on a moto table.
"""

import importlib
import logging
import os
import sys
import time

import pytest
from moto import mock_aws

from idp_common.notifications.catalogue import register_type, unregister_type
from idp_common.notifications.keys import (
    encode_cursor,
    message_sk,
    new_notification_id,
    partition_key,
)
from idp_common.notifications.models import NotificationType
from tests.unit.notifications._table import REGION, create_notifications_table

LAMBDA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../../nested/api-resolvers/src/lambda/notifications_resolver",
    )
)

ALICE_EMAIL = "Alice.Example@Example.COM"
ALICE_SUB = "11111111-aaaa-4bbb-8ccc-000000000001"
BOB_EMAIL = "bob@example.com"
BOB_SUB = "22222222-aaaa-4bbb-8ccc-000000000002"
SECRET_TITLE = "Quarterly figures for review"

PROBE_TYPE = NotificationType(
    id="test.resolver", display_name="Resolver probe", description="Test-only type"
)


def _claims(email, sub, groups=("Viewer",)):
    return {
        "email": email,
        "sub": sub,
        "cognito:username": sub,
        "cognito:groups": list(groups),
    }


def _event(field, args=None, *, claims=None):
    return {
        "info": {"fieldName": field},
        "arguments": {} if args is None else args,
        "identity": {
            "claims": _claims(ALICE_EMAIL, ALICE_SUB) if claims is None else claims
        },
    }


@pytest.fixture()
def mod(monkeypatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    with mock_aws():
        monkeypatch.setenv("NOTIFICATIONS_TABLE_NAME", create_notifications_table())
        register_type(PROBE_TYPE)
        sys.path.insert(0, LAMBDA_DIR)
        sys.modules.pop("index", None)
        try:
            yield importlib.import_module("index")
        finally:
            sys.path.remove(LAMBDA_DIR)
            sys.modules.pop("index", None)
            unregister_type(PROBE_TYPE.id)


def _seed(mod, rk, *, n=1, read=False, title=SECRET_TITLE):
    """Write ``n`` live messages for ``rk`` directly; return their ids, oldest first."""
    store = mod._SERVICE.store
    pk = partition_key(rk)
    now_ms = int(time.time() * 1000) - 10_000
    ids = []
    for i in range(n):
        msg_id = new_notification_id(now_ms + i)
        item = {
            "PK": pk,
            "SK": message_sk(msg_id),
            "Type": PROBE_TYPE.id,
            "Title": title,
            "Body": "",
            "Severity": "info",
            "Source": "tests",
            "CreatedAt": "2026-01-01T00:00:00.000Z",
            "ExpiresAfter": int(time.time()) + 86400,
        }
        if read:
            item["ReadAt"] = "2026-01-01T00:00:01.000Z"
        else:
            item["UnreadPK"] = pk
        store.client.put_item(item)
        ids.append(msg_id)
    return ids


ALICE_RK = ALICE_EMAIL.strip().lower()


@pytest.mark.unit
class TestRouting:
    def test_list_routes_and_dumps_camel_case(self, mod):
        ids = _seed(mod, ALICE_RK, n=3)
        out = mod.handler(_event("listInboxMessages", {"limit": 2}), None)
        assert set(out) == {"items", "nextToken", "unreadCount"}
        assert [m["id"] for m in out["items"]] == list(reversed(ids))[:2]
        assert out["nextToken"] is not None
        assert out["unreadCount"] == 3
        assert {"createdAt", "readAt", "read"} <= set(out["items"][0])

    def test_unread_count(self, mod):
        _seed(mod, ALICE_RK, n=2)
        _seed(mod, ALICE_RK, n=1, read=True)
        assert mod.handler(_event("getInboxUnreadCount"), None) == 2

    def test_get_and_update_preferences(self, mod):
        out = mod.handler(_event("getInboxPreferences"), None)
        assert out["mutedTypes"] == []
        assert PROBE_TYPE.id in {t["id"] for t in out["types"]}
        out = mod.handler(
            _event("updateInboxPreferences", {"mutedTypes": [PROBE_TYPE.id]}), None
        )
        assert out["mutedTypes"] == [PROBE_TYPE.id]
        row = next(t for t in out["types"] if t["id"] == PROBE_TYPE.id)
        assert row["muted"] is True
        assert {"displayName", "defaultSeverity"} <= set(row)
        assert out["updatedAt"]

    def test_mark_read_and_unread(self, mod):
        (msg_id,) = _seed(mod, ALICE_RK)
        out = mod.handler(
            _event("markInboxMessages", {"ids": [msg_id], "read": True}), None
        )
        assert out == {"updated": 1, "notFoundIds": [], "unreadCount": 0}
        out = mod.handler(
            _event("markInboxMessages", {"ids": [msg_id], "read": False}), None
        )
        assert out["unreadCount"] == 1

    def test_mark_all_read(self, mod):
        _seed(mod, ALICE_RK, n=3)
        out = mod.handler(_event("markAllInboxMessagesRead"), None)
        assert out == {"updated": 3, "notFoundIds": [], "unreadCount": 0}

    def test_delete(self, mod):
        keep, gone = _seed(mod, ALICE_RK, n=2)
        out = mod.handler(_event("deleteInboxMessages", {"ids": [gone]}), None)
        assert out == {"updated": 1, "notFoundIds": [], "unreadCount": 1}
        listed = mod.handler(_event("listInboxMessages"), None)
        assert [m["id"] for m in listed["items"]] == [keep]

    def test_clear_read(self, mod):
        (unread,) = _seed(mod, ALICE_RK)
        _seed(mod, ALICE_RK, n=2, read=True)
        out = mod.handler(_event("clearReadInboxMessages"), None)
        assert out["updated"] == 2
        listed = mod.handler(_event("listInboxMessages"), None)
        assert [m["id"] for m in listed["items"]] == [unread]

    def test_unknown_field_is_value_error(self, mod):
        with pytest.raises(ValueError) as exc:
            mod.handler(_event("listInboxNotifications"), None)
        assert type(exc.value) is ValueError

    def test_unconfigured_store_is_runtime_error(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_SERVICE", None)
        with pytest.raises(RuntimeError):
            mod.handler(_event("listInboxMessages"), None)


@pytest.mark.unit
class TestIdentity:
    def test_user_argument_is_rejected(self, mod):
        with pytest.raises(ValueError) as exc:
            mod.handler(_event("listInboxMessages", {"userId": BOB_EMAIL}), None)
        assert type(exc.value) is ValueError
        assert "userId" in str(exc.value)
        assert BOB_EMAIL not in str(exc.value)

    def test_no_arg_operation_rejects_arguments(self, mod):
        with pytest.raises(ValueError) as exc:
            mod.handler(_event("getInboxUnreadCount", {"userId": BOB_EMAIL}), None)
        assert type(exc.value) is ValueError
        assert BOB_EMAIL not in str(exc.value)

    def test_mixed_case_email_claim_reads_lowercased_partition(self, mod):
        (msg_id,) = _seed(mod, "alice.example@example.com")
        out = mod.handler(_event("listInboxMessages"), None)
        assert [m["id"] for m in out["items"]] == [msg_id]

    def test_other_users_messages_are_invisible(self, mod):
        _seed(mod, ALICE_RK, n=2)
        bob = _claims(BOB_EMAIL, BOB_SUB)
        out = mod.handler(_event("listInboxMessages", claims=bob), None)
        assert out["items"] == [] and out["unreadCount"] == 0

    def test_forged_cursor_stays_in_callers_partition(self, mod):
        ids = _seed(mod, ALICE_RK, n=2)
        bob = _claims(BOB_EMAIL, BOB_SUB)
        token = encode_cursor(new_notification_id(int(time.time() * 1000) + 60_000))
        out = mod.handler(
            _event("listInboxMessages", {"nextToken": token}, claims=bob), None
        )
        assert not {m["id"] for m in out["items"]} & set(ids)

    def test_no_groups_is_permission_error_logged_at_warning(self, mod, caplog):
        claims = _claims(ALICE_EMAIL, ALICE_SUB, groups=())
        with caplog.at_level(logging.WARNING):
            with pytest.raises(PermissionError):
                mod.handler(_event("listInboxMessages", claims=claims), None)
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Rejecting" in r.getMessage() for r in warnings)
        assert all(r.exc_info is None for r in warnings)

    def test_no_identity_claim_is_permission_error(self, mod):
        claims = {"cognito:groups": ["Viewer"]}
        with pytest.raises(PermissionError):
            mod.handler(_event("listInboxMessages", claims=claims), None)

    def test_logs_carry_no_identity_or_message_text(self, mod, caplog):
        (msg_id,) = _seed(mod, ALICE_RK)
        # INFO is the deployed LOG_LEVEL; DEBUG wire traces are botocore's own.
        with caplog.at_level(logging.INFO):
            mod.handler(_event("listInboxMessages"), None)
            mod.handler(
                _event("markInboxMessages", {"ids": [msg_id], "read": True}), None
            )
        text = "\n".join(r.getMessage() for r in caplog.records).lower()
        assert "field=listInboxMessages".lower() in text
        for secret in (ALICE_RK, ALICE_SUB, SECRET_TITLE.lower()):
            assert secret not in text


@pytest.mark.unit
class TestErrorClasses:
    """The dispatcher maps by class name, so the class must be exact (D20)."""

    @pytest.mark.parametrize(
        "field,args",
        [
            ("listInboxMessages", {"nextToken": "not-a-cursor"}),
            ("listInboxMessages", {"limit": "ten"}),
            ("listInboxMessages", {"limit": 2.5}),
            ("listInboxMessages", {"limit": True}),
            ("listInboxMessages", {"severities": ["fatal"]}),
            ("markInboxMessages", {"ids": ["../etc"], "read": True}),
            ("markInboxMessages", {"ids": [], "read": True}),
            ("markInboxMessages", {"ids": ["0" * 25]}),
            ("deleteInboxMessages", {"ids": []}),
            ("deleteInboxMessages", {"ids": ["x" * 25]}),
            ("updateInboxPreferences", {"mutedTypes": ["unknown.type"]}),
        ],
    )
    def test_bad_arguments_raise_exactly_value_error(self, mod, field, args):
        with pytest.raises(ValueError) as exc:
            mod.handler(_event(field, args), None)
        assert type(exc.value) is ValueError
        assert exc.value.__cause__ is None

    def test_bad_cursor_message_is_exact_value_error(self, mod):
        with pytest.raises(ValueError) as exc:
            mod.handler(_event("listInboxMessages", {"nextToken": "%%%"}), None)
        assert type(exc.value) is ValueError

    def test_cross_user_id_is_resource_not_found(self, mod):
        (bobs,) = _seed(mod, BOB_EMAIL)
        for field, args in (
            ("markInboxMessages", {"ids": [bobs], "read": True}),
            ("deleteInboxMessages", {"ids": [bobs]}),
        ):
            with pytest.raises(Exception) as exc:
                mod.handler(_event(field, args), None)
            assert type(exc.value).__name__ == "ResourceNotFound"
        bob = _claims(BOB_EMAIL, BOB_SUB)
        out = mod.handler(_event("listInboxMessages", claims=bob), None)
        assert [m["id"] for m in out["items"]] == [bobs]
        assert out["unreadCount"] == 1

    def test_mix_of_own_and_other_ids_reports_not_found(self, mod):
        (mine,) = _seed(mod, ALICE_RK)
        (bobs,) = _seed(mod, BOB_EMAIL)
        out = mod.handler(
            _event("markInboxMessages", {"ids": [mine, bobs], "read": True}), None
        )
        assert out["updated"] == 1 and out["notFoundIds"] == [bobs]


@pytest.mark.unit
class TestLimitClamp:
    def test_limit_zero_clamps_to_one(self, mod):
        _seed(mod, ALICE_RK, n=3)
        out = mod.handler(_event("listInboxMessages", {"limit": 0}), None)
        assert len(out["items"]) == 1 and out["nextToken"] is not None

    def test_limit_above_max_clamps_to_100(self, mod):
        _seed(mod, ALICE_RK, n=101)
        out = mod.handler(_event("listInboxMessages", {"limit": 1000}), None)
        assert len(out["items"]) == 100 and out["nextToken"] is not None

    def test_limit_absent_uses_default(self, mod):
        _seed(mod, ALICE_RK, n=21)
        out = mod.handler(_event("listInboxMessages", {"limit": None}), None)
        assert len(out["items"]) == 20
