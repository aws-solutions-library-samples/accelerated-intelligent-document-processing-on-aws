# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``queryKnowledgeBase``: a knowledge base session continues only for its owner.

Bedrock starts a RetrieveAndGenerate session on a question sent without a
``sessionId`` and returns its id; the browser sends that id back to continue the
conversation. The resolver records the id against the caller's Cognito ``sub`` in
``ChatSessionOwnersTable`` (under a ``knowledge-base#`` key) before handing it back,
and passes a ``sessionId`` from a request to Bedrock only when that record names the
caller. Measured against moto's DynamoDB, whose condition-expression engine decides
the conditional writes here.

The question text itself is not logged at INFO.
"""

import json
import logging
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from tests.unit.lambdas._lambda_loader import load_lambda

pytestmark = pytest.mark.unit

REGION = "us-east-1"
OWNERS = "owners"
QUESTION = "What is the total on the March invoice for account 4417?"


def _identity(sub):
    return {"sub": sub, "claims": {"sub": sub, "cognito:groups": ["Admin"]}}


def _event(sub, session_id=None, question=QUESTION):
    arguments = {"input": question}
    if session_id:
        arguments["sessionId"] = session_id
    event = {"arguments": arguments}
    if sub is not None:
        event["identity"] = _identity(sub)
    return event


@pytest.fixture
def resolver(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("KB_ID", "kb-1")
    monkeypatch.setenv("MODEL_ID", "m")
    monkeypatch.setenv("CHAT_SESSION_OWNERS_TABLE", OWNERS)
    monkeypatch.setenv("TENANT_ISOLATION_MODE", "Off")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=OWNERS,
            KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        module = load_lambda(
            "query_knowledgebase_resolver_session",
            "nested/api-resolvers/src/lambda/query_knowledgebase_resolver",
        )
        client = MagicMock()
        started = iter(["bedrock-session-1", "bedrock-session-2", "bedrock-session-3"])

        def retrieve_and_generate(**request):
            session_id = request.get("sessionId") or next(started)
            return {"sessionId": session_id, "output": {"text": "An answer."}}

        client.retrieve_and_generate.side_effect = retrieve_and_generate
        monkeypatch.setattr(module, "KB_CLIENT", client)
        yield module, client, table


def _sent_session(client):
    return client.retrieve_and_generate.call_args.kwargs.get("sessionId")


def test_a_new_session_is_recorded_against_the_caller(resolver):
    module, client, table = resolver
    out = json.loads(module.handler(_event("sub-a"), None))
    assert out["sessionId"] == "bedrock-session-1"
    assert _sent_session(client) is None
    item = table.get_item(Key={"sessionId": "knowledge-base#bedrock-session-1"})["Item"]
    assert item["ownerSub"] == "sub-a"


def test_the_owner_continues_their_session(resolver):
    module, client, _table = resolver
    first = json.loads(module.handler(_event("sub-a"), None))
    out = json.loads(module.handler(_event("sub-a", first["sessionId"]), None))
    assert _sent_session(client) == first["sessionId"]
    assert out["sessionId"] == first["sessionId"]


def test_another_callers_session_id_is_refused_before_bedrock(resolver):
    module, client, table = resolver
    first = json.loads(module.handler(_event("sub-a"), None))
    client.retrieve_and_generate.reset_mock()
    with pytest.raises(PermissionError, match="belongs to another user"):
        module.handler(_event("sub-b", first["sessionId"]), None)
    client.retrieve_and_generate.assert_not_called()
    item = table.get_item(Key={"sessionId": f"knowledge-base#{first['sessionId']}"})
    assert item["Item"]["ownerSub"] == "sub-a"


def test_a_session_id_nobody_recorded_starts_a_new_session(resolver):
    """An id with no record is not continued, and is not claimed by the caller."""
    module, client, table = resolver
    out = json.loads(module.handler(_event("sub-b", "unrecorded-session"), None))
    assert _sent_session(client) is None
    assert out["sessionId"] == "bedrock-session-1"
    assert "Item" not in table.get_item(
        Key={"sessionId": "knowledge-base#unrecorded-session"}
    )


def test_a_session_that_cannot_be_recorded_is_not_handed_back(resolver, monkeypatch):
    module, _client, _table = resolver
    monkeypatch.setattr(module, "OWNERS_TABLE", "")
    out = json.loads(module.handler(_event("sub-a"), None))
    assert "sessionId" not in out
    assert out["output"]["text"] == "An answer."


def test_a_session_id_is_refused_when_ownership_cannot_be_checked(
    resolver, monkeypatch
):
    module, client, _table = resolver
    monkeypatch.setattr(module, "OWNERS_TABLE", "")
    with pytest.raises(PermissionError, match="cannot be verified"):
        module.handler(_event("sub-a", "bedrock-session-1"), None)
    client.retrieve_and_generate.assert_not_called()


def test_a_chat_session_row_does_not_open_a_knowledge_base_session(resolver):
    """The two kinds of session id are kept under different keys."""
    module, client, table = resolver
    table.put_item(Item={"sessionId": "shared-id", "ownerSub": "sub-b"})
    json.loads(module.handler(_event("sub-b", "shared-id"), None))
    assert _sent_session(client) is None


def test_the_question_is_not_logged_at_info(resolver, caplog):
    module, _client, _table = resolver
    with caplog.at_level(logging.INFO):
        module.handler(_event("sub-a"), None)
    info = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.INFO
    )
    assert "Amazon Bedrock KB request" in info
    assert f"queryChars={len(QUESTION)}" in info
    assert "March invoice" not in info
