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
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

_REPO_LAMBDA = os.path.join(
    os.path.dirname(__file__),
    "../../../../nested/api-resolvers/src/lambda",
)
_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")


def _load(module_name, rel_path):
    """Load a resolver's index.py as a fresh module by absolute path."""
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(_REPO_LAMBDA, rel_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
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


@pytest.mark.unit
def test_list_agents_allows_direct_lambda_invocation(list_agents_index):
    """No 'identity' (IAM/backend invoke) bypasses the Cognito group check."""
    event = {"arguments": {}}
    result = list_agents_index.handler(event, None)
    assert result == [{"name": "doc-agent"}]


# --- sendAgentChatMessage ---------------------------------------------------


@pytest.fixture
def agent_chat_index(monkeypatch):
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "")
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.return_value = MagicMock()
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
        "identity": {"claims": {"cognito:groups": [group]}},
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
def test_agent_chat_forwards_only_the_group_claim_to_the_processor(monkeypatch):
    """The resolver must give the processor the groups, and nothing else.

    The processor needs the caller's groups to apply its own check, and it logs
    its whole event — so the rest of the identity object (email, source IP,
    token-derived claims) must not be forwarded.
    """
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "agent-chat-processor")
    with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
        mock_resource.return_value.Table.return_value = MagicMock()
        module = _load("agent_chat_resolver_fwd", "agent_chat_resolver/index.py")
        event = {
            "identity": {
                "claims": {"cognito:groups": ["Author"], "email": "a@example.com"},
                "username": "author-user",
            },
            "arguments": {"prompt": "hi", "sessionId": "s1"},
        }
        module.handler(event, None)

    payload = json.loads(mock_client.return_value.invoke.call_args.kwargs["Payload"])
    assert payload["identity"] == {"claims": {"cognito:groups": ["Author"]}}
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

    PermissionError (not a returned error body) so the dispatcher maps it to
    403/Unauthorized, matching the resolver.
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

    That covers the backend `lambda:InvokeFunction` path and the streaming
    Function URL, whose transport forwards no Cognito group claim (GAP-07 in
    scripts/api_rbac_expectations.yaml). Denying them here would take chat
    streaming offline for every user rather than restricting it to a group.
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
