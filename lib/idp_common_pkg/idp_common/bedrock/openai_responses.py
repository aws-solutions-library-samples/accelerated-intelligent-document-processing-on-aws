# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OpenAI Responses API backend for Amazon Bedrock (``bedrock-mantle`` endpoint).

The OpenAI frontier models on Bedrock (GPT-5.4, GPT-5.5, GPT-5.6
Sol/Terra/Luna) are served **only** through the OpenAI-compatible Responses API
on the ``bedrock-mantle`` endpoint.
They do NOT support the Converse / InvokeModel / Chat Completions / Messages
APIs that the rest of the accelerator relies on (confirmed via the Bedrock
model cards and the API-compatibility table). This module provides a second
invocation backend that:

  1. Detects ``openai.gpt-5.*`` model IDs.
  2. Resolves the correct ``bedrock-mantle`` region (these models are only
     available in a subset of regions, with no geo/global cross-region).
  3. Translates the accelerator's Converse-shaped ``(system_prompt, content)``
     inputs into an OpenAI Responses request.
  4. Invokes the REST endpoint with a SigV4-signed HTTP POST (reusing the
     existing assume-role session from :mod:`idp_common.bedrock.session`), so
     no API key / secret is required.
  5. Translates the OpenAI response and ``usage`` object back into the exact
     Converse-shaped ``{"response": ..., "metering": ...}`` structure every
     downstream service expects — so NO service code needs to change.

These models are reasoning models that reject ``temperature``/``top_p``/
``top_k`` (mirroring the Claude 4.7+ behavior in
:func:`idp_common.bedrock.client.is_claude_4_7_model`). Sampling parameters are
intentionally omitted and ``reasoning.effort`` is set to a sensible default.

Prompt caching differs by model generation:

  * **GPT-5.4 / GPT-5.5** cache **automatically** — any prompt prefix over ~1,024
    tokens is cached with no request changes. ``<<CACHEPOINT>>`` markers carry no
    meaning here and are stripped during translation.
  * **GPT-5.6** (Sol/Terra/Luna) caches **explicitly** — the request must carry
    ``prompt_cache_options={"mode": "explicit"}``, a stable ``prompt_cache_key``,
    and a ``prompt_cache_breakpoint`` on the content block ending the cached
    prefix. When a ``<<CACHEPOINT>>`` marker (or ``cachePoint`` block) is present
    for a 5.6 model, this module translates it into those explicit fields.

Schema-enforced output (opt-in)
-------------------------------
Unlike the Converse / InvokeModel surface — where ``strict`` tool use and
``output_config.format`` are both rejected by ``bedrock-runtime`` — the OpenAI
Responses API exposes a constrained-decoding format:

    ``text.format = {"type": "json_schema", "name": ..., "schema": ...,
    "strict": true}``

:func:`build_responses_request` and :func:`invoke_responses_api` accept an
optional ``output_schema``. It is **strictly opt-in**: when it is ``None`` the
request body and the returned structure are byte-for-byte what they were before
the parameter existed. See :func:`to_strict_json_schema` for the schema
normalization that strict mode requires, and the
:class:`OutputSchemaRejectedError` / :class:`OutputRefusalError` /
:class:`IncompleteOutputError` docstrings for the failure modes.
"""

import base64
import copy
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Union

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from botocore.exceptions import (
    ConnectTimeoutError,
    ReadTimeoutError,
)
from botocore.httpsession import URLLib3Session

from .model_utils import get_model_max_output_tokens
from .session import get_bedrock_session

logger = logging.getLogger(__name__)

# SigV4 signing service name for the bedrock-mantle endpoint. Matches the IAM
# action namespace (``bedrock-mantle:CreateInference``). Overridable via env var
# in case AWS changes the signing name without a code change.
MANTLE_SIGNING_NAME = os.environ.get("BEDROCK_MANTLE_SIGNING_NAME", "bedrock-mantle")

# Default reasoning effort for the GPT-5.x reasoning models. These models do not
# accept temperature/top_p/top_k; reasoning.effort is the supported control.
# This is the fallback when no per-service reasoning_effort is configured; it can
# also be overridden globally via the BEDROCK_MANTLE_REASONING_EFFORT env var.
DEFAULT_REASONING_EFFORT = os.environ.get("BEDROCK_MANTLE_REASONING_EFFORT", "medium")

# Valid OpenAI reasoning effort levels.
VALID_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})


def _normalize_reasoning_effort(reasoning_effort: Optional[str]) -> str:
    """Validate/normalize a reasoning effort value, falling back to the default."""
    if not reasoning_effort:
        return DEFAULT_REASONING_EFFORT
    effort = str(reasoning_effort).lower().strip()
    if effort not in VALID_REASONING_EFFORTS:
        logger.warning(
            "Invalid reasoning_effort '%s' (valid: %s); using '%s'.",
            reasoning_effort,
            sorted(VALID_REASONING_EFFORTS),
            DEFAULT_REASONING_EFFORT,
        )
        return DEFAULT_REASONING_EFFORT
    return effort


# Default ``text.format.name`` when a caller supplies an output schema without a
# name. The Responses API requires a name on a json_schema format block.
DEFAULT_OUTPUT_SCHEMA_NAME = "structured_output"

# Allowed characters for ``text.format.name`` (mirrors the OpenAI constraint on
# response-format / tool names: alphanumerics, underscores, hyphens, <= 64 chars).
_SCHEMA_NAME_ALLOWED = re.compile(r"[^a-zA-Z0-9_-]")
_SCHEMA_NAME_MAX_LEN = 64

# Vendor-extension prefix used throughout the accelerator's class schemas
# (``x-aws-idp-*``, ``x-aws-stickler-*``). These are meaningless to the model and
# are unknown keywords to the endpoint's schema validator, so they are stripped
# before the schema goes on the wire — same discipline as
# extraction/validation.py and bda/bda_blueprint_service.py.
_VENDOR_KEY_PREFIX = "x-"

# JSON-Schema keywords whose values are themselves schemas (or lists of schemas)
# and therefore need the same strict-mode treatment applied recursively.
_SCHEMA_MAP_KEYWORDS = ("properties", "$defs", "definitions", "patternProperties")
_SCHEMA_LIST_KEYWORDS = ("anyOf", "allOf", "oneOf", "prefixItems")

# HTTP timeouts (connect, read) — mirror the Config used for the converse client
# (connect_timeout=10, read_timeout=300) in client.py.
_HTTP_TIMEOUT = (10, 300)

# Byte read size for the streaming SSE reader.
_STREAM_READ_CHUNK = 256

# OpenAI Responses models served on bedrock-mantle. Add new GPT-5.x variants
# here as they launch — mirrors the one-line-to-extend pattern of
# _CLAUDE_4_7_BASE_NAMES in client.py.
_RESPONSES_API_MODELS = frozenset(
    {
        "openai.gpt-5.4",
        "openai.gpt-5.5",
        "openai.gpt-5.6-sol",
        "openai.gpt-5.6-terra",
        "openai.gpt-5.6-luna",
    }
)

# Region availability per model (no geo/global cross-region inference; model IDs
# carry no region prefix). Source: Bedrock model cards + region-compatibility
# page (GPT-5.4/5.5/5.6). GPT-5.6 Sol is not in us-west-2; Terra/Luna are.
_MODEL_REGIONS: Dict[str, frozenset] = {
    "openai.gpt-5.5": frozenset({"us-east-1", "us-east-2"}),
    "openai.gpt-5.4": frozenset(
        {"us-east-1", "us-east-2", "us-west-2", "us-gov-west-1"}
    ),
    "openai.gpt-5.6-sol": frozenset({"us-east-1", "us-east-2"}),
    "openai.gpt-5.6-terra": frozenset({"us-east-1", "us-east-2", "us-west-2"}),
    "openai.gpt-5.6-luna": frozenset({"us-east-1", "us-east-2", "us-west-2"}),
}

# Per-model fallback region when the configured region is unavailable.
_MODEL_DEFAULT_REGION: Dict[str, str] = {
    "openai.gpt-5.5": "us-east-1",
    "openai.gpt-5.4": "us-east-1",
    "openai.gpt-5.6-sol": "us-east-1",
    "openai.gpt-5.6-terra": "us-east-1",
    "openai.gpt-5.6-luna": "us-east-1",
}


class OutputSchemaRejectedError(RuntimeError):
    """The endpoint rejected the request while an ``output_schema`` was attached.

    Raised instead of a bare ``RuntimeError`` on a terminal (non-retryable)
    client error when the request carried a ``text.format`` json_schema block, so
    a caller can distinguish "this schema is not acceptable to the endpoint" from
    every other 4xx and fall back to a prompt-based schema. Subclasses
    ``RuntimeError``, so existing ``except RuntimeError`` handlers are unaffected.
    """


class OutputRefusalError(RuntimeError):
    """The model refused to answer instead of emitting the requested schema.

    The Responses API signals this with a ``{"type": "refusal", "refusal": ...}``
    content block rather than ``output_text``. Extracting text would yield an
    empty string, so this is raised rather than returned — a refusal is not a
    successful empty extraction. Carries ``.metering`` (the request was billed)
    and ``.refusal``.
    """

    def __init__(
        self,
        message: str,
        refusal: Optional[str] = None,
        metering: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.refusal = refusal
        self.metering = metering or {}


class IncompleteOutputError(RuntimeError):
    """The response stopped before the schema-constrained JSON was finished.

    ``status == "incomplete"`` (usually ``incomplete_details.reason ==
    "max_output_tokens"``). Under constrained decoding the partial text is
    invalid JSON by construction, so returning it would hand the caller
    unparseable output. Carries ``.metering`` (the request was billed) and
    ``.reason``.
    """

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        metering: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.metering = metering or {}


def _model_supports_explicit_cache(base_model_id: str) -> bool:
    """Return True if the model uses explicit prompt-cache breakpoints.

    GPT-5.6 (Sol/Terra/Luna) requires explicit ``prompt_cache_options`` +
    ``prompt_cache_breakpoint``; GPT-5.4/5.5 cache automatically and need no
    request changes.
    """
    return base_model_id.startswith("openai.gpt-5.6")


def _strip_region_prefix(model_id: str) -> str:
    """Strip a leading region qualifier (us./eu./global.) if present."""
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in ("us", "eu", "global"):
        return parts[1]
    return model_id


def is_openai_responses_model(model_id: Optional[str]) -> bool:
    """Return True if the model must be invoked via the OpenAI Responses API.

    Handles a defensive region prefix even though these IDs carry none today.

    Args:
        model_id: Bedrock model ID (e.g., ``openai.gpt-5.4``).

    Returns:
        True if the model is an OpenAI GPT-5.x Responses-API model.
    """
    if not model_id:
        return False
    base = _strip_region_prefix(model_id)
    if base in _RESPONSES_API_MODELS:
        return True
    # Forward-compatible: any future openai.gpt-5.x lands on the same backend.
    return base.startswith("openai.gpt-5")


def resolve_mantle_region(model_id: str, configured_region: Optional[str]) -> str:
    """Resolve the bedrock-mantle region to use for a given model.

    Resolution order:
      1. ``BEDROCK_MANTLE_REGION`` env var (operator pin).
      2. ``configured_region`` if the model is available there.
      3. Per-model default region (logs a cross-region warning).

    Args:
        model_id: The OpenAI Responses model ID.
        configured_region: The region the BedrockClient is configured for.

    Returns:
        The AWS region to target on the bedrock-mantle endpoint.
    """
    base = _strip_region_prefix(model_id)
    allowed = _MODEL_REGIONS.get(base, frozenset())

    pinned = os.environ.get("BEDROCK_MANTLE_REGION", "").strip()
    if pinned:
        if allowed and pinned not in allowed:
            logger.warning(
                "BEDROCK_MANTLE_REGION=%s is not in the known availability set "
                "for %s (%s); using it anyway.",
                pinned,
                base,
                sorted(allowed),
            )
        return pinned

    if configured_region and (not allowed or configured_region in allowed):
        return configured_region

    # Fall back to a known-available region. Prefer a gov region when the
    # configured region is GovCloud and the model supports it.
    default_region = _MODEL_DEFAULT_REGION.get(base, "us-east-2")
    if (
        configured_region
        and configured_region.startswith("us-gov-")
        and any(r.startswith("us-gov-") for r in allowed)
    ):
        default_region = next(r for r in sorted(allowed) if r.startswith("us-gov-"))

    logger.warning(
        "Model %s is not available in region %s. Routing the bedrock-mantle "
        "request to %s instead (cross-region data movement). Set "
        "BEDROCK_MANTLE_REGION to control this.",
        base,
        configured_region,
        default_region,
    )
    return default_region


def _mantle_endpoint(region: str) -> str:
    """Build the OpenAI Responses endpoint URL for a region."""
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1/responses"


def _system_text(system_prompt: Union[str, List[Dict[str, Any]]]) -> str:
    """Flatten a string-or-list system prompt into a single instructions string."""
    if isinstance(system_prompt, str):
        return system_prompt
    parts: List[str] = []
    for item in system_prompt or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _image_to_data_uri(image_block: Dict[str, Any]) -> Optional[str]:
    """Convert a Converse image content block to an OpenAI image data URI.

    Handles both raw ``bytes`` and already base64-encoded ``str`` sources.

    Returns:
        A ``data:image/...;base64,...`` URI, or None if it can't be parsed.
    """
    image = image_block.get("image", {})
    fmt = image.get("format", "png")
    source = image.get("source", {})
    raw = source.get("bytes")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        b64 = base64.b64encode(raw).decode("utf-8")
    elif isinstance(raw, str):
        # Already base64-encoded (e.g., from the LambdaHook path).
        b64 = raw
    else:
        return None
    return f"data:image/{fmt};base64,{b64}"


def _sanitize_schema_name(name: Optional[str]) -> str:
    """Coerce a schema name into the endpoint's allowed character set.

    Falls back to :data:`DEFAULT_OUTPUT_SCHEMA_NAME` when nothing usable remains.
    The name is a label only — it does not affect decoding — so silently
    sanitizing it (with a debug log) is safe.
    """
    cleaned = _SCHEMA_NAME_ALLOWED.sub("_", str(name or "").strip())[
        :_SCHEMA_NAME_MAX_LEN
    ]
    cleaned = cleaned.strip("_")
    if not cleaned:
        return DEFAULT_OUTPUT_SCHEMA_NAME
    if cleaned != name:
        logger.debug("Sanitized output schema name %r -> %r", name, cleaned)
    return cleaned


def _widen_to_nullable(node: Dict[str, Any]) -> Dict[str, Any]:
    """Add ``"null"`` to a schema node's ``type`` so an optional field can be null.

    Strict mode requires **every** property to appear in ``required``, so an
    originally-optional property has to be expressed as a nullable union instead
    (the shape OpenAI's own structured-output docs prescribe).

    A node with no ``type`` — a bare ``$ref``, or an ``anyOf`` — is returned
    unchanged: widening a ``$ref`` would require rewriting it as
    ``anyOf: [{$ref: ...}, {type: "null"}]``, which changes the caller's schema
    more than this function should. Such a property stays effectively required.
    """
    node_type = node.get("type")
    if isinstance(node_type, str):
        if node_type != "null":
            node["type"] = [node_type, "null"]
    elif isinstance(node_type, list):
        if "null" not in node_type:
            node["type"] = [*node_type, "null"]
    return node


def to_strict_json_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a JSON Schema so ``strict: true`` will accept it.

    Pure and non-mutating (the input is deep-copied) and **idempotent**, so an
    already-strict schema passes through unchanged.

    Strict mode on the Responses API imposes three rules, confirmed from every
    example in OpenAI's structured-outputs guide:

    1. every object sets ``additionalProperties: false``;
    2. every declared property is listed in ``required``;
    3. an optional value is modelled as a nullable union
       (``"type": ["string", "null"]``), not by omission from ``required``.

    This function applies all three recursively, and additionally strips
    ``x-*`` vendor-extension keywords (``x-aws-idp-*``, ``x-aws-stickler-*``) —
    they carry no meaning for the model and are unknown keywords to the
    endpoint's schema validator.

    Caveats, deliberately not papered over:

    * A property whose schema is a bare ``$ref`` cannot be widened to nullable
      (see :func:`_widen_to_nullable`); it becomes genuinely required.
    * The full list of JSON-Schema keywords the endpoint accepts under strict
      mode is not documented for ``bedrock-mantle``. Keywords other than the
      ``x-*`` extensions are passed through verbatim; if the endpoint rejects
      one, :class:`OutputSchemaRejectedError` surfaces it rather than the
      keyword being silently dropped.

    Args:
        schema: A JSON Schema object (typically an accelerator class schema).

    Returns:
        A new schema dict safe to send as ``text.format.schema``.

    Raises:
        ValueError: If ``schema`` is not a dict.
    """
    if not isinstance(schema, dict):
        raise ValueError(
            f"output_schema must be a JSON-Schema dict, got {type(schema).__name__}"
        )
    return _strictify_node(copy.deepcopy(schema))


def _strictify_node(node: Any) -> Any:
    """Recursive worker for :func:`to_strict_json_schema` (mutates in place)."""
    if isinstance(node, list):
        return [_strictify_node(item) for item in node]
    if not isinstance(node, dict):
        return node

    # 1. Drop vendor extensions — unknown keywords to the endpoint validator.
    for key in [
        k for k in node if isinstance(k, str) and k.startswith(_VENDOR_KEY_PREFIX)
    ]:
        del node[key]

    # 2. Recurse into every nested schema position.
    for keyword in _SCHEMA_MAP_KEYWORDS:
        nested = node.get(keyword)
        if isinstance(nested, dict):
            node[keyword] = {k: _strictify_node(v) for k, v in nested.items()}
    for keyword in _SCHEMA_LIST_KEYWORDS:
        nested = node.get(keyword)
        if isinstance(nested, list):
            node[keyword] = [_strictify_node(item) for item in nested]
    if "items" in node:
        node["items"] = _strictify_node(node["items"])
    if isinstance(node.get("not"), dict):
        node["not"] = _strictify_node(node["not"])

    # 3. Apply the object rules.
    properties = node.get("properties")
    if isinstance(properties, dict):
        previously_required = set(node.get("required") or [])
        for prop_name, prop_schema in properties.items():
            if isinstance(prop_schema, dict) and prop_name not in previously_required:
                _widen_to_nullable(prop_schema)
        node["required"] = list(properties.keys())
        node["additionalProperties"] = False

    return node


def build_output_text_format(
    output_schema: Dict[str, Any],
    output_schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the Responses API ``text`` block that enforces a JSON Schema.

    The field is ``text.format`` (the Responses API shape) — **not** the Chat
    Completions ``response_format``.

    Args:
        output_schema: JSON Schema the output must conform to. Normalized via
            :func:`to_strict_json_schema`.
        output_schema_name: Label for the schema. Sanitized; defaults to
            :data:`DEFAULT_OUTPUT_SCHEMA_NAME`.

    Returns:
        ``{"format": {"type": "json_schema", "name": ..., "strict": True,
        "schema": ...}}``

    Raises:
        ValueError: If the schema is not a dict, or its root is not an object.
            Strict mode requires an object root; a non-object root would be
            rejected at the endpoint, so it is caught here with a clear message
            instead of costing a round trip.
    """
    strict_schema = to_strict_json_schema(output_schema)

    root_type = strict_schema.get("type")
    root_types = root_type if isinstance(root_type, list) else [root_type]
    if "object" not in root_types:
        raise ValueError(
            "output_schema root must be a JSON-Schema object (strict structured "
            f"output does not support a {root_type!r} root). Wrap it, e.g. "
            '{"type": "object", "properties": {"items": <your schema>}}.'
        )

    return {
        "format": {
            "type": "json_schema",
            "name": _sanitize_schema_name(output_schema_name),
            "strict": True,
            "schema": strict_schema,
        }
    }


def build_responses_request(
    system_prompt: Union[str, List[Dict[str, Any]]],
    content: List[Dict[str, Any]],
    max_tokens: Optional[Union[int, str]],
    model_id: str,
    reasoning_effort: Optional[str] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    output_schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Translate Converse-shaped inputs into an OpenAI Responses request body.

    Args:
        system_prompt: System prompt (string or list of ``{"text": ...}``).
        content: Converse content blocks (text/image/cachePoint).
        max_tokens: Requested max output tokens (capped to the model limit).
        model_id: The OpenAI Responses model ID.
        reasoning_effort: Reasoning effort (minimal/low/medium/high). Falls back
            to DEFAULT_REASONING_EFFORT when None or invalid.
        output_schema: Optional JSON Schema for constrained decoding. When None
            (the default) no ``text`` key is added and the body is byte-identical
            to what this function produced before the parameter existed.
        output_schema_name: Optional label for the schema; ignored when
            ``output_schema`` is None.

    Returns:
        A dict suitable for JSON-encoding as the Responses request body.

    Raises:
        ValueError: If ``output_schema`` is given but is not a JSON-Schema dict
            with an object root.
    """
    base_model_id = _strip_region_prefix(model_id)
    explicit_cache = _model_supports_explicit_cache(base_model_id)

    # Track the index of the last input item that ends a cache prefix, so we can
    # attach an explicit ``prompt_cache_breakpoint`` to it (GPT-5.6 only). For
    # GPT-5.4/5.5 caching is automatic and markers are simply stripped.
    input_items: List[Dict[str, Any]] = []
    cache_breakpoint_index: Optional[int] = None
    for item in content or []:
        if "text" in item and isinstance(item["text"], str):
            raw_text = item["text"]
            has_marker = "<<CACHEPOINT>>" in raw_text
            text = raw_text.replace("<<CACHEPOINT>>", "")
            input_items.append({"type": "input_text", "text": text})
            if has_marker and explicit_cache:
                # The prefix up to and including this block is cacheable.
                cache_breakpoint_index = len(input_items) - 1
        elif "image" in item:
            data_uri = _image_to_data_uri(item)
            if data_uri:
                input_items.append({"type": "input_image", "image_url": data_uri})
            else:
                logger.warning("Skipping unparseable image content block.")
        elif "cachePoint" in item:
            # A structured cachePoint block marks the end of a cacheable prefix.
            if explicit_cache and input_items:
                cache_breakpoint_index = len(input_items) - 1
            # (No content of its own to append.)
        else:
            logger.debug("Skipping unsupported content block keys: %s", list(item))

    # Resolve and cap max output tokens against the model's documented limit.
    resolved_max = None
    try:
        model_cap = get_model_max_output_tokens(model_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not resolve max output tokens for %s: %s", model_id, e)
        model_cap = None

    if max_tokens is not None:
        try:
            requested = int(max_tokens)
            resolved_max = min(requested, model_cap) if model_cap else requested
        except (ValueError, TypeError):
            logger.warning(
                "Invalid max_tokens value '%s'; using model cap.", max_tokens
            )
            resolved_max = model_cap
    else:
        resolved_max = model_cap

    body: Dict[str, Any] = {
        "model": base_model_id,
        "input": [{"role": "user", "content": input_items}],
        # Reasoning models on Bedrock reject temperature/top_p/top_k — omit them.
        "reasoning": {"effort": _normalize_reasoning_effort(reasoning_effort)},
        "stream": False,
        # Do not retain conversation state server-side; this is stateless
        # document processing, not a chat session.
        "store": False,
    }

    system_text = _system_text(system_prompt)
    if system_text:
        body["instructions"] = system_text

    if resolved_max:
        body["max_output_tokens"] = resolved_max

    # Schema-enforced output (opt-in). Nothing is added when output_schema is
    # None, so the default request body is unchanged.
    if output_schema is not None:
        body["text"] = build_output_text_format(output_schema, output_schema_name)

    # Explicit prompt caching (GPT-5.6 only). Attach a breakpoint to the content
    # block that ends the cacheable prefix and derive a stable cache key from the
    # cached prefix so identical prefixes reuse the cache and different prefixes
    # do not. Instructions (system prompt) are part of the cached prefix.
    if explicit_cache and cache_breakpoint_index is not None:
        input_items[cache_breakpoint_index]["prompt_cache_breakpoint"] = {
            "mode": "explicit"
        }
        body["prompt_cache_options"] = {"mode": "explicit"}
        body["prompt_cache_key"] = _derive_cache_key(
            base_model_id, system_text, input_items[: cache_breakpoint_index + 1]
        )

    return body


def _derive_cache_key(
    base_model_id: str,
    system_text: str,
    cached_items: List[Dict[str, Any]],
) -> str:
    """Derive a deterministic prompt cache key for the cached prefix.

    The key is a hash of the model, the system instructions, and the cached
    prefix content (text verbatim; images by their data-URI). Identical prefixes
    yield the same key (cache hit); any change yields a different key (no stale
    reuse). ``prompt_cache_breakpoint`` markers are excluded from the hash so
    they don't perturb the key.
    """
    hasher = hashlib.sha256()
    hasher.update(base_model_id.encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update((system_text or "").encode("utf-8"))
    for item in cached_items:
        hasher.update(b"\x00")
        if item.get("type") == "input_text":
            hasher.update(b"t:")
            hasher.update(item.get("text", "").encode("utf-8"))
        elif item.get("type") == "input_image":
            hasher.update(b"i:")
            hasher.update(item.get("image_url", "").encode("utf-8"))
    return f"idp:{hasher.hexdigest()[:32]}"


def _extract_output_text(openai_json: Dict[str, Any]) -> str:
    """Concatenate assistant text from an OpenAI Responses payload.

    Prefers the structured ``output`` array (skipping ``reasoning`` items);
    falls back to the ``output_text`` convenience field.
    """
    texts: List[str] = []
    for out_item in openai_json.get("output", []) or []:
        if not isinstance(out_item, dict):
            continue
        if out_item.get("type") == "reasoning":
            continue
        if out_item.get("type") == "message":
            for block in out_item.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    texts.append(block.get("text", ""))

    if texts:
        return "".join(texts)

    # Fallback to the SDK convenience field if the structured parse found nothing.
    fallback = openai_json.get("output_text")
    if isinstance(fallback, str):
        return fallback
    return ""


def _extract_refusal(openai_json: Dict[str, Any]) -> Optional[str]:
    """Return the model's refusal message, or None if it did not refuse.

    A refusal replaces the ``output_text`` block with
    ``{"type": "refusal", "refusal": "..."}`` — note that ``status`` can still be
    ``"completed"``, so status alone does not detect it. Also honours a top-level
    ``refusal`` field if the endpoint sets one.
    """
    for out_item in openai_json.get("output", []) or []:
        if not isinstance(out_item, dict) or out_item.get("type") != "message":
            continue
        for block in out_item.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "refusal":
                return block.get("refusal") or "(no refusal text)"

    top_level = openai_json.get("refusal")
    if isinstance(top_level, str) and top_level:
        return top_level
    return None


def _incomplete_reason(openai_json: Dict[str, Any]) -> Optional[str]:
    """Return the ``incomplete_details.reason`` when the response was truncated.

    Returns None for any status other than ``"incomplete"``. The common reason is
    ``"max_output_tokens"``; a content filter also reports here.
    """
    if openai_json.get("status") != "incomplete":
        return None
    details = openai_json.get("incomplete_details") or {}
    if isinstance(details, dict):
        return details.get("reason") or "unknown"
    return "unknown"


def _map_usage(openai_json: Dict[str, Any]) -> Dict[str, int]:
    """Map the OpenAI Responses ``usage`` object to Converse/metering keys.

    The metering keys MUST be exactly these names — they are matched against
    pricing units in config_library/pricing.yaml and any unknown key is treated
    as a billable unit. ``reasoning_tokens`` is intentionally NOT included here
    (emitted as a CloudWatch metric for observability only).
    """
    usage = openai_json.get("usage", {}) or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    input_details = usage.get("input_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens", 0) or 0)
    # GPT-5.6 explicit caching has a (30-min) cache-write price. If the Responses
    # usage object reports written tokens, meter them; otherwise 0. The key name
    # is not yet documented, so probe the likely candidates defensively.
    cache_write_tokens = int(
        input_details.get("cache_creation_tokens")
        or input_details.get("cache_write_tokens")
        or 0
    )
    # IMPORTANT: OpenAI's ``input_tokens`` is the TOTAL prompt count and
    # *includes* the cached + cache-write tokens as a subset. The accelerator's
    # cost model (and the Bedrock Converse convention it mirrors) expects
    # ``inputTokens`` to be the DISJOINT fresh/uncached count, billed separately
    # from cacheReadInputTokens / cacheWriteInputTokens. Subtract the cache
    # portions so cached tokens are not billed twice (full input rate + cache
    # rate). Clamp at 0 defensively. Verified live: input_tokens 4508 with
    # cache_read 3193 → fresh 1315 (consistent cold vs warm).
    fresh_input_tokens = max(0, input_tokens - cached_tokens - cache_write_tokens)
    return {
        "inputTokens": fresh_input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadInputTokens": cached_tokens,
        "cacheWriteInputTokens": cache_write_tokens,
    }


def _reasoning_tokens(openai_json: Dict[str, Any]) -> int:
    """Extract reasoning token count (observability metric only)."""
    usage = openai_json.get("usage", {}) or {}
    details = usage.get("output_tokens_details") or {}
    return int(details.get("reasoning_tokens", 0) or 0)


def translate_response(
    openai_json: Dict[str, Any], model_id: str, context: str
) -> Dict[str, Any]:
    """Translate an OpenAI Responses payload into the accelerator's contract.

    Args:
        openai_json: Parsed OpenAI Responses JSON.
        model_id: The original model ID (used as the metering key segment).
        context: Metering context (e.g., "Extraction").

    Returns:
        ``{"response": <converse-shaped>, "metering": {...}}``.
    """
    text = _extract_output_text(openai_json)
    usage = _map_usage(openai_json)

    converse_response = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": openai_json.get("status", "end_turn"),
        "usage": usage,
    }

    return {
        "response": converse_response,
        "metering": {f"{context}/bedrock/{model_id}": {**usage, "requests": 1}},
    }


def _sign_and_send(body: Dict[str, Any], region: str) -> "URLLib3Session.send":  # type: ignore[name-defined]
    """SigV4-sign and POST the request body to the bedrock-mantle endpoint.

    Returns the botocore HTTP response object (has ``.status_code`` and
    ``.text``). Reuses the assume-role-aware session from session.py.
    """
    session = get_bedrock_session(region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials available to sign bedrock-mantle request"
        )
    frozen = credentials.get_frozen_credentials()

    url = _mantle_endpoint(region)
    data = json.dumps(body).encode("utf-8")
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(frozen, MANTLE_SIGNING_NAME, region).add_auth(aws_request)

    http = URLLib3Session(timeout=_HTTP_TIMEOUT)
    return http.send(aws_request.prepare())


def _sign_request(body: Dict[str, Any], region: str) -> tuple:
    """SigV4-sign a request and return (url, headers, data) for urllib3.

    Used by the streaming path, which needs ``preload_content=False`` —
    something botocore's URLLib3Session does not expose (it buffers the whole
    body). We reuse the same SigV4 signing and assume-role session here.
    """
    session = get_bedrock_session(region)
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError(
            "No AWS credentials available to sign bedrock-mantle request"
        )
    frozen = credentials.get_frozen_credentials()

    url = _mantle_endpoint(region)
    data = json.dumps(body).encode("utf-8")
    aws_request = AWSRequest(
        method="POST",
        url=url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(frozen, MANTLE_SIGNING_NAME, region).add_auth(aws_request)
    prepared = aws_request.prepare()
    return url, dict(prepared.headers), data


def _iter_sse_data_objects(raw_stream):
    """Yield parsed JSON objects from an SSE byte stream.

    The bedrock-mantle Responses stream emits standard Server-Sent Events:
    records separated by a blank line, with the payload on ``data:`` lines.
    Each event also carries a JSON ``type`` (e.g. ``response.output_text.delta``,
    ``response.completed``) which we use rather than the ``event:`` line.
    """
    buffer = ""
    for chunk in raw_stream:
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", "replace")
        while "\n\n" in buffer:
            record, buffer = buffer.split("\n\n", 1)
            for line in record.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    logger.debug(
                        "Skipping unparseable SSE data line: %s", payload[:120]
                    )


def stream_responses_api(
    client: Any,
    model_id: str,
    system_prompt: Union[str, List[Dict[str, Any]]],
    content: List[Dict[str, Any]],
    max_tokens: Optional[Union[int, str]],
    context: str,
    reasoning_effort: Optional[str] = None,
):
    """Stream an OpenAI GPT-5.x response from the bedrock-mantle Responses API.

    Yields incremental text deltas (str) as they arrive, then yields a final
    dict ``{"metering": {...}, "text": <full text>}`` as the last item so the
    caller can record token usage. Unlike :func:`invoke_responses_api` this does
    NOT retry — streaming is interactive (chat) and a mid-stream failure can't be
    transparently retried once deltas have been emitted.

    Args:
        client: BedrockClient instance (for ``_put_metric`` / ``region``).
        model_id: OpenAI Responses model ID.
        system_prompt: System prompt.
        content: Converse content blocks.
        max_tokens: Requested max output tokens.
        context: Metering context.
        reasoning_effort: Reasoning effort (minimal/low/medium/high).

    Raises:
        RuntimeError: On a non-200 response before any deltas are produced.
    """
    # Lazy import: urllib3 ships with botocore/requests and is always present,
    # but keep the import local so the module's import cost is unchanged.
    import urllib3

    client._put_metric("BedrockRequestsTotal", 1)

    region = resolve_mantle_region(model_id, client.region)
    body = build_responses_request(
        system_prompt, content, max_tokens, model_id, reasoning_effort
    )
    body["stream"] = True

    logger.info(
        "Bedrock Mantle (Responses API, streaming) request: model=%s region=%s "
        "max_output_tokens=%s reasoning=%s",
        body["model"],
        region,
        body.get("max_output_tokens"),
        body.get("reasoning"),
    )

    url, headers, data = _sign_request(body, region)
    http = urllib3.PoolManager()
    request_start_time = time.time()

    response = http.request(
        "POST",
        url,
        body=data,
        headers=headers,
        preload_content=False,
        decode_content=True,
        timeout=urllib3.Timeout(connect=_HTTP_TIMEOUT[0], read=_HTTP_TIMEOUT[1]),
    )

    try:
        if response.status != 200:
            body_text = response.read(cache_content=False)[:1000].decode(
                "utf-8", "replace"
            )
            client._put_metric("BedrockRequestsFailed", 1)
            if response.status == 429 or response.status >= 500:
                client._put_metric("BedrockThrottling", 1)
            else:
                client._put_metric("BedrockNonRetryableErrors", 1)
            raise RuntimeError(
                f"bedrock-mantle streaming request failed: HTTP {response.status}: "
                f"{body_text}"
            )

        full_text_parts: List[str] = []
        final_usage: Optional[Dict[str, int]] = None
        reasoning_tokens = 0

        for obj in _iter_sse_data_objects(response.stream(amt=_STREAM_READ_CHUNK)):
            obj_type = obj.get("type")
            if obj_type == "response.output_text.delta":
                delta = obj.get("delta") or ""
                if delta:
                    full_text_parts.append(delta)
                    yield delta
            elif obj_type == "response.completed":
                resp_obj = obj.get("response", {}) or {}
                final_usage = _map_usage(resp_obj)
                reasoning_tokens = _reasoning_tokens(resp_obj)
            elif obj_type == "error" or "error" in obj and obj.get("error"):
                err = obj.get("error") or obj
                raise RuntimeError(f"bedrock-mantle stream error: {err}")
    finally:
        response.release_conn()

    # Emit metrics + a final metering record.
    duration = time.time() - request_start_time
    client._put_metric("BedrockRequestsSucceeded", 1)
    client._put_metric("BedrockTotalLatency", duration * 1000, "Milliseconds")
    if final_usage is None:
        final_usage = {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
    client._put_metric("InputTokens", final_usage["inputTokens"])
    client._put_metric("OutputTokens", final_usage["outputTokens"])
    client._put_metric("TotalTokens", final_usage["totalTokens"])
    client._put_metric("CacheReadInputTokens", final_usage["cacheReadInputTokens"])
    client._put_metric("CacheWriteInputTokens", 0)
    if reasoning_tokens:
        client._put_metric("OpenAIReasoningTokens", reasoning_tokens)

    yield {
        "metering": {f"{context}/bedrock/{model_id}": {**final_usage, "requests": 1}},
        "text": "".join(full_text_parts),
    }


def _raise_on_structured_output_failure(
    openai_json: Dict[str, Any],
    model_id: str,
    metering: Dict[str, Any],
) -> None:
    """Raise if a schema-constrained response refused or was truncated.

    Called only when the caller supplied an ``output_schema``, so it cannot
    change behaviour on the default path. Returns None when the response looks
    usable; an empty body with no refusal and no truncation is logged as a
    warning rather than raised, since that is indistinguishable from a model that
    legitimately produced nothing.
    """
    refusal = _extract_refusal(openai_json)
    if refusal:
        raise OutputRefusalError(
            f"{model_id} refused the schema-constrained request instead of "
            f"returning output: {refusal}",
            refusal=refusal,
            metering=metering,
        )

    reason = _incomplete_reason(openai_json)
    if reason:
        raise IncompleteOutputError(
            f"{model_id} returned an incomplete schema-constrained response "
            f"(reason: {reason}). Under strict json_schema decoding the partial "
            f"text is not valid JSON; raise max_tokens or reduce the input.",
            reason=reason,
            metering=metering,
        )

    if not _extract_output_text(openai_json):
        logger.warning(
            "Schema-constrained response from %s contained no output text "
            "(status=%s). Downstream parsing will see an empty result.",
            model_id,
            openai_json.get("status"),
        )


def invoke_responses_api(
    client: Any,
    model_id: str,
    system_prompt: Union[str, List[Dict[str, Any]]],
    content: List[Dict[str, Any]],
    max_tokens: Optional[Union[int, str]],
    max_retries: int,
    context: str,
    reasoning_effort: Optional[str] = None,
    output_schema: Optional[Dict[str, Any]] = None,
    output_schema_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Invoke an OpenAI GPT-5.x model via the bedrock-mantle Responses API.

    Mirrors BedrockClient._invoke_with_retry: retries on HTTP 429/5xx and
    connection/read timeouts, reusing the client's backoff and metric helpers
    so the existing CloudWatch alarms keep working.

    Schema-enforced output (``output_schema``)
    -----------------------------------------
    This is the one surface in the accelerator with a native constrained-decoding
    format: ``text.format = {"type": "json_schema", "strict": true, ...}``. Pass
    ``output_schema`` to use it. It is **opt-in**; with ``output_schema=None``
    (the default) both the request body and the return value are byte-identical
    to the pre-existing behaviour, including on every failure path.

    Three failure modes are surfaced explicitly rather than swallowed, each as a
    ``RuntimeError`` subclass so existing handlers keep working:

    * **The endpoint rejects the schema** — a terminal 4xx while a
      ``text.format`` block was attached raises
      :class:`OutputSchemaRejectedError`. A caller can catch it and retry with
      ``output_schema=None`` (prompt-based schema) as a fallback.
    * **The model refuses** — a ``refusal`` content block raises
      :class:`OutputRefusalError`. Text extraction would otherwise return an
      empty string that looks like a successful empty extraction.
    * **The response is truncated** — ``status == "incomplete"`` raises
      :class:`IncompleteOutputError`. Under constrained decoding the partial text
      is invalid JSON by construction.

    The refusal/incomplete errors carry ``.metering`` because the request was
    billed; a caller that accounts for cost should record it before re-raising.
    CloudWatch token metrics are emitted before the raise for the same reason.

    Caveat — **not yet live-verified on ``bedrock-mantle``.** ``text.format`` with
    ``strict: true`` is the documented OpenAI Responses API contract, and the AWS
    Responses API documentation defers to it for request-field details while
    enumerating the endpoint's behaviour differences (structured output is not
    among them). It has not been exercised against a live GPT-5.x deployment from
    this repo. Until it is, treat the guarantee as unconfirmed: keep validating
    the parsed result against the schema downstream instead of assuming the
    endpoint honoured the format block.

    Args:
        client: The BedrockClient instance (for ``_put_metric``,
            ``_calculate_backoff``, ``region``).
        model_id: OpenAI Responses model ID.
        system_prompt: System prompt.
        content: Converse content blocks.
        max_tokens: Requested max output tokens.
        max_retries: Maximum retry attempts.
        context: Metering context.
        reasoning_effort: Reasoning effort (minimal/low/medium/high). Falls back
            to DEFAULT_REASONING_EFFORT when None or invalid.
        output_schema: Optional JSON Schema for strict constrained decoding.
            Normalized by :func:`to_strict_json_schema` before being sent.
        output_schema_name: Optional label for the schema; ignored when
            ``output_schema`` is None.

    Returns:
        ``{"response": <converse-shaped>, "metering": {...}}`` — unchanged by this
        feature, so downstream callers need no changes.

    Raises:
        ValueError: If ``output_schema`` is not a JSON-Schema dict with an object
            root (raised before any network call).
        OutputSchemaRejectedError: Terminal client error while a schema was
            attached.
        OutputRefusalError: The model refused instead of answering.
        IncompleteOutputError: The response was truncated mid-JSON.
        RuntimeError: On other non-retryable errors or after exhausting retries.
    """
    client._put_metric("BedrockRequestsTotal", 1)

    region = resolve_mantle_region(model_id, client.region)
    body = build_responses_request(
        system_prompt,
        content,
        max_tokens,
        model_id,
        reasoning_effort,
        output_schema=output_schema,
        output_schema_name=output_schema_name,
    )

    logger.info(
        "Bedrock Mantle (Responses API) request: model=%s region=%s "
        "max_output_tokens=%s reasoning=%s output_schema=%s",
        body["model"],
        region,
        body.get("max_output_tokens"),
        body.get("reasoning"),
        body.get("text", {}).get("format", {}).get("name") if output_schema else None,
    )

    request_start_time = time.time()
    last_error: Optional[str] = None

    for retry_count in range(max_retries):
        try:
            attempt_start = time.time()
            response = _sign_and_send(body, region)
            duration = time.time() - attempt_start
            status = response.status_code

            if status == 200:
                openai_json = json.loads(response.text)
                client._put_metric("BedrockRequestsSucceeded", 1)
                client._put_metric(
                    "BedrockRequestLatency", duration * 1000, "Milliseconds"
                )
                if retry_count > 0:
                    client._put_metric("BedrockRetrySuccess", 1)

                result = translate_response(openai_json, model_id, context)
                usage = result["response"]["usage"]
                client._put_metric("InputTokens", usage["inputTokens"])
                client._put_metric("OutputTokens", usage["outputTokens"])
                client._put_metric("TotalTokens", usage["totalTokens"])
                client._put_metric(
                    "CacheReadInputTokens", usage["cacheReadInputTokens"]
                )
                client._put_metric("CacheWriteInputTokens", 0)
                reasoning = _reasoning_tokens(openai_json)
                if reasoning:
                    client._put_metric("OpenAIReasoningTokens", reasoning)

                total_duration = time.time() - request_start_time
                client._put_metric(
                    "BedrockTotalLatency", total_duration * 1000, "Milliseconds"
                )

                # Schema-enforced output: surface a refusal or a truncated
                # response instead of handing back empty/invalid JSON. Only
                # applies when the caller opted in, so the default path is
                # unchanged. Metrics above are already recorded (the request was
                # billed) and the metering is attached to the exception.
                if output_schema is not None:
                    _raise_on_structured_output_failure(
                        openai_json, model_id, result["metering"]
                    )

                logger.info(
                    "Bedrock Mantle request successful after %d attempt(s). "
                    "Duration: %.2fs. Token usage: %s",
                    retry_count + 1,
                    duration,
                    usage,
                )
                return result

            # Non-200: decide retryable vs terminal.
            body_text = response.text[:1000] if response.text else ""
            last_error = f"HTTP {status}: {body_text}"

            if status == 429 or status >= 500:
                if status == 429:
                    client._put_metric("BedrockThrottles", 1)
                    client._put_metric("BedrockThrottling", 1)
                else:
                    client._put_metric("BedrockServiceUnavailable", 1)

                if retry_count >= max_retries - 1:
                    client._put_metric("BedrockRequestsFailed", 1)
                    client._put_metric("BedrockMaxRetriesExceeded", 1)
                    raise RuntimeError(
                        f"bedrock-mantle request failed after {max_retries} "
                        f"attempts: {last_error}"
                    )
                backoff = client._calculate_backoff(retry_count)
                logger.warning(
                    "bedrock-mantle %s (attempt %d/%d). Retrying in %.2fs.",
                    last_error,
                    retry_count + 1,
                    max_retries,
                    backoff,
                )
                time.sleep(backoff)
                continue

            # Terminal client error (400/403/404/422, etc).
            client._put_metric("BedrockRequestsFailed", 1)
            client._put_metric("BedrockNonRetryableErrors", 1)
            if output_schema is not None:
                # The request carried a text.format json_schema block, so the
                # schema is a prime suspect. Raise a distinguishable subclass so
                # a caller can fall back to a prompt-based schema instead of
                # treating every 4xx alike. The endpoint's error text is
                # preserved verbatim — nothing is swallowed.
                raise OutputSchemaRejectedError(
                    f"bedrock-mantle rejected the request while an output_schema "
                    f"was attached (schema name "
                    f"{body.get('text', {}).get('format', {}).get('name')!r}): "
                    f"{last_error}. If the schema is the cause, retry without "
                    f"output_schema (prompt-based schema) — do not assume the "
                    f"format block was applied."
                )
            raise RuntimeError(f"bedrock-mantle request failed: {last_error}")

        except (
            ConnectTimeoutError,
            ReadTimeoutError,
            BotoConnectionError,
        ) as e:
            last_error = f"{type(e).__name__}: {e}"
            client._put_metric("BedrockTimeouts", 1)
            if retry_count >= max_retries - 1:
                client._put_metric("BedrockRequestsFailed", 1)
                client._put_metric("BedrockMaxRetriesExceeded", 1)
                raise RuntimeError(
                    f"bedrock-mantle request failed after {max_retries} "
                    f"attempts: {last_error}"
                ) from e
            backoff = client._calculate_backoff(retry_count)
            logger.warning(
                "bedrock-mantle connection error %s (attempt %d/%d). "
                "Retrying in %.2fs.",
                last_error,
                retry_count + 1,
                max_retries,
                backoff,
            )
            time.sleep(backoff)
            continue

    # Loop exhausted without returning (should be unreachable).
    client._put_metric("BedrockRequestsFailed", 1)
    raise RuntimeError(
        f"bedrock-mantle request failed after {max_retries} attempts: {last_error}"
    )
