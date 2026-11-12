# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The agent_chat resolver (REST path) and per-session opt-in agents.

The resolver stores the user message itself before it invokes the processor, so
it is where an opt-in message on a session holding Analytics or Error Analyzer
results is moved to a new session. Loaded from its file against moto's DynamoDB
with its own vendored ``chat_session_owner``; the processor Lambda client is a
recorder.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../..")
_RESOLVER_DIR = os.path.join(
    _REPO_ROOT, "nested/api-resolvers/src/lambda/agent_chat_resolver"
)

SUB = "aaaaaaaa-0000-4000-8000-000000000001"


@pytest.fixture
def resolver(monkeypatch):
    for k, v in {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "CHAT_MESSAGES_TABLE": "messages",
        "CHAT_SESSIONS_TABLE": "sessions",
        "CHAT_SESSION_OWNERS_TABLE": "owners",
        "AGENT_CHAT_PROCESSOR_FUNCTION": "processor-fn",
    }.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, keys in (
            ("messages", ("PK", "SK")),
            ("sessions", ("userId", "sessionId")),
            ("owners", ("sessionId",)),
        ):
            ddb.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": k, "KeyType": t}
                    for k, t in zip(keys, ("HASH", "RANGE"))
                ],
                AttributeDefinitions=[
                    {"AttributeName": k, "AttributeType": "S"} for k in keys
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        # The resolver imports siblings by bare name, as Lambda lays them out.
        saved = {k: sys.modules.pop(k, None) for k in ("chat_session_owner",)}
        sys.path.insert(0, _RESOLVER_DIR)
        try:
            spec = importlib.util.spec_from_file_location(
                "agent_chat_resolver_session_agents",
                os.path.join(_RESOLVER_DIR, "index.py"),
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            sys.path.pop(0)
        module.lambda_client = MagicMock()
        yield module, ddb.Table("owners")
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _send(module, sid, **arguments):
    event = {
        "arguments": {"prompt": "hi", "sessionId": sid, **arguments},
        "identity": {
            "username": "user-a",
            "sub": SUB,
            "claims": {"cognito:groups": ["Author"], "sub": SUB},
        },
    }
    return module.handler(event, None)


def _payload(module):
    return json.loads(module.lambda_client.invoke.call_args.kwargs["Payload"])


def _flag(owners, sid):
    owners.update_item(
        Key={"sessionId": sid},
        UpdateExpression="SET dataResultsAt = :t",
        ExpressionAttributeValues={":t": 1},
    )


@pytest.mark.unit
def test_external_mcp_agents_are_off_unless_opted_in(resolver):
    module, _ = resolver
    _send(module, "s1")
    assert _payload(module)["enableExternalMcpAgents"] is False
    _send(module, "s1", enableExternalMcpAgents=True)
    assert _payload(module)["enableExternalMcpAgents"] is True


@pytest.mark.unit
@pytest.mark.parametrize("value", ["true", 1, "yes", [], {}, None])
def test_a_non_boolean_does_not_opt_in_to_external_mcp_agents(resolver, value):
    module, _ = resolver
    _send(module, "s1", enableExternalMcpAgents=value)
    assert _payload(module)["enableExternalMcpAgents"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "opt_in", [{"enableCodeIntelligence": True}, {"enableExternalMcpAgents": True}]
)
def test_an_opt_in_on_a_flagged_session_moves_to_a_new_session(resolver, opt_in):
    module, owners = resolver
    _send(module, "s1")
    _flag(owners, "s1")

    response = _send(module, "s1", **opt_in)

    new_sid = response["sessionId"]
    assert new_sid != "s1"
    assert _payload(module)["sessionId"] == new_sid
    item = owners.get_item(Key={"sessionId": new_sid})["Item"]
    assert item["ownerSub"] == SUB
    assert "dataResultsAt" not in item


@pytest.mark.unit
def test_a_flagged_session_without_an_opt_in_keeps_its_id(resolver):
    module, owners = resolver
    _send(module, "s1")
    _flag(owners, "s1")
    assert _send(module, "s1")["sessionId"] == "s1"


@pytest.mark.unit
def test_an_opt_in_on_an_unflagged_session_keeps_its_id(resolver):
    module, _ = resolver
    _send(module, "s1")
    assert _send(module, "s1", enableCodeIntelligence=True)["sessionId"] == "s1"


@pytest.mark.unit
def test_the_streaming_route_declares_the_opt_in_strictly():
    """``app.py`` imports FastAPI, absent here, so the declaration is read."""
    source = open(
        os.path.join(_REPO_ROOT, "src/lambda/chat_stream_processor/app.py")
    ).read()
    flat = " ".join(source.split())
    assert "enableExternalMcpAgents: StrictBool = False" in flat
    assert '"enableExternalMcpAgents": body.enableExternalMcpAgents' in flat
