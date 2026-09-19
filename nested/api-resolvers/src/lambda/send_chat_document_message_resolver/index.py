# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lightweight AppSync mutation handler for Chat-with-Document.

Two call paths hit this resolver:

1. **UI → mutation (method="chat")**: validates inputs, kicks off the long-running
   processor Lambda via async invoke, and returns a ``ChatDocumentMessage`` with
   ``status=QUEUED`` so the user sees an immediate ACK. The AppSync 30-second
   synchronous limit is no longer a problem because the real work runs out of
   band.

2. **Processor Lambda → mutation (method in {"assistant_status",
   "assistant_stream", "assistant_final", "assistant_error"})**: simply passes
   the message through so the AppSync subscription fan-out delivers it to the
   UI. The resolver does not invoke the processor again.

RBAC:
  * All authenticated Cognito users can reach the mutation (schema-level
    ``@aws_cognito_user_pools @aws_iam``).
  * On first user message, the resolver records the caller's Cognito ``sub`` as
    the session owner in DynamoDB. Subsequent calls for the same session from a
    different user will be rejected (prevents session hijacking).
  * The processor enforces ``allowedConfigVersions`` scope before calling
    Bedrock, resolved from the verified claims this resolver forwards as
    ``identity`` — see ``_forwarded_identity`` here and
    ``_allowed_config_versions_for_event`` in ``chat_with_document_processor``.

Mirrors the pattern used by the ``agent_chat_resolver`` Lambda.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_dynamodb = boto3.resource("dynamodb")
_lambda = boto3.client("lambda")

SESSIONS_TABLE = os.environ.get("CHAT_DOCUMENT_SESSIONS_TABLE", "")
PROCESSOR_FUNCTION_ARN = os.environ.get("CHAT_DOCUMENT_PROCESSOR_FUNCTION_ARN", "")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "1"))

# Methods that are published *by the processor*. When we see these, we should
# pass the message through for fan-out to subscribers but NOT invoke the
# processor again.
_PROCESSOR_METHODS = frozenset(
    {
        "assistant_status",
        "assistant_stream",
        "assistant_final",
        "assistant_error",
    }
)


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _caller_sub(event: dict) -> str:
    """Extract the Cognito sub from the AppSync identity payload.

    Returns empty string when invoked via IAM (processor publish) — callers
    should treat that as "system" and skip ownership checks.
    """
    identity = event.get("identity") or {}
    if not isinstance(identity, dict):
        return ""
    # Cognito user pools identity
    sub = identity.get("sub") or ""
    if sub:
        return str(sub)
    # Fallback to username (older integrations)
    return str(identity.get("username") or "")


def _forwarded_identity(event: dict) -> dict | None:
    """Minimal ``identity`` to pass to the processor, or ``None`` if there is none.

    The processor resolves the caller's ``allowedConfigVersions`` itself, so it
    needs the caller's email — the only identifier that joins a Cognito principal
    to a UsersTable row. Forward ONLY that claim: the processor logs its event, so
    the rest of the identity object (tokens, source IP, group list it does not
    read) does not belong in its log group. Mirrors ``_forwarded_identity`` in
    ``agent_chat_resolver``, which forwards only the group claim for the same
    reason.

    The claims here are the ones API Gateway's Cognito authorizer verified —
    ``idp_common.api_adapter.normalize_event`` rebuilds ``identity`` from them and
    refuses an event that asserts a contradicting one.

    Returns ``None`` for an identity-less invocation (a direct
    ``lambda:InvokeFunction``, gated by IAM on the function ARN). That is the
    signal the processor reads to stand its scope check down; it is deliberately
    distinct from omitting the key, which the processor treats as a wiring
    regression and denies.
    """
    identity = event.get("identity")
    if identity is None:
        return None
    claims = identity.get("claims") or {}
    email = claims.get("email") or identity.get("username") or ""
    return {"claims": {"email": str(email)}}


def _check_session_ownership(session_id: str, caller_sub: str) -> None:
    """Enforce that ``caller_sub`` owns ``session_id`` (or is first user)."""
    if not SESSIONS_TABLE or not caller_sub:
        # No ownership table configured or system call — skip.
        return
    table = _dynamodb.Table(SESSIONS_TABLE)
    try:
        resp = table.get_item(Key={"sessionId": session_id})
    except ClientError as e:
        logger.warning(
            "Could not verify session ownership (failing open): %s", e
        )
        return
    existing = resp.get("Item")
    if existing is None:
        # First message for this session — claim ownership.
        ttl = int(time.time()) + DATA_RETENTION_DAYS * 24 * 60 * 60
        table.put_item(
            Item={
                "sessionId": session_id,
                "ownerSub": caller_sub,
                "createdAt": _now_iso(),
                "ExpiresAfter": ttl,
            }
        )
        return
    if existing.get("ownerSub") and existing["ownerSub"] != caller_sub:
        raise Exception(
            "Unauthorized: this chat session belongs to another user"
        )


def _build_response(
    session_id: str,
    role: str,
    content: str,
    method: str,
    status: str,
    model_id: str | None = None,
    is_processing: bool = True,
    timestamp: str | None = None,
) -> dict:
    return {
        "sessionId": session_id,
        "role": role,
        "content": content,
        "timestamp": timestamp or _now_iso(),
        "method": method,
        "status": status,
        "modelId": model_id or "",
        "isProcessing": is_processing,
    }


def handler(event, _context):  # noqa: ANN001
    """AppSync sendChatDocumentMessage resolver."""
    args = event.get("arguments", {}) or {}
    session_id = str(args.get("sessionId") or "").strip()
    if not session_id:
        raise Exception("sessionId is required")

    method = str(args.get("method") or "chat").strip() or "chat"

    # --- Processor-originated publish: just pass through -----------------
    if method in _PROCESSOR_METHODS:
        return _build_response(
            session_id=session_id,
            role=str(args.get("role") or "assistant"),
            content=str(args.get("content") or ""),
            method=method,
            status=str(args.get("status") or ""),
            model_id=args.get("modelId"),
            is_processing=bool(args.get("isProcessing"))
            if args.get("isProcessing") is not None
            else (method not in ("assistant_final", "assistant_error")),
            timestamp=args.get("timestamp"),
        )

    # --- User-initiated request (method="chat") --------------------------
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise Exception("prompt is required for method='chat'")
    if len(prompt) > 100_000:
        raise Exception("prompt exceeds 100,000 character limit")

    s3_uri = str(args.get("s3Uri") or "").strip()
    if not s3_uri:
        raise Exception("s3Uri is required for method='chat'")

    ui_model_id = str(args.get("modelId") or "").strip()

    # RBAC: ensure only the session creator may continue the session.
    caller = _caller_sub(event)
    _check_session_ownership(session_id, caller)

    # Fire-and-forget invoke of the processor.
    if not PROCESSOR_FUNCTION_ARN:
        raise Exception(
            "CHAT_DOCUMENT_PROCESSOR_FUNCTION_ARN not configured on Lambda"
        )
    turn_id = str(uuid.uuid4())
    try:
        _lambda.invoke(
            FunctionName=PROCESSOR_FUNCTION_ARN,
            InvocationType="Event",
            Payload=json.dumps(
                {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "prompt": prompt,
                    "s3Uri": s3_uri,
                    "modelId": ui_model_id,
                    # The claims the caller was authenticated with, so the
                    # processor resolves their config-version scope from an
                    # identity a transport verified rather than from anything the
                    # request body asserted. The key must be present even when it
                    # is null: the processor denies a turn whose event carries no
                    # `identity` key at all, so a wiring regression here fails
                    # closed instead of quietly disabling the check.
                    "identity": _forwarded_identity(event),
                }
            ).encode("utf-8"),
        )
        logger.info(
            "Dispatched chat-doc processor for sessionId=%s turnId=%s model=%s",
            session_id,
            turn_id,
            ui_model_id or "(from config)",
        )
    except ClientError as e:
        logger.error("Failed to invoke chat-doc processor: %s", e)
        raise Exception(f"Failed to start chat: {e}") from e

    # Return an immediate ACK so the UI can render the user's bubble and
    # start listening on the subscription. Processor will publish
    # subsequent status / streaming / final events.
    return _build_response(
        session_id=session_id,
        role="user",
        content=prompt,
        method="chat",
        status="QUEUED",
        model_id=ui_model_id,
        is_processing=True,
    )
