# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda Function URL streaming endpoint for chat (GovCloud / FedRAMP path).

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
``lambda:InvokeFunctionUrl`` on this function. The caller's Cognito ``sub`` is
extracted from the request context (Function URL forwards the SigV4 identity)
and threaded into the processors for session-ownership + RBAC scope checks,
identical to the AppSync resolver path.
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
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from sse import caller_sub_from_request_context, now_iso, sse

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


def _run_in_thread(target, q: "queue.Queue") -> threading.Thread:
    """Run ``target`` in a daemon thread; it pushes events onto ``q``."""

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
async def chat_document(request: Request) -> StreamingResponse:
    body = await request.json()
    session_id = str(body.get("sessionId") or "")
    caller_sub = _caller_sub(request) or str(body.get("callerSub") or "")

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
                    "turnId": str(body.get("turnId") or ""),
                    "prompt": str(body.get("prompt") or ""),
                    "s3Uri": str(body.get("s3Uri") or ""),
                    "modelId": str(body.get("modelId") or ""),
                    "callerSub": caller_sub,
                },
                None,
            )
        finally:
            doc_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")


@app.post("/chat/agent")
async def chat_agent(request: Request) -> StreamingResponse:
    body = await request.json()
    session_id = str(body.get("sessionId") or "")
    caller_sub = str(body.get("callerSub") or "") or _caller_sub(request)

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
                    "prompt": str(body.get("prompt") or ""),
                    "method": str(body.get("method") or "chat"),
                    # Default off: opt-in only (third-party MCP data flow).
                    # `is True`, not bool(): this body is raw client JSON with no
                    # schema validation on this route, and bool("false") is True.
                    "enableCodeIntelligence": body.get("enableCodeIntelligence")
                    is True,
                    "callerSub": caller_sub,
                    "timestamp": now_iso(),
                },
                None,
            )
        finally:
            agent_proc.set_sink(None)

    _run_in_thread(_produce, q)
    return StreamingResponse(_drain(q), media_type="text/event-stream")
