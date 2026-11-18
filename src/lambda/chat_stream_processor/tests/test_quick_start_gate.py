# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The agent processor's Quick Start gate, against both copies the streaming route runs.

The streaming route hands ``_enforce_agent_chat_groups`` the requested method, and
calls it from the vendored copy, so the Admin-only rule for ``quick_start`` is
exercised there as well as in the deployed source, as ``test_thinking_filter.py``
does for the stream filter. So is the stack's ``EnableQuickStartWidget``, which the
template passes in as ``QUICK_START_ENABLED``.
"""

from __future__ import annotations

import pytest
from test_thinking_filter import _COPIES, _load

pytestmark = pytest.mark.unit


@pytest.fixture(params=sorted(_COPIES))
def processor(request):
    return _load(_COPIES[request.param], f"quick_start_gate_{request.param}")


def _event(group: str, method: str) -> dict:
    return {"identity": {"claims": {"cognito:groups": [group]}}, "method": method}


@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_quick_start_is_refused_to_a_non_admin(processor, group):
    with pytest.raises(PermissionError, match="Quick Start requires the Admin group"):
        processor._enforce_agent_chat_groups(_event(group, "quick_start"))


def test_quick_start_is_allowed_to_an_admin(processor):
    processor._enforce_agent_chat_groups(_event("Admin", "quick_start"))


@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_ordinary_agent_chat_keeps_its_three_groups(processor, group):
    processor._enforce_agent_chat_groups(_event(group, "chat"))


def test_both_copies_keep_quick_start_to_admins(processor):
    assert processor._QUICK_START_ROLES == ("Admin",)


@pytest.mark.parametrize("identity", [True, False], ids=["signed-in", "backend"])
def test_quick_start_is_refused_to_everyone_when_the_stack_turns_it_off(
    processor, monkeypatch, identity
):
    monkeypatch.setenv("QUICK_START_ENABLED", "false")
    event = _event("Admin", "quick_start") if identity else {"method": "quick_start"}
    with pytest.raises(PermissionError, match="Quick Start is turned off"):
        processor._enforce_agent_chat_groups(event)


@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_ordinary_agent_chat_is_unaffected_when_quick_start_is_off(
    processor, monkeypatch, group
):
    monkeypatch.setenv("QUICK_START_ENABLED", "false")
    processor._enforce_agent_chat_groups(_event(group, "chat"))


@pytest.mark.parametrize("value", ["true", "TRUE", None])
def test_quick_start_stays_on_unless_the_stack_says_false(
    processor, monkeypatch, value
):
    if value is None:
        monkeypatch.delenv("QUICK_START_ENABLED", raising=False)
    else:
        monkeypatch.setenv("QUICK_START_ENABLED", value)
    processor._enforce_agent_chat_groups(_event("Admin", "quick_start"))
