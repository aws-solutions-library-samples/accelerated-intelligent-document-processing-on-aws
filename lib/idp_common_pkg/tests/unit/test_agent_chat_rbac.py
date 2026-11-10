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
        "identity": {
            "username": "u@example.com",
            "claims": {"cognito:groups": [group]},
        },
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
        "callerSub": "u@example.com",
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
        "callerSub": "u@example.com",
    }
    result = agent_chat_processor.handler(event, None)
    assert result["body"] == "reached the agent work"


@pytest.mark.unit
def test_processor_allows_identity_less_invocation(agent_chat_processor, monkeypatch):
    """An invocation carrying no groups at all is gated by IAM, not by group.

    That is the backend `lambda:InvokeFunction` path. Both chat routes forward
    verified claims, the streaming Function URL from the ID token it verifies, so
    neither reaches this branch.
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


# --- a session another user owns is refused ----------------------------------
# A conversation's messages and agent memory are keyed by session id alone, so the
# session record (userId, sessionId) is what says who owns it.


def _tables(owned, earlier_messages, memory=False, fail=False):
    sessions, messages, memories = MagicMock(), MagicMock(), MagicMock()
    if fail:
        sessions.get_item.side_effect = RuntimeError("throttled")
    sessions.get_item.return_value = {"Item": {"sessionId": "s1"}} if owned else {}
    messages.query.return_value = {"Items": [{"PK": "s1"}] if earlier_messages else []}
    memories.query.return_value = {
        "Items": [{"PK": "conversation#s1"}] if memory else []
    }
    return {"sessions": sessions, "messages": messages, "memory": memories}


_TABLE_NAMES = {
    "test-sessions": "sessions",
    "test-messages": "messages",
    "test-memory": "memory",
}


def _resource_for(tables):
    resource = MagicMock()
    resource.Table.side_effect = lambda name: tables[_TABLE_NAMES[name]]
    return resource


@pytest.fixture
def resolver_with_tables(agent_chat_index, monkeypatch):
    monkeypatch.setattr(agent_chat_index, "ID_HELPER_CHAT_MEMORY_TABLE", "test-memory")

    def install(**state):
        tables = _tables(**state)
        monkeypatch.setattr(agent_chat_index, "dynamodb", _resource_for(tables))
        return tables

    return install


def _send(index, method="chat"):
    return index.handler(
        {
            "identity": {
                "username": "b@example.com",
                "claims": {"cognito:groups": ["Admin"]},
            },
            "arguments": {
                "prompt": "what did we discuss?",
                "sessionId": "s1",
                "method": method,
            },
        },
        None,
    )


@pytest.mark.unit
@pytest.mark.parametrize("method", ["chat", "quick_start", "assistant_final_response"])
def test_resolver_refuses_a_session_another_user_owns(
    agent_chat_index, resolver_with_tables, method
):
    tables = resolver_with_tables(owned=False, earlier_messages=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _send(agent_chat_index, method)
    tables["sessions"].get_item.assert_called_once_with(
        Key={"userId": "b@example.com", "sessionId": "s1"}
    )
    tables["messages"].put_item.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("owned", "earlier_messages"), [(True, True), (False, False)], ids=["own", "new"]
)
def test_resolver_continues_the_callers_own_session_or_starts_a_new_one(
    agent_chat_index, resolver_with_tables, owned, earlier_messages
):
    resolver_with_tables(owned=owned, earlier_messages=earlier_messages)
    assert _send(agent_chat_index)["sessionId"] == "s1"


@pytest.mark.unit
def test_resolver_refuses_when_ownership_cannot_be_checked(
    agent_chat_index, resolver_with_tables
):
    resolver_with_tables(owned=False, earlier_messages=False, fail=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _send(agent_chat_index)


@pytest.mark.unit
def test_resolver_leaves_the_backend_publish_path_alone(
    agent_chat_index, resolver_with_tables
):
    tables = resolver_with_tables(owned=False, earlier_messages=True)
    event = {
        "arguments": {"prompt": "x", "sessionId": "s1", "method": "assistant_stream"}
    }
    assert agent_chat_index.handler(event, None)["sessionId"] == "s1"
    tables["sessions"].get_item.assert_not_called()


@pytest.fixture
def processor_with_tables(agent_chat_processor, monkeypatch):
    monkeypatch.setattr(agent_chat_processor, "_CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setattr(agent_chat_processor, "_CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setattr(
        agent_chat_processor,
        "get_cached_boto3_session",
        MagicMock(side_effect=_ReachedAgentWork("reached the agent work")),
    )

    monkeypatch.setenv("ID_HELPER_CHAT_MEMORY_TABLE", "test-memory")

    def install(**state):
        tables = _tables(**state)
        monkeypatch.setattr(
            agent_chat_processor.boto3,
            "resource",
            MagicMock(return_value=_resource_for(tables)),
        )
        return tables

    return install


def _turn(processor, caller="b@example.com"):
    return processor.handler(
        {
            "identity": {"claims": {"cognito:groups": ["Author"]}},
            "sessionId": "s1",
            "prompt": "what did we discuss?",
            "callerSub": caller,
        },
        None,
    )


@pytest.mark.unit
def test_processor_refuses_a_session_another_user_owns_before_any_work(
    agent_chat_processor, processor_with_tables
):
    processor_with_tables(owned=False, earlier_messages=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _turn(agent_chat_processor)
    assert not agent_chat_processor.get_cached_boto3_session.called


@pytest.mark.unit
@pytest.mark.parametrize(
    ("owned", "earlier_messages"), [(True, True), (False, False)], ids=["own", "new"]
)
def test_processor_continues_the_callers_own_session_or_starts_a_new_one(
    agent_chat_processor, processor_with_tables, owned, earlier_messages
):
    processor_with_tables(owned=owned, earlier_messages=earlier_messages)
    assert _turn(agent_chat_processor)["body"] == "reached the agent work"


@pytest.mark.unit
@pytest.mark.parametrize("caller", [""], ids=["no-caller-key"])
def test_processor_refuses_a_verified_caller_it_cannot_attribute(
    agent_chat_processor, processor_with_tables, caller
):
    processor_with_tables(owned=True, earlier_messages=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _turn(agent_chat_processor, caller=caller)


@pytest.mark.unit
def test_processor_and_resolver_query_the_same_ownership_record(agent_chat_processor):
    """The two copies of the predicate must read the same key and the same table."""
    resolver = _load("agent_chat_resolver_ownership", "agent_chat_resolver/index.py")
    for module in (agent_chat_processor, resolver):
        src = open(module.__file__).read()
        body = src[src.index("def _session_belongs_to_another_user(") :]
        body = body[: body.index("\ndef ")]
        assert 'Key={"userId": user_id, "sessionId": session_id}' in body
        assert 'Key("PK").eq(session_id)' in body
        assert "return True" in body


@pytest.mark.unit
def test_resolver_refuses_a_session_whose_messages_are_gone_but_memory_remains(
    agent_chat_index, resolver_with_tables
):
    """Agent memory outlives the messages, so an id with memory is still in use."""
    tables = resolver_with_tables(owned=False, earlier_messages=False, memory=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _send(agent_chat_index)
    tables["memory"].query.assert_called_once()
    tables["messages"].put_item.assert_not_called()


@pytest.mark.unit
def test_processor_refuses_a_session_whose_messages_are_gone_but_memory_remains(
    agent_chat_processor, processor_with_tables
):
    processor_with_tables(owned=False, earlier_messages=False, memory=True)
    with pytest.raises(PermissionError, match="belongs to another user"):
        _turn(agent_chat_processor)
    assert not agent_chat_processor.get_cached_boto3_session.called


@pytest.mark.unit
def test_the_ownership_check_reads_memory_under_the_providers_key():
    """Both predicates restate the memory key; it must stay the provider's."""
    from idp_common.agents.utils import memory_provider

    provider_src = open(memory_provider.__file__).read()
    assert 'return f"conversation#{self.session_id}"' in provider_src
    for rel in (
        "src/lambda/agent_chat_processor/index.py",
        "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py",
    ):
        src = open(os.path.join(_REPO_ROOT, rel)).read()
        assert 'Key("PK").eq(f"conversation#{session_id}")' in src, rel


# --- Quick Start (method=quick_start) is Admin only --------------------------
# Quick Start creates a configuration profile and activates it, and the
# configuration API lets only an Admin create a profile.


def _quick_start_event(group):
    return {
        "identity": {
            "username": "u@example.com",
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
    assert agent_chat_processor._QUICK_START_GROUPS == resolver._QUICK_START_GROUPS
    assert agent_chat_processor._QUICK_START_GROUPS == ("Admin",)
