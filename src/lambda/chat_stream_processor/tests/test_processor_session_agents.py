# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which agents the agent chat processor builds the orchestrator with, per session.

Run against both copies of the processor (the deployed function, reached through
the agent_chat resolver, and the copy vendored into the streaming endpoint). The
factory is the recorder from ``test_processor_session_owner``; the session flag is
written and read by the real ``idp_common.chat_session_owner`` against moto, and
the agent selection is the real ``idp_common.chat_session_agents``.

A data agent "returning a result" is driven through the gate the processor hands
the orchestrator (``session_gate``), which is exactly what the orchestrator's
Analytics tool calls before it hands its result on.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("idp_common.chat_session_agents")

from test_processor_session_owner import (  # noqa: E402,F401
    A,
    B,
    _event,
    _owner,
    _stored,
    world,
)

pytestmark = pytest.mark.unit

CI = "Code-Intelligence-Agent"
MCP = "remote-tools"
AGENTS = [
    {"agent_id": "Analytics-Agent", "opt_in": None},
    {"agent_id": "Error-Analyzer-Agent", "opt_in": None},
    {"agent_id": CI, "opt_in": "code_intelligence"},
    {"agent_id": MCP, "opt_in": "external_mcp"},
    {"agent_id": "Quick-Start-Agent", "opt_in": None},
]
OPT_IN_AGENTS = {CI, MCP}


@pytest.fixture
def chat(world, monkeypatch):  # noqa: F811
    """``world`` with a full agent list, a sink, and a configurable answer."""
    world.factory.list_available_agents = lambda: [dict(a) for a in AGENTS]
    world.events = []
    world.data_agent_answers = False

    def sink(session_id, content, method, is_processing=True, tool_metadata=None):
        world.events.append(
            {"sessionId": session_id, "method": method, "content": content}
        )

    world.processor.set_sink(sink)

    async def answer(_client, _orchestrator, _prompt, _session_id):
        if world.data_agent_answers:
            # What the orchestrator's Analytics tool does before handing its
            # result on.
            world.factory.calls[-1]["session_gate"].note_data_result("Analytics-Agent")
        return "the answer"

    monkeypatch.setattr(world.processor, "stream_agent_response", answer)
    yield world
    world.processor.set_sink(None)


def _built(chat):
    call = chat.factory.calls[-1]
    return call["session_id"], set(call["agent_ids"])


def _run_analytics(chat, sid, user=A, **extra):
    chat.data_agent_answers = True
    try:
        assert (
            chat.processor.handler(_event(user, sid, **extra), None)["statusCode"]
            == 200
        )
    finally:
        chat.data_agent_answers = False


def _flag(chat, sid):
    item = chat.owners.get_item(Key={"sessionId": sid}).get("Item") or {}
    return item.get("dataResultsAt")


def test_opt_in_agents_are_off_by_default(chat):
    chat.processor.handler(_event(A, "s1"), None)
    _, agents = _built(chat)
    assert agents == {"Analytics-Agent", "Error-Analyzer-Agent"}


def test_each_opt_in_adds_only_its_own_agents(chat):
    chat.processor.handler(_event(A, "s1", enableExternalMcpAgents=True), None)
    assert _built(chat)[1] & OPT_IN_AGENTS == {MCP}
    chat.processor.handler(_event(A, "s1", enableCodeIntelligence=True), None)
    assert _built(chat)[1] & OPT_IN_AGENTS == {CI}


def test_a_non_boolean_does_not_opt_in(chat):
    chat.processor.handler(
        _event(A, "s1", enableExternalMcpAgents="true", enableCodeIntelligence=1),
        None,
    )
    assert not _built(chat)[1] & OPT_IN_AGENTS


def test_a_data_result_flags_the_session_once(chat):
    _run_analytics(chat, "s1")
    first = _flag(chat, "s1")
    assert first
    assert any(e["method"] == "session_data_results" for e in chat.events)
    _run_analytics(chat, "s1")
    assert _flag(chat, "s1") == first


def test_analytics_then_code_intelligence_never_builds_it_into_that_session(chat):
    """The key case, on the streaming path (the processor persists the turn)."""
    _run_analytics(chat, "s1")
    chat.events.clear()

    chat.processor.handler(
        _event(A, "s1", enableCodeIntelligence=True, enableExternalMcpAgents=True),
        None,
    )

    for call in chat.factory.calls:
        if call["session_id"] == "s1":
            assert not set(call["agent_ids"]) & OPT_IN_AGENTS, call["agent_ids"]
    # The turn ran in a new session, owned by the same user, with empty memory:
    # memory is keyed on (user, session), and the new id has none.
    new_sid, agents = _built(chat)
    assert new_sid != "s1"
    assert agents & OPT_IN_AGENTS == OPT_IN_AGENTS
    assert _owner(chat, new_sid) == A["sub"]
    assert not _flag(chat, new_sid)
    (replaced,) = [e for e in chat.events if e["method"] == "session_replaced"]
    assert json.loads(replaced["content"]) == {
        "sessionId": new_sid,
        "previousSessionId": "s1",
    }
    # The turn is stored under the new session, not the flagged one.
    assert [m["role"] for m in _stored(chat, new_sid)] == ["user", "assistant"]
    assert len(_stored(chat, "s1")) == 2  # only the Analytics turn


def test_analytics_then_code_intelligence_on_the_resolver_path(chat):
    """The resolver stored the user message; the processor leaves the agents out."""
    _run_analytics(chat, "s1")
    chat.processor.handler(
        _event(
            A,
            "s1",
            enableCodeIntelligence=True,
            enableExternalMcpAgents=True,
            persistUserMessage=False,
        ),
        None,
    )
    sid, agents = _built(chat)
    assert sid == "s1"
    assert not agents & OPT_IN_AGENTS
    assert not any(e["method"] == "session_replaced" for e in chat.events)


def test_a_flagged_session_without_an_opt_in_keeps_its_id(chat):
    _run_analytics(chat, "s1")
    chat.processor.handler(_event(A, "s1"), None)
    assert _built(chat)[0] == "s1"


def test_the_gate_closes_opt_in_agents_within_the_turn(chat):
    """An orchestrator built with Code Intelligence refuses it after Analytics."""
    chat.data_agent_answers = True
    chat.processor.handler(_event(A, "s1", enableCodeIntelligence=True), None)
    gate = chat.factory.calls[-1]["session_gate"]
    assert CI in chat.factory.calls[-1]["agent_ids"]
    assert not gate.allows(CI)
    assert not gate.allows(MCP)
    assert gate.allows("Error-Analyzer-Agent")


def test_the_flag_belongs_to_one_users_session(chat):
    _run_analytics(chat, "s-a", user=A)
    chat.processor.handler(_event(B, "s-b", enableCodeIntelligence=True), None)
    sid, agents = _built(chat)
    assert sid == "s-b"
    assert CI in agents


def test_a_failed_read_of_the_flag_leaves_opt_in_agents_out(chat, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(chat.processor, "holds_data_results", broken)
    chat.processor.handler(
        _event(A, "s1", enableCodeIntelligence=True, persistUserMessage=False), None
    )
    assert not _built(chat)[1] & OPT_IN_AGENTS


def test_a_turn_with_no_user_records_nothing(chat):
    chat.data_agent_answers = True
    chat.processor.handler(_event(None, "s-x"), None)
    gate = chat.factory.calls[-1]["session_gate"]
    assert not gate.allows(CI)
    assert _owner(chat, "s-x") is None
