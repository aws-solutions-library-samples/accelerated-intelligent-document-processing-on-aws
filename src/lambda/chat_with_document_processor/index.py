# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Long-running Chat-with-Document processor.

Invoked asynchronously by ``send_chat_document_message_resolver``. Does the
heavy lifting that cannot fit inside AppSync's 30-second synchronous resolver
budget:

  1. Looks up the document in the TrackingTable (to get ``ConfigVersion``).
  2. Enforces RBAC ``allowedConfigVersions`` scope for non-admin callers —
     same pattern as ``reprocess_document_resolver`` in the AppSync stack.
  3. Loads the ``chat`` section from the document's config version via
     ``idp_common.config.get_config``.
  4. Fetches the full document text from S3 (cached per-document under
     ``<objectKey>/summary/fulltext.txt``).
  5. Invokes Bedrock via the Converse Stream API and publishes incremental
     token deltas (``assistant_stream``) to the AppSync subscription with
     light throttling (~200 ms / 200-char batches) so the UI can render the
     answer as it's generated.
  6. Publishes status updates (``LOADING_DOCUMENT``, ``CALLING_MODEL``,
     ``STREAMING``) and a final ``assistant_final`` event with the full
     accumulated text.

Errors are caught and published as ``assistant_error`` so the UI can render
them inline instead of leaving the user with a spinning wheel.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

from idp_common.bedrock.client import (
    CLAUDE_EFFORT_LEVELS,
    GROK_EFFORT_LEVELS,
    is_claude_effort_model,
    is_grok_model,
    strips_sampling_params,
)
from idp_common.bedrock.model_utils import parse_model_id
from idp_common.bedrock.openai_responses import is_openai_responses_model
from idp_common.config import get_config
from idp_common.config_scope import scope_allows

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")
_bedrock_runtime = None  # Lazily created in the target region


DEFAULT_SYSTEM_PROMPT = (
    "You are an assistant that answers questions about the attached document "
    "text. If you don't know the answer, say so. Do not invent information. "
    "Use the prior chat history provided as context. Respond in plain text, "
    "not JSON."
)

# Streaming throttle: publish a delta at most every ``STREAM_FLUSH_INTERVAL_S``
# seconds OR once the pending buffer crosses ``STREAM_FLUSH_CHAR_THRESHOLD``
# characters — whichever happens first. Keeps AppSync mutation rate well under
# throttling limits without making the UI feel choppy.
STREAM_FLUSH_INTERVAL_S = 0.2
STREAM_FLUSH_CHAR_THRESHOLD = 200


# --- Emission sink -------------------------------------------------------
#
# All status / delta / final / error events flow through ``_emit`` rather than
# being hard-wired to any transport. The chat always runs behind the streaming
# Lambda Function URL endpoint (``chat_stream_processor``), which injects a sink
# via ``set_sink`` that writes SSE ``data: {json}`` chunks to the HTTP response
# stream. If no sink is installed, events are dropped with a warning — there is
# no AppSync fallback.
#
# A sink is any callable with the ``_emit`` keyword signature below.
_active_sink = None  # type: ignore[var-annotated]


def set_sink(sink) -> None:  # noqa: ANN001
    """Install the emission sink (used by the streaming endpoint).

    Pass ``None`` to clear the installed sink.
    """
    global _active_sink
    _active_sink = sink


# --- Non-streaming persistence (GovCloud poll path) ----------------------
#
# In commercial regions the browser streams the answer live over the Lambda
# Function URL, and this processor never needs to persist chat messages. In
# GovCloud (no Lambda Function URLs) the UI instead POLLS getChatMessages, so
# the final assistant message must be written to the ChatMessages table.
#
# IMPORTANT: this is gated on there being NO active emission sink. The streaming
# endpoint (chat_stream_processor) installs a sink via set_sink() before calling
# the handler; the async/poll path (send_chat_document_message_resolver -> Event
# invoke) does NOT. So persistence runs ONLY on the non-streaming path — commercial
# streaming deployments keep their prior behavior (no server-side doc-chat message
# storage). We gate on the sink rather than CHAT_MESSAGES_TABLE because BOTH the
# streaming and standalone functions have that env var set, so it alone would not
# distinguish the two paths.
_CHAT_MESSAGES_TABLE = os.environ.get("CHAT_MESSAGES_TABLE") or ""
_DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))


def _persist_chat_messages(session_id: str, prompt: str, assistant_text: str) -> None:
    """Write the user prompt + final assistant reply to the ChatMessages table.

    Keyed by ``PK=sessionId`` / ``SK=<iso timestamp>`` — the same shape
    getChatMessages queries and returns in chronological order. Best-effort:
    never raises (a persistence failure must not fail the chat turn).

    No-op when an emission sink is active (i.e. running behind the streaming
    endpoint), so only the non-streaming/poll path persists.
    """
    if _active_sink is not None:
        return
    if not _CHAT_MESSAGES_TABLE or not session_id:
        return
    try:
        table = _dynamodb.Table(_CHAT_MESSAGES_TABLE)
        expires_after = int(time.time()) + (_DATA_RETENTION_DAYS * 24 * 60 * 60)
        user_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        table.put_item(
            Item={
                "PK": session_id,
                "SK": user_ts,
                "role": "user",
                "content": prompt,
                "timestamp": user_ts,
                "isProcessing": False,
                "ExpiresAfter": expires_after,
            }
        )
        if assistant_text:
            assistant_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            table.put_item(
                Item={
                    "PK": session_id,
                    "SK": assistant_ts,
                    "role": "assistant",
                    "content": assistant_text,
                    "timestamp": assistant_ts,
                    "isProcessing": False,
                    "ExpiresAfter": expires_after,
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to persist chat-doc messages for %s: %s", session_id, e)


def _emit(
    session_id: str,
    method: str,
    status: str,
    content: str,
    role: str = "assistant",
    model_id: str = "",
    is_processing: bool = True,
) -> None:
    """Route an event to the active sink.

    The streaming endpoint installs the sink via ``set_sink``. If no sink is
    installed there is nowhere to deliver the event, so it is dropped with a
    warning rather than failing the chat work.
    """
    if _active_sink is None:
        logger.warning(
            "No emission sink installed; dropping chat event "
            "(method=%s status=%s)",
            method,
            status,
        )
        return
    _active_sink(
        session_id=session_id,
        method=method,
        status=status,
        content=content,
        role=role,
        model_id=model_id,
        is_processing=is_processing,
    )


def _get_bedrock_runtime():
    global _bedrock_runtime
    if _bedrock_runtime is None:
        # Honor BEDROCK_ASSUME_ROLE_ARN for cross-account hub-account routing.
        # See lib/idp_common_pkg/idp_common/bedrock/session.py.
        from idp_common.bedrock.session import get_bedrock_session

        region = os.environ.get("AWS_REGION")
        _bedrock_runtime = get_bedrock_session(region).client(
            "bedrock-runtime",
            region_name=region,
        )
    return _bedrock_runtime


def _default_model_for_region() -> str:
    region = os.environ.get("AWS_REGION", "us-east-1")
    if region.startswith("eu-"):
        return "eu.anthropic.claude-opus-4-8:1m"
    return "us.anthropic.claude-opus-4-8:1m"


def _s3_object_exists(bucket: str, key: str) -> bool:
    try:
        _s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def _get_document_record(object_key: str) -> dict:
    tracking_table = _dynamodb.Table(os.environ["TRACKING_TABLE_NAME"])
    resp = tracking_table.get_item(Key={"PK": f"doc#{object_key}", "SK": "none"})
    return resp.get("Item") or {}


def _get_user_allowed_config_versions(caller_sub: str) -> list[str] | None:
    """Fetch caller's ``allowedConfigVersions`` for scope enforcement.

    Returns:
      - ``None`` when the user has no scope restriction (admin or unrestricted)
      - A list of version names the user is allowed to read
    """
    users_table_name = os.environ.get("USERS_TABLE_NAME") or ""
    if not users_table_name or not caller_sub:
        return None
    try:
        table = _dynamodb.Table(users_table_name)
        resp = table.query(
            IndexName="SubIndex",
            KeyConditionExpression=boto3.dynamodb.conditions.Key("sub").eq(caller_sub),
            Limit=1,
        )
        items = resp.get("Items") or []
        if not items:
            return None
        scope = items[0].get("allowedConfigVersions")
        return list(scope) if scope else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not fetch user scope (failing open): %s", e)
        return None


def _get_full_text(bucket: str, object_key: str, document: dict) -> str:
    pages = document.get("Pages") or []
    sorted_pages = sorted(pages, key=lambda p: p.get("Id", 0))
    parts: list[str] = []
    for page in sorted_pages:
        text_uri = page.get("TextUri")
        if not text_uri:
            continue
        text_key = text_uri.replace(f"s3://{bucket}/", "")
        try:
            resp = _s3.get_object(Bucket=bucket, Key=text_key)
            parts.append(
                f"<page-number>{page.get('Id')}</page-number>\n"
                + resp["Body"].read().decode("utf-8")
                + "\n"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to load page %s: %s", page.get("Id"), e)
    return "\n".join(parts)


def _resolve_chat_settings(config_version: str | None):
    """Load ``chat.*`` settings from the document's config version.

    Falls back to ``summarization.*`` for configs created before the chat
    section existed. See ``ChatConfig`` in ``idp_common.config.models`` for
    defaults.
    """
    config_table = os.environ["CONFIGURATION_TABLE_NAME"]
    try:
        config = get_config(
            table_name=config_table,
            as_model=False,
            version=config_version,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Failed to load config version=%s via idp_common: %s", config_version, e
        )
        config = {}

    chat_cfg = (config or {}).get("chat") or {}
    summ_cfg = (config or {}).get("summarization") or {}

    model_id = (
        chat_cfg.get("model") or summ_cfg.get("model") or _default_model_for_region()
    )
    system_prompt = (
        chat_cfg.get("system_prompt")
        or summ_cfg.get("system_prompt")
        or DEFAULT_SYSTEM_PROMPT
    )

    def _to_float(v, default: float) -> float:
        try:
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _to_int(v, default: int) -> int:
        try:
            return int(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    # max_tokens is an optional cap. When unset (empty/None) resolve the
    # selected model's maximum output limit — the streaming path below needs a
    # concrete int for the Converse ``inferenceConfig.maxTokens`` field (there
    # is no "use model max" sentinel in the API). Fall back to 4096 only if the
    # model is unknown to the limits table.
    def _resolve_max_tokens() -> int:
        raw = chat_cfg.get("max_tokens")
        if raw not in (None, ""):
            return _to_int(raw, 4096)
        try:
            from idp_common.bedrock.model_utils import get_model_max_output_tokens

            return get_model_max_output_tokens(model_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Could not resolve model max output tokens for %s: %s; using 4096",
                model_id,
                e,
            )
            return 4096

    return {
        "model_id": model_id,
        "system_prompt": system_prompt,
        "temperature": _to_float(chat_cfg.get("temperature"), 0.0),
        "max_tokens": _resolve_max_tokens(),
        "reasoning_effort": chat_cfg.get("reasoning_effort") or "medium",
    }


def _invoke_openai_responses_and_emit(
    session_id: str,
    selected_model_id: str,
    system_prompt: str,
    user_text: str,
    max_tokens: int,
    reasoning_effort: str | None,
) -> str:
    """Stream an OpenAI GPT-5.x response via the bedrock-mantle Responses API.

    GPT-5.x models are served on the OpenAI Responses API (not the Converse
    Stream API used for other models). This consumes the SSE delta generator
    from ``idp_common`` and publishes ``assistant_stream`` deltas with the same
    throttling as the Converse path, so the UI renders tokens as they arrive.

    Returns the full accumulated assistant text.
    """
    from idp_common.bedrock import stream_responses_api
    from idp_common.bedrock.client import default_client

    buffer: list[str] = []
    buffer_len = 0
    last_flush_ts = time.time()
    full_text_parts: list[str] = []
    first_delta_seen = False

    def _flush(force: bool = False) -> None:
        nonlocal buffer, buffer_len, last_flush_ts
        if not buffer:
            return
        now = time.time()
        if not (
            force
            or buffer_len >= STREAM_FLUSH_CHAR_THRESHOLD
            or (now - last_flush_ts) >= STREAM_FLUSH_INTERVAL_S
        ):
            return
        delta = "".join(buffer)
        buffer = []
        buffer_len = 0
        last_flush_ts = now
        _emit(
            session_id=session_id,
            method="assistant_stream",
            status="STREAMING",
            content=delta,
            model_id=selected_model_id,
            is_processing=True,
        )

    try:
        for item in stream_responses_api(
            client=default_client,
            model_id=selected_model_id,
            system_prompt=system_prompt,
            content=[{"text": user_text}],
            max_tokens=max_tokens,
            context="ChatWithDocument",
            reasoning_effort=reasoning_effort,
        ):
            # The generator yields text deltas (str), then a final metering dict.
            if not isinstance(item, str):
                continue
            if not first_delta_seen:
                first_delta_seen = True
                _emit(
                    session_id=session_id,
                    method="assistant_status",
                    status="STREAMING",
                    content="Streaming response…",
                    model_id=selected_model_id,
                )
            full_text_parts.append(item)
            buffer.append(item)
            buffer_len += len(item)
            _flush()
    finally:
        _flush(force=True)

    return "".join(full_text_parts)


def _invoke_bedrock_stream_and_emit(
    session_id: str,
    selected_model_id: str,
    system_prompt: str,
    user_text: str,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> str:
    """Stream a Bedrock Converse response, publishing deltas + a final event.

    Mirrors the suffix-handling and parameter-exclusion behavior of
    ``idp_common.bedrock.client.BedrockClient`` — which currently only exposes
    the non-streaming Converse API. Specifically:

      * ``:1m`` suffix  → stripped from modelId, ``anthropic_beta`` opt-in flag
        added to ``additionalModelRequestFields``.
      * ``:priority`` / ``:flex`` suffix → stripped from modelId, passed as the
        Converse ``serviceTier={"type": ...}`` parameter. (Note: this is the
        Bedrock *service tier* parameter, not ``performanceConfig.latency`` —
        those are separate: ``performanceConfig.latency`` only accepts
        ``optimized``/``standard``; ``serviceTier.type`` accepts
        ``priority``/``flex``/``default``.)
      * Claude 4.7+ and xAI Grok → ``temperature`` / ``top_p`` are skipped
        (Bedrock rejects them for these models — deprecated on Claude, a hard
        400 naming the field on Grok).
      * Reasoning effort → routed to the carrier the model actually reads:
        ``output_config.effort`` for effort-capable Claude, ``reasoning.effort``
        for Grok. Values outside a model's vocabulary are dropped rather than
        forwarded, because Bedrock silently ignores unrecognized
        ``additionalModelRequestFields`` keys.

    When idp_common grows a streaming helper, this function should delegate
    to it.

    Returns the full accumulated assistant text.
    """
    client = _get_bedrock_runtime()

    # Claude 4.7+ and Grok reject temperature/top_p; everything else gets
    # temperature. Grok returns a 400 naming the field, so this is not optional:
    # chat.temperature always resolves to a float (never None), which means
    # every Grok chat turn would fail without this gate.
    inference_config: dict = {"maxTokens": max_tokens}
    if not strips_sampling_params(selected_model_id) and temperature is not None:
        inference_config["temperature"] = temperature

    # Start with the user-supplied model ID, then peel off the service-tier
    # suffix first (``:priority`` / ``:flex``) and the ``:1m`` context suffix
    # second. parse_model_id only recognizes tier suffixes at the end of the
    # ID and leaves ``:1m`` alone.
    use_model_id, tier_from_suffix = parse_model_id(selected_model_id)

    additional_model_fields: dict | None = None
    if use_model_id.endswith(":1m"):
        use_model_id = use_model_id[:-3]
        additional_model_fields = {"anthropic_beta": ["context-1m-2025-08-07"]}

    # Reasoning effort. `chat.reasoning_effort` was previously resolved from
    # config and then dropped on this path (it only reached the OpenAI Responses
    # branch), so the documented knob did nothing for Converse models. The two
    # families use different carriers and different vocabularies, and Bedrock
    # ignores unknown additionalModelRequestFields keys silently — so an
    # out-of-vocabulary value must be dropped, not passed through.
    if reasoning_effort:
        effort = str(reasoning_effort).lower().strip()
        effort_field: tuple[str, dict] | None = None
        if is_claude_effort_model(use_model_id) and effort in CLAUDE_EFFORT_LEVELS:
            effort_field = ("output_config", {"effort": effort})
        elif is_grok_model(use_model_id) and effort in GROK_EFFORT_LEVELS:
            effort_field = ("reasoning", {"effort": effort})
        if effort_field:
            if additional_model_fields is None:
                additional_model_fields = {}
            additional_model_fields[effort_field[0]] = effort_field[1]
            logger.info(
                "Chat using reasoning effort '%s' for %s", effort, selected_model_id
            )

    # Normalize service tier for the Converse ``serviceTier`` parameter.
    # Only ``priority`` and ``flex`` are valid to send explicitly; ``standard``
    # is the implicit default and should be omitted. Same normalization as
    # idp_common.bedrock.client.BedrockClient (see lines 703-732 of client.py).
    service_tier_param: dict | None = None
    if tier_from_suffix in ("priority", "flex"):
        service_tier_param = {"type": tier_from_suffix}
        logger.info(
            "Extracted service tier '%s' from model ID; base=%s",
            tier_from_suffix,
            use_model_id,
        )

    messages = [
        {
            "role": "user",
            "content": [{"text": user_text}],
        }
    ]
    system = [{"text": system_prompt}] if system_prompt else None

    logger.info(
        "Calling bedrock:converse_stream model=%s max_tokens=%d tier=%s (raw=%s)",
        use_model_id,
        max_tokens,
        tier_from_suffix or "default",
        selected_model_id,
    )

    kwargs: dict = {
        "modelId": use_model_id,
        "messages": messages,
        "inferenceConfig": inference_config,
    }
    if system:
        kwargs["system"] = system
    if additional_model_fields:
        kwargs["additionalModelRequestFields"] = additional_model_fields
    if service_tier_param:
        kwargs["serviceTier"] = service_tier_param

    response = client.converse_stream(**kwargs)
    stream = response.get("stream")
    if stream is None:
        raise RuntimeError("Bedrock converse_stream returned no stream")

    # Let the UI switch from "Calling model…" to "Streaming response…" as
    # soon as the first delta arrives (not earlier — avoids a "Streaming"
    # pill on errors that fire before any tokens).
    first_delta_seen = False

    buffer: list[str] = []
    buffer_len = 0
    last_flush_ts = time.time()
    full_text_parts: list[str] = []

    def _flush_if_needed(force: bool = False) -> None:
        nonlocal buffer, buffer_len, last_flush_ts
        if not buffer:
            return
        now = time.time()
        should_flush = (
            force
            or buffer_len >= STREAM_FLUSH_CHAR_THRESHOLD
            or (now - last_flush_ts) >= STREAM_FLUSH_INTERVAL_S
        )
        if not should_flush:
            return
        delta = "".join(buffer)
        buffer = []
        buffer_len = 0
        last_flush_ts = now
        _emit(
            session_id=session_id,
            method="assistant_stream",
            status="STREAMING",
            content=delta,
            model_id=selected_model_id,
            is_processing=True,
        )

    try:
        for event in stream:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta") or {}
                text = delta.get("text") or ""
                if not text:
                    continue
                if not first_delta_seen:
                    first_delta_seen = True
                    _emit(
                        session_id=session_id,
                        method="assistant_status",
                        status="STREAMING",
                        content="Streaming response…",
                        model_id=selected_model_id,
                    )
                full_text_parts.append(text)
                buffer.append(text)
                buffer_len += len(text)
                _flush_if_needed()
            elif "messageStop" in event:
                # Stream complete — make sure pending buffer is published
                # before the final event.
                _flush_if_needed(force=True)
            elif "internalServerException" in event or "throttlingException" in event:
                err_msg = event.get(
                    "internalServerException", event.get("throttlingException", {})
                ).get("message", "Bedrock stream error")
                raise RuntimeError(f"Bedrock stream error: {err_msg}")
            # Other event types (messageStart, metadata, etc.) are ignored.
    finally:
        _flush_if_needed(force=True)

    return "".join(full_text_parts)


def _remove_text_between_brackets(text: str) -> str:
    """Strip a leading JSON object that some models wrap error messages in."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[:start] + text[end + 1 :]
    return text


def handler(event, _context):  # noqa: ANN001
    """Process one Chat-with-Document turn end-to-end.

    Expected event payload (from ``send_chat_document_message_resolver``)::

        {
          "sessionId": "...",
          "turnId":    "...",
          "prompt":    "...",
          "s3Uri":     "uploads/doc.pdf",
          "modelId":   "us.anthropic.claude-opus-4-8:1m" | "",
          "callerSub": "<cognito sub>"
        }
    """
    logger.info("chat-doc processor invoked: session=%s", event.get("sessionId"))

    session_id = event.get("sessionId") or ""
    turn_id = event.get("turnId") or ""
    prompt = event.get("prompt") or ""
    object_key = event.get("s3Uri") or ""
    ui_model_id = (event.get("modelId") or "").strip()
    caller_sub = event.get("callerSub") or ""

    if not (session_id and prompt and object_key):
        logger.error("Missing required fields in chat-doc event: %s", event)
        _emit(
            session_id=session_id or "unknown",
            method="assistant_error",
            status="ERROR",
            content="Internal error: missing required fields.",
            is_processing=False,
        )
        return {"ok": False, "reason": "invalid_event"}

    try:
        # --- 1. Look up the document record ---------------------------------
        _emit(
            session_id=session_id,
            method="assistant_status",
            status="LOADING_DOCUMENT",
            content="Loading document…",
        )
        document = _get_document_record(object_key)
        if not document:
            raise Exception(f"Document '{object_key}' not found")

        config_version = (
            document.get("ConfigVersion")
            or document.get("ConfigurationVersion")
            or None
        )

        # --- 2. RBAC scope enforcement --------------------------------------
        # Fails CLOSED, matching the document-list resolvers: a scoped caller
        # cannot chat with a document that carries no ConfigVersion, because an
        # unstamped document cannot be proven to be in their scope.
        allowed_versions = _get_user_allowed_config_versions(caller_sub)
        if not scope_allows(allowed_versions, config_version):
            logger.warning(
                "Scope denied: caller_sub=%s allowed=%s doc_version=%s",
                caller_sub,
                allowed_versions,
                config_version,
            )
            _emit(
                session_id=session_id,
                method="assistant_error",
                status="ERROR",
                content=(
                    "You do not have access to this document's configuration version."
                ),
                is_processing=False,
            )
            return {"ok": False, "reason": "scope_denied"}

        # --- 3. Resolve chat settings ---------------------------------------
        chat_settings = _resolve_chat_settings(config_version)
        selected_model_id = ui_model_id or chat_settings["model_id"]

        # --- 4. Assemble or load cached full text ---------------------------
        output_bucket = os.environ["OUTPUT_BUCKET"]
        fulltext_key = object_key + "/summary/fulltext.txt"
        if not _s3_object_exists(output_bucket, fulltext_key):
            page_count = len(document.get("Pages") or [])
            _emit(
                session_id=session_id,
                method="assistant_status",
                status="LOADING_DOCUMENT",
                content=f"Loading document text ({page_count} pages)…",
            )
            content_str = _get_full_text(output_bucket, object_key, document)
            _s3.put_object(
                Bucket=output_bucket,
                Key=fulltext_key,
                Body=content_str.encode("utf-8"),
            )
        else:
            resp = _s3.get_object(Bucket=output_bucket, Key=fulltext_key)
            content_str = resp["Body"].read().decode("utf-8")

        # --- 5. Invoke Bedrock (streaming) ---------------------------------
        _emit(
            session_id=session_id,
            method="assistant_status",
            status="CALLING_MODEL",
            content=f"Querying {selected_model_id}…",
            model_id=selected_model_id,
        )

        user_text = content_str + "\n\n" + "The user's question is: " + prompt
        t_start = time.time()
        if is_openai_responses_model(selected_model_id):
            # OpenAI GPT-5.x: bedrock-mantle Responses API (non-streaming).
            full_text = _invoke_openai_responses_and_emit(
                session_id=session_id,
                selected_model_id=selected_model_id,
                system_prompt=chat_settings["system_prompt"],
                user_text=user_text,
                max_tokens=chat_settings["max_tokens"],
                reasoning_effort=chat_settings.get("reasoning_effort"),
            )
        else:
            full_text = _invoke_bedrock_stream_and_emit(
                session_id=session_id,
                selected_model_id=selected_model_id,
                system_prompt=chat_settings["system_prompt"],
                user_text=user_text,
                temperature=chat_settings["temperature"],
                max_tokens=chat_settings["max_tokens"],
                reasoning_effort=chat_settings.get("reasoning_effort"),
            )
        elapsed_s = time.time() - t_start
        cleaned = _remove_text_between_brackets(full_text).strip("\n")
        logger.info(
            "Bedrock streamed %.1fs (model=%s, chars=%d)",
            elapsed_s,
            selected_model_id,
            len(cleaned),
        )

        # --- 6. Publish final answer ---------------------------------------
        _emit(
            session_id=session_id,
            method="assistant_final",
            status="COMPLETE",
            content=cleaned,
            model_id=selected_model_id,
            is_processing=False,
        )
        # Persist for the non-streaming (GovCloud poll) path. No-op when
        # CHAT_MESSAGES_TABLE is unset (commercial/streaming builds).
        _persist_chat_messages(session_id, prompt, cleaned)
        return {"ok": True, "turnId": turn_id}

    except Exception as e:  # noqa: BLE001
        logger.exception("chat-doc processor failed: %s", e)
        _emit(
            session_id=session_id,
            method="assistant_error",
            status="ERROR",
            content=str(e),
            is_processing=False,
        )
        # Persist the error as the assistant reply so a polling client sees a
        # terminal message instead of waiting until timeout.
        _persist_chat_messages(session_id, prompt, f"Error: {e}")
        return {"ok": False, "error": str(e)}
