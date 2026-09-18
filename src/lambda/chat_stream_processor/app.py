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
SigV4 using the authenticated Cognito Identity Pool role. The role is granted
``lambda:InvokeFunctionUrl`` on this function. The SigV4 principal is read from
the request context and threaded into the processors as ``callerSub`` for
session-ownership + RBAC scope checks.

What this transport does and does not give us:

* It **authenticates**. Only a principal granted both halves of the
  ``AuthType=AWS_IAM`` check can reach any route: the resource permission on the
  function (``ChatStreamProcessorUrlPermission``, which uses
  ``lambda:InvokeFunctionUrl``) and an identity policy on the caller's role
  (``CognitoAuthorizedRole``'s ``ChatStreamInvoke``, which grants
  ``lambda:InvokeFunction`` **and** ``lambda:InvokeFunctionUrl``).
  Counter-intuitively it is ``lambda:InvokeFunction`` that actually gates the
  identity side — granting only ``lambda:InvokeFunctionUrl`` there returns 403
  AccessDeniedException at invoke time. That is measured, not inferred; see the
  note on the ``ChatStreamInvoke`` policy in ``template.yaml``.
* It does **not** carry Cognito group claims.
  ``requestContext.authorizer.iam.cognitoIdentity`` is documented as unused by
  Function URLs (always ``null`` or absent), and an assumed-role ARN has no
  group claim, so there is no verified ``cognito:groups`` to enforce against
  here. Group enforcement on the equivalent operation therefore lives on the
  dispatcher path, and the residual difference is tracked as GAP-07 in
  ``scripts/api_rbac_expectations.yaml``. ``_caller_identity`` is where a
  verified claims source plugs in; ``_enforce_groups_or_403`` is what turns the
  claims it returns into a denial (see both docstrings).
* A body-supplied ``callerSub`` is a fallback only, never an override — see
  ``_resolve_caller_sub``. On this deployment the transport principal is NOT
  per-user (measured — see ``_GENERIC_IDENTITY_POOL_SESSION_NAMES`` in
  ``sse.py``), so the body value is in fact what attribution is keyed to today.
  Both routes use the one helper, so they cannot drift apart.

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
from pydantic import BaseModel, Field, StrictBool
from sse import (
    CallerIdentityConflict,
    caller_sub_from_request_context,
    now_iso,
    resolve_caller_sub,
    sse,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI()

# Sentinel pushed onto the queue to signal the producer thread is done.
_DONE = object()


def _caller_sub(request: Request) -> str:
    """Extract the Cognito sub from the Function URL request context header."""
    return caller_sub_from_request_context(
        request.headers.get("x-amzn-request-context")
    )


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
    # Client-asserted caller identity. Only honoured when the transport does not
    # supply a per-user identity of its own, and never when it contradicts one —
    # see _resolve_caller_sub.
    callerSub: str = Field(default="", max_length=256)


class DocumentChatRequest(_ChatRequest):
    turnId: str = Field(default="", max_length=128)
    s3Uri: str = Field(default="", max_length=2048)
    modelId: str = Field(default="", max_length=256)


class AgentChatRequest(_ChatRequest):
    method: str = Field(default="chat", max_length=64)
    enableCodeIntelligence: StrictBool = False


def _caller_identity() -> dict | None:
    """Verified Cognito claims for the caller, or ``None`` if none are available.

    Shaped like the ``identity`` object the resolvers receive
    (``{"claims": {"cognito:groups": [...]}}``) because the processors' group gate
    reads that shape, and it is what the dispatcher path already supplies.

    On a Lambda Function URL there is nothing to build it from. The only identity
    the transport forwards is the SigV4 principal —
    ``requestContext.authorizer.iam.cognitoIdentity`` is documented as "Function
    URLs don't use this parameter. Lambda sets this to null or excludes this from
    the JSON" — and an assumed-role ARN carries no Cognito group claim. So this
    returns ``None``, and the processors' group gate stands down for this
    transport exactly as it does for backend (IAM) invocations.

    Supplying real claims here needs the browser to present its Cognito ID token
    to this endpoint in addition to signing the request, which is a change to the
    transport's auth contract (UI + template + verification) and is tracked as
    GAP-07 in scripts/api_rbac_expectations.yaml.

    This is where the claims are OBTAINED, but it is not the only place that
    change touches. The processor's own ``PermissionError`` cannot produce a 403
    on this transport: ``StreamingResponse`` has already committed HTTP 200 by
    the time ``_produce`` runs, and ``_run_in_thread`` converts anything the
    producer raises into an ``assistant_error`` SSE frame on that 200 — the very
    error-to-stream conversion the processor's gate was placed outside its own
    ``try`` to avoid. So the denial is applied a second time, synchronously, in
    ``_enforce_groups_or_403`` below, which is called from the route before the
    response is returned.
    """
    return None


def _enforce_groups_or_403(identity: dict | None) -> None:
    """Apply the Agent Chat group gate BEFORE the SSE response is committed.

    Delegates to the processor's own ``_enforce_agent_chat_groups`` so there is
    one policy and one predicate, and translates its ``PermissionError`` into a
    real HTTP 403 — which is only possible here, ahead of
    ``StreamingResponse``. Once the stream is open the status is fixed at 200 and
    a denial could only be rendered as an error frame in the body.

    A no-op while ``_caller_identity`` returns ``None`` (GAP-07): an
    identity-less invocation is IAM-gated, exactly as a backend
    ``lambda:InvokeFunction`` is. It exists so that closing GAP-07 needs no
    second change on this path.
    """
    try:
        agent_proc._enforce_agent_chat_groups({"identity": identity})
    except PermissionError as exc:
        logger.warning("Rejecting agent chat: %s", exc)
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _resolve_caller_sub(request: Request, claimed: str) -> str:
    """Caller identity for this request; 403 if the body contradicts the token.

    Thin HTTP adapter over ``sse.resolve_caller_sub`` (which holds the decision
    and its rationale). Both routes go through this one helper so their identity
    precedence cannot drift apart again.
    """
    try:
        return resolve_caller_sub(_caller_sub(request), claimed)
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
    caller_sub = _resolve_caller_sub(request, body.callerSub)

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
                    "callerSub": caller_sub,
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
    # Same precedence as /chat/document: the verified identity wins and a
    # contradicting body value is refused. This route previously preferred the
    # body value, which let the persisted attribution of an agent chat session be
    # chosen by the client.
    caller_sub = _resolve_caller_sub(request, body.callerSub)
    # Group gate, applied here rather than only inside the processor: a 403 is
    # only reachable before StreamingResponse is returned. No-op while
    # _caller_identity() is None (GAP-07).
    identity = _caller_identity()
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
                    # Group membership the caller was authorized under, already
                    # checked by _enforce_groups_or_403 above. The Function URL
                    # transport carries no Cognito group claim (see the "Auth
                    # model" note in the module docstring), so this is None today
                    # and the processor's group gate stands down for this
                    # transport; the dispatcher path supplies real claims.
                    "identity": identity,
                    "timestamp": now_iso(),
                },
                None,
            )
        finally:
            agent_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")
