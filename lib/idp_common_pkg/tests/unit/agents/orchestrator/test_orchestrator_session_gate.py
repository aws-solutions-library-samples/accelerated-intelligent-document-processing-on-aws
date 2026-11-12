# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The orchestrator's sub-agent tools and the per-turn ``session_gate``.

``create_orchestrator_agent`` is run for real with the model, ``strands.Agent``
and the ``@tool`` decorator replaced, so each tool is the plain async generator
the orchestrator would call. The value the orchestrator receives from a tool is
the generator's last yield (``strands.tools.decorator``: "The last event is the
result"), which is what these tests read.
"""

# ruff: noqa: E402

import asyncio
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from idp_common.chat_session_agents import UNAVAILABLE_MESSAGE, SessionAgentGate

CI = "Code-Intelligence-Agent"


class _SubAgent:
    def __init__(self, events):
        self._events = events

    async def stream_async(self, _query):
        for event in self._events:
            yield event


def _build(monkeypatch, gate, answers):
    """Build the orchestrator; return {agent_id: tool} and the create_agent calls.

    ``create_agent`` stays replaced for the rest of the test, because a tool
    resolves the factory when it runs, not when it is built.
    """
    from idp_common.agents.factory import agent_factory
    from idp_common.agents.orchestrator import agent as orch

    created = []

    @contextmanager
    def create_agent(agent_id, **kwargs):
        created.append((agent_id, kwargs))
        yield _SubAgent(answers[agent_id])

    captured = {}

    def fake_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent_factory, "create_agent", create_agent)
    with (
        patch.object(orch, "tool", lambda f: f),
        patch.object(orch, "create_strands_bedrock_model", lambda **_: object()),
        patch.object(orch, "get_chat_companion_model_id", lambda: "model"),
        patch.object(orch.strands, "Agent", fake_agent),
    ):
        orch.create_orchestrator_agent(
            config={},
            session=None,
            agent_ids=list(answers),
            session_gate=gate,
            user_id="sub-a",
        )
    tools = {t._original_agent_id: t for t in captured["tools"]}
    return tools, created


def _last(tool):
    async def run():
        last = None
        async for value in tool("q"):
            last = value
        return last

    return asyncio.run(run())


ANSWERS = {
    "Analytics-Agent": [{"data": "x"}, {"result": "42 documents"}],
    CI: [{"result": "code answer"}],
}


@pytest.mark.unit
def test_code_intelligence_is_refused_after_analytics_in_the_same_turn(monkeypatch):
    recorded = []
    gate = SessionAgentGate(
        holds_data_results=False,
        opt_in_agent_ids={CI},
        record=lambda: recorded.append(1),
    )
    tools, created = _build(monkeypatch, gate, ANSWERS)

    assert _last(tools[CI]) == "code answer"  # before any data result
    assert _last(tools["Analytics-Agent"]) == "42 documents"
    assert recorded == [1]
    created.clear()
    assert _last(tools[CI]) == UNAVAILABLE_MESSAGE
    assert created == []  # the sub-agent was never built


@pytest.mark.unit
def test_a_result_that_cannot_be_recorded_is_withheld(monkeypatch):
    def fail():
        raise RuntimeError("write failed")

    gate = SessionAgentGate(
        holds_data_results=False, opt_in_agent_ids={CI}, record=fail
    )
    tools, _ = _build(monkeypatch, gate, ANSWERS)
    last = _last(tools["Analytics-Agent"])
    assert "42 documents" not in str(last)
    assert "Error" in str(last)


@pytest.mark.unit
def test_a_stream_without_a_result_event_still_flags(monkeypatch):
    recorded = []
    gate = SessionAgentGate(
        holds_data_results=False,
        opt_in_agent_ids={CI},
        record=lambda: recorded.append(1),
    )
    tools, _ = _build(
        monkeypatch, gate, {"Analytics-Agent": [{"data": "rows"}], CI: []}
    )
    _last(tools["Analytics-Agent"])
    assert recorded == [1]
    assert not gate.allows(CI)


@pytest.mark.unit
def test_the_gate_is_not_passed_to_sub_agents(monkeypatch):
    gate = SessionAgentGate(holds_data_results=False, opt_in_agent_ids={CI})
    tools, created = _build(monkeypatch, gate, ANSWERS)
    _last(tools["Analytics-Agent"])
    ((_, kwargs),) = created
    assert "session_gate" not in kwargs
    assert kwargs["user_id"] == "sub-a"


@pytest.mark.unit
def test_the_registry_marks_code_intelligence_as_opt_in():
    from idp_common.agents.factory import agent_factory

    entry = {a["agent_id"]: a for a in agent_factory.list_available_agents()}
    assert entry[CI]["opt_in"] == "code_intelligence"
    assert entry["Analytics-Agent"]["opt_in"] is None


@pytest.mark.unit
def test_the_registry_marks_every_external_mcp_agent_as_opt_in(monkeypatch):
    """Run ``registry.py`` afresh with one External MCP server in the secret.

    Loaded as a new module inside the factory package, so its relative imports
    resolve and it builds its own factory, leaving the shared one untouched. Tool
    discovery is made to fail, which the registry tolerates.
    """
    import importlib.util
    import json
    from pathlib import Path

    import boto3
    from moto import mock_aws

    import idp_common.agents.external_mcp.agent as mcp_agent
    import idp_common.agents.factory as factory_pkg

    def no_discovery(**_kwargs):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(mcp_agent, "create_external_mcp_agent", no_discovery)
    monkeypatch.setenv("AWS_STACK_NAME", "probe")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.client("secretsmanager", region_name="us-east-1").create_secret(
            Name="probe/external-mcp-agents/credentials",
            SecretString=json.dumps(
                [
                    {
                        "mcp_url": "https://remote.example/mcp",
                        "transport": "sse",
                        "agent_name": "Remote Tools",
                    }
                ]
            ),
        )
        path = Path(factory_pkg.__file__).with_name("registry.py")
        spec = importlib.util.spec_from_file_location(
            "idp_common.agents.factory._registry_probe", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

    entry = {a["agent_id"]: a for a in module.agent_factory.list_available_agents()}
    assert entry["remote_tools"]["opt_in"] == "external_mcp"
    assert entry[CI]["opt_in"] == "code_intelligence"
