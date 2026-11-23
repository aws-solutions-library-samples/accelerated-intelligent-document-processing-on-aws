# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The agent chat processor builds memory only for the session's own user.

Run against both copies of the processor (the deployed function and the copy
vendored into the streaming endpoint). The agent factory is a recorder, so what
is asserted is what the processor asks for: whose memory the orchestrator is
built with, and whether a turn on someone else's session gets that far at all.
Ownership is decided by the real ``idp_common.chat_session_owner`` against
moto's DynamoDB (moto evaluates the conditional writes, not DynamoDB).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import boto3
import pytest
from moto import mock_aws

pytest.importorskip("idp_common.chat_session_owner")

import idp_common.chat_session_agents as chat_session_agents  # noqa: E402
import idp_common.chat_session_owner as chat_session_owner  # noqa: E402

pytestmark = pytest.mark.unit

_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LAMBDA = os.path.dirname(_PKG)
_COPIES = {
    "source": os.path.join(_LAMBDA, "agent_chat_processor", "index.py"),
    "vendored": os.path.join(_PKG, "vendored", "agent_chat_processor.py"),
}

A = {"sub": "aaaaaaaa-0000-4000-8000-000000000001", "email": "a@example.com"}
B = {"sub": "bbbbbbbb-0000-4000-8000-000000000002", "email": "b@example.com"}


class _Factory:
    def __init__(self):
        self.calls: list[dict] = []

    def list_available_agents(self):
        return [{"agent_id": "Analytics-Agent"}]

    def _record(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(close=lambda: None)

    def create_conversational_orchestrator(self, **kwargs):
        return self._record(**kwargs)

    def create_conversational_agent(self, **kwargs):
        return self._record(**kwargs)


def _stubs(factory) -> dict[str, types.ModuleType]:
    made = {}

    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__path__ = []
        for k, v in attrs.items():
            setattr(m, k, v)
        made[name] = m

    class _ErrorHandler:
        @staticmethod
        def format_error_for_frontend(exc):
            return {"errorType": "test", "message": str(exc)}

    mod("idp_common")
    mod("idp_common.agents")
    mod("idp_common.agents.analytics", get_analytics_config=lambda *a, **k: {})
    mod("idp_common.agents.common")
    mod("idp_common.agents.common.config", configure_logging=lambda *a, **k: None)
    mod(
        "idp_common.agents.common.bedrock_error_messages",
        BedrockErrorMessageHandler=_ErrorHandler,
    )
    mod("idp_common.agents.factory", agent_factory=factory)
    mod("idp_common.config_scope", unscoped_is_denied=lambda: False)
    made["idp_common.chat_session_owner"] = chat_session_owner
    made["idp_common.chat_session_agents"] = chat_session_agents
    mod("idp_common.utils")
    mod("idp_common.utils.log_sanitizer", sanitize_event_for_logging=lambda e, *a: e)
    return made


@pytest.fixture(params=sorted(_COPIES))
def world(request, monkeypatch):
    for k, v in {
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "CHAT_MESSAGES_TABLE": "messages",
        "CHAT_SESSIONS_TABLE": "sessions",
        "CHAT_SESSION_OWNERS_TABLE": "owners",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")

        def table(name, *keys):
            return ddb.create_table(
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

        tables = types.SimpleNamespace(
            messages=table("messages", "PK", "SK"),
            sessions=table("sessions", "userId", "sessionId"),
            owners=table("owners", "sessionId"),
        )
        factory = _Factory()
        for name, module in _stubs(factory).items():
            monkeypatch.setitem(sys.modules, name, module)
        name = f"_acp_owner_{request.param}"
        spec = importlib.util.spec_from_file_location(name, _COPIES[request.param])
        processor = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, processor)
        spec.loader.exec_module(processor)  # type: ignore[union-attr]

        async def _answer(*_args, **_kwargs):
            return "the answer"

        monkeypatch.setattr(processor, "stream_agent_response", _answer)
        yield types.SimpleNamespace(
            processor=processor, factory=factory, **vars(tables)
        )


def _event(user, sid, **extra):
    event = {"sessionId": sid, "prompt": "hi", "method": "chat"}
    if user is not None:
        event["callerSub"] = user["email"]
        event["identity"] = {
            "claims": {"cognito:groups": ["Author"], "sub": user["sub"]}
        }
    else:
        event["identity"] = None
    event.update(extra)
    return event


def _owner(world, sid):
    item = world.owners.get_item(Key={"sessionId": sid}).get("Item")
    return item and item["ownerSub"]


def _stored(world, sid):
    return world.messages.query(
        KeyConditionExpression="PK = :s", ExpressionAttributeValues={":s": sid}
    )["Items"]


def test_the_owners_turn_builds_memory_keyed_on_them(world):
    assert world.processor.handler(_event(A, "s-a"), None)["statusCode"] == 200
    (call,) = world.factory.calls
    assert call["user_id"] == A["sub"]
    assert call["session_id"] == "s-a"
    assert _owner(world, "s-a") == A["sub"]
    assert [m["role"] for m in _stored(world, "s-a")] == ["user", "assistant"]


def test_another_users_session_is_refused_before_memory_is_built(world):
    world.processor.handler(_event(A, "s-a"), None)
    world.factory.calls.clear()
    with pytest.raises(PermissionError):
        world.processor.handler(_event(B, "s-a"), None)
    assert world.factory.calls == []
    assert len(_stored(world, "s-a")) == 2
    assert "Item" not in world.sessions.get_item(
        Key={"userId": B["email"], "sessionId": "s-a"}
    )


def test_a_session_from_before_the_owner_table_is_not_given_to_another_user(world):
    world.messages.put_item(Item={"PK": "old", "SK": "t1", "content": "A old"})
    world.sessions.put_item(Item={"userId": A["email"], "sessionId": "old"})
    with pytest.raises(PermissionError):
        world.processor.handler(_event(B, "old"), None)
    assert world.factory.calls == []
    world.processor.handler(_event(A, "old"), None)
    assert _owner(world, "old") == A["sub"]


def test_a_turn_with_no_verified_user_gets_no_memory_and_stores_nothing(world):
    # callerSub without an identity is not a user: nothing a transport verified.
    world.processor.handler(_event(None, "s-x", callerSub=A["email"]), None)
    (call,) = world.factory.calls
    assert call["user_id"] == ""
    assert _stored(world, "s-x") == []
    assert _owner(world, "s-x") is None


def test_an_unconfigured_owner_table_refuses_the_turn(world, monkeypatch):
    monkeypatch.setattr(world.processor, "_CHAT_SESSION_OWNERS_TABLE", None)
    with pytest.raises(PermissionError):
        world.processor.handler(_event(A, "s-a"), None)
    assert world.factory.calls == []
