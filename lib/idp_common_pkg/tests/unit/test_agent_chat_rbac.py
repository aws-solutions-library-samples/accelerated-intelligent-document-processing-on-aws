# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Server-side RBAC tests for Agent Chat (closes former GAP-03).

`listAvailableAgents` and `sendAgentChatMessage` restrict Agent Chat to
Admin/Author/Viewer (Reviewer excluded). The single REST route's Cognito
authorizer only authenticates, so the group gate must live in the resolver.
These tests verify: a Reviewer is rejected with PermissionError (the dispatcher
maps that to 403/Unauthorized), an allowed group proceeds, and a direct Lambda
invocation (no 'identity', the IAM backend publish path) bypasses the check.

The last section covers the **processor**, `src/lambda/agent_chat_processor`,
where the agent work actually happens. The resolver is one hop in front of it and
was the only place the group was checked, so any other route to the processor
reached the agents ungated. It now applies the same check itself, on the groups
the resolver forwards.
"""

import importlib.util
import json
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_LAMBDA = os.path.join(
    os.path.dirname(__file__),
    "../../../../nested/api-resolvers/src/lambda",
)
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")


def _load(module_name, rel_path):
    """Load a resolver's index.py as a fresh module by absolute path.

    The resolver's own directory goes on ``sys.path`` for the duration of the
    exec, because that is where Lambda puts it: a resolver may import a sibling
    module by bare name (``from log_sanitizer import ...``) and that resolves at
    runtime only because the handler's directory is the package root. Loading
    the file by path alone does not reproduce that, so without this the import
    fails here while working in the deployed function.
    """
    path = os.path.join(_REPO_LAMBDA, rel_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, os.path.dirname(path))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def _load_from_repo(module_name, rel_path):
    """Load any Lambda source file by a repo-root-relative path."""
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_REPO_ROOT, rel_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --- listAvailableAgents ----------------------------------------------------


@pytest.fixture
def list_agents_index():
    # The resolver does `from idp_common.agents.factory import agent_factory`,
    # and that package pulls in `strands` (not installed in the unit env). Stub
    # the factory module in sys.modules so the import resolves to a mock.
    import types

    mock_factory = MagicMock()
    mock_factory.list_available_agents.return_value = [{"name": "doc-agent"}]
    stub = types.ModuleType("idp_common.agents.factory")
    stub.agent_factory = mock_factory
    with patch.dict(sys.modules, {"idp_common.agents.factory": stub}):
        module = _load("list_available_agents_index", "list_available_agents/index.py")
        yield module


@pytest.mark.unit
def test_list_agents_rejects_reviewer(list_agents_index):
    """A Reviewer must not enumerate Agent Chat agents."""
    event = {"identity": {"claims": {"cognito:groups": ["Reviewer"]}}, "arguments": {}}
    with pytest.raises(PermissionError, match="Admin, Author or Viewer"):
        list_agents_index.handler(event, None)
    # The factory must never be reached when authorization fails.
    assert not list_agents_index.agent_factory.list_available_agents.called


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_list_agents_allows_permitted_groups(list_agents_index, group):
    """Admin/Author/Viewer may list agents."""
    event = {"identity": {"claims": {"cognito:groups": [group]}}, "arguments": {}}
    result = list_agents_index.handler(event, None)
    assert result == [{"name": "doc-agent"}]


# --- Quick Start's agent is reached only from its own panel -------------------
# Quick Start runs from its own panel, offered to Admins. As a Document Analytics
# agent it would run without the profile writes it exists for, and its read tools
# would return every profile's schemas whatever the caller's configuration scope.

_QUICK_START = "Quick-Start-Agent"


@pytest.fixture
def agent_request_index(monkeypatch):
    monkeypatch.setenv("AGENT_TABLE", "agents")
    monkeypatch.setenv("AGENT_PROCESSOR_FUNCTION", "proc")
    with patch("boto3.resource") as resource, patch("boto3.client") as client:
        module = _load("agent_request_handler_qs", "agent_request_handler/index.py")
        yield module, resource.return_value.Table.return_value, client.return_value


@pytest.fixture
def analytics_processor():
    stubs = {}
    for name, attrs in {
        "idp_common.agents.analytics": ("get_analytics_config", "parse_agent_response"),
        "idp_common.agents.common.config": ("configure_logging",),
        "idp_common.agents.factory": ("agent_factory",),
    }.items():
        stub = types.ModuleType(name)
        for attr in attrs:
            setattr(stub, attr, MagicMock())
        stubs[name] = stub
    with (
        patch.dict(sys.modules, stubs),
        patch("boto3.resource"),
        patch("boto3.Session"),
    ):
        yield _load_from_repo(
            "analytics_agent_processor_qs", "src/lambda/agent_processor/index.py"
        )


def _agent_query(group, agent_ids):
    return {
        "identity": {
            "claims": {"cognito:groups": [group], "email": "u@example.com"},
            "username": "u1",
            "sub": "sub-u",
        },
        "arguments": {"query": "draft a schema for paystubs", "agentIds": agent_ids},
    }


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_list_agents_never_offers_quick_starts_agent(list_agents_index, group):
    list_agents_index.agent_factory.list_available_agents.return_value = [
        {"agent_id": "Analytics-Agent"},
        {"agent_id": _QUICK_START},
    ]
    event = {"identity": {"claims": {"cognito:groups": [group]}}, "arguments": {}}
    assert list_agents_index.handler(event, None) == [{"agent_id": "Analytics-Agent"}]


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_submit_agent_query_refuses_quick_starts_agent(agent_request_index, group):
    module, table, lam = agent_request_index
    for agent_ids in ([_QUICK_START], ["Analytics-Agent", _QUICK_START]):
        with pytest.raises(PermissionError, match="not as an analytics agent"):
            module.handler(_agent_query(group, agent_ids), None)
    table.put_item.assert_not_called()
    lam.invoke.assert_not_called()


@pytest.mark.unit
def test_submit_agent_query_still_runs_the_analytics_agents(agent_request_index):
    module, table, _ = agent_request_index
    module.handler(_agent_query("Viewer", ["Analytics-Agent"]), None)
    assert table.put_item.called


@pytest.mark.unit
def test_the_analytics_processor_refuses_quick_starts_agent(analytics_processor):
    with pytest.raises(ValueError, match="not as an analytics agent"):
        analytics_processor.process_agent_query("draft a schema", [_QUICK_START])
    assert not analytics_processor.agent_factory.create_agent.called
    assert not analytics_processor.agent_factory.create_orchestrator_agent.called


@pytest.mark.unit
def test_every_route_names_quick_starts_agent_by_its_registered_id(
    list_agents_index, agent_request_index, analytics_processor
):
    from idp_common.chat_session_agents import QUICK_START_AGENT_ID

    assert QUICK_START_AGENT_ID == _QUICK_START
    assert list_agents_index._QUICK_START_AGENT_ID == QUICK_START_AGENT_ID
    assert agent_request_index[0]._QUICK_START_AGENT_ID == QUICK_START_AGENT_ID
    assert analytics_processor.QUICK_START_AGENT_ID == QUICK_START_AGENT_ID
    registry = (
        Path(__file__).resolve().parents[2]
        / "idp_common"
        / "agents"
        / "factory"
        / "registry.py"
    ).read_text(encoding="utf-8")
    assert re.search(
        rf"agent_id=[\"']{re.escape(QUICK_START_AGENT_ID)}[\"']", registry
    ), "the agent registry no longer registers Quick Start under the id the gates match"


@pytest.mark.unit
def test_list_agents_allows_direct_lambda_invocation(list_agents_index):
    """No 'identity' (IAM/backend invoke) bypasses the Cognito group check."""
    event = {"arguments": {}}
    result = list_agents_index.handler(event, None)
    assert result == [{"name": "doc-agent"}]


# --- sendAgentChatMessage ---------------------------------------------------


def _unused_session_table():
    """A table double answering for a session id nobody has used yet."""
    table = MagicMock()
    table.get_item.return_value = {}
    table.query.return_value = {"Items": []}
    return table


@pytest.fixture
def agent_chat_index(monkeypatch):
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("CHAT_SESSION_OWNERS_TABLE", "test-owners")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "")
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.return_value = _unused_session_table()
        module = _load("agent_chat_resolver_index", "agent_chat_resolver/index.py")
        yield module


@pytest.mark.unit
def test_agent_chat_rejects_reviewer(agent_chat_index):
    """A Reviewer must not send Agent Chat messages."""
    event = {
        "identity": {"claims": {"cognito:groups": ["Reviewer"]}},
        "arguments": {"prompt": "hi", "sessionId": "s1"},
    }
    with pytest.raises(PermissionError, match="Admin, Author or Viewer"):
        agent_chat_index.handler(event, None)


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_agent_chat_allows_permitted_groups(agent_chat_index, group):
    """Admin/Author/Viewer may send Agent Chat messages (no PermissionError)."""
    event = {
        "identity": {"claims": {"cognito:groups": [group]}, "sub": "sub-1"},
        "arguments": {"prompt": "hi", "sessionId": "s1"},
    }
    result = agent_chat_index.handler(event, None)
    assert result["role"] == "user"
    assert result["sessionId"] == "s1"


@pytest.mark.unit
def test_agent_chat_allows_direct_lambda_invocation(agent_chat_index):
    """No 'identity' (IAM backend publish path) bypasses the Cognito group check."""
    event = {"arguments": {"prompt": "hi", "sessionId": "s1"}}
    result = agent_chat_index.handler(event, None)
    assert result["sessionId"] == "s1"


@pytest.mark.unit
def test_agent_chat_forwards_only_the_group_claim_and_sub_to_the_processor(
    monkeypatch,
):
    """The resolver must give the processor the groups and the sub, nothing else.

    The processor needs the caller's groups to apply its own check and the sub to
    know whose session and memory the turn is. It logs its whole event — so the
    rest of the identity object (email, source IP, token-derived claims) must not
    be forwarded.
    """
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("CHAT_SESSION_OWNERS_TABLE", "test-owners")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "agent-chat-processor")
    with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
        mock_resource.return_value.Table.return_value = _unused_session_table()
        module = _load("agent_chat_resolver_fwd", "agent_chat_resolver/index.py")
        event = {
            "identity": {
                "claims": {"cognito:groups": ["Author"], "email": "a@example.com"},
                "username": "author-user",
                "sub": "sub-1",
            },
            "arguments": {"prompt": "hi", "sessionId": "s1"},
        }
        module.handler(event, None)

    payload = json.loads(mock_client.return_value.invoke.call_args.kwargs["Payload"])
    assert payload["identity"] == {
        "claims": {"cognito:groups": ["Author"], "sub": "sub-1"}
    }
    assert "email" not in json.dumps(payload["identity"])


@pytest.mark.unit
def test_agent_chat_forwards_no_identity_for_backend_invocations(monkeypatch):
    """An identity-less (IAM) invocation must stay identity-less downstream."""
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "agent-chat-processor")
    with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
        mock_resource.return_value.Table.return_value = MagicMock()
        module = _load("agent_chat_resolver_fwd_iam", "agent_chat_resolver/index.py")
        module.handler({"arguments": {"prompt": "hi", "sessionId": "s1"}}, None)

    payload = json.loads(mock_client.return_value.invoke.call_args.kwargs["Payload"])
    assert payload["identity"] is None


# --- agent_chat_processor (the Lambda that runs the agents) -------------------


@pytest.fixture
def agent_chat_processor():
    """Load src/lambda/agent_chat_processor/index.py with the agent stack stubbed.

    The module imports `idp_common.agents.*`, which pulls in `strands` (not
    installed in the unit environment), and its work is all AWS/Bedrock calls.
    Stub those imports so the authorization gate — which runs before any of it —
    can be tested on its own.
    """
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
        yield _load_from_repo(
            "agent_chat_processor_index", "src/lambda/agent_chat_processor/index.py"
        )


class _ReachedAgentWork(Exception):
    """Sentinel proving execution got past authorization into the real work."""


@pytest.mark.unit
@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([], id="no-group"),
        pytest.param(["Reviewer"], id="reviewer"),
        pytest.param(["SomeOtherGroup"], id="unrelated-group"),
    ],
)
def test_processor_denies_caller_without_an_authorized_group(
    agent_chat_processor, monkeypatch, groups
):
    """A caller in no permitted group must not reach the agents.

    What is asserted is that the denial happens BEFORE any agent or Bedrock call
    and that it raises rather than returning an error body — so it cannot be
    caught by the handler's convert-error-to-stream-frame path and reported as a
    successful turn carrying an error message.

    It is deliberately NOT asserted that this produces an HTTP 403, because on
    neither live path does it. The dispatcher's errorType->403 mapping reads a
    SYNCHRONOUS invoke response, and the resolver invokes this function with
    InvocationType="Event"; the resolver's own PermissionError is what the
    dispatcher maps, and it is raised before this function is ever invoked. On the
    streaming path the 403 comes from _enforce_groups_or_403 in
    src/lambda/chat_stream_processor/app.py, which applies this same predicate in
    the route before StreamingResponse commits a 200. This copy of the check is
    defence in depth for a caller that reaches the function directly.
    """
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork),
    )
    event = {
        "identity": {"claims": {"cognito:groups": groups}},
        "sessionId": "s1",
        "prompt": "hi",
    }
    with pytest.raises(PermissionError, match="Admin, Author or Viewer"):
        agent_chat_processor.handler(event, None)
    # Nothing may have run: the gate is before the work, not around it.
    assert not agent_chat_processor.get_cached_boto3_session.called


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Admin", "Author", "Viewer"])
def test_processor_allows_permitted_groups(agent_chat_processor, monkeypatch, group):
    """Admin/Author/Viewer pass the gate and proceed into the agent work.

    The sentinel stands in for that work: reaching it proves authorization
    passed, without running Bedrock. The handler converts it into its 500
    error return, which is the expected shape for a failure inside the work.
    """
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork("reached the agent work")),
    )
    event = {
        "identity": {"claims": {"cognito:groups": [group]}},
        "sessionId": "s1",
        "prompt": "hi",
    }
    result = agent_chat_processor.handler(event, None)
    assert result["body"] == "reached the agent work"


@pytest.mark.unit
def test_processor_accepts_a_single_group_claim_string(
    agent_chat_processor, monkeypatch
):
    """cognito:groups can arrive as a bare string, not a list."""
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork("reached the agent work")),
    )
    event = {
        "identity": {"claims": {"cognito:groups": "Viewer"}},
        "sessionId": "s1",
        "prompt": "hi",
    }
    result = agent_chat_processor.handler(event, None)
    assert result["body"] == "reached the agent work"


@pytest.mark.unit
def test_processor_allows_identity_less_invocation(agent_chat_processor, monkeypatch):
    """An invocation carrying no groups at all is gated by IAM, not by group.

    That is the backend `lambda:InvokeFunction` path. Both web UI paths forward
    the caller's verified groups (the streaming Function URL from the ID token it
    verifies), so they are group-checked rather than reaching this branch.
    """
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork("reached the agent work")),
    )
    result = agent_chat_processor.handler({"sessionId": "s1", "prompt": "hi"}, None)
    assert result["body"] == "reached the agent work"


@pytest.mark.unit
def test_processor_and_resolver_agree_on_the_group_list(agent_chat_processor):
    """One policy, enforced in two places — they must not drift apart."""
    resolver = _load("agent_chat_resolver_groups", "agent_chat_resolver/index.py")
    assert agent_chat_processor._AGENT_CHAT_GROUPS == resolver._AGENT_CHAT_GROUPS


# --- Quick Start (method=quick_start) is Admin only --------------------------
# Quick Start creates a configuration profile and activates it, and the
# configuration API lets only an Admin create a profile.


def _quick_start_event(group):
    return {
        "identity": {
            "username": "u@example.com",
            "sub": "sub-1",
            "claims": {"cognito:groups": [group]},
        },
        "arguments": {"prompt": "paystubs", "sessionId": "s1", "method": "quick_start"},
    }


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_resolver_refuses_quick_start_to_a_non_admin(agent_chat_index, group):
    with pytest.raises(PermissionError, match="Quick Start requires the Admin group"):
        agent_chat_index.handler(_quick_start_event(group), None)


@pytest.mark.unit
def test_resolver_allows_quick_start_to_an_admin(agent_chat_index):
    result = agent_chat_index.handler(_quick_start_event("Admin"), None)
    assert result["sessionId"] == "s1"


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_resolver_still_allows_ordinary_agent_chat_to_non_admins(
    agent_chat_index, group
):
    event = _quick_start_event(group)
    event["arguments"]["method"] = "chat"
    assert agent_chat_index.handler(event, None)["sessionId"] == "s1"


@pytest.mark.unit
@pytest.mark.parametrize("identity", [True, False], ids=["signed-in", "backend"])
def test_resolver_refuses_quick_start_when_the_stack_turns_it_off(
    agent_chat_index, monkeypatch, identity
):
    monkeypatch.setenv("QUICK_START_ENABLED", "false")
    event = _quick_start_event("Admin")
    if not identity:
        del event["identity"]
    with pytest.raises(PermissionError, match="Quick Start is turned off"):
        agent_chat_index.handler(event, None)


@pytest.mark.unit
def test_resolver_keeps_agent_chat_when_quick_start_is_off(
    agent_chat_index, monkeypatch
):
    monkeypatch.setenv("QUICK_START_ENABLED", "false")
    event = _quick_start_event("Viewer")
    event["arguments"]["method"] = "chat"
    assert agent_chat_index.handler(event, None)["sessionId"] == "s1"


@pytest.mark.unit
@pytest.mark.parametrize("group", ["Author", "Viewer"])
def test_processor_refuses_quick_start_to_a_non_admin_before_any_work(
    agent_chat_processor, monkeypatch, group
):
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork),
    )
    event = {
        "identity": {"claims": {"cognito:groups": [group]}},
        "sessionId": "s1",
        "prompt": "paystubs",
        "method": "quick_start",
        "callerSub": "u@example.com",
    }
    with pytest.raises(PermissionError, match="Quick Start requires the Admin group"):
        agent_chat_processor.handler(event, None)
    assert not agent_chat_processor.get_cached_boto3_session.called


@pytest.mark.unit
def test_processor_allows_quick_start_to_an_admin(agent_chat_processor, monkeypatch):
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork("reached the agent work")),
    )
    event = {
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "sessionId": "s1",
        "prompt": "paystubs",
        "method": "quick_start",
        "callerSub": "u@example.com",
    }
    assert agent_chat_processor.handler(event, None)["body"] == "reached the agent work"


@pytest.mark.unit
def test_processor_and_resolver_agree_on_quick_starts_groups(agent_chat_processor):
    resolver = _load("agent_chat_resolver_qs_groups", "agent_chat_resolver/index.py")
    assert agent_chat_processor._QUICK_START_ROLES == resolver._QUICK_START_ROLES
    assert agent_chat_processor._QUICK_START_ROLES == ("Admin",)


@pytest.mark.unit
@pytest.mark.parametrize("value", ["true", "TRUE", " false ", "False", "", None])
def test_processor_and_resolver_read_the_quick_start_flag_alike(
    agent_chat_processor, monkeypatch, value
):
    resolver = _load("agent_chat_resolver_qs_flag", "agent_chat_resolver/index.py")
    if value is None:
        monkeypatch.delenv("QUICK_START_ENABLED", raising=False)
    else:
        monkeypatch.setenv("QUICK_START_ENABLED", value)
    assert (
        agent_chat_processor._quick_start_enabled() == resolver._quick_start_enabled()
    )
