# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The agent processor's caller gates, against both copies the streaming route runs.

The streaming route calls ``_enforce_agent_chat_groups`` and
``_refuse_foreign_session`` from the vendored copy, so they are exercised there as
well as in the deployed source, as ``test_thinking_filter.py`` does for the stream
filter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from test_thinking_filter import _COPIES, _load

pytestmark = pytest.mark.unit

_TABLES = {
    "sessions": "test-sessions",
    "messages": "test-messages",
    "memory": "test-memory",
}


@pytest.fixture(params=sorted(_COPIES))
def processor(request, monkeypatch):
    module = _load(_COPIES[request.param], f"gates_{request.param}")
    monkeypatch.setattr(module, "_CHAT_SESSIONS_TABLE", _TABLES["sessions"])
    monkeypatch.setattr(module, "_CHAT_MESSAGES_TABLE", _TABLES["messages"])
    monkeypatch.setenv("ID_HELPER_CHAT_MEMORY_TABLE", _TABLES["memory"])
    return module


def _install_tables(
    module, monkeypatch, owned, messages=False, memory=False, fail=False
):
    tables = {name: MagicMock() for name in _TABLES}
    if fail:
        tables["sessions"].get_item.side_effect = RuntimeError("throttled")
    tables["sessions"].get_item.return_value = (
        {"Item": {"sessionId": "s1"}} if owned else {}
    )
    tables["messages"].query.return_value = {
        "Items": [{"PK": "s1"}] if messages else []
    }
    tables["memory"].query.return_value = {
        "Items": [{"PK": "conversation#s1"}] if memory else []
    }
    by_name = {table: tables[key] for key, table in _TABLES.items()}
    resource = MagicMock()
    resource.Table.side_effect = lambda name: by_name[name]
    monkeypatch.setattr(module.boto3, "resource", MagicMock(return_value=resource))
    return tables


def _event(groups=("Author",), method="chat", caller="b@example.com"):
    return {
        "identity": {"claims": {"cognito:groups": list(groups)}},
        "sessionId": "s1",
        "method": method,
        "callerSub": caller,
    }


@pytest.mark.parametrize(
    ("state", "label"),
    [
        ({"owned": False, "messages": True}, "messages"),
        ({"owned": False, "memory": True}, "leftover memory"),
        ({"owned": False, "fail": True}, "a failed lookup"),
    ],
)
def test_a_session_in_use_by_someone_else_is_refused(
    processor, monkeypatch, state, label
):
    _install_tables(processor, monkeypatch, **state)
    with pytest.raises(PermissionError, match="belongs to another user"):
        processor._refuse_foreign_session(_event())


@pytest.mark.parametrize(
    "state", [{"owned": True, "messages": True}, {"owned": False}], ids=["own", "new"]
)
def test_the_callers_own_or_a_new_session_is_allowed(processor, monkeypatch, state):
    tables = _install_tables(processor, monkeypatch, **state)
    processor._refuse_foreign_session(_event())
    tables["sessions"].get_item.assert_called_once_with(
        Key={"userId": "b@example.com", "sessionId": "s1"}
    )


def test_a_verified_caller_without_a_key_is_refused(processor, monkeypatch):
    _install_tables(processor, monkeypatch, owned=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        processor._refuse_foreign_session(_event(caller=""))


def test_an_iam_invocation_is_not_checked(processor, monkeypatch):
    tables = _install_tables(processor, monkeypatch, owned=False, messages=True)
    processor._refuse_foreign_session({"sessionId": "s1", "callerSub": "x"})
    tables["sessions"].get_item.assert_not_called()


@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_quick_start_is_refused_to_a_non_admin(processor, group):
    with pytest.raises(PermissionError, match="Quick Start requires the Admin group"):
        processor._enforce_agent_chat_groups(
            _event(groups=(group,), method="quick_start")
        )


def test_quick_start_is_allowed_to_an_admin(processor):
    processor._enforce_agent_chat_groups(
        _event(groups=("Admin",), method="quick_start")
    )


@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_ordinary_agent_chat_is_still_allowed_to_non_admins(processor, group):
    processor._enforce_agent_chat_groups(_event(groups=(group,)))
