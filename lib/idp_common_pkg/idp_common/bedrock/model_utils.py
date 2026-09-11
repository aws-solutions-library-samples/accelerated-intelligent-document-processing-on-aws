# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Utility functions for Bedrock model ID handling.

This module provides utilities for parsing model IDs and extracting
service tier information from model ID suffixes.
"""

import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


def parse_model_id(model_id: str) -> Tuple[str, Optional[str]]:
    """
    Parse a model ID to extract the base model ID and service tier.

    Model IDs can include an optional service tier suffix in the format:
    <base-model-id>[:<service-tier>]

    Examples:
        >>> parse_model_id("us.amazon.nova-2-lite-v1:0")
        ("us.amazon.nova-2-lite-v1:0", None)

        >>> parse_model_id("us.amazon.nova-2-lite-v1:0:flex")
        ("us.amazon.nova-2-lite-v1:0", "flex")

        >>> parse_model_id("us.amazon.nova-2-lite-v1:0:priority")
        ("us.amazon.nova-2-lite-v1:0", "priority")

        A version-less model ID still parses. Every Bedrock model ID used here
        historically ended in a ``:<version>`` segment, so a tier suffix meant a
        third colon-part; newer families (e.g. ``us.xai.grok-4.6``) carry no
        version segment, which left a 2-part ID with the tier silently
        unextracted and the whole string sent to Bedrock as the modelId:

        >>> parse_model_id("us.xai.grok-4.6:flex")
        ("us.xai.grok-4.6", "flex")

    Args:
        model_id: The model ID string, potentially with service tier suffix

    Returns:
        Tuple of (base_model_id, service_tier) where service_tier is None
        if no valid tier suffix is present
    """
    if not model_id:
        return model_id, None

    # Split on colons
    parts = model_id.split(":")

    # A single part cannot carry a tier suffix.
    if len(parts) < 2:
        return model_id, None

    # Check if last part is a valid service tier
    potential_tier = parts[-1].lower().strip()
    valid_tiers = ["flex", "priority"]

    if potential_tier in valid_tiers:
        # Reconstruct base model ID without the tier suffix
        base_model_id = ":".join(parts[:-1])
        return base_model_id, potential_tier

    # Last part is not a valid tier, return as-is
    return model_id, None


def resolve_model_id_from_arn(model_id: str) -> str:
    """
    Reduce a Bedrock model ARN to the model ID it names.

    Bedrock accepts either a bare model ID (``us.anthropic.claude-sonnet-4-6``)
    or an ARN. GovCloud has no bare cross-region inference-profile path, so a
    GovCloud config must name an account-scoped inference-profile ARN. Callers
    that look a model up in a catalog keyed by model ID (pricing.yaml) need the
    trailing segment.

    ARN fields are colon-delimited and the resource segment itself contains
    colons (model IDs end in ``:0``), so the split is bounded at 5 and the
    resource type is then stripped at the first ``/``.

    Anything that is not a well-formed ARN — a bare model ID, ``LambdaHook``,
    or a malformed string — is returned unchanged. Opaque resources
    (``application-inference-profile/<uuid>``, ``provisioned-model/<id>``)
    resolve to that opaque id, since the underlying foundation model cannot be
    determined without a GetInferenceProfile API call.

    Examples:
        >>> resolve_model_id_from_arn(
        ...     "arn:aws-us-gov:bedrock:us-gov-west-1:123456789012"
        ...     ":inference-profile/us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0"
        ... )
        'us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0'

        >>> resolve_model_id_from_arn(
        ...     "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
        ... )
        'anthropic.claude-sonnet-4-6'

        >>> resolve_model_id_from_arn("us.anthropic.claude-sonnet-4-6")
        'us.anthropic.claude-sonnet-4-6'

    Args:
        model_id: A Bedrock model ID or ARN.

    Returns:
        The model ID named by the ARN, or the input unchanged when it is not a
        parseable ARN.
    """
    if not model_id or not model_id.startswith("arn:"):
        return model_id

    # arn:<partition>:<service>:<region>:<account>:<resource-type>/<resource-id>
    parts = model_id.split(":", 5)
    if len(parts) < 6:
        return model_id

    resource = parts[5]
    if "/" not in resource:
        return model_id

    return resource.split("/", 1)[1] or model_id


@lru_cache(maxsize=1)
def _load_model_limits() -> list[dict]:
    """
    Load model limits from config_library/model_config_limits.yaml on disk.

    This is the OFFLINE fallback (dev checkouts, idp-cli, notebooks). At runtime
    in a deployed stack the DynamoDB Configuration Table is authoritative — see
    _get_effective_model_limits(). Mirrors how config_library/pricing.yaml is
    resolved by merge_utils._load_valid_bedrock_models: a single source file in
    config_library/, nothing bundled into the wheel.

    Returns:
        List of model limit entries with pattern and max_output_tokens.

    Raises:
        FileNotFoundError: If model_config_limits.yaml cannot be found.
        ValueError: If the YAML file is malformed or missing required fields.
    """
    limit_paths = [
        # 1. Development / publish: repo-root config_library/ (5 parents up).
        Path(__file__).parent.parent.parent.parent.parent
        / "config_library"
        / "model_config_limits.yaml",
        # 2. Fallback: IDP_PROJECT_ROOT or current working directory.
        Path(os.environ.get("IDP_PROJECT_ROOT", "."))
        / "config_library"
        / "model_config_limits.yaml",
    ]

    limit_file = None
    for path in limit_paths:
        if path.exists():
            limit_file = path
            break

    if not limit_file:
        raise FileNotFoundError(
            "model_config_limits.yaml not found. "
            "Ensure idp-cli is run from repository root or set IDP_PROJECT_ROOT environment variable. "
            f"Searched paths: {[str(p) for p in limit_paths]}"
        )

    try:
        with open(limit_file) as f:
            config = yaml.safe_load(f)

        if not config or "model_limits" not in config:
            raise ValueError(
                f"model_config_limits.yaml is missing 'model_limits' key: {limit_file}"
            )

        model_limits = config.get("model_limits", [])
        logger.debug(
            "Loaded %d model limit patterns from model_config_limits.yaml",
            len(model_limits),
        )
        return model_limits

    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse model_config_limits.yaml: {e}") from e


# Effective-limits cache. Model limits are consulted on every Bedrock call, so
# a per-call DynamoDB read is too slow, but caching forever would require a
# cold start to pick up UI edits. A 60s TTL bounds staleness after an admin
# saves in the UI while collapsing thousands of hot-path calls to ~1 read/min
# per warm container (same TTL the configuration_resolver uses for user scopes).
_LIMITS_CACHE: dict = {"data": None, "ts": 0.0}
_LIMITS_CACHE_TTL_SECONDS = 60


def _clear_model_limits_cache() -> None:
    """Reset both limit caches (for tests and tooling)."""
    _LIMITS_CACHE["data"] = None
    _LIMITS_CACHE["ts"] = 0.0
    _load_model_limits.cache_clear()


def _load_model_limits_from_dynamodb() -> Optional[list[dict]]:
    """
    Load merged model limits (Default + Custom) from the Configuration Table.

    Returns None — never raises — when the table is unavailable so callers fall
    back to the on-disk config_library/ YAML: no CONFIGURATION_TABLE_NAME env
    var (offline/idp-cli/notebooks), missing records, or any read/validation
    error. ConfigurationManager is imported lazily because config/merge_utils
    already imports this module (a top-level import would create a cycle).
    """
    if not os.environ.get("CONFIGURATION_TABLE_NAME"):
        return None
    try:
        from idp_common.config.configuration_manager import (  # noqa: PLC0415
            ConfigurationManager,
        )

        merged = ConfigurationManager().get_merged_model_config_limits()
        if merged is None or not merged.model_limits:
            return None
        return [
            entry.model_dump(mode="python", exclude_none=True)
            for entry in merged.model_limits
        ]
    except Exception as e:  # fallback must be total — never break a Bedrock call
        logger.debug(
            "Model limits unavailable from DynamoDB, using config_library YAML: %s",
            e,
        )
        return None


def _get_effective_model_limits() -> list[dict]:
    """
    Return the model limits in effect: DynamoDB (merged Default/Custom) when a
    Configuration Table is reachable, else the config_library/ YAML on disk.
    Results are cached for _LIMITS_CACHE_TTL_SECONDS.
    """
    now = time.time()
    if (
        _LIMITS_CACHE["data"] is not None
        and (now - _LIMITS_CACHE["ts"]) < _LIMITS_CACHE_TTL_SECONDS
    ):
        return _LIMITS_CACHE["data"]

    limits = _load_model_limits_from_dynamodb()
    if limits is None:
        limits = _load_model_limits()

    _LIMITS_CACHE["data"] = limits
    _LIMITS_CACHE["ts"] = now
    return limits


def get_model_max_output_tokens(model_id: str) -> int:
    """
    Get the maximum output tokens supported by a Bedrock model.

    This function determines model-specific maximum token limits from the
    effective limits list: the DynamoDB Configuration Table (merged
    DefaultModelConfigLimits + CustomModelConfigLimits, editable in the UI) when
    available, else config_library/model_config_limits.yaml on disk. Patterns
    are matched in order (first match wins). There is no AWS API that exposes
    the per-model output limit, so these limits are the source of truth; an
    unmatched model raises ValueError (callers can fall back to
    parse_max_tokens_limit_from_error on a Bedrock over-limit
    ValidationException to self-heal a missing entry).

    Token Limits by Model Family (from model_config_limits.yaml):
        - Claude Sonnet 5 / Opus 4.6-4.8 / Sonnet 4.6: 128,000 tokens
        - Other Claude 4.x models: 64,000 tokens
        - Claude 3.x models: 8,192 tokens
        - Amazon Nova models: 10,000 tokens
        - OpenAI GPT-5.x: 128,000 tokens

    Args:
        model_id: Bedrock model identifier (e.g., "us.anthropic.claude-sonnet-4-20250514-v1:0")

    Returns:
        Maximum output tokens supported by the model

    Raises:
        FileNotFoundError: If model_config_limits.yaml cannot be found
        ValueError: If model_config_limits.yaml is malformed
        RuntimeError: If no pattern matches the model_id (should never happen with ".*" catch-all)

    Examples:
        >>> get_model_max_output_tokens("us.anthropic.claude-sonnet-4-20250514-v1:0")
        64000
        >>> get_model_max_output_tokens("us.amazon.nova-lite-v1:0")
        10000
        >>> get_model_max_output_tokens("us.anthropic.claude-3-haiku-20240307-v1:0")
        8192
    """
    model_id_lower = model_id.lower()

    # DynamoDB (UI-editable) when available, else disk YAML (raises if the
    # file is also unavailable or malformed)
    model_limits = _get_effective_model_limits()

    # Match against patterns in order (first match wins)
    for limit_entry in model_limits:
        pattern = limit_entry.get("pattern", "")
        max_tokens = limit_entry.get("max_output_tokens")

        if not pattern or max_tokens is None:
            logger.warning(
                "Skipping malformed model limit entry",
                extra={"entry": limit_entry},
            )
            continue

        try:
            matched = re.search(pattern, model_id_lower)
        except re.error:
            # A malformed regex should never crash the Bedrock hot path.
            # ModelLimitEntry validates patterns at save time, but data written
            # through a bypass path or hand-edited YAML could still be invalid.
            logger.warning(
                "Skipping model limit entry with invalid regex pattern",
                extra={"pattern": pattern},
            )
            continue

        if matched:
            logger.debug(
                "Matched model limit pattern",
                extra={
                    "model_id": model_id,
                    "pattern": pattern,
                    "max_output_tokens": max_tokens,
                },
            )
            return max_tokens

    # No pattern matched - unknown/unsupported model
    raise ValueError(
        f"Unsupported model ID: {model_id}. "
        f"No max_tokens limit defined in model_config_limits.yaml. "
        f"Supported model families: Claude 4.x, Claude 3.x, Amazon Nova. "
        f"To add this model, run scripts/discover_model_limits.py to test its actual limits."
    )


def get_model_max_input_tokens(model_id: str) -> int:
    """Get the maximum INPUT (context-window) tokens supported by a model.

    Companion to :func:`get_model_max_output_tokens`, backed by the same
    EFFECTIVE limits (DynamoDB Configuration Table when available, else
    ``config_library/model_config_limits.yaml`` on disk) via the
    ``max_input_tokens`` field. Drives
    model-aware auto-sizing of shard token/page budgets and confidence list-batch
    sizes (see ``idp_common.bedrock.sizing``). Patterns are matched in order
    (first match wins), so ``:1m`` extended-context variants — which have their
    own 1,000,000-token patterns placed before the base entries — resolve to the
    large window rather than the base 200K.

    Args:
        model_id: Bedrock model identifier (e.g. ``us.anthropic.claude-sonnet-5:1m``)

    Returns:
        Maximum input/context tokens supported by the model.

    Raises:
        ValueError: If no pattern matches, or the matched entry has no
            ``max_input_tokens`` (callers should fall back to a safe default).

    Examples:
        >>> get_model_max_input_tokens("us.anthropic.claude-sonnet-5")
        200000
        >>> get_model_max_input_tokens("us.anthropic.claude-sonnet-5:1m")
        1000000
        >>> get_model_max_input_tokens("us.amazon.nova-lite-v1:0")
        300000
    """
    model_id_lower = model_id.lower()
    # Use the EFFECTIVE limits (DynamoDB Configuration Table when available, else
    # config_library/model_config_limits.yaml on disk) — same source as
    # get_model_max_output_tokens — so a window edited in the UI's Model Limits
    # page also drives input-based auto-sizing.
    model_limits = _get_effective_model_limits()

    for limit_entry in model_limits:
        pattern = limit_entry.get("pattern", "")
        max_input = limit_entry.get("max_input_tokens")
        if not pattern:
            continue
        if re.search(pattern, model_id_lower):
            if max_input is None:
                raise ValueError(
                    f"Model ID {model_id} matched pattern {pattern!r} but that "
                    "entry has no 'max_input_tokens'. Add it to "
                    "model_config_limits.yaml."
                )
            logger.debug(
                "Matched model input-window pattern",
                extra={
                    "model_id": model_id,
                    "pattern": pattern,
                    "max_input_tokens": max_input,
                },
            )
            return max_input

    raise ValueError(
        f"Unsupported model ID: {model_id}. No max_input_tokens defined in "
        "model_config_limits.yaml. Supported families: Claude, Amazon Nova, "
        "OpenAI GPT-5.x."
    )


# Bedrock's ValidationException for an over-limit maxTokens request states the
# real cap in the message. There is no AWS API that exposes the per-model output
# limit, so this error text is the authoritative source we fall back to when
# model_config_limits.yaml is missing/stale for a model. Two observed formats:
#   Claude: "max_tokens: 999999 > 128000, which is the maximum allowed number of
#            output tokens for anthropic.claude-sonnet-5"
#   Nova:   "The maximum tokens you requested exceeds the model limit of 10000."
_MAX_TOKENS_LIMIT_PATTERNS = (
    re.compile(r">\s*(\d+)\s*,\s*which is the maximum", re.IGNORECASE),
    re.compile(r"model limit of\s*(\d+)", re.IGNORECASE),
)


def parse_max_tokens_limit_from_error(message: str) -> Optional[int]:
    """Extract the true max-output-tokens limit from a Bedrock ValidationException.

    Returns the limit as an int when the message matches a known over-limit
    format, otherwise None (the error is unrelated to max_tokens).
    """
    if not message:
        return None
    for pattern in _MAX_TOKENS_LIMIT_PATTERNS:
        m = pattern.search(message)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None
