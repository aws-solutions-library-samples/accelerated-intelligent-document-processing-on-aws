# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda Function URL streaming endpoint for chat (commercial partition only).

This replaces the AppSync mutation -> subscription fan-out used to deliver chat
token deltas. Instead of publishing each delta as an AppSync mutation, the two
chat processors are driven here behind a Lambda **Function URL** with
``InvokeMode=RESPONSE_STREAM`` and the **AWS Lambda Web Adapter** (LWA). The
browser POSTs directly to the Function URL (SigV4-signed with Cognito Identity
Pool credentials; the URL is ``AuthType=AWS_IAM``) and reads the streamed
Server-Sent-Events (SSE) body incrementally.

Two routes:
  * ``POST /chat/document`` — Chat-with-Document. Body:
    ``{sessionId, prompt, s3Uri, modelId, turnId?}``.
  * ``POST /chat/agent``    — Agent chat. Body:
    ``{sessionId, prompt, method?, enableCodeIntelligence?,
    enableExternalMcpAgents?}``.

Each emitted event is written as one SSE frame::

    data: {"method": "...", "status": "...", "content": "...", ...}\n\n

The shapes mirror exactly what the AppSync subscriptions delivered, so the UI
state machines (``handleUpdate`` / ``handleStreamingMessage`` / ``addMessage``)
are reused unchanged — only the source of the events differs.

Auth model
----------
The Function URL is ``AuthType=AWS_IAM``; the browser signs the request with
SigV4 using the authenticated Cognito Identity Pool role, which is granted
``lambda:InvokeFunction`` on this function. That proves the caller is a signed-in
user of this deployment and nothing more: the credentials are the same for every
user of the pool (see the measured note in ``sse.py``), and the transport forwards
no Cognito claims.

So the browser also presents its Cognito **ID token**, in the ``x-idp-id-token``
header (``Authorization`` carries the SigV4 signature). Every chat route verifies
it with ``idp_common.cognito_id_token.verify_id_token`` — signature against the
user pool's key set, issuer, audience (the web UI's app client), ``token_use`` and
expiry — before anything else, and answers 401 when it is missing or invalid.
The verified claims are the only source of the caller's identity:

* ``sub`` is who the session belongs to. The route registers or checks the
  session id against ``ChatSessionOwnersTable`` before the stream opens and
  answers 403 for a session another user owns (``_claim_session_or_403``), and
  the agent processor keys conversation memory on it.
* ``cognito:groups`` feeds the same group checks the REST routes apply
  (``_enforce_groups_or_403``, ``_require_a_group_or_403``).
* ``email`` is what Chat-with-Document's ``allowedConfigVersions`` lookup
  resolves the caller by.

Anything identity-like in the request body is ignored.

Every refusal is raised in the route, before ``StreamingResponse`` is returned:
after that the status is fixed at 200 and a denial could only arrive as an error
frame in the body.

This transport exists in the **commercial partition only**. On GovCloud the UI
falls back to the REST dispatcher plus polling (``ChatPanel.tsx``), so every
statement above is scoped to commercial deployments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time

# agent_chat_processor / chat_with_document_processor are the *exact* processor
# sources, copied into this package at build time by the Makefile
# (BuildMethod: makefile). They are the same files deployed as
# ChatWithDocumentProcessorFunction / AgentChatProcessorFunction, so there is a
# single source of truth for the Bedrock/agent orchestration logic.
import agent_chat_processor as agent_proc  # type: ignore
import chat_with_document_processor as doc_proc  # type: ignore
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictBool
from sse import now_iso, sse

from idp_common.chat_session_owner import (
    claim_chat_session,
    has_stored_messages,
    holds_session_row,
)
from idp_common.cognito_id_token import IdTokenRejected, verify_id_token

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI()

# Sentinel pushed onto the queue to signal the producer thread is done.
_DONE = object()


#: Header the web UI sends its Cognito ID token in. ``Authorization`` is taken by
#: the SigV4 signature the Function URL itself checks.
ID_TOKEN_HEADER = "x-idp-id-token"

_DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))


# --- request schemas ---------------------------------------------------------
# The route bodies used to be raw ``await request.json()`` dicts with no schema,
# so every field had to be defensively re-coerced at each use site and a
# malformed body surfaced as a 500 from deep inside a processor. Declaring them
# lets Starlette reject a malformed body with a 422 before any processing starts
# and bounds every string the processors go on to use.
#
# ``StrictBool`` (not ``bool``) for the opt-in flag: Pydantic's lax mode coerces
# ``1``, ``"yes"`` and ``"on"`` to True (measured), so a non-boolean body value
# could turn the flag ON. The flag gates a third-party MCP data flow, so only a
# literal JSON boolean may do that.
#
# Unknown keys are ignored rather than rejected (Pydantic's default), so adding
# a field to a request in the web UI cannot 422 against an older deployment.


class _ChatRequest(BaseModel):
    """Fields common to both chat routes.

    There is no identity field. Earlier web UI builds send ``callerSub``; it is
    an unknown key, so it is ignored, and the caller is taken from the verified
    ID token instead.
    """

    sessionId: str = Field(default="", max_length=128)
    prompt: str = Field(default="", max_length=100_000)


class DocumentChatRequest(_ChatRequest):
    turnId: str = Field(default="", max_length=128)
    s3Uri: str = Field(default="", max_length=2048)
    modelId: str = Field(default="", max_length=256)


class AgentChatRequest(_ChatRequest):
    method: str = Field(default="chat", max_length=64)
    enableCodeIntelligence: StrictBool = False
    # Default off, like Code Intelligence: External MCP agents send their query
    # to a server outside the deployment.
    enableExternalMcpAgents: StrictBool = False


def _verified_caller(request: Request) -> dict:
    """Claims of the caller's verified Cognito ID token; 401 when there is none.

    The single source of the caller's identity on this endpoint. Raised as a 401
    before any work, so an unauthenticated request never reaches a processor.
    """
    token = request.headers.get(ID_TOKEN_HEADER, "")
    try:
        return verify_id_token(
            token,
            issuer=os.environ.get("COGNITO_ISSUER", ""),
            audience=os.environ.get("USER_POOL_CLIENT_ID", ""),
        )
    except IdTokenRejected as exc:
        logger.warning("Rejecting chat request: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: a valid sign-in token is required",
        ) from exc


def _groups(claims: dict) -> list[str]:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return [str(g) for g in groups]


def _caller_identity(claims: dict) -> dict:
    """The ``identity`` the processors read, built from verified claims.

    Shaped like the ``identity`` the dispatcher path supplies
    (``{"claims": {...}}``) and holding only what the processors read: the groups
    for the agent-chat group check, the ``email`` Chat-with-Document resolves
    ``allowedConfigVersions`` by, and the ``sub`` a session belongs to. The
    processors log their event, so nothing else from the token is forwarded.
    """
    return {
        "claims": {
            "cognito:groups": _groups(claims),
            "email": str(claims.get("email") or ""),
            "sub": str(claims.get("sub") or ""),
        }
    }


def _session_user_id(claims: dict) -> str:
    """The id the caller's ``ChatSessionsTable`` rows are filed under.

    The same value the REST resolvers use (``identity.username``, which the API
    adapter sets to the ``email`` claim, then ``cognito:username``, then ``sub``),
    so a session started on either path appears in one session list.
    """
    return str(
        claims.get("email") or claims.get("cognito:username") or claims.get("sub") or ""
    )


def _claim_session_or_403(session_id: str, claims: dict) -> None:
    """Refuse a session id another user owns, before the stream opens.

    Registers the id to the caller if nobody owns it and it has no stored history
    (``idp_common.chat_session_owner``). Fails closed: an unconfigured owner table
    or a DynamoDB error refuses the turn.
    """
    if not session_id:
        return  # the processors reject a missing sessionId themselves
    owners = os.environ.get("CHAT_SESSION_OWNERS_TABLE", "")
    messages = os.environ.get("CHAT_MESSAGES_TABLE", "")
    sessions = os.environ.get("CHAT_SESSIONS_TABLE", "")
    refusal = HTTPException(
        status_code=403,
        detail="Unauthorized: this chat session belongs to another user",
    )
    if not (owners and messages and sessions):
        logger.error("Chat session owner tables are not configured; refusing")
        raise refusal
    import boto3

    dynamodb = boto3.resource("dynamodb")
    messages_table = dynamodb.Table(messages)
    sessions_table = dynamodb.Table(sessions)
    user_id = _session_user_id(claims)
    try:
        owned = claim_chat_session(
            dynamodb.Table(owners),
            session_id,
            str(claims.get("sub") or ""),
            expires_after=int(time.time()) + _DATA_RETENTION_DAYS * 24 * 60 * 60,
            allow_new=True,
            has_stored_messages=lambda: has_stored_messages(messages_table, session_id),
            holds_earlier_record=lambda: holds_session_row(
                sessions_table, user_id, session_id
            ),
        )
    except Exception as exc:  # noqa: BLE001 - any failure to decide refuses
        logger.error("Could not verify chat session ownership: %s", exc)
        raise refusal from exc
    if not owned:
        logger.warning("Rejecting chat request: the session belongs to another user")
        raise refusal


def _enforce_groups_or_403(identity: dict) -> None:
    """Apply the Agent Chat group gate BEFORE the SSE response is committed.

    Delegates to the processor's own ``_enforce_agent_chat_groups`` so there is
    one policy and one predicate, and translates its ``PermissionError`` into a
    real HTTP 403 — which is only possible here, ahead of
    ``StreamingResponse``. Once the stream is open the status is fixed at 200 and
    a denial could only be rendered as an error frame in the body.
    """
    try:
        agent_proc._enforce_agent_chat_groups({"identity": identity})
    except PermissionError as exc:
        logger.warning("Rejecting agent chat: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _require_a_group_or_403(identity: dict) -> None:
    """Chat-with-Document's floor: the caller must hold at least one group.

    The REST route's dispatcher applies the same ``ANY_GROUP`` floor to
    ``sendChatDocumentMessage``. A signed-in user in no group is what a
    domain-scoped self-signup produces before an administrator assigns a role.
    """
    if not identity["claims"]["cognito:groups"]:
        logger.warning("Rejecting document chat: caller is in no group")
        raise HTTPException(
            status_code=403,
            detail="Unauthorized: chat requires membership of a group",
        )


def _run_in_thread(target, q: "queue.Queue") -> threading.Thread:
    """Run ``target`` in a daemon thread; it pushes events onto ``q``.

    Everything raised inside ``target`` becomes an ``assistant_error`` SSE frame
    on an HTTP 200, because the response status was fixed the moment
    ``StreamingResponse`` was returned. An **authorization** denial must
    therefore never reach here: it has to be raised in the route, before the
    response object is constructed, or it is silently downgraded from a 403 to a
    200 carrying an error message. ``_enforce_groups_or_403`` is what keeps that
    true for /chat/agent.
    """

    def _wrapped() -> None:
        try:
            target()
        except Exception as e:  # noqa: BLE001
            logger.exception("stream producer failed: %s", e)
            q.put(
                sse(
                    {
                        "method": "assistant_error",
                        "status": "ERROR",
                        "content": str(e),
                        "role": "assistant",
                        "isProcessing": False,
                        "timestamp": now_iso(),
                    }
                )
            )
        finally:
            q.put(_DONE)

    t = threading.Thread(target=_wrapped, daemon=True)
    t.start()
    return t


async def _drain(q: "queue.Queue"):
    """Async generator that yields SSE frames from the producer queue."""
    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is _DONE:
            break
        yield item


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/chat/document")
async def chat_document(
    request: Request, body: DocumentChatRequest
) -> StreamingResponse:
    session_id = body.sessionId
    # Every check below reads the verified token, and all of them run before the
    # stream is committed so a refusal is a real 401/403.
    claims = _verified_caller(request)
    identity = _caller_identity(claims)
    _require_a_group_or_403(identity)
    _claim_session_or_403(session_id, claims)

    q: "queue.Queue" = queue.Queue()

    def _sink(
        session_id: str,
        method: str,
        status: str,
        content: str,
        role: str = "assistant",
        model_id: str = "",
        is_processing: bool = True,
    ) -> None:
        q.put(
            sse(
                {
                    "sessionId": session_id,
                    "method": method,
                    "status": status,
                    "content": content,
                    "role": role,
                    "modelId": model_id,
                    "isProcessing": is_processing,
                    "timestamp": now_iso(),
                }
            )
        )

    def _produce() -> None:
        doc_proc.set_sink(_sink)
        try:
            doc_proc.handler(
                {
                    "sessionId": session_id,
                    "turnId": body.turnId,
                    "prompt": body.prompt,
                    "s3Uri": body.s3Uri,
                    "modelId": body.modelId,
                    # Claims from the verified ID token. The processor resolves
                    # the caller's allowedConfigVersions from them.
                    "identity": identity,
                },
                None,
            )
        finally:
            doc_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")


@app.post("/chat/agent")
async def chat_agent(request: Request, body: AgentChatRequest) -> StreamingResponse:
    session_id = body.sessionId
    claims = _verified_caller(request)
    identity = _caller_identity(claims)
    # Group gate, applied here rather than only inside the processor: a 403 is
    # only reachable before StreamingResponse is returned.
    _enforce_groups_or_403(identity)
    _claim_session_or_403(session_id, claims)

    q: "queue.Queue" = queue.Queue()

    def _sink(
        session_id: str,
        content: str,
        method: str,
        is_processing: bool = True,
        tool_metadata=None,
    ) -> None:
        # Mirror the AppSync subscription payload shape (onAgentChatMessageUpdate):
        # the UI reads role/content/method (messageType)/isProcessing/toolMetadata.
        payload = {
            "sessionId": session_id,
            "role": "assistant",
            "content": content,
            "messageType": method,
            "method": method,
            "isProcessing": is_processing,
            "timestamp": now_iso(),
        }
        if tool_metadata:
            payload["toolMetadata"] = tool_metadata
        q.put(sse(payload))

    def _produce() -> None:
        agent_proc.set_sink(_sink)
        try:
            agent_proc.handler(
                {
                    "sessionId": session_id,
                    "prompt": body.prompt,
                    "method": body.method or "chat",
                    # Default off: opt-in only (third-party MCP data flow).
                    # Typed StrictBool on AgentChatRequest, so a non-boolean is
                    # rejected with a 422 before reaching here.
                    "enableCodeIntelligence": body.enableCodeIntelligence,
                    "enableExternalMcpAgents": body.enableExternalMcpAgents,
                    # The id the caller's session list is filed under, from the
                    # verified token. The processor files the turn there.
                    "callerSub": _session_user_id(claims),
                    # Claims from the verified ID token: the groups the caller
                    # was authorized under (checked again by the processor) and
                    # the sub their session and memory belong to.
                    "identity": identity,
                    "timestamp": now_iso(),
                },
                None,
            )
        finally:
            agent_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")
