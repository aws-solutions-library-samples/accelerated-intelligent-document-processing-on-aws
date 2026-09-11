# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Prompt-cache facts the product needs to reason about, in one place (#780).

A ``<<CACHEPOINT>>`` only creates a cache entry if the prefix before it clears the
model's **minimum cacheable prefix**. Below it Bedrock returns ``cacheWrite = 0`` and
``cacheRead = 0``, raises nothing, and bills the prefix at full input price on every
request. The minimum is model-dependent and NOT monotonic across generations, so
"newer is safer" is false; measured in ``docs/benchmarking/prompt-caching.md``.

Scope and known limits of the estimate here:

- It models the **Simple-mode extraction** prefix only. Classification, assessment
  (confidence) and rule-validation prompts also carry markers and are not checked.
- ``chars/4`` was calibrated against Claude Sonnet 4.6's tokenizer (within ~10% on
  the 32 surveyed classes). Opus 4.7 introduced a new tokenizer, shared by Opus 4.8,
  Opus 5 and Fable 5, that yields roughly 1.0-1.35x as many tokens; there the estimate
  runs LOW, which errs toward warning (the conservative direction).
- Two prefix contributors are not counted, both defaulting off and both pushing the
  real prefix UP (again conservative): the forced-tool ``toolSpec`` and the
  multi-instance detection probe property.
- An application inference profile ARN is not resolved to its base model here, so it
  yields ``None`` and no warning, although the client does cache for it.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Mapping, Optional

CACHEPOINT_MARKER = "<<CACHEPOINT>>"

# Published per-model minimum cacheable prefix, in tokens. Order matters: the first
# matching pattern wins, so the specific families precede the broad ones. Amazon
# Nova is deliberately absent: its minimum was measured at <=355 tokens, below any
# shipped class, so it never needs a warning.
_MIN_PREFIX_TIERS = (
    (re.compile(r"claude-(opus-5|fable-5)"), 512),
    (re.compile(r"claude-opus-4-7"), 2048),
    (re.compile(r"claude-(opus-4-6|opus-4-5|haiku-4-5)"), 4096),
    # Sonnet 5, Sonnet 4.6, Opus 4.8, Sonnet 4.5, Sonnet 4, Opus 4.1, Opus 4, 3.7 Sonnet
    (
        re.compile(r"claude-(sonnet-5|sonnet-4|opus-4-8|opus-4-1|opus-4|3-7-sonnet)"),
        1024,
    ),
)

# chars/4 against Bedrock's own count (Sonnet 4.6 tokenizer) on the 32 surveyed
# classes: -8.8% to +5.0% (benchmarks/results/v0.6.7/prompt-cache/). Callers treat
# the estimate as +-10% when deciding whether a class is at the boundary.
_CHARS_PER_TOKEN = 4.0
# Relative error band of the estimate; a class inside it is "may not cache".
ESTIMATE_TOLERANCE = 0.10


def min_cacheable_prefix_tokens(model_id: Optional[str]) -> Optional[int]:
    """The model's minimum cacheable prefix, or None when unknown / not applicable."""
    if not model_id:
        return None
    for pattern, minimum in _MIN_PREFIX_TIERS:
        if pattern.search(model_id):
            return minimum
    return None


def schema_prose(class_schema: Dict[str, Any]) -> str:
    """The class schema as the Simple-mode prompt renders it: cleaned of
    ``x-aws-idp-*`` keys and pretty-printed (mirrors
    ``ExtractionService._format_schema_for_prompt``)."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: clean(v)
                for k, v in node.items()
                if not str(k).startswith("x-aws-idp-")
            }
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node

    return json.dumps(clean(class_schema), indent=2)


def estimate_tokens(text: str) -> int:
    return int(math.ceil(len(text) / _CHARS_PER_TOKEN))


def estimate_prefix_tokens(
    system_prompt: str,
    task_prompt: str,
    class_schema: Dict[str, Any],
    class_id: str,
    few_shot_text: str = "",
) -> Optional[int]:
    """Estimated tokens Bedrock counts before the FIRST cache point of a Simple-mode
    extraction request for ``class_schema``: the system prompt plus the task prompt
    up to the marker with the class placeholders substituted. None when the task
    prompt has no marker (nothing would cache either way)."""
    if CACHEPOINT_MARKER not in (task_prompt or ""):
        return None
    head = task_prompt.split(CACHEPOINT_MARKER)[0]
    head = (
        head.replace("{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}", schema_prose(class_schema))
        .replace("{DOCUMENT_CLASS}", class_id)
        .replace("{FEW_SHOT_EXAMPLES}", few_shot_text or "")
    )
    return estimate_tokens((system_prompt or "") + head)


# --------------------------------------------------------------------------- #
# Reading cache efficiency back out of metering (#780 item 2)
# --------------------------------------------------------------------------- #
#
# Bedrock's usage block (kept numeric by ``client.numeric_usage``) lands in the
# metering map under ``"<Phase>/bedrock/<modelId>"``. Three states are worth
# telling apart, none of which raises anything on its own:
#
#   caching       reads are landing; the ~0.1x read price applies to the prefix
#   write-only    writes with no reads: paying 1.25x on the prefix, collecting nothing
#   never-cached  reads AND writes are zero: the cache point is inert (prefix below
#                 the model's minimum, or no cache point sent)
#
# Two further states keep the report honest: ``disabled`` (the configuration said
# ``prompt_cache: off``, so zero/zero is the intended outcome) and ``no-cache-data``
# (the backend reported no cache units at all, e.g. a LambdaHook or a model whose
# usage block has none; nothing can be concluded).

CACHE_UNIT_READ = "cacheReadInputTokens"
CACHE_UNIT_WRITE = "cacheWriteInputTokens"
CACHE_STATES = (
    "caching",
    "write-only",
    "never-cached",
    "disabled",
    "no-cache-point",
    "no-cache-data",
)


def model_supports_cache_point(model_id: Optional[str]) -> Optional[bool]:
    """Whether the client would send a ``cachePoint`` for this model at all.

    Mirrors ``BedrockClient._is_model_cachepoint_supported`` without a client: an
    exact member of ``CACHEPOINT_SUPPORTED_MODELS`` is ``True``, an inference-profile
    ARN is ``None`` (the client resolves it live; unknown here, so never reported as
    unsupported), anything else is ``False`` — the client strips the markers for it.
    """
    if not model_id:
        return None
    from idp_common.bedrock.client import CACHEPOINT_SUPPORTED_MODELS

    if model_id in CACHEPOINT_SUPPORTED_MODELS:
        return True
    if "inference-profile" in model_id:
        return None
    return False


# Models that cache WITHOUT a Converse ``cachePoint`` block, so
# ``model_supports_cache_point() is False`` says nothing about whether they cache.
#
# This distinction exists because "we do not send this model a cachePoint" and
# "this model cannot cache" are different facts, and conflating them produced a
# user-facing message that was wrong for every model below. Matched on the base
# name (region prefix stripped) so the us./global./eu. profiles all hit.
#
#   * ``openai.gpt-6-astra`` — Converse, implicit. MEASURED: a repeated
#     2,707-token prefix billed inputTokens=2 / cacheReadInputTokens=2707
#     (us-west-2, 2026-09-10). An explicit cachePoint is REJECTED
#     (AccessDeniedException), which is exactly why it is not in
#     CACHEPOINT_SUPPORTED_MODELS.
#   * ``openai.gpt-5.4`` / ``openai.gpt-5.5`` — bedrock-mantle Responses API,
#     automatic: any prefix over ~1,024 tokens is reused with no request change.
#
# Deliberately ABSENT:
#   * ``openai.gpt-5.6`` (Sol/Terra/Luna) — bedrock-mantle with EXPLICIT
#     breakpoints: ``openai_responses.py`` only sends ``prompt_cache_options`` /
#     ``prompt_cache_breakpoint`` when a ``<<CACHEPOINT>>`` marker is present, so
#     for these models "no marker" really does mean "no caching" and the remedy
#     IS to add one. Listing them here would tell a user the opposite. (The
#     ``no-cache-point`` state they land in even WITH a marker — because the
#     marker is translated, not sent as a Converse block — is a separate,
#     pre-existing reporting gap, not something this set should paper over.)
#   * xAI Grok — its model card advertises implicit caching, but four
#     back-to-back identical 20,033-token prompts all reported
#     cacheReadInputTokens=0, so no caching benefit is claimed for it.
_IMPLICIT_CACHE_BASE_NAMES = (
    "openai.gpt-6-astra",
    "openai.gpt-5.4",
    "openai.gpt-5.5",
)


def model_caches_implicitly(model_id: Optional[str]) -> bool:
    """True if the model caches without being sent a Converse ``cachePoint``.

    Use this to word a ``no-cache-point`` report honestly: for these models the
    absence of a cache point is expected and caching may still be happening (or
    about to, once a prefix is seen twice), so it must NOT be reported as the model
    being unable to cache.
    """
    if not model_id:
        return False
    base = model_id.split("/")[-1]
    parts = base.split(".", 1)
    if len(parts) == 2 and parts[0] in ("us", "eu", "global"):
        base = parts[1]
    return base.startswith(_IMPLICIT_CACHE_BASE_NAMES)


def cache_state(
    cache_read: float,
    cache_write: float,
    *,
    has_cache_units: bool,
    disabled: bool = False,
    cache_point_sent: Optional[bool] = None,
) -> str:
    """Classify one metering aggregate. Measured reads or writes win over the
    configuration flag: if tokens were cached, caching happened.

    ``cache_point_sent=False`` means the caller knows no cache point reached the
    model (no ``<<CACHEPOINT>>`` marker in the prompt, or a model the client does
    not send cache points to). That matters because Claude models report
    ``cacheReadInputTokens: 0`` even when no cache point was sent, so zero/zero
    alone cannot tell an inert cache point from an absent one.
    """
    if cache_read > 0:
        return "caching"
    if cache_write > 0:
        return "write-only"
    if disabled:
        return "disabled"
    if cache_point_sent is False:
        return "no-cache-point"
    if has_cache_units:
        return "never-cached"
    return "no-cache-data"


def summarize_cache_usage(
    metering: Mapping[str, Any],
    *,
    context_prefix: str = "Extraction",
    disabled: bool = False,
    cache_point_sent: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """Sum the Bedrock cache units of every metering key in one phase and classify.

    ``context_prefix`` matches the first key segment by prefix, so ``"Extraction"``
    also covers the escalation contexts (``ExtractionEscalation``,
    ``Extraction-Escalation``). Returns ``None`` when the phase has no Bedrock
    metering at all (nothing to report), otherwise a JSON-friendly dict that is
    stable for the section result.json, the Processing Report and Athena.
    """
    input_tokens = read = write = 0.0
    requests = 0.0
    saw_requests = has_cache_units = False
    model_ids: set[str] = set()
    for key, units in metering.items():
        parts = str(key).split("/")
        if len(parts) < 3 or parts[1] != "bedrock":
            continue
        if not parts[0].startswith(context_prefix):
            continue
        if not isinstance(units, Mapping):
            continue
        model_ids.add("/".join(parts[2:]))
        input_tokens += _num(units.get("inputTokens"))
        if CACHE_UNIT_READ in units or CACHE_UNIT_WRITE in units:
            has_cache_units = True
        read += _num(units.get(CACHE_UNIT_READ))
        write += _num(units.get(CACHE_UNIT_WRITE))
        if "requests" in units:
            saw_requests = True
            requests += _num(units.get("requests"))
    if not model_ids:
        return None

    denominator = input_tokens + read + write
    minimum: Optional[int] = None
    for model_id in sorted(model_ids):
        minimum = min_cacheable_prefix_tokens(model_id)
        if minimum is not None:
            break
    return {
        "state": cache_state(
            read,
            write,
            has_cache_units=has_cache_units,
            disabled=disabled,
            cache_point_sent=cache_point_sent,
        ),
        "cache_point_sent": cache_point_sent,
        "input_tokens": int(input_tokens),
        "cache_read_input_tokens": int(read),
        "cache_write_input_tokens": int(write),
        "requests": int(requests) if saw_requests else None,
        "read_share": round(read / denominator, 4) if denominator else None,
        "model_ids": sorted(model_ids),
        "min_cacheable_prefix_tokens": minimum,
    }


def describe_cache_state(summary: Mapping[str, Any]) -> str:
    """One plain-text line for the section's Processing Report."""
    state = summary.get("state")
    read = int(summary.get("cache_read_input_tokens") or 0)
    write = int(summary.get("cache_write_input_tokens") or 0)
    uncached = int(summary.get("input_tokens") or 0)
    requests = summary.get("requests")
    counts = f"{read:,} read / {write:,} written / {uncached:,} uncached input tokens"
    if requests:
        counts += f" over {int(requests)} request(s)"
    if state == "caching":
        share = summary.get("read_share") or 0.0
        return f"caching — {share:.0%} of input read from cache ({counts})"
    if state == "write-only":
        return (
            f"write-only — paid 1.25x to write {write:,} tokens and no read landed "
            f"({counts}); expected when a class is processed once per 5-minute TTL"
        )
    if state == "never-cached":
        minimum = summary.get("min_cacheable_prefix_tokens")
        models = ", ".join(summary.get("model_ids") or []) or "the model"
        floor = (
            f"{models}'s minimum cacheable prefix of {int(minimum):,} tokens"
            if minimum
            else f"{models}'s minimum cacheable prefix"
        )
        return (
            f"never cached — the cache point was inert ({counts}); the prompt "
            f"prefix is probably below {floor}: run 'idp-cli config validate' for "
            f"the per-class estimate"
        )
    if state == "disabled":
        return f"off by configuration (extraction.prompt_cache: off; {counts})"
    if state == "no-cache-point":
        # "We sent no cachePoint" must not be reported as "this model cannot
        # cache" — that is false for every model in _IMPLICIT_CACHE_BASE_NAMES.
        models = summary.get("model_ids") or []
        implicit = bool(models) and all(model_caches_implicitly(m) for m in models)
        if implicit:
            return (
                f"no Converse cache point was sent ({counts}) — expected for this "
                f"model, which caches implicitly; a prefix seen again within the "
                f"cache TTL is reported as caching once reads land"
            )
        return (
            f"no cache point reached the model ({counts}); the prompt has no "
            f"<<CACHEPOINT>> marker, or this model is not one the client sends "
            f"cachePoint blocks to"
        )
    return f"no cache usage reported by this model or backend ({counts})"


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0
