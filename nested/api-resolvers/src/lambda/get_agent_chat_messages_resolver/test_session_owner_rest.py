# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One user cannot reach another user's chat session through the REST resolvers.

Drives the four resolvers that touch a session id — sendAgentChatMessage,
getChatMessages, deleteChatSession and sendChatDocumentMessage — against one set
of tables, the way they share them in a deployment. Chat history is stored under
``PK=sessionId`` with no user term, so what keeps user B out of user A's history
is the owner ``ChatSessionOwnersTable`` records for the id. The shape these tests
exist for: B sending one message under A's id used to create B's own session row,
and that row was what the history read accepted as ownership.

Measured against moto's DynamoDB. The conditional writes the owner registry rests
on are evaluated by moto's engine, not DynamoDB's.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

pytestmark = pytest.mark.unit

LAMBDA_DIR = Path(__file__).resolve().parents[1]

A = {"sub": "aaaaaaaa-0000-4000-8000-000000000001", "email": "a@example.com"}
B = {"sub": "bbbbbbbb-0000-4000-8000-000000000002", "email": "b@example.com"}


def _identity(user, groups=("Author",)):
    """The identity ``idp_common.api_adapter.normalize_event`` builds."""
    return {
        "username": user["email"],
        "sub": user["sub"],
        "claims": {
            "sub": user["sub"],
            "email": user["email"],
            "cognito:groups": list(groups),
        },
    }


def _load(resolver: str):
    directory = LAMBDA_DIR / resolver
    for shared in ("chat_session_owner", "log_sanitizer"):
        sys.modules.pop(shared, None)
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location(
            f"{resolver}_under_test", directory / "index.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    finally:
        sys.path.remove(str(directory))
    return module


@pytest.fixture
def world(monkeypatch):
    for k, v in {
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "CHAT_MESSAGES_TABLE": "messages",
        "CHAT_SESSIONS_TABLE": "sessions",
        "CHAT_SESSION_OWNERS_TABLE": "owners",
        "CHAT_DOCUMENT_SESSIONS_TABLE": "doc-sessions",
        "AGENT_CHAT_PROCESSOR_FUNCTION": "agent-processor",
        "CHAT_DOCUMENT_PROCESSOR_FUNCTION_ARN": "doc-processor",
        "ENFORCE_CHAT_SESSION_OWNERSHIP": "true",
    }.items():
        monkeypatch.setenv(k, v)
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

        tables = {
            "messages": table("messages", "PK", "SK"),
            "sessions": table("sessions", "userId", "sessionId"),
            "owners": table("owners", "sessionId"),
            "doc_sessions": table("doc-sessions", "sessionId"),
        }
        send = _load("agent_chat_resolver")
        send.lambda_client = MagicMock(name="lambda")
        read = _load("get_agent_chat_messages_resolver")
        delete = _load("delete_agent_chat_session_resolver")
        doc = _load("send_chat_document_message_resolver")
        doc._lambda = MagicMock(name="lambda")
        yield {"send": send, "read": read, "delete": delete, "doc": doc, **tables}


def _send(world, user, sid, prompt="hello"):
    return world["send"].handler(
        {
            "arguments": {"prompt": prompt, "sessionId": sid},
            "identity": _identity(user),
        },
        None,
    )


def _read(world, user, sid):
    return world["read"].handler(
        {"arguments": {"sessionId": sid}, "identity": _identity(user)}, None
    )


def _delete(world, user, sid):
    return world["delete"].handler(
        {"arguments": {"sessionId": sid}, "identity": _identity(user)}, None
    )


def _doc(world, user, sid):
    return world["doc"].handler(
        {
            "arguments": {"sessionId": sid, "prompt": "q", "s3Uri": "s3://b/k"},
            "identity": _identity(user),
        },
        None,
    )


def _owner(world, sid):
    item = world["owners"].get_item(Key={"sessionId": sid}).get("Item")
    return item and item["ownerSub"]


def _messages(world, sid):
    return world["messages"].query(
        KeyConditionExpression="PK = :s", ExpressionAttributeValues={":s": sid}
    )["Items"]


def test_the_first_sender_owns_the_session_and_reads_it_back(world):
    _send(world, A, "s-a", "A secret")
    assert _owner(world, "s-a") == A["sub"]
    assert [m["content"] for m in _read(world, A, "s-a")] == ["A secret"]
    payload = world["send"].lambda_client.invoke.call_args.kwargs["Payload"]
    assert A["sub"] in payload, "the processor must be told whose turn it is"


def test_user_b_cannot_send_under_user_as_session(world):
    _send(world, A, "s-a", "A secret")
    world["send"].lambda_client.invoke.reset_mock()
    with pytest.raises(PermissionError):
        _send(world, B, "s-a", "B probe")
    assert [m["content"] for m in _messages(world, "s-a")] == ["A secret"]
    assert "Item" not in world["sessions"].get_item(
        Key={"userId": B["email"], "sessionId": "s-a"}
    )
    world["send"].lambda_client.invoke.assert_not_called()


def test_user_b_gets_no_history_from_user_as_session(world):
    _send(world, A, "s-a", "A secret")
    with pytest.raises(PermissionError):
        _read(world, B, "s-a")


def test_the_row_a_message_used_to_create_does_not_open_the_history(world):
    _send(world, A, "s-a", "A secret")
    # What one message from B under A's id used to leave behind.
    world["sessions"].put_item(Item={"userId": B["email"], "sessionId": "s-a"})
    with pytest.raises(PermissionError):
        _read(world, B, "s-a")
    with pytest.raises(PermissionError):
        _delete(world, B, "s-a")
    assert len(_messages(world, "s-a")) == 1


def test_document_chat_cannot_take_an_agent_chat_session(world):
    # The two chat surfaces store messages under the same key, so they share
    # one owner namespace.
    _send(world, A, "s-a", "A secret")
    with pytest.raises(PermissionError):
        _doc(world, B, "s-a")
    world["doc"]._lambda.invoke.assert_not_called()
    with pytest.raises(PermissionError):
        _read(world, B, "s-a")


def test_user_b_cannot_delete_user_as_history(world):
    _send(world, A, "s-a", "A secret")
    with pytest.raises(PermissionError):
        _delete(world, B, "s-a")
    assert len(_messages(world, "s-a")) == 1
    assert _delete(world, A, "s-a") is True
    assert _messages(world, "s-a") == []


def test_reading_an_unused_id_does_not_reserve_it(world):
    with pytest.raises(PermissionError):
        _read(world, B, "s-new")
    assert _owner(world, "s-new") is None
    _send(world, A, "s-new")
    assert _owner(world, "s-new") == A["sub"]


def test_a_session_from_before_the_owner_table_stays_its_owners(world):
    world["messages"].put_item(Item={"PK": "old", "SK": "t1", "content": "A old"})
    world["sessions"].put_item(Item={"userId": A["email"], "sessionId": "old"})
    with pytest.raises(PermissionError):
        _read(world, B, "old")
    with pytest.raises(PermissionError):
        _send(world, B, "old")
    assert _owner(world, "old") is None
    assert [m["content"] for m in _read(world, A, "old")] == ["A old"]
    assert _owner(world, "old") == A["sub"]


def test_a_document_session_from_before_the_owner_table_stays_its_owners(world):
    world["messages"].put_item(Item={"PK": "olddoc", "SK": "t1", "content": "A doc"})
    world["doc_sessions"].put_item(Item={"sessionId": "olddoc", "ownerSub": A["sub"]})
    with pytest.raises(PermissionError):
        _doc(world, B, "olddoc")
    _doc(world, A, "olddoc")
    assert _owner(world, "olddoc") == A["sub"]
    assert [m["content"] for m in _read(world, A, "olddoc")] == ["A doc"]


@pytest.mark.parametrize("resolver", ["read", "delete"])
def test_an_unconfigured_owner_table_denies(world, monkeypatch, resolver):
    _send(world, A, "s-a")
    monkeypatch.setattr(world[resolver], "CHAT_SESSION_OWNERS_TABLE", None)
    call = _read if resolver == "read" else _delete
    with pytest.raises(PermissionError):
        call(world, A, "s-a")


@pytest.mark.parametrize("method", ["assistant_final_response", "assistant_error"])
def test_a_signed_in_caller_cannot_store_an_assistant_message(world, method):
    """Assistant messages come only from the processor's identity-less path."""
    _send(world, A, "s-a", "A secret")
    with pytest.raises(PermissionError):
        world["send"].handler(
            {
                "arguments": {
                    "prompt": "planted",
                    "sessionId": "s-a",
                    "method": method,
                },
                "identity": _identity(B),
            },
            None,
        )
    assert [m["content"] for m in _messages(world, "s-a")] == ["A secret"]
    # Nor can it give an unused id history that would lock its first user out.
    with pytest.raises(PermissionError):
        world["send"].handler(
            {
                "arguments": {
                    "prompt": "planted",
                    "sessionId": "s-new",
                    "method": method,
                },
                "identity": _identity(B),
            },
            None,
        )
    assert _messages(world, "s-new") == []
    _send(world, A, "s-new")
    assert _owner(world, "s-new") == A["sub"]


def test_the_backend_path_still_stores_assistant_messages(world):
    _send(world, A, "s-a", "A secret")
    world["send"].handler(
        {
            "arguments": {
                "prompt": "the answer",
                "sessionId": "s-a",
                "method": "assistant_final_response",
            }
        },
        None,
    )
    assert [m["content"] for m in _read(world, A, "s-a")] == ["A secret", "the answer"]
