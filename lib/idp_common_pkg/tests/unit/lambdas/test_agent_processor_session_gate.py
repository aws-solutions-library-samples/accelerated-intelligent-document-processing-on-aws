# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``submitAgentQuery`` processor builds its orchestrator with the session gate.

``src/lambda/agent_processor/index.py`` serves the Agent Analysis page and the
Troubleshoot dialog. These tests drive its ``process_agent_query`` through the
real agent factory and the real ``create_orchestrator_agent``, with only the model,
``strands.Agent`` and the ``@tool`` decorator replaced, so each sub-agent tool is
the plain async generator the orchestrator would call. Building stops there (the
stand-in ``strands.Agent`` raises once it has the tools), and the tools are then
run in the order a model might call them.

So the assertion that the Code Intelligence Agent is refused after the Analytics
Agent goes red if the processor stops passing ``session_gate``. The Quick Start
test pins the processor's explicit refusal, which runs before the selection; on
this route that refusal, not the selection's own Quick Start filter, is what keeps
Quick Start out.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from idp_common.chat_session_agents import UNAVAILABLE_MESSAGE

from ._lambda_loader import load_lambda

CI = "Code-Intelligence-Agent"
ANALYTICS = "Analytics-Agent"

ANSWERS = {
    ANALYTICS: [{"data": "x"}, {"result": "42 documents"}],
    "Error-Analyzer-Agent": [{"result": "no errors"}],
    CI: [{"result": "code answer"}],
}


class _Built(Exception):
    """Raised by the stand-in ``strands.Agent`` once the tools exist."""


class _SubAgent:
    def __init__(self, events):
        self._events = events

    async def stream_async(self, _query):
        for event in self._events:
            yield event


@pytest.fixture(scope="module")
def processor():
    return load_lambda(
        "agent_processor_index_session_gate",
        "src/lambda/agent_processor",
        patches={"boto3.resource": MagicMock()},
    )


@pytest.fixture
def build(processor, monkeypatch):
    """Run ``process_agent_query`` up to the orchestrator; return its tools.

    ``create_agent`` stays replaced for the rest of the test, because a tool
    resolves the factory when it runs, not when it is built.
    """
    from idp_common.agents.orchestrator import agent as orch

    created = []

    @contextmanager
    def create_agent(agent_id, **kwargs):
        created.append(agent_id)
        yield _SubAgent(ANSWERS[agent_id])

    monkeypatch.setattr(processor.agent_factory, "create_agent", create_agent)
    monkeypatch.setattr(processor, "get_analytics_config", lambda: {})

    def _build(agent_ids):
        captured = {}

        def fake_agent(**kwargs):
            captured.update(kwargs)
            raise _Built()

        with (
            patch.object(orch, "tool", lambda f: f),
            patch.object(orch, "create_strands_bedrock_model", lambda **_: object()),
            patch.object(orch, "get_chat_companion_model_id", lambda: "model"),
            patch.object(orch.strands, "Agent", fake_agent),
        ):
            with pytest.raises(_Built):
                processor.process_agent_query(
                    "q", agent_ids, job_id="job-1", user_id="sub-a"
                )
        return {t._original_agent_id: t for t in captured["tools"]}, created

    return _build


def _last(tool):
    async def run():
        last = None
        async for value in tool("q"):
            last = value
        return last

    return asyncio.run(run())


@pytest.mark.unit
def test_code_intelligence_is_refused_after_analytics_in_the_same_job(build):
    tools, created = build([ANALYTICS, CI])

    assert _last(tools[ANALYTICS]) == "42 documents"
    created.clear()
    assert _last(tools[CI]) == UNAVAILABLE_MESSAGE
    assert created == []  # the sub-agent was never built


@pytest.mark.unit
def test_code_intelligence_runs_before_any_data_result(build):
    """Selecting it is the opt-in; only a data result closes it."""
    tools, _ = build([CI, "Error-Analyzer-Agent"])

    assert _last(tools[CI]) == "code answer"
    assert _last(tools["Error-Analyzer-Agent"]) == "no errors"
    assert _last(tools[CI]) == UNAVAILABLE_MESSAGE


@pytest.mark.unit
def test_the_orchestrator_is_built_with_the_requested_agents_in_order(build):
    tools, _ = build(["Error-Analyzer-Agent", ANALYTICS, CI])

    assert list(tools) == ["Error-Analyzer-Agent", ANALYTICS, CI]


@pytest.mark.unit
def test_an_unknown_agent_id_is_refused(processor, monkeypatch):
    monkeypatch.setattr(processor, "get_analytics_config", lambda: {})
    with pytest.raises(ValueError, match="not found in registry"):
        processor.process_agent_query("q", [ANALYTICS, "No-Such-Agent"])


@pytest.mark.unit
def test_quick_start_is_refused(processor):
    with pytest.raises(ValueError, match="Quick Start"):
        processor.process_agent_query("q", [ANALYTICS, "Quick-Start-Agent"])


@pytest.mark.unit
def test_a_single_opt_in_agent_runs_directly(processor, monkeypatch):
    """One agent alone has no other agent's result in its context, so no gate."""
    calls = []

    class _Stop(Exception):
        pass

    def create_agent(agent_id, **kwargs):
        calls.append((agent_id, kwargs))
        raise _Stop()

    monkeypatch.setattr(processor.agent_factory, "create_agent", create_agent)
    monkeypatch.setattr(processor, "get_analytics_config", lambda: {})
    with pytest.raises(_Stop):
        processor.process_agent_query("q", [CI], job_id="job-1", user_id="sub-a")

    ((agent_id, kwargs),) = calls
    assert agent_id == CI
    assert "session_gate" not in kwargs
