# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The streaming chat routes, driven end to end: who the caller is, and whose session.

``app.py`` imports FastAPI, which the unit environment does not install, so a
minimal stand-in for the four names it uses is placed in ``sys.modules`` (the
route functions are plain coroutines once the decorator returns them). The two
processor modules are replaced by recorders. Everything this file asserts on is
real: the ID token is minted with a local RSA key and verified by
``idp_common.cognito_id_token`` through PyJWT (only the key-set fetch is
replaced), and session ownership is decided by ``idp_common.chat_session_owner``
against moto's DynamoDB. Moto, not DynamoDB, evaluates the conditional writes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

import boto3
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from moto import mock_aws

pytest.importorskip("idp_common.cognito_id_token")

from idp_common import cognito_id_token  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[1]
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_EXAMPLE"
CLIENT_ID = "exampleclientid"
KID = "kid-1"
# Values of the Cognito ``token_use`` claim.
USE_ID = "id"
USE_ACCESS = "access"
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

A = {"sub": "aaaaaaaa-0000-4000-8000-000000000001", "email": "a@example.com"}
B = {"sub": "bbbbbbbb-0000-4000-8000-000000000002", "email": "b@example.com"}


# --- stand-ins ----------------------------------------------------------------


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _App:
    def get(self, _path):
        return lambda f: f

    def post(self, _path):
        return lambda f: f


class StreamingResponse:
    def __init__(self, body, media_type=None):
        self.body = body


class _Request:
    def __init__(self, headers: dict[str, str]):
        self.headers = {k.lower(): v for k, v in headers.items()}


class _Processor(types.ModuleType):
    """Records every event a route hands it."""

    def __init__(self, name: str):
        super().__init__(name)
        self.events: list[dict] = []

    def set_sink(self, _sink):
        pass

    def handler(self, event, _context):
        self.events.append(event)

    @staticmethod
    def _enforce_agent_chat_groups(event):
        # The processor's policy: Admin, Author, Viewer. Delegation to the real
        # function is asserted structurally in test_stream_processor.py.
        groups = (event.get("identity") or {}).get("claims", {}).get(
            "cognito:groups"
        ) or []
        if not {"Admin", "Author", "Viewer"}.intersection(groups):
            raise PermissionError("Unauthorized: Agent Chat requires a group")


@pytest.fixture
def env(monkeypatch):
    # The tables are created in this region; the app resolves its own from the
    # environment, so pin both rather than inherit the shell's.
    for k, v in {
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        owners = ddb.create_table(
            TableName="owners",
            KeySchema=[{"AttributeName": "sessionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "sessionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        messages = ddb.create_table(
            TableName="messages",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sessions = ddb.create_table(
            TableName="sessions",
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "sessionId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "sessionId", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for k, v in {
            "COGNITO_ISSUER": ISSUER,
            "USER_POOL_CLIENT_ID": CLIENT_ID,
            "CHAT_SESSION_OWNERS_TABLE": "owners",
            "CHAT_MESSAGES_TABLE": "messages",
            "CHAT_SESSIONS_TABLE": "sessions",
        }.items():
            monkeypatch.setenv(k, v)

        client = jwt.PyJWKClient(cognito_id_token.jwks_url(ISSUER), cache_keys=False)
        jwk = RSAAlgorithm.to_jwk(SIGNING_KEY.public_key(), as_dict=True)
        jwk.update({"kid": KID, "alg": "RS256"})
        client.fetch_data = lambda: {"keys": [jwk]}  # type: ignore[method-assign]
        monkeypatch.setattr(cognito_id_token, "_jwks_client", lambda _issuer: client)

        agent = _Processor("agent_chat_processor")
        doc = _Processor("chat_with_document_processor")
        fastapi = types.ModuleType("fastapi")
        fastapi.FastAPI = _App  # type: ignore[attr-defined]
        fastapi.HTTPException = HTTPException  # type: ignore[attr-defined]
        fastapi.Request = _Request  # type: ignore[attr-defined]
        responses = types.ModuleType("fastapi.responses")
        responses.StreamingResponse = StreamingResponse  # type: ignore[attr-defined]
        for name, module in {
            "fastapi": fastapi,
            "fastapi.responses": responses,
            "agent_chat_processor": agent,
            "chat_with_document_processor": doc,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.syspath_prepend(str(APP_DIR))
        spec = importlib.util.spec_from_file_location(
            "chat_stream_app_under_test", APP_DIR / "app.py"
        )
        app = importlib.util.module_from_spec(spec)
        # app.py postpones annotations, and pydantic resolves them through the
        # module's entry in sys.modules.
        monkeypatch.setitem(sys.modules, spec.name, app)
        spec.loader.exec_module(app)  # type: ignore[union-attr]
        yield types.SimpleNamespace(
            app=app,
            agent=agent,
            doc=doc,
            owners=owners,
            messages=messages,
            sessions=sessions,
        )


def _token(user: dict, groups=("Author",), **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": user["sub"],
        "email": user["email"],
        "cognito:username": user["email"],
        "cognito:groups": list(groups),
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "token_use": USE_ID,
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, SIGNING_KEY, algorithm="RS256", headers={"kid": KID})


def _run(coro):
    return asyncio.run(coro)


def _drain(response):
    async def _all():
        return [frame async for frame in response.body]

    return asyncio.run(_all())


def _agent(env, headers: dict, **body):
    request = _Request(headers)
    payload = env.app.AgentChatRequest(**{"prompt": "hi", **body})
    response = _run(env.app.chat_agent(request, payload))
    _drain(response)
    return response


def _document(env, headers: dict, **body):
    request = _Request(headers)
    payload = env.app.DocumentChatRequest(**{"prompt": "hi", **body})
    response = _run(env.app.chat_document(request, payload))
    _drain(response)
    return response


def _as(user, **kw) -> dict:
    return {"x-idp-id-token": _token(user, **kw)}


def _owner(env, sid):
    item = env.owners.get_item(Key={"sessionId": sid}).get("Item")
    return item and item["ownerSub"]


# --- who the caller is ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("route", ["agent", "document"])
def test_a_request_with_no_token_is_refused_before_anything_runs(env, route):
    call = _agent if route == "agent" else _document
    with pytest.raises(HTTPException) as refused:
        call(env, {}, sessionId="s-1")
    assert refused.value.status_code == 401
    assert not env.agent.events and not env.doc.events
    assert _owner(env, "s-1") is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides", [{"aud": "another-client"}, {"token_use": USE_ACCESS}, {"exp": 1}]
)
def test_a_token_the_pool_did_not_issue_for_the_web_ui_is_refused(env, overrides):
    with pytest.raises(HTTPException) as refused:
        _agent(env, _as(A, **overrides), sessionId="s-1")
    assert refused.value.status_code == 401
    assert not env.agent.events


@pytest.mark.unit
def test_the_processor_is_told_the_verified_caller(env):
    _agent(env, _as(A), sessionId="s-a")
    (event,) = env.agent.events
    assert event["identity"]["claims"]["sub"] == A["sub"]
    assert event["identity"]["claims"]["cognito:groups"] == ["Author"]
    assert event["callerSub"] == A["email"]
    assert _owner(env, "s-a") == A["sub"]


@pytest.mark.unit
def test_a_body_caller_identity_is_ignored(env):
    # B names A as the caller in the body. The turn is B's, filed under B.
    _agent(env, _as(B), sessionId="s-b", callerSub=A["email"])
    (event,) = env.agent.events
    assert event["callerSub"] == B["email"]
    assert event["identity"]["claims"]["sub"] == B["sub"]
    assert _owner(env, "s-b") == B["sub"]


# --- whose session -------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("route", ["agent", "document"])
def test_another_users_session_is_refused_before_the_stream(env, route):
    _agent(env, _as(A), sessionId="s-a")
    env.agent.events.clear()
    call = _agent if route == "agent" else _document
    with pytest.raises(HTTPException) as refused:
        call(env, _as(B), sessionId="s-a", callerSub=A["email"])
    assert refused.value.status_code == 403
    assert not env.agent.events and not env.doc.events
    assert _owner(env, "s-a") == A["sub"]


@pytest.mark.unit
def test_a_session_row_of_the_callers_own_does_not_make_the_session_theirs(env):
    # Sending one message under A's id used to create B's own row for it.
    _agent(env, _as(A), sessionId="s-a")
    env.sessions.put_item(Item={"userId": B["email"], "sessionId": "s-a"})
    with pytest.raises(HTTPException) as refused:
        _agent(env, _as(B), sessionId="s-a")
    assert refused.value.status_code == 403


@pytest.mark.unit
def test_a_session_from_before_the_registry_belongs_to_whoever_holds_its_row(env):
    env.messages.put_item(Item={"PK": "old", "SK": "t1", "content": "A's history"})
    env.sessions.put_item(Item={"userId": A["email"], "sessionId": "old"})
    with pytest.raises(HTTPException):
        _agent(env, _as(B), sessionId="old")
    assert _owner(env, "old") is None
    _agent(env, _as(A), sessionId="old")
    assert _owner(env, "old") == A["sub"]


@pytest.mark.unit
def test_an_unconfigured_owner_table_refuses(env, monkeypatch):
    monkeypatch.delenv("CHAT_SESSION_OWNERS_TABLE")
    with pytest.raises(HTTPException) as refused:
        _agent(env, _as(A), sessionId="s-a")
    assert refused.value.status_code == 403
    assert not env.agent.events


# --- groups, now that the claims arrive ----------------------------------------


@pytest.mark.unit
def test_agent_chat_refuses_a_group_the_rest_route_refuses(env):
    with pytest.raises(HTTPException) as refused:
        _agent(env, _as(A, groups=("Reviewer",)), sessionId="s-r")
    assert refused.value.status_code == 403
    assert not env.agent.events


@pytest.mark.unit
def test_document_chat_refuses_a_caller_in_no_group(env):
    with pytest.raises(HTTPException) as refused:
        _document(env, _as(A, groups=()), sessionId="s-d")
    assert refused.value.status_code == 403
    assert not env.doc.events


@pytest.mark.unit
def test_document_chat_forwards_the_claims_its_scope_check_reads(env):
    _document(env, _as(A), sessionId="s-d", s3Uri="s3://b/k")
    (event,) = env.doc.events
    claims = event["identity"]["claims"]
    assert claims == {
        "cognito:groups": ["Author"],
        "email": A["email"],
        "sub": A["sub"],
    }
