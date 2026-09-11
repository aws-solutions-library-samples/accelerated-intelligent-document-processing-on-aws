# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Code Intelligence Agent must be opt-in, not opt-out.

The agent registers a hardcoded, unauthenticated third-party MCP server
(DeepWiki), so an omitted ``enableCodeIntelligence`` must resolve to False on
every entry point. These tests pin that default so a future refactor cannot
silently restore an unconditional third-party data flow.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")
_RESOLVER = os.path.join(
    _REPO_ROOT, "nested/api-resolvers/src/lambda/agent_chat_resolver/index.py"
)
_PROCESSORS = (
    "src/lambda/agent_chat_processor/index.py",
    "src/lambda/chat_stream_processor/vendored/agent_chat_processor.py",
)


@pytest.fixture
def resolver(monkeypatch):
    """Load the agent chat resolver with a stubbed processor Lambda client."""
    monkeypatch.setenv("CHAT_MESSAGES_TABLE", "test-messages")
    monkeypatch.setenv("CHAT_SESSIONS_TABLE", "test-sessions")
    monkeypatch.setenv("AGENT_CHAT_PROCESSOR_FUNCTION", "processor-fn")
    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.return_value = MagicMock()
        spec = importlib.util.spec_from_file_location(
            "agent_chat_resolver_default_off", _RESOLVER
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        yield module


def _invoked_payload(resolver_module):
    """Return the payload the resolver forwarded to the processor Lambda."""
    call = resolver_module.lambda_client.invoke.call_args
    assert call is not None, "processor Lambda was never invoked"
    return json.loads(call.kwargs["Payload"])


@pytest.mark.unit
def test_omitted_flag_defaults_to_disabled(resolver):
    """No enableCodeIntelligence argument means no third-party data flow."""
    event = {"arguments": {"prompt": "hi", "sessionId": "s1"}}
    resolver.handler(event, None)
    assert _invoked_payload(resolver)["enableCodeIntelligence"] is False


@pytest.mark.unit
def test_explicit_opt_in_is_honored(resolver):
    """An explicit True still enables the agent."""
    event = {
        "arguments": {
            "prompt": "hi",
            "sessionId": "s1",
            "enableCodeIntelligence": True,
        }
    }
    resolver.handler(event, None)
    assert _invoked_payload(resolver)["enableCodeIntelligence"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "value", ["false", "no", "0", 0, 1, "true", [], {}, None, "", "False"]
)
def test_non_boolean_cannot_opt_in(resolver, value):
    """Only a literal boolean True opts in.

    The HTTP dispatcher's schema rejects non-booleans, but the resolver is also
    reachable by direct (IAM) invocation with no validation — and a truthy
    coercion would read the string ``"false"`` as consent.
    """
    event = {
        "arguments": {
            "prompt": "hi",
            "sessionId": "s1",
            "enableCodeIntelligence": value,
        }
    }
    resolver.handler(event, None)
    assert _invoked_payload(resolver)["enableCodeIntelligence"] is False


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", _PROCESSORS)
def test_processor_requires_literal_true(rel_path):
    """Both processor copies require a literal boolean True.

    The processors import ``strands`` at module scope, so assert on the source
    rather than paying for a full import just to read one default.
    """
    source = open(os.path.join(_REPO_ROOT, rel_path)).read()
    assert 'event.get("enableCodeIntelligence") is True' in source
    assert 'event.get("enableCodeIntelligence", True)' not in source


@pytest.mark.unit
def test_stream_processor_requires_literal_true():
    """The streaming (SSE) entry point takes raw client JSON, so it must be strict."""
    source = open(
        os.path.join(_REPO_ROOT, "src/lambda/chat_stream_processor/app.py")
    ).read()
    # Collapse whitespace so the assertion survives reformatting by the linter.
    flat = " ".join(source.split())
    assert 'body.get("enableCodeIntelligence") is True' in flat
    assert 'body.get("enableCodeIntelligence", True)' not in flat
    # bool() would read the JSON string "false" as an opt-in.
    assert 'bool( body.get("enableCodeIntelligence"' not in flat
