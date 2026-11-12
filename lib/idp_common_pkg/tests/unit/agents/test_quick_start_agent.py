# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Quick Start agent's construction, and how the chat processor routes to it."""

import importlib.util
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

QUICK_START_AGENT_ID = "Quick-Start-Agent"

QUICK_START_TOOLS = {
    "search_catalog",
    "author_schema_from_prompt",
    "refine_schema",
    "create_config_version",
    "activate_config_version",
    "list_config_versions",
    "get_class_schema",
    "list_sample_documents",
    "list_available_extensions",
    "estimate_generation_cost",
    "request_document_generation",
    "generate_from_existing_config",
}


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "src" / "lambda" / "agent_chat_processor").is_dir():
            return parent
    raise RuntimeError("repo root not found")


def _agent_kwargs():
    from idp_common.agents.quick_start import agent as quick_start

    with (
        patch.object(
            quick_start, "create_strands_bedrock_model", return_value=MagicMock()
        ),
        patch.object(quick_start.strands, "Agent") as agent_class,
    ):
        quick_start.create_quick_start_agent(session=MagicMock(), model_id="model")
    return agent_class.call_args.kwargs


def _tool_names(tools) -> set:
    return {tool.tool_name for tool in tools}


def test_the_agent_is_given_exactly_the_quick_start_tools():
    from idp_common.agents.quick_start import agent as quick_start

    kwargs = _agent_kwargs()
    assert _tool_names(kwargs["tools"]) == QUICK_START_TOOLS
    assert kwargs["system_prompt"] == quick_start.SYSTEM_PROMPT


def test_the_prompt_names_every_tool_the_agent_has_and_no_other():
    from idp_common.agents.quick_start import agent as quick_start
    from idp_common.agents.quick_start import tools

    named = {
        name
        for name in tools.__all__
        if re.search(rf"\b{name}\b", quick_start.SYSTEM_PROMPT)
    }
    assert named == _tool_names(_agent_kwargs()["tools"])


@pytest.fixture
def processor(monkeypatch):
    factory = MagicMock()
    factory.list_available_agents.return_value = [
        {"agent_id": QUICK_START_AGENT_ID},
        {"agent_id": "Analytics-Agent"},
        {"agent_id": "Code-Intelligence-Agent"},
    ]
    stubs = {}
    for name, attr, value in (
        ("idp_common.agents.analytics", "get_analytics_config", MagicMock()),
        ("idp_common.agents.common.config", "configure_logging", MagicMock()),
        ("idp_common.agents.factory", "agent_factory", factory),
        (
            "idp_common.agents.common.bedrock_error_messages",
            "BedrockErrorMessageHandler",
            MagicMock(),
        ),
    ):
        module = stubs.setdefault(name, types.ModuleType(name))
        setattr(module, attr, value)
    path = _repo_root() / "src/lambda/agent_chat_processor/index.py"
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("quick_start_routing", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["quick_start_routing"] = module
        spec.loader.exec_module(module)
        monkeypatch.setattr(
            module, "get_cached_boto3_session", MagicMock(return_value="session")
        )
        monkeypatch.setattr(
            module, "get_analytics_config", MagicMock(return_value={"cfg": True})
        )
        monkeypatch.setattr(
            module, "stream_agent_response", AsyncMock(return_value="final text")
        )
        monkeypatch.setattr(module, "_persist_chat_turn", MagicMock())
        yield module


def _send(processor, **event):
    event = {
        "identity": {"claims": {"cognito:groups": ["Author"]}},
        "sessionId": "s1",
        "prompt": "I process paystubs",
        **event,
    }
    result = processor.handler(event, None)
    assert result["statusCode"] == 200, result
    return processor.agent_factory


def _surface(processor) -> str:
    return processor._persist_chat_turn.call_args.args[2]


def test_quick_start_mode_runs_the_quick_start_agent_with_memory(processor):
    factory = _send(processor, method="quick_start")

    factory.create_conversational_agent.assert_called_once_with(
        agent_id=QUICK_START_AGENT_ID,
        session_id="s1",
        config={"cfg": True},
        session="session",
        # Memory is keyed on the signed-in user; this event carries no `sub`.
        user_id="",
    )
    factory.create_conversational_orchestrator.assert_not_called()
    assert _surface(processor) == "quick_start"


def test_chat_mode_keeps_the_quick_start_agent_out_of_the_orchestrator(processor):
    factory = _send(processor)

    factory.create_conversational_agent.assert_not_called()
    factory.create_conversational_orchestrator.assert_called_once()
    agent_ids = factory.create_conversational_orchestrator.call_args.kwargs["agent_ids"]
    assert agent_ids == ["Analytics-Agent"]
    assert _surface(processor) == "chat"


def test_quick_start_mode_falls_back_to_the_orchestrator_when_the_agent_is_absent(
    processor,
):
    processor.agent_factory.list_available_agents.return_value = [
        {"agent_id": "Analytics-Agent"}
    ]
    factory = _send(processor, method="quick_start")

    factory.create_conversational_agent.assert_not_called()
    factory.create_conversational_orchestrator.assert_called_once()
    assert _surface(processor) == "quick_start"
