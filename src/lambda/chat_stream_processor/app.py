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
    ``{sessionId, prompt, method?, enableCodeIntelligence?}``.

Each emitted event is written as one SSE frame::

    data: {"method": "...", "status": "...", "content": "...", ...}\n\n

The shapes mirror exactly what the AppSync subscriptions delivered, so the UI
state machines (``handleUpdate`` / ``handleStreamingMessage`` / ``addMessage``)
are reused unchanged — only the source of the events differs.

Auth model
----------
The Function URL is ``AuthType=AWS_IAM``; the browser signs the request with
SigV4 using the authenticated Cognito Identity Pool role, which is granted
``lambda:InvokeFunctionUrl`` and ``lambda:InvokeFunction`` on this function
(``CognitoAuthorizedRole``'s ``ChatStreamInvoke``, with
``ChatStreamProcessorUrlPermission`` on the function). That proves the caller is
a signed-in user of this deployment. The SigV4 principal is a pool-wide constant,
so it says nothing about which user, and Function URLs forward no Cognito claims.

So the browser also sends its User Pool ID token in the ``x-idp-id-token``
header, inside the signed request, and ``_caller_identity`` verifies it
(``id_token.py``) before either route opens its stream. A request without a valid
token is refused with a 403. The verified claims are what every decision here is
keyed to:

* the agent-chat group gate (``_enforce_groups_or_403``, the processor's own
  predicate) and the any-group floor on Chat-with-Document
  (``_enforce_any_group_or_403``), the same floors the REST operations carry;
* Chat-with-Document's ``allowedConfigVersions`` scope check and tenant
  isolation, which the processors apply to the ``identity`` they are given;
* the key a turn's chat history is filed under, derived from the claims the way
  the REST path derives it. A body-supplied ``callerSub`` that differs is
  refused (``_resolve_caller_sub``).

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

# agent_chat_processor / chat_with_document_processor are the *exact* processor
# sources, copied into this package at build time by the Makefile
# (BuildMethod: makefile). They are the same files deployed as
# ChatWithDocumentProcessorFunction / AgentChatProcessorFunction, so there is a
# single source of truth for the Bedrock/agent orchestration logic.
import agent_chat_processor as agent_proc  # type: ignore
import chat_with_document_processor as doc_proc  # type: ignore
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from id_token import ID_TOKEN_HEADER, InvalidIdToken, verified_identity
from pydantic import BaseModel, Field, StrictBool
from sse import (
    CallerIdentityConflict,
    history_key,
    now_iso,
    resolve_caller_sub,
    sse,
)

from idp_common.config_scope import unscoped_is_denied

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI()

# Sentinel pushed onto the queue to signal the producer thread is done.
_DONE = object()

#: The groups Chat-with-Document admits: every group a user signs in with, the
#: ``ANY_GROUP`` floor its REST operation carries in the dispatcher's manifest.
_ANY_GROUP = ("Admin", "Annotator", "Author", "Reviewer", "Viewer")


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
    """Fields common to both chat routes."""

    sessionId: str = Field(default="", max_length=128)
    prompt: str = Field(default="", max_length=100_000)
    # Client-asserted caller identity. Never honoured over the verified ID token,
    # and refused when it contradicts it — see _resolve_caller_sub.
    callerSub: str = Field(default="", max_length=256)


class DocumentChatRequest(_ChatRequest):
    turnId: str = Field(default="", max_length=128)
    s3Uri: str = Field(default="", max_length=2048)
    modelId: str = Field(default="", max_length=256)


class AgentChatRequest(_ChatRequest):
    method: str = Field(default="chat", max_length=64)
    enableCodeIntelligence: StrictBool = False


def _caller_identity(request: Request) -> dict:
    """Verified Cognito claims for the caller; a 403 when there are none.

    Shaped like the ``identity`` object the resolvers receive
    (``{"claims": {"cognito:groups": [...], "email": ..., "sub": ...}}``) because
    the processors read that shape. ``cognito:groups`` is what the group gates
    read, and ``email`` and ``sub`` are what Chat-with-Document's
    ``allowedConfigVersions`` lookup resolves the caller by.

    Called at the top of each route, before ``StreamingResponse``: once the
    response exists its status is fixed at 200 and a refusal could only be an
    error frame in the body.
    """
    try:
        return verified_identity(request.headers.get(ID_TOKEN_HEADER, ""))
    except InvalidIdToken as exc:
        logger.warning("Rejecting streamed chat: %s", exc)
        raise HTTPException(
            status_code=403,
            detail="Unauthorized: a valid Cognito ID token is required",
        ) from exc


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


def _enforce_any_group_or_403(identity: dict) -> None:
    """Chat-with-Document's floor: the caller must hold a group.

    The REST operation's ``ANY_GROUP`` floor is applied by the dispatcher, which
    this transport does not go through, so it is applied here, before the stream
    opens. A user with no group, which a domain-scoped self-signup produces, is
    refused on both routes alike.
    """
    groups = identity["claims"].get("cognito:groups") or []
    if not set(groups).intersection(_ANY_GROUP):
        logger.warning("Rejecting document chat: caller is in no group")
        raise HTTPException(
            status_code=403, detail="Unauthorized: your account has no group"
        )


def _refuse_unverified_when_unscoped_is_denied_or_403(identity: dict | None) -> None:
    """Tenant isolation: refuse a turn with no verified identity.

    Applies under ``TenantIsolationMode=Strict`` or an external
    ``TenantGrantSource`` (``IdpClaim`` or ``Api``). ``_caller_identity`` already
    refuses a request without a valid ID token, so on these routes ``identity``
    is never ``None``; the check stays as defence in depth for the one condition
    tenant isolation is defined by, serving a turn unattributed. Both processors
    refuse too.
    """
    if identity is None and unscoped_is_denied():
        logger.warning(
            "Rejecting streamed chat: tenant isolation requires a verified "
            "caller identity"
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "Unauthorized: this deployment runs with tenant isolation, "
                "which requires a verified caller identity"
            ),
        )


def _resolve_caller_sub(identity: dict, claimed: str) -> str:
    """Caller identity for this request; 403 if the body contradicts the token.

    Thin HTTP adapter over ``sse.resolve_caller_sub`` (which holds the decision
    and its rationale). Both routes go through this one helper so their identity
    precedence cannot drift apart again.
    """
    try:
        return resolve_caller_sub(history_key(identity), claimed)
    except CallerIdentityConflict as exc:
        logger.warning("Rejecting chat request: %s", exc)
        raise HTTPException(
            status_code=403,
            detail="Unauthorized: caller identity mismatch",
        ) from exc


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
    identity = _caller_identity(request)
    # Called for its refusal, not its result: it 403s a body-supplied identity
    # that contradicts the verified one, before the stream is committed. The
    # processor resolves the caller from `identity` below rather than from a field
    # a client can choose.
    _resolve_caller_sub(identity, body.callerSub)
    _refuse_unverified_when_unscoped_is_denied_or_403(identity)
    _enforce_any_group_or_403(identity)

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
                    # The verified claims. The processor resolves the caller's
                    # allowedConfigVersions from them, and treats an absent
                    # `identity` key as a wiring regression and denies the turn.
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
    identity = _caller_identity(request)
    # Same precedence as /chat/document: the verified identity wins and a
    # contradicting body value is refused, so the persisted attribution of an
    # agent chat session cannot be chosen by the client.
    caller_sub = _resolve_caller_sub(identity, body.callerSub)
    # Group gate, applied here rather than only inside the processor: a 403 is
    # only reachable before StreamingResponse is returned.
    _refuse_unverified_when_unscoped_is_denied_or_403(identity)
    _enforce_groups_or_403(identity)

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
                    "callerSub": caller_sub,
                    # The verified claims the caller was authorized under,
                    # already checked by _enforce_groups_or_403 above; the
                    # processor applies its own gate to them again.
                    "identity": identity,
                    "timestamp": now_iso(),
                },
                None,
            )
        finally:
            agent_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")
