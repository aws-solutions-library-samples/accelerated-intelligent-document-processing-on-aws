# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Features that cannot be scoped to one tenant are refused to non-Admins in Strict.

``TenantIsolationMode=Strict`` keeps every document path inside one tenant through
the configuration scope. Four surfaces answer across every document in the
deployment and carry nothing to scope them by:

* **Agent Chat** (``sendAgentChatMessage`` and the processor behind it) — the
  agents query the reporting database and every execution;
* **agent queries** (``submitAgentQuery``) — the same agents, older entry point;
* the **Document Knowledge Base** (``queryKnowledgeBase``) — a retrieval index over
  every document;
* **MCP tools** — machine callers through the AgentCore gateway, which supplies no
  user identity at all.

In Strict the first three are limited to the Admin group, and MCP is refused
outright, because nothing there can tell an Admin from anyone else. In Off mode
each is exactly what it was. Every refusal is asserted to happen before the work
(no Bedrock, no DynamoDB write, no tool execution), not merely to be reported.
"""

import importlib.util
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../.."))


def _load(module_name, rel_path):
    path = os.path.join(_ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _identity(*groups):
    return {"claims": {"cognito:groups": list(groups), "email": "u@example.com"}}


@pytest.fixture(params=["Off", "Strict", "IdpClaim", "Api"])
def mode(request, monkeypatch):
    """``Off`` serves everyone; the other three each deny an unscoped caller.

    ``IdpClaim`` and ``Api`` run with isolation Off but tenants granted from outside
    the stack, which confines users just as Strict does, so the cross-tenant surfaces
    must be Admin-only there too.
    """
    if request.param in ("IdpClaim", "Api"):
        monkeypatch.setenv("TENANT_ISOLATION_MODE", "Off")
        monkeypatch.setenv("TENANT_GRANT_SOURCE", request.param)
    else:
        monkeypatch.setenv("TENANT_ISOLATION_MODE", request.param)
        monkeypatch.setenv("TENANT_GRANT_SOURCE", "Manual")
    return request.param


# --- sendAgentChatMessage ----------------------------------------------------


@pytest.fixture
def agent_chat_resolver(monkeypatch):
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "m")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "s")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "")
    with patch("boto3.resource") as resource, patch("boto3.client"):
        resource.return_value.Table.return_value = MagicMock()
        module = _load(
            "strict_agent_chat_resolver",
            "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py",
        )
        yield module, resource.return_value.Table.return_value


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_agent_chat_is_admin_only_in_strict(agent_chat_resolver, mode, group):
    module, table = agent_chat_resolver
    event = {
        "identity": _identity(group),
        "arguments": {"prompt": "hi", "sessionId": "s1"},
    }
    if mode != "Off":
        with pytest.raises(PermissionError, match="tenant isolation"):
            module.handler(event, None)
        table.put_item.assert_not_called()
    else:
        assert module.handler(event, None)["sessionId"] == "s1"


@pytest.mark.unit
def test_agent_chat_admin_and_backend_paths_are_unaffected(agent_chat_resolver, mode):
    module, _ = agent_chat_resolver
    admin = {
        "identity": _identity("Admin"),
        "arguments": {"prompt": "hi", "sessionId": "s1"},
    }
    assert module.handler(admin, None)["sessionId"] == "s1"
    backend = {"arguments": {"prompt": "hi", "sessionId": "s1"}}
    assert module.handler(backend, None)["sessionId"] == "s1"


# --- agent_chat_processor ----------------------------------------------------


@pytest.fixture
def agent_chat_processor():
    stubs = {}
    for name, attr, value in (
        ("idp_common.agents.analytics", "get_analytics_config", MagicMock()),
        ("idp_common.agents.common.config", "configure_logging", MagicMock()),
        ("idp_common.agents.factory", "agent_factory", MagicMock()),
        (
            "idp_common.agents.common.bedrock_error_messages",
            "BedrockErrorMessageHandler",
            MagicMock(),
        ),
    ):
        module = stubs.setdefault(name, types.ModuleType(name))
        setattr(module, attr, value)
    with patch.dict(sys.modules, stubs):
        yield _load(
            "strict_agent_chat_processor", "src/lambda/agent_chat_processor/index.py"
        )


@pytest.mark.unit
def test_the_processor_applies_the_same_rule(agent_chat_processor, mode):
    gate = agent_chat_processor._enforce_agent_chat_groups
    gate({"identity": _identity("Admin")})
    gate({"identity": None})
    if mode != "Off":
        with pytest.raises(PermissionError, match="tenant isolation"):
            gate({"identity": _identity("Author")})
    else:
        gate({"identity": _identity("Author")})


@pytest.mark.unit
def test_the_vendored_processor_is_the_same_file():
    """The streaming endpoint ships a copy; a rule on one and not the other is a
    route around it."""
    with open(os.path.join(_ROOT, "src/lambda/agent_chat_processor/index.py")) as a:
        with open(
            os.path.join(
                _ROOT,
                "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py",
            )
        ) as b:
            assert a.read() == b.read()


# --- submitAgentQuery --------------------------------------------------------


@pytest.fixture
def agent_request_handler(monkeypatch):
    monkeypatch.setenv("AGENT_TABLE", "agents")
    monkeypatch.setenv("AGENT_PROCESSOR_FUNCTION", "proc")
    with patch("boto3.resource") as resource, patch("boto3.client") as client:
        module = _load(
            "strict_agent_request_handler",
            "nested/api-resolvers/src/lambda/agent_request_handler/index.py",
        )
        yield module, resource.return_value.Table.return_value, client.return_value


@pytest.mark.unit
def test_agent_queries_are_admin_only_in_strict(agent_request_handler, mode):
    module, table, lam = agent_request_handler
    event = {
        "identity": {**_identity("Viewer"), "username": "u1"},
        "arguments": {"query": "how many?", "agentIds": ["analytics"]},
    }
    if mode != "Off":
        with pytest.raises(PermissionError, match="tenant isolation"):
            module.handler(event, None)
        table.put_item.assert_not_called()
        lam.invoke.assert_not_called()
    else:
        module.handler(event, None)
        assert table.put_item.called
    admin = {**event, "identity": {**_identity("Admin"), "username": "a1"}}
    module.handler(admin, None)
    assert table.put_item.called


# --- queryKnowledgeBase ------------------------------------------------------


@pytest.fixture
def kb_resolver(monkeypatch):
    monkeypatch.setenv("KB_ID", "kb")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MODEL_ID", "m")
    with patch("boto3.client"):
        module = _load(
            "strict_kb_resolver",
            "nested/api-resolvers/src/lambda/query_knowledgebase_resolver/index.py",
        )
    return module


@pytest.mark.unit
@pytest.mark.parametrize("groups", [["Viewer"], ["Author", "Reviewer"]])
def test_the_knowledge_base_is_admin_only_in_strict(kb_resolver, mode, groups):
    event = {"identity": _identity(*groups), "arguments": {"input": "q"}}
    with (
        patch.object(
            kb_resolver, "get_kb_response", return_value={"output": "x"}
        ) as kb,
        patch.object(kb_resolver, "markdown_response", return_value=""),
    ):
        if mode != "Off":
            with pytest.raises(PermissionError, match="tenant isolation"):
                kb_resolver.handler(event, None)
            kb.assert_not_called()
        else:
            kb_resolver.handler(event, None)
            assert kb.called
        kb.reset_mock()
        kb_resolver.handler({**event, "identity": _identity("Admin")}, None)
        assert kb.called


# --- MCP ---------------------------------------------------------------------


@pytest.fixture
def mcp_handler():
    directory = os.path.join(_ROOT, "src/lambda/agentcore_mcp_handler")
    for name in list(sys.modules):
        if name in ("index", "tools") or name.startswith("tools."):
            del sys.modules[name]
    sys.path.insert(0, directory)
    try:
        import index  # type: ignore

        yield index
    finally:
        sys.path.remove(directory)
        for name in list(sys.modules):
            if name in ("index", "tools") or name.startswith("tools."):
                del sys.modules[name]


@pytest.mark.unit
def test_mcp_tools_are_refused_in_strict(mcp_handler, mode):
    tool = MagicMock()
    tool.execute.return_value = {"ok": True}
    context = SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": "t___search"}
        )
    )
    with patch.object(mcp_handler, "get_tool", return_value=tool):
        result = mcp_handler.lambda_handler({"query": "x"}, context)
    if mode != "Off":
        assert result["statusCode"] == 403
        assert "tenant isolation" in json.loads(result["body"])["error"]
        tool.execute.assert_not_called()
    else:
        assert result["statusCode"] == 200
        tool.execute.assert_called_once()
