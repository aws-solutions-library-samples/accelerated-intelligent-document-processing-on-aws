# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Model router for Amazon Bedrock — dispatches model calls to the right backend.

Most models (Anthropic Claude, Amazon Nova) use the standard Converse API via
boto3 (``bedrock_runtime.converse``). OpenAI GPT-5.x models on Bedrock (gpt-5.4,
gpt-5.5) are served ONLY through the OpenAI-compatible Responses API on the
``bedrock-mantle`` endpoint — they do NOT support Converse / InvokeModel.

The entry point is ``converse_or_responses(...)``: callers build a normal
Converse-shaped request and this router decides where to send it. For Converse
models it's a thin passthrough to ``bedrock_runtime.converse``. For OpenAI models
it:

1. Detects ``openai.gpt-5.*`` model IDs.
2. Resolves the correct ``bedrock-mantle`` region (US-only, no geo/global).
3. Translates Converse-shaped (system, messages) into a Responses request.
4. SigV4-signs an HTTP POST (no API key — uses the Lambda's IAM role).
5. Translates the OpenAI response + usage back into the Converse-shaped dict
   every caller already expects — so detection/synthetic/audio code is unchanged.

GPT-5.x are reasoning models: they reject temperature / top_p / top_k. Sampling
params are omitted and ``reasoning.effort`` (minimal|low|medium|high) is used.

The OpenAI Responses translation is adapted from the AWS IDP Accelerator's
openai_responses.py reference.
"""

import base64
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
)
from botocore.httpsession import URLLib3Session

logger = logging.getLogger(__name__)


class MantleNonRetryableError(RuntimeError):
    """A deterministic bedrock-mantle failure that must NOT be retried.

    Raised for client errors (HTTP 4xx invalid_request, e.g. an unsupported
    reasoning_effort value) where every retry would fail identically. Callers
    must propagate this rather than retry — and must NOT swallow it into an
    empty result, since for PII detection a swallowed failure means undetected
    PII (a silent leak).
    """


# SigV4 signing service name for the bedrock-mantle endpoint.
MANTLE_SIGNING_NAME = os.environ.get("BEDROCK_MANTLE_SIGNING_NAME", "bedrock-mantle")

# Default reasoning effort when none configured.
DEFAULT_REASONING_EFFORT = os.environ.get("BEDROCK_MANTLE_REASONING_EFFORT", "medium")
# All effort tokens the OpenAI Responses API defines, ordered least→most.
EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")
VALID_REASONING_EFFORTS = frozenset(EFFORT_ORDER)

# Per-model reasoning efforts OFFERED for this phase. Bedrock (verified via
# tests/probe_reasoning_efforts.py) ACCEPTS none/low/medium/high/xhigh for
# GPT-5.4/5.5 and rejects 'minimal'. We intentionally OFFER only none/low here:
# medium+ reasoning can exceed the 300s per-call read timeout on PII-dense
# documents (confirmed in the overnight matrix), and the retries then risk the
# Lambda timeout. Re-enable the higher tiers after the detection-chunking
# refactor that keeps each call under the timeout. The UI is driven from these
# sets so an unsupported/unsafe value cannot be selected.
_GPT5_EFFORTS = frozenset({"none", "low"})
_MODEL_SUPPORTED_EFFORTS: Dict[str, frozenset] = {
    "openai.gpt-5.4": _GPT5_EFFORTS,
    "openai.gpt-5.5": _GPT5_EFFORTS,
}

# (connect, read) timeouts — mirror the Converse client config.
_HTTP_TIMEOUT = (10, 300)

# OpenAI Responses models served on bedrock-mantle. Extend as new ones launch.
_RESPONSES_API_MODELS = frozenset({"openai.gpt-5.4", "openai.gpt-5.5"})

# Region availability per model (no geo/global cross-region; IDs carry no prefix).
_MODEL_REGIONS: Dict[str, frozenset] = {
    # GPT-5.5: us-east-2 only (confirmed by Bedrock team; not yet rolled out to
    # us-east-1 despite the model card listing it).
    "openai.gpt-5.5": frozenset({"us-east-2"}),
    # GPT-5.4: verified reliable (6/6) in us-east-1, us-east-2, us-west-2.
    "openai.gpt-5.4": frozenset(
        {"us-east-1", "us-east-2", "us-west-2", "us-gov-west-1"}
    ),
}
_MODEL_DEFAULT_REGION: Dict[str, str] = {
    "openai.gpt-5.5": "us-east-2",
    "openai.gpt-5.4": "us-east-2",
}

# ============================================================
# Model capability registry
# ============================================================
# Describes how each model family's inference request must be shaped. This is
# the SINGLE source of truth — adding/adjusting a model is one entry here (or an
# override in config.yaml model.capabilities). Keys are matched as a substring
# of the model id, checked MOST-SPECIFIC FIRST (order matters).
#
# Fields:
#   api: "converse" | "responses"      — which backend
#   sampling: bool                      — accepts temperature/top_p/top_k?
#   thinking: None | "enabled_budget" | "adaptive_effort" | "reasoning_effort"
#   efforts: list[str]                  — valid effort values (effort-based models)
#   max_output_tokens: int | None       — output cap (clamps maxTokens + chunk size)
#
# VERIFIED against Bedrock (tests/probe_reasoning_efforts.py + the Opus probe):
#   - Opus 4.7/4.8: reject temperature/top_k; adaptive thinking + output_config.effort
#     (low/medium/high/max/xhigh); old enabled+budget thinking is rejected.
#   - GPT-5.4/5.5: reject temperature/top_k; reasoning.effort none/low/medium/high/xhigh.
#   - Nova v1: temperature/top_k OK; no thinking; 10000 output cap.
#   - Legacy Claude (Sonnet 4.6, Haiku 4.5, Opus 4.5/4.6): temperature OK; enabled+budget thinking.
_CAPABILITY_DEFAULTS = [
    (
        "opus-4-8",
        {
            "api": "converse",
            "sampling": False,
            "thinking": "adaptive_effort",
            "efforts": ["low", "medium", "high", "xhigh"],
            "max_output_tokens": 64000,
        },
    ),
    (
        "opus-4-7",
        {
            "api": "converse",
            "sampling": False,
            "thinking": "adaptive_effort",
            "efforts": ["low", "medium", "high", "xhigh"],
            "max_output_tokens": 64000,
        },
    ),
    (
        "openai.gpt-5",
        {
            "api": "responses",
            "sampling": False,
            "thinking": "reasoning_effort",
            "efforts": ["none", "low"],
            "max_output_tokens": None,
        },
    ),
    # GPT-6 Astra is NOT on the Responses API like the GPT-5.x entry above — it is
    # an ordinary Converse model (see idp_common/bedrock/client.py). It must be
    # listed explicitly because it matches none of the substrings here and would
    # otherwise land on _CAPABILITY_FALLBACK, whose `sampling: True` sends
    # `temperature` to a model that rejects it with a 400 — failing every step.
    #
    # `thinking: None` is a deliberate limitation, not an oversight: Astra carries
    # effort in `additionalModelRequestFields.reasoning.effort`, and the Converse
    # branch of _apply_thinking only implements Claude's `enabled_budget` and
    # `adaptive_effort` styles. Sending no effort is safe (Astra's reasoning is
    # always on and defaults sensibly); wiring the carrier needs a new style here.
    # xAI Grok has the identical gap for the identical reason.
    (
        "openai.gpt-6-astra",
        {
            "api": "converse",
            "sampling": False,
            "thinking": None,
            "efforts": [],
            "max_output_tokens": 128000,
        },
    ),
    (
        "nova",
        {
            "api": "converse",
            "sampling": True,
            "thinking": None,
            "efforts": [],
            "max_output_tokens": 10000,
        },
    ),
    # Generic Claude fallback (Sonnet 4.6, Haiku 4.5, Opus 4.5/4.6, etc.)
    (
        "anthropic",
        {
            "api": "converse",
            "sampling": True,
            "thinking": "enabled_budget",
            "efforts": [],
            "max_output_tokens": 64000,
        },
    ),
    (
        "claude",
        {
            "api": "converse",
            "sampling": True,
            "thinking": "enabled_budget",
            "efforts": [],
            "max_output_tokens": 64000,
        },
    ),
]

# Safe fallback for an unknown model: standard Converse, sampling allowed, no thinking.
_CAPABILITY_FALLBACK = {
    "api": "converse",
    "sampling": True,
    "thinking": None,
    "efforts": [],
    "max_output_tokens": None,
}


def _most_specific_key(keys, base: str):
    """Return the key that is a substring of ``base`` and is MOST SPECIFIC.

    Specificity = the match that STARTS LATEST in the model id (tie-break:
    longest key). ORDER-INDEPENDENT, so it's robust to the config being
    serialized with reordered keys — when written to S3, YAML sorts the
    capabilities keys alphabetically, putting generic 'anthropic' before
    specific 'opus-4-8'. A model id like 'us.anthropic.claude-opus-4-8' contains
    BOTH 'anthropic' (early) and 'opus-4-8' (late); we must pick 'opus-4-8'.
    """
    best, best_start, best_len = None, -1, -1
    for k in keys:
        kl = str(k).lower()
        start = base.find(kl)
        if start == -1:
            continue
        if start > best_start or (start == best_start and len(kl) > best_len):
            best, best_start, best_len = k, start, len(kl)
    return best


def get_model_capabilities(
    model_id: Optional[str], config: Optional[Dict] = None
) -> Dict[str, Any]:
    """Resolve a model's inference capabilities.

    Resolution:
      1. Start from the built-in default for the model family (or the safe
         fallback for unknown models).
      2. Layer any matching config.yaml ``model.capabilities`` entry ON TOP as a
         SPARSE DELTA — config can override the whole entry OR just one field
         (e.g. only ``max_output_tokens``); unspecified fields keep the default.

    Matching is ORDER-INDEPENDENT (most-specific key by match position), so it
    works regardless of how the config serializes the keys (S3 sorts them
    alphabetically). Built-in defaults mean known models work even with no
    config capabilities section; config edits take precedence.
    """
    base = (model_id or "").lower()

    # 1) Built-in default (most-specific match), else safe fallback.
    caps = dict(_CAPABILITY_FALLBACK)
    default_map = dict(_CAPABILITY_DEFAULTS)
    dk = _most_specific_key(list(default_map.keys()), base)
    if dk:
        caps = dict(default_map[dk])

    # 2) Config override layered on top as a sparse delta (most-specific match).
    overrides = {}
    if config:
        overrides = (config.get("model", {}) or {}).get("capabilities", {}) or {}
    ok = _most_specific_key(list(overrides.keys()), base)
    if ok:
        caps.update(overrides[ok] or {})

    return caps


def _strip_region_prefix(model_id: str) -> str:
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in ("us", "eu", "global"):
        return parts[1]
    return model_id


def is_openai_responses_model(model_id: Optional[str]) -> bool:
    """True if the model must be invoked via the OpenAI Responses API."""
    if not model_id:
        return False
    base = _strip_region_prefix(model_id)
    if base in _RESPONSES_API_MODELS:
        return True
    return base.startswith("openai.gpt-5")


def supported_reasoning_efforts(model_id: Optional[str]) -> list:
    """Reasoning efforts a model accepts, ordered least→most reasoning.

    VERIFIED per-model (tests/probe_reasoning_efforts.py). Use this to drive the
    UI so users can only pick a value the selected model actually supports.
    Returns [] for non-OpenAI models (reasoning effort does not apply).
    """
    if not is_openai_responses_model(model_id):
        return []
    base = _strip_region_prefix(model_id or "")
    supported = _MODEL_SUPPORTED_EFFORTS.get(base, _GPT5_EFFORTS)
    return [e for e in EFFORT_ORDER if e in supported]


def _normalize_reasoning_effort(reasoning_effort: Optional[str]) -> str:
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


def resolve_mantle_region(model_id: str, configured_region: Optional[str]) -> str:
    """Resolve the bedrock-mantle region for a model.

    Order: BEDROCK_MANTLE_REGION env > configured region (if available) >
    per-model default region.
    """
    base = _strip_region_prefix(model_id)
    allowed = _MODEL_REGIONS.get(base, frozenset())

    pinned = os.environ.get("BEDROCK_MANTLE_REGION", "").strip()
    if pinned:
        return pinned
    if configured_region and (not allowed or configured_region in allowed):
        return configured_region

    default_region = _MODEL_DEFAULT_REGION.get(base, "us-east-2")
    logger.warning(
        "Model %s not available in region %s; routing bedrock-mantle request to %s "
        "(cross-region data movement). Set BEDROCK_MANTLE_REGION to control this.",
        base,
        configured_region,
        default_region,
    )
    return default_region


def _mantle_endpoint(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1/responses"


def _system_text(system: Union[str, List[Dict[str, Any]]]) -> str:
    """Flatten a Converse system prompt (str or [{'text': ...}]) to a string."""
    if isinstance(system, str):
        return system
    parts: List[str] = []
    for item in system or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _image_to_data_uri(image_block: Dict[str, Any]) -> Optional[str]:
    """Convert a Converse image content block to an OpenAI image data URI."""
    image = image_block.get("image", {})
    fmt = image.get("format", "png")
    raw = image.get("source", {}).get("bytes")
    if raw is None:
        return None
    if isinstance(raw, bytes):
        b64 = base64.b64encode(raw).decode("utf-8")
    elif isinstance(raw, str):
        b64 = raw
    else:
        return None
    return f"data:image/{fmt};base64,{b64}"


def build_responses_request(
    system: Union[str, List[Dict[str, Any]]],
    content: List[Dict[str, Any]],
    max_tokens: Optional[int],
    model_id: str,
    reasoning_effort: Optional[str] = None,
) -> Dict[str, Any]:
    """Translate Converse-shaped (system, content) into a Responses request body."""
    base_model_id = _strip_region_prefix(model_id)
    input_items: List[Dict[str, Any]] = []
    for item in content or []:
        if "text" in item and isinstance(item["text"], str):
            text = item["text"].replace("<<CACHEPOINT>>", "")
            input_items.append({"type": "input_text", "text": text})
        elif "image" in item:
            data_uri = _image_to_data_uri(item)
            if data_uri:
                input_items.append({"type": "input_image", "image_url": data_uri})
            else:
                logger.warning("Skipping unparseable image content block.")
        elif "cachePoint" in item:
            continue  # prefix caching unsupported on these models
        else:
            logger.debug("Skipping unsupported content keys: %s", list(item))

    body: Dict[str, Any] = {
        "model": base_model_id,
        "input": [{"role": "user", "content": input_items}],
        "reasoning": {"effort": _normalize_reasoning_effort(reasoning_effort)},
        "stream": False,
        "store": False,
    }
    system_text = _system_text(system)
    if system_text:
        body["instructions"] = system_text
    if max_tokens:
        try:
            body["max_output_tokens"] = int(max_tokens)
        except (ValueError, TypeError):
            pass
    return body


def _extract_output_text(openai_json: Dict[str, Any]) -> str:
    """Concatenate assistant text from a Responses payload (skip reasoning items)."""
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
    fallback = openai_json.get("output_text")
    return fallback if isinstance(fallback, str) else ""


def _map_usage(openai_json: Dict[str, Any]) -> Dict[str, int]:
    """Map the Responses usage object to Converse usage keys."""
    usage = openai_json.get("usage", {}) or {}
    input_tokens = int(usage.get("input_tokens", 0) or 0)
    output_tokens = int(usage.get("output_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
        "cacheReadInputTokens": cached,
        "cacheWriteInputTokens": 0,
    }


def translate_response(openai_json: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a Responses payload into the Converse-shaped dict callers expect."""
    text = _extract_output_text(openai_json)
    usage = _map_usage(openai_json)
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": openai_json.get("status", "end_turn"),
        "usage": usage,
    }


def _sign_and_send(body: Dict[str, Any], region: str):
    """SigV4-sign and POST the request to the bedrock-mantle endpoint."""
    session = boto3.Session()
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


def invoke_responses_api(
    model_id: str,
    system: Union[str, List[Dict[str, Any]]],
    content: List[Dict[str, Any]],
    max_tokens: Optional[int],
    region: str,
    reasoning_effort: Optional[str] = None,
    max_retries: int = 5,
    token_tracker: Any = None,
) -> Dict[str, Any]:
    """Invoke an OpenAI GPT-5.x model via bedrock-mantle. Returns Converse-shaped dict.

    Retries on HTTP 429/5xx and connection/read timeouts with exponential backoff.
    """
    mantle_region = resolve_mantle_region(model_id, region)
    body = build_responses_request(
        system, content, max_tokens, model_id, reasoning_effort
    )
    logger.info(
        "bedrock-mantle request: model=%s region=%s max_output_tokens=%s reasoning=%s",
        body["model"],
        mantle_region,
        body.get("max_output_tokens"),
        body.get("reasoning"),
    )

    last_error = None
    for attempt in range(max_retries):
        try:
            response = _sign_and_send(body, mantle_region)
            status = response.status_code
            if status == 200:
                openai_json = json.loads(response.text)
                result = translate_response(openai_json)
                if token_tracker:
                    token_tracker.track(result)
                return result

            body_text = (response.text or "")[:1000]
            last_error = f"HTTP {status}: {body_text}"
            if status == 429 or status >= 500:
                if attempt >= max_retries - 1:
                    raise RuntimeError(
                        f"bedrock-mantle failed after {max_retries} attempts: {last_error}"
                    )
                wait = 2**attempt
                logger.warning(
                    "bedrock-mantle %s (attempt %d/%d), retrying in %ds",
                    last_error,
                    attempt + 1,
                    max_retries,
                    wait,
                )
                time.sleep(wait)
                continue
            # Terminal client error (400/403/404/422) — deterministic, never
            # retry. Raise a typed error so callers fail loud (never swallow).
            raise MantleNonRetryableError(
                f"bedrock-mantle request failed: {last_error}"
            )

        except (ConnectTimeoutError, ReadTimeoutError, BotoConnectionError) as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= max_retries - 1:
                raise RuntimeError(
                    f"bedrock-mantle failed after {max_retries} attempts: {last_error}"
                ) from e
            wait = 2**attempt
            logger.warning(
                "bedrock-mantle connection error %s (attempt %d/%d), retrying in %ds",
                last_error,
                attempt + 1,
                max_retries,
                wait,
            )
            time.sleep(wait)

    raise RuntimeError(
        f"bedrock-mantle failed after {max_retries} attempts: {last_error}"
    )


def _thinking_enabled(config: Optional[Dict], step: str) -> bool:
    if not config:
        return False
    section = config.get("synthetic" if step == "synthetic" else "detection", {}) or {}
    return bool(section.get("enable_thinking", False))


def _thinking_budget(config: Optional[Dict], step: str) -> int:
    section = (config or {}).get(
        "synthetic" if step == "synthetic" else "detection", {}
    ) or {}
    try:
        return int(section.get("thinking_budget_tokens", 4000))
    except (ValueError, TypeError):
        return 4000


def _resolve_effort(
    config: Optional[Dict], step: str, caps: Dict[str, Any]
) -> Optional[str]:
    """Effort value for effort-based models, validated against the model's set.

    Reuses the configured reasoning_effort (per step) as the unified 'effort'
    knob for both OpenAI (reasoning.effort) and new-style Claude
    (output_config.effort). If the configured value isn't valid for this model
    (e.g. 'none' for Claude), falls back to 'low' (or the first valid value).
    """
    valid = caps.get("efforts") or []
    if not valid:
        return None
    eff = None
    if config:
        from helpers.model_config_helper import get_reasoning_effort

        eff = get_reasoning_effort(config, step=step)
    if eff in valid:
        return eff
    return "low" if "low" in valid else valid[0]


def apply_model_capabilities(
    kwargs: Dict[str, Any],
    model_id: str,
    config: Optional[Dict] = None,
    step: str = "detection",
) -> Optional[str]:
    """Shape Converse kwargs per the model's capabilities (mutates in place).

    Centralizes all per-model request shaping — the single place this logic
    lives:
      - clamp maxTokens to the model's output limit
      - apply the model's thinking/effort format (enabled+budget, adaptive+effort,
        or none) for the requested step
      - strip temperature/top_p/top_k for models that reject them (and whenever
        a thinking block is added — Claude thinking requires no sampling)

    Returns the effort string to use on the Responses (mantle) path; None for
    Converse models.
    """
    caps = get_model_capabilities(model_id, config)
    ic = kwargs.get("inferenceConfig")
    if not isinstance(ic, dict):
        ic = {}
        kwargs["inferenceConfig"] = ic

    # 1) Clamp maxTokens to the model's output limit.
    cap = caps.get("max_output_tokens")
    if cap and int(ic.get("maxTokens", 0) or 0) > cap:
        ic["maxTokens"] = cap

    amrf = dict(kwargs.get("additionalModelRequestFields") or {})
    style = caps.get("thinking")
    effort = _resolve_effort(config, step, caps)

    # 2) Thinking/effort format.
    if style == "enabled_budget" and _thinking_enabled(config, step):
        budget = _thinking_budget(config, step)
        # Bedrock requires maxTokens > thinking.budget_tokens (the thinking budget
        # is part of the output budget). Keep the budget under the model's output
        # cap with answer headroom, and ensure maxTokens leaves room after thinking.
        out_cap = caps.get("max_output_tokens") or 64000
        budget = max(1024, min(budget, out_cap - 1024))
        needed = min(budget + 1024, out_cap)
        if int(ic.get("maxTokens", 0) or 0) < needed:
            ic["maxTokens"] = needed
        amrf = {"thinking": {"type": "enabled", "budget_tokens": budget}}
    elif style == "adaptive_effort" and _thinking_enabled(config, step):
        block: Dict[str, Any] = {"thinking": {"type": "adaptive"}}
        if effort:
            block["output_config"] = {"effort": effort}
        amrf = block

    thinking_added = "thinking" in amrf

    # 3) Strip sampling for models that reject it, or whenever thinking is on
    #    (Claude extended thinking requires temperature/top_k be absent).
    if not caps.get("sampling", True) or thinking_added:
        ic.pop("temperature", None)
        ic.pop("topP", None)
        amrf.pop("top_k", None)
        nested = amrf.get("inferenceConfig")
        if isinstance(nested, dict):
            nested.pop("topK", None)
            if not nested:
                amrf.pop("inferenceConfig", None)

    if amrf:
        kwargs["additionalModelRequestFields"] = amrf
    else:
        kwargs.pop("additionalModelRequestFields", None)
    return effort


def converse_or_responses(
    bedrock_runtime,
    kwargs: Dict[str, Any],
    region: str,
    reasoning_effort: Optional[str] = None,
    token_tracker: Any = None,
    config: Optional[Dict] = None,
    step: str = "detection",
):
    """Route a Converse-shaped request to the right backend.

    If the model is an OpenAI GPT-5.x model, translate and call the bedrock-mantle
    Responses API. Otherwise call bedrock_runtime.converse(**kwargs) unchanged.

    Args:
        bedrock_runtime: boto3 bedrock-runtime client (used for non-OpenAI models).
        kwargs: Converse kwargs (modelId, messages, system, inferenceConfig, ...).
        region: AWS region the pipeline is running in.
        reasoning_effort: OpenAI reasoning effort (ignored by other models).
        token_tracker: Optional tracker with a .track(response) method.

    Returns:
        Converse-shaped response dict.
    """
    model_id = kwargs.get("modelId", "")
    # Centralized per-model request shaping: maxTokens clamp, thinking/effort
    # format, and sampling-param stripping — all driven by the capability
    # registry. This is the single place per-model rules are applied.
    caps_effort = apply_model_capabilities(kwargs, model_id, config, step)
    if reasoning_effort is None:
        reasoning_effort = caps_effort

    if not is_openai_responses_model(model_id):
        response = bedrock_runtime.converse(**kwargs)
        if token_tracker:
            token_tracker.track(response)
        return response

    # OpenAI path: pull system + user content out of the Converse kwargs.
    system = kwargs.get("system", [])
    messages = kwargs.get("messages", [])
    content: List[Dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user":
            content.extend(msg.get("content", []))
    max_tokens = kwargs.get("inferenceConfig", {}).get("maxTokens")
    return invoke_responses_api(
        model_id=model_id,
        system=system,
        content=content,
        max_tokens=max_tokens,
        region=region,
        reasoning_effort=reasoning_effort,
        token_tracker=token_tracker,
    )
