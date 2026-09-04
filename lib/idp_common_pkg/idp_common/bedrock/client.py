# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Bedrock client module for interacting with Amazon Bedrock models.

This module provides a class-based interface for invoking Bedrock models
with built-in retry logic, metrics tracking, and configuration options.
"""

import copy
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional, Union

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from urllib3.exceptions import ReadTimeoutError as Urllib3ReadTimeoutError

from .model_utils import (
    get_model_max_output_tokens,
    parse_max_tokens_limit_from_error,
    parse_model_id,
    resolve_model_id_from_arn,
)
from .openai_responses import invoke_responses_api, is_openai_responses_model
from .session import get_bedrock_session

# Sentinel value for LambdaHook model selection
LAMBDA_HOOK_MODEL_ID = "LambdaHook"


# Dummy exception classes for requests timeouts if requests is not available
class _RequestsReadTimeout(Exception):
    """Fallback exception class when requests library is not available."""

    pass


class _RequestsConnectTimeout(Exception):
    """Fallback exception class when requests library is not available."""

    pass


try:
    from requests.exceptions import (
        ConnectTimeout as RequestsConnectTimeout,
    )
    from requests.exceptions import (
        ReadTimeout as RequestsReadTimeout,
    )
except ImportError:
    # Fallback if requests is not available - use dummy exception classes
    RequestsReadTimeout = _RequestsReadTimeout  # type: ignore[misc,assignment]
    RequestsConnectTimeout = _RequestsConnectTimeout  # type: ignore[misc,assignment]


logger = logging.getLogger(__name__)

# Default retry settings
DEFAULT_MAX_RETRIES = 7
DEFAULT_INITIAL_BACKOFF = 2  # seconds
DEFAULT_MAX_BACKOFF = 300  # 5 minutes


# Claude 4.7 and later model base names that don't support
# temperature/top_k/top_p parameters. These parameters are deprecated for these
# models and cause runtime errors. The set covers Claude 4.7, 4.8, and any
# future generations with the same behavior — add new base names here as
# needed (e.g., sonnet-4-7, haiku-4-7, opus-4-9).
#
# NOTE: This set is consulted by BOTH the traditional Bedrock invocation path
# (this file's BedrockClient.invoke_model) AND the agentic extraction path
# (idp_common/extraction/agentic_idp.py::_get_inference_params). When adding
# a new Claude 4.7+ variant here, no other code changes are required.
_CLAUDE_4_7_BASE_NAMES = {
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    # Claude Opus 5 keeps the Opus 4.7/4.8 request surface: temperature/top_p/
    # top_k are rejected (400). Thinking is ON by default on Opus 5 (unlike
    # 4.7/4.8); the Converse path does not send a `thinking` field, so requests
    # run adaptive thinking within max_tokens.
    "anthropic.claude-opus-5",
    # Claude Sonnet 5 shares the Opus-4.7+ request surface: it REJECTS non-default
    # temperature/top_p/top_k (400). IDP's default decoding config sets top_k=5 /
    # top_p=0.0, so Sonnet 5 must be treated like the sampling-param-stripped models
    # (this is a deliberate deviation from Sonnet 4.6, which still accepts them).
    "anthropic.claude-sonnet-5",
}


def is_claude_4_7_model(model_id: str) -> bool:
    """Check if a model is a Claude 4.7+ variant that doesn't support temperature/top_k/top_p.

    Handles region prefixes (us., eu., global.) and :1m suffix automatically.

    Args:
        model_id: Bedrock model ID (e.g., 'us.anthropic.claude-opus-4-7:1m')

    Inference-profile ARNs are resolved first. Without this, a config naming
    ``arn:...:inference-profile/us.anthropic.claude-sonnet-5`` — the form
    docs/configuration.md recommends for cost allocation, and the only form
    available in GovCloud — would not be recognized, so ``temperature`` would be
    sent to a model that rejects it with a 400. Opaque
    ``application-inference-profile`` ARNs still cannot be resolved offline.

    Returns:
        True if the model is a Claude 4.7+ variant
    """
    model_id = resolve_model_id_from_arn(model_id)
    # Strip region prefix (us., eu., global.)
    parts = model_id.split(".", 1)
    if len(parts) == 2 and parts[0] in ("us", "eu", "global"):
        base = parts[1]
    else:
        base = model_id
    # Strip :1m suffix
    if base.endswith(":1m"):
        base = base[:-3]
    return base in _CLAUDE_4_7_BASE_NAMES


# Backwards-compatible alias for internal callers that still reference the
# private name. New code should import `is_claude_4_7_model`.
_is_claude_4_7_model = is_claude_4_7_model


# Claude models that accept the reasoning-effort control
# (output_config.effort: low|medium|high|xhigh|max). Verified live on Bedrock
# Converse: effort measurably changes output tokens on these; Sonnet 4.5 and
# Haiku 4.5 REJECT effort (400). Kept as base names (region prefix + :1m stripped
# by is_claude_effort_model). GPT-5.x reasoning models use the OpenAI Responses
# `reasoning.effort` control instead (see openai_responses.py), not this path.
_CLAUDE_EFFORT_BASE_NAMES = {
    "anthropic.claude-sonnet-5",
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-opus-4-5",
    "anthropic.claude-opus-4-6",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-opus-5",
    "anthropic.claude-fable-5",
}

# Effort levels accepted by Claude models (a superset of the OpenAI Responses
# levels, which also allow "minimal"). "max"/"xhigh" are Claude-only.
CLAUDE_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def _strip_region_and_1m(model_id: str) -> str:
    """Normalize a model ID to its base name: strip us./eu./global. prefix and
    the :1m suffix. Also tolerates Opus 4.5/4.6 dated/`-v1` foundation IDs by
    matching on a prefix in is_claude_effort_model."""
    parts = model_id.split(".", 1)
    base = (
        parts[1] if len(parts) == 2 and parts[0] in ("us", "eu", "global") else model_id
    )
    if base.endswith(":1m"):
        base = base[:-3]
    return base


def is_claude_effort_model(model_id: str) -> bool:
    """True if the Claude model accepts output_config.effort.

    Handles region prefixes, :1m, and dated/versioned foundation IDs
    (e.g. anthropic.claude-opus-4-6-v1, ...-4-5-20250514-v1:0) by prefix match.
    """
    if not model_id:
        return False
    base = _strip_region_and_1m(model_id)
    return any(base.startswith(name) for name in _CLAUDE_EFFORT_BASE_NAMES)


# ---------------------------------------------------------------------------
# xAI Grok
# ---------------------------------------------------------------------------
# Grok is the first non-Anthropic/non-Nova family to reach the Converse API, so
# several assumptions that held while only Claude and Nova used this path had to
# be generalized. Everything below was verified live against
# ``us.xai.grok-4.6`` on bedrock-runtime Converse in us-west-2 on 2026-09-02:
#
#   * ``inferenceConfig.temperature`` and ``inferenceConfig.topP`` are REJECTED
#     ("This model doesn't support the temperature field"), so Grok joins the
#     sampling-param-stripped set — see ``strips_sampling_params``.
#   * ``maxTokens`` must ride in ``inferenceConfig``. Claude's
#     ``additionalModelRequestFields.max_tokens`` is accepted but SILENTLY
#     IGNORED (a request capped at 100 there returned 135 output tokens).
#     Unknown ``additionalModelRequestFields`` keys are silently ignored too, so
#     the exact carrier matters and a wrong one fails open, not loudly.
#   * Reasoning is always on. The effort carrier is
#     ``additionalModelRequestFields.reasoning.effort``; Claude's
#     ``output_config.effort`` is silently ignored here.
#   * ``document`` content blocks are REJECTED ("This model doesn't support
#     documents") — see ``document_blocks_unsupported_reason``. Images work.
#   * ``toolConfig`` works with all three ``toolChoice`` modes and emits
#     ``toolUse``, so agentic extraction is supported (unlike GPT-5.x).
#   * Explicit ``cachePoint`` blocks raise AccessDeniedException, so Grok is
#     deliberately absent from CACHEPOINT_SUPPORTED_MODELS below. The model card
#     advertises implicit caching, but 4 back-to-back identical 20,033-token
#     prompts all reported ``cacheReadInputTokens=0``, so no caching benefit is
#     claimed.
#   * Flex/Priority service tiers are REJECTED ("The provided service tier is
#     not supported for this model") despite the model card advertising both, so
#     no tier-suffixed Grok IDs are offered.
_GROK_BASE_PREFIX = "xai.grok"

# Effort levels accepted by Grok. NOTE this is NOT the same vocabulary as
# CLAUDE_EFFORT_LEVELS: Grok adds "none" and REJECTS "max" ("Unsupported value:
# 'max' is not supported with the 'us.xai.grok-4.6' model").
GROK_EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh")


def is_grok_model(model_id: str) -> bool:
    """True if ``model_id`` names an xAI Grok model.

    Handles the ``us.``/``global.`` cross-region inference-profile prefixes.
    Grok has no in-region form on bedrock-runtime — the bare ``xai.grok-4.6`` is
    rejected ("Invocation of model ID xai.grok-4.6 with on-demand throughput
    isn't supported") — but the bare name is matched anyway so the predicate is
    about the family, not the routing.

    Inference-profile **ARNs** are resolved first. This matters more here than
    for the Claude predicates: docs/configuration.md actively recommends passing
    an inference-profile ARN for cost-allocation tagging, and an ARN is the only
    form available in GovCloud. Because Grok's rejections are unconditional (a
    400 on temperature, a hard refusal of document blocks), missing the ARN form
    would fail 100% of requests rather than degrade quietly.

    LIMITATION: ``application-inference-profile/<uuid>`` ARNs are opaque — the
    underlying foundation model cannot be determined without a
    GetInferenceProfile call — so those still return False. A Grok application
    inference profile will therefore bypass these gates.
    """
    if not model_id:
        return False
    resolved = resolve_model_id_from_arn(model_id)
    return _strip_region_and_1m(resolved).startswith(_GROK_BASE_PREFIX)


def strips_sampling_params(model_id: str) -> bool:
    """True if the model REJECTS ``temperature`` / ``topP`` / ``top_k``.

    Covers Claude 4.7+ (where these are deprecated) and xAI Grok (which returns
    a 400 naming the offending field). Callers must omit the whole
    ``inferenceConfig`` sampling group for these models rather than passing
    defaults.
    """
    return is_claude_4_7_model(model_id) or is_grok_model(model_id)


# Converse ``document`` content-block capability gate.
#
# Discovery and any other path that hands Bedrock a whole PDF relies on
# ``document`` blocks. Two families cannot accept them and would otherwise
# silently drop the document and hallucinate an answer, so callers fail loudly
# instead. Kept beside ``tool_config_unsupported_reason`` (same shape) so every
# per-model Converse capability gate lives in one place.
DOCUMENT_BLOCK_UNSUPPORTED_ROUTES: Dict[str, str] = {
    "openai-responses": (
        "OpenAI GPT-5.x models are served by the bedrock-mantle Responses API, "
        "which accepts only text and image input"
    ),
    "xai-grok": (
        "xAI Grok models reject Converse document blocks (\"This model doesn't "
        "support documents\"); their input modalities are text and image only"
    ),
}


def document_blocks_unsupported_reason(model_id: Optional[str]) -> Optional[str]:
    """Explain why ``model_id`` cannot accept Converse ``document`` blocks.

    Returns None when the model can accept them.
    """
    if is_openai_responses_model(model_id):
        return DOCUMENT_BLOCK_UNSUPPORTED_ROUTES["openai-responses"]
    if model_id and is_grok_model(model_id):
        return DOCUMENT_BLOCK_UNSUPPORTED_ROUTES["xai-grok"]
    return None


def supports_document_blocks(model_id: Optional[str]) -> bool:
    """True if ``model_id`` can accept whole-PDF Converse ``document`` blocks."""
    return document_blocks_unsupported_reason(model_id) is None


# Base model names that support cachePoint (without region prefix)
# Used to check inference profiles by resolving their underlying foundation model
_CACHEPOINT_BASE_MODELS = set()

# Models that support cachePoint functionality.
# NOTE: OpenAI GPT-5.x models (openai.gpt-5.*) are intentionally NOT listed here.
# They are served via the bedrock-mantle Responses API (see openai_responses.py)
# and do not support Bedrock prompt-prefix caching; <<CACHEPOINT>> markers are
# stripped for them during request translation.
CACHEPOINT_SUPPORTED_MODELS = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-opus-4-6-v1:1m",
    "us.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-7:1m",
    "us.anthropic.claude-opus-4-8",
    "us.anthropic.claude-opus-4-8:1m",
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-opus-5:1m",
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "us.anthropic.claude-opus-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-6:1m",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-sonnet-5:1m",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-lite-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.amazon.nova-2-lite-v1:0",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "eu.anthropic.claude-3-7-sonnet-20250219-v1:0",
    "eu.anthropic.claude-sonnet-4-20250514-v1:0",
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "eu.anthropic.claude-sonnet-4-6",
    "eu.anthropic.claude-sonnet-4-6:1m",
    "eu.anthropic.claude-sonnet-5",
    "eu.anthropic.claude-sonnet-5:1m",
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "eu.anthropic.claude-opus-4-5-20251101-v1:0",
    "eu.anthropic.claude-opus-4-6-v1",
    "eu.anthropic.claude-opus-4-6-v1:1m",
    "eu.anthropic.claude-opus-4-7",
    "eu.anthropic.claude-opus-4-7:1m",
    "eu.anthropic.claude-opus-4-8",
    "eu.anthropic.claude-opus-4-8:1m",
    "eu.anthropic.claude-opus-5",
    "eu.anthropic.claude-opus-5:1m",
    "eu.amazon.nova-lite-v1:0",
    "eu.amazon.nova-pro-v1:0",
    "eu.amazon.nova-2-lite-v1:0",
    "eu.amazon.nova-2-lite-v1:0:priority",
    "eu.amazon.nova-2-lite-v1:0:flex",
    "global.amazon.nova-2-lite-v1:0",
    "global.amazon.nova-2-lite-v1:0:priority",
    "global.amazon.nova-2-lite-v1:0:flex",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "global.anthropic.claude-sonnet-4-6",
    "global.anthropic.claude-sonnet-4-6:1m",
    "global.anthropic.claude-sonnet-5",
    "global.anthropic.claude-sonnet-5:1m",
    "global.anthropic.claude-opus-4-5-20251101-v1:0",
    "global.anthropic.claude-opus-4-6-v1",
    "global.anthropic.claude-opus-4-6-v1:1m",
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-opus-4-7:1m",
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-8:1m",
    "global.anthropic.claude-opus-5",
    "global.anthropic.claude-opus-5:1m",
]

# Converse tool-use (``toolConfig`` / ``toolChoice``) capability gate.
#
# Verified live across every currently selectable family (Claude 3.5/3.7/4.x/5
# and Nova Lite / Pro / 2-Lite): all of them accept a ``toolConfig`` AND all
# three ``toolChoice`` modes (``auto`` / ``any`` / ``tool``) on the Converse API,
# and actually emit the ``toolUse`` block. So — unlike CACHEPOINT_SUPPORTED_MODELS
# above — there is no per-family allow-list to maintain here. The only real
# question is "does this model reach Converse at all", and exactly two routes in
# invoke_model do not. They are named here so the exclusions are discoverable
# and testable rather than buried in an `if`.
#
# NOTE: there is no hard-grammar/strict mode available on bedrock-runtime.
# ``toolSpec.strict``, ``output_config.format`` and ``response_format`` are all
# rejected ("Extra inputs are not permitted") on BOTH Converse and InvokeModel,
# so a forced ``toolChoice`` is the strongest schema enforcement available.
TOOL_CONFIG_UNSUPPORTED_ROUTES: Dict[str, str] = {
    "LambdaHook": (
        "the LambdaHook route posts a Converse-shaped payload to a customer-owned "
        "Lambda function, which is not required to implement tool use"
    ),
    "openai-responses": (
        "OpenAI GPT-5.x models are served by the bedrock-mantle Responses API, "
        "which does not accept a Converse toolConfig (it has its own tools schema)"
    ),
}


def tool_config_unsupported_reason(model_id: str) -> Optional[str]:
    """Explain why ``model_id`` cannot carry a Converse ``toolConfig``.

    Args:
        model_id: Bedrock model ID, the ``LambdaHook`` sentinel, or an
            inference profile ARN.

    Returns:
        A human-readable reason string if this model is routed somewhere other
        than the Converse API, or None if a ``toolConfig`` can be used.
    """
    if model_id == LAMBDA_HOOK_MODEL_ID:
        return TOOL_CONFIG_UNSUPPORTED_ROUTES["LambdaHook"]
    if is_openai_responses_model(model_id):
        return TOOL_CONFIG_UNSUPPORTED_ROUTES["openai-responses"]
    return None


def supports_tool_config(model_id: str) -> bool:
    """True if ``model_id`` reaches the Converse API and can carry a toolConfig.

    Callers that want to degrade gracefully (e.g. fall back to a prose schema in
    the prompt) should check this first; passing a ``tool_config`` for a model
    that returns False raises a ValueError rather than silently dropping the
    schema.
    """
    return tool_config_unsupported_reason(model_id) is None


# Build set of base model names (without region/tier prefixes) for inference profile resolution.
# e.g., "us.anthropic.claude-sonnet-4-6" -> "anthropic.claude-sonnet-4-6"
# and "eu.amazon.nova-2-lite-v1:0:priority" -> "amazon.nova-2-lite-v1:0"
for _model_id in CACHEPOINT_SUPPORTED_MODELS:
    _parts = _model_id.split(".", 1)
    if len(_parts) == 2 and _parts[0] in ("us", "eu", "global"):
        _base = _parts[1]
        # Strip tier suffixes (:priority, :flex) but keep version suffixes (:0, :1m)
        if _base.endswith(":priority") or _base.endswith(":flex"):
            _base = _base.rsplit(":", 1)[0]
        _CACHEPOINT_BASE_MODELS.add(_base)

# Module-level cache for inference profile -> cachepoint support resolution
_inference_profile_cachepoint_cache: Dict[str, bool] = {}


class BedrockClient:
    """Client for interacting with Amazon Bedrock models and custom Lambda hooks."""

    def __init__(
        self,
        region: Optional[str] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        metrics_enabled: bool = True,
    ):
        """
        Initialize a Bedrock client.

        Args:
            region: AWS region (defaults to AWS_REGION env var or us-west-2)
            max_retries: Maximum number of retry attempts
            initial_backoff: Initial backoff time in seconds
            max_backoff: Maximum backoff time in seconds
            metrics_enabled: Whether to publish metrics
        """
        self.region = region or os.environ.get("AWS_REGION")
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.max_backoff = max_backoff
        self.metrics_enabled = metrics_enabled
        self._client = None
        self._bedrock_control_client = None
        self._lambda_client = None
        self._s3_client = None

    @property
    def client(self):
        """Lazy-loaded Bedrock client."""
        config = Config(
            connect_timeout=10,
            read_timeout=300,  # allow plenty of time for large extraction or assessment inferences
        )
        if self._client is None:
            self._client = get_bedrock_session(self.region).client(
                "bedrock-runtime", region_name=self.region, config=config
            )
        return self._client

    @property
    def lambda_client(self):
        """Lazy-loaded Lambda client for LambdaHook invocations."""
        # Lambda invocations stay in the calling account, so this client
        # uses default credentials regardless of BEDROCK_ASSUME_ROLE_ARN.
        if self._lambda_client is None:
            self._lambda_client = boto3.client("lambda", region_name=self.region)
        return self._lambda_client

    @property
    def bedrock_control_client(self):
        """Lazy-loaded Bedrock control plane client for GetInferenceProfile etc."""
        if self._bedrock_control_client is None:
            self._bedrock_control_client = get_bedrock_session(self.region).client(
                "bedrock", region_name=self.region
            )
        return self._bedrock_control_client

    @property
    def s3_client(self):
        """Lazy-loaded S3 client for LambdaHook image uploads."""
        if self._s3_client is None:
            self._s3_client = boto3.client("s3", region_name=self.region)
        return self._s3_client

    def _is_model_cachepoint_supported(self, model_id: str) -> bool:
        """
        Check if a model supports cachePoint, including inference profile resolution.

        For standard model IDs (e.g., "us.anthropic.claude-sonnet-4-6"), checks
        the CACHEPOINT_SUPPORTED_MODELS list directly.

        For inference profile ARNs (containing "inference-profile" or
        "application-inference-profile"), resolves the underlying foundation
        model via the GetInferenceProfile API and checks if that base model
        supports cachePoint. Results are cached to avoid repeated API calls.

        Args:
            model_id: Bedrock model ID or inference profile ARN

        Returns:
            True if the model (or underlying model for inference profiles) supports cachePoint
        """
        # Fast path: direct match against the known list
        if model_id in CACHEPOINT_SUPPORTED_MODELS:
            return True

        # Check if this is an inference profile ARN
        if "inference-profile" not in model_id:
            return False

        # Check module-level cache
        if model_id in _inference_profile_cachepoint_cache:
            cached = _inference_profile_cachepoint_cache[model_id]
            logger.debug(
                f"Inference profile cachepoint support (cached): {model_id} -> {cached}"
            )
            return cached

        # Resolve the inference profile to its underlying foundation model
        try:
            response = self.bedrock_control_client.get_inference_profile(
                inferenceProfileIdentifier=model_id
            )
            models = response.get("models", [])
            if not models:
                logger.warning(
                    f"Inference profile {model_id} has no models listed. "
                    "Cannot determine cachePoint support."
                )
                _inference_profile_cachepoint_cache[model_id] = False
                return False

            # Extract the base model name from the first model's ARN.
            # Model ARN format: arn:aws:bedrock:<region>::foundation-model/<base-model-name>
            # e.g., "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"
            first_model_arn = models[0].get("modelArn", "")
            if "foundation-model/" in first_model_arn:
                base_model_name = first_model_arn.split("foundation-model/")[-1]
            else:
                logger.warning(
                    f"Cannot parse foundation model from ARN: {first_model_arn}"
                )
                _inference_profile_cachepoint_cache[model_id] = False
                return False

            supported = base_model_name in _CACHEPOINT_BASE_MODELS
            _inference_profile_cachepoint_cache[model_id] = supported

            logger.info(
                f"Resolved inference profile {model_id} -> "
                f"foundation model '{base_model_name}' -> "
                f"cachePoint {'supported' if supported else 'not supported'}"
            )
            return supported

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            logger.warning(
                f"Failed to resolve inference profile {model_id} for cachePoint check "
                f"({error_code}): {e}. Disabling cachePoint for this model."
            )
            _inference_profile_cachepoint_cache[model_id] = False
            return False
        except Exception as e:
            logger.warning(
                f"Unexpected error resolving inference profile {model_id} "
                f"for cachePoint check: {e}. Disabling cachePoint for this model."
            )
            _inference_profile_cachepoint_cache[model_id] = False
            return False

    def __call__(
        self,
        model_id: str,
        system_prompt: Union[str, List[Dict[str, str]]],
        content: List[Dict[str, Any]],
        temperature: Union[float, str] = 0.0,
        top_k: Optional[Union[float, str]] = None,
        top_p: Optional[Union[float, str]] = None,
        max_tokens: Optional[Union[int, str]] = None,
        max_retries: Optional[int] = None,
        context: str = "Unspecified",
        service_tier: Optional[str] = None,
        model_lambda_hook_arn: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        tool_config: Optional[Dict[str, Any]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Make the instance callable with the same signature as the original function.

        This allows instances to be used as drop-in replacements for the function.

        Args:
            model_id: The Bedrock model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')
            system_prompt: The system prompt as string or list of content objects
            content: The content for the user message (can include text and images)
            temperature: The temperature parameter for model inference (float or string)
            top_k: Optional top_k parameter (float or string)
            top_p: Optional top_p parameter (float or string)
            max_tokens: Optional max_tokens parameter (int or string)
            max_retries: Optional override for the instance's max_retries setting
            service_tier: Optional service tier (priority, standard, flex)
            tool_config: Optional Converse toolConfig (see invoke_model)
            tool_choice: Optional Converse toolChoice (see invoke_model)

        Returns:
            Bedrock response object with metering information
        """
        # Use instance max_retries if not overridden
        effective_max_retries = (
            max_retries if max_retries is not None else self.max_retries
        )

        return self.invoke_model(
            model_id=model_id,
            system_prompt=system_prompt,
            content=content,
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            max_tokens=max_tokens,
            max_retries=effective_max_retries,
            context=context,
            service_tier=service_tier,
            model_lambda_hook_arn=model_lambda_hook_arn,
            reasoning_effort=reasoning_effort,
            tool_config=tool_config,
            tool_choice=tool_choice,
        )

    def _preprocess_content_for_cachepoint(
        self, content: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Process content list to handle <<CACHEPOINT>> tags in text elements.

        For text elements containing <<CACHEPOINT>> tags, this function will split the text
        and insert cachePoint elements at the tag positions.

        Args:
            content: The content list for the user message (can include text and images)

        Returns:
            Processed content list with cachePoint elements inserted
        """
        if not content:
            return content

        processed_content = []
        cachepoint_count = 0

        for item in content:
            # If it's a text element, check for <<CACHEPOINT>> tags
            if (
                "text" in item
                and isinstance(item["text"], str)
                and "<<CACHEPOINT>>" in item["text"]
            ):
                # Log that we found a cachepoint tag
                logger.debug(
                    f"Found <<CACHEPOINT>> tags in text content: {item['text'][:50]}..."
                )

                # Split the text by the tag
                text_parts = item["text"].split("<<CACHEPOINT>>")
                logger.debug(
                    f"Split text into {len(text_parts)} parts at cachepoint tags"
                )

                # Add each text part interspersed with cachePoint elements
                for i, text_part in enumerate(text_parts):
                    # Only add non-empty text parts
                    if text_part:
                        # Count words in this part
                        word_count = len(text_part.split())
                        logger.debug(f"Text part {i + 1}: {word_count} words")
                        processed_content.append({"text": text_part})
                    else:
                        logger.debug(f"Text part {i + 1}: Empty, skipping")

                    # Add cachePoint after each text part except the last one
                    if i < len(text_parts) - 1:
                        cachepoint_count += 1
                        logger.debug(
                            f"Inserting cachePoint #{cachepoint_count} after text part {i + 1}"
                        )
                        processed_content.append({"cachePoint": {"type": "default"}})
            else:
                # If not a text element or no tags, add it as is
                content_type = (
                    "text"
                    if "text" in item
                    else "image"
                    if "image" in item
                    else "other"
                )
                logger.debug(
                    f"No cachepoint tags in {content_type} content, passing through unchanged"
                )
                processed_content.append(item)

        if cachepoint_count > 0:
            logger.info(
                f"Processed content with {cachepoint_count} cachepoint insertions"
            )

        return processed_content

    @staticmethod
    def _reject_invalid_tool_property_names(tool_config: Dict[str, Any]) -> None:
        """Fail locally on property names Bedrock will reject, naming the fix.

        Bedrock enforces ``^[a-zA-Z0-9_.-]{1,64}$`` on ``inputSchema`` property
        keys, so a document class authored for humans (``"Account Number"``) is
        rejected — four shipped presets contain such names (GitHub #709). Left to
        the service, that surfaces as a ``ValidationException`` from deep inside a
        retry ladder, naming a JSON path rather than a config field.

        This does NOT sanitize on the caller's behalf. Renaming here would hand
        back a response keyed by names the caller never asked for, with no map to
        reverse it — silently renaming every field of every extraction. The caller
        must sanitize *and* keep the map, so it can restore the authored names:

            from idp_common.bedrock.tool_schema import (
                restore_names, sanitize_tool_schema,
            )
            clean, name_map = sanitize_tool_schema(class_schema)
            ...  # send `clean` as the toolSpec inputSchema
            fields = restore_names(model_output, name_map)

        Raises:
            ValueError: naming the offending property paths and the helper.
        """
        from idp_common.bedrock.tool_schema import (
            find_document_metadata_keywords,
            find_invalid_property_names,
        )

        offenders: List[str] = []
        metadata: List[str] = []
        for tool in tool_config.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            spec = tool.get("toolSpec")
            if not isinstance(spec, dict):
                continue
            schema = (spec.get("inputSchema") or {}).get("json")
            for path in find_invalid_property_names(schema):
                offenders.append(f"{spec.get('name', '?')}::{path}")
            for path in find_document_metadata_keywords(schema):
                metadata.append(f"{spec.get('name', '?')}::{path}")

        if metadata:
            # Bedrock meta-validates the schema itself: `$id` must be an RFC 3986
            # URI-reference, and an IDP class schema sets `$id` to the class NAME,
            # so a class like "Policy Application Form" fails on the spaces. That
            # surfaced as a ValidationException naming a JSON path, from inside the
            # retry ladder, after the request was already billed.
            raise ValueError(
                f"tool_config carries schema-DOCUMENT metadata that Bedrock "
                f"meta-validates and will reject: {', '.join(metadata[:8])}. "
                f"These keywords ($id/$schema/$anchor/$comment and x-aws-idp-* "
                f"extensions) constrain nothing, so strip them with "
                f"idp_common.bedrock.tool_schema.strip_non_wire_keywords() (already "
                f"applied by sanitize_tool_schema)."
            )

        if offenders:
            shown = ", ".join(offenders[:8])
            more = f" (+{len(offenders) - 8} more)" if len(offenders) > 8 else ""
            raise ValueError(
                f"tool_config contains {len(offenders)} property name(s) Bedrock "
                f"will reject (must match ^[a-zA-Z0-9_.-]{{1,64}}$): {shown}{more}. "
                f"Run idp_common.bedrock.tool_schema.sanitize_tool_schema() on the "
                f"class schema and idp_common.bedrock.tool_schema.restore_names() "
                f"on the response — refusing to rename fields silently, which "
                f"would change every extracted field name with no way back. Note "
                f"Bedrock only validates the TOP level, so nested offenders listed "
                f"here would be accepted today and break later."
            )

    @staticmethod
    def _resolve_tool_config(
        model_id: str,
        tool_config: Optional[Dict[str, Any]],
        tool_choice: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Validate and merge the tool-use parameters into one ``toolConfig``.

        Returns None when neither parameter was supplied, so the Converse request
        is left exactly as it was before tool support existed.

        Args:
            model_id: The model ID (or ``LambdaHook`` sentinel) being invoked.
            tool_config: Converse ``toolConfig`` dict, or None.
            tool_choice: Converse ``toolChoice`` dict to merge in, or None.

        Returns:
            The effective ``toolConfig`` to put on the wire, or None.

        Raises:
            ValueError: If the model does not reach the Converse API, or if
                ``tool_choice`` was given without ``tool_config``.
        """
        if tool_config is None and tool_choice is None:
            return None

        reason = tool_config_unsupported_reason(model_id)
        if reason:
            raise ValueError(
                f"tool_config/tool_choice is not supported for model "
                f"'{model_id}': {reason}. Refusing to drop the tool schema "
                f"silently — check supports_tool_config(model_id) first and use "
                f"a prompt-based schema for this model."
            )

        if tool_config is None:
            raise ValueError(
                "tool_choice was provided without tool_config. The Converse API "
                "rejects a toolChoice with no tools; pass the toolConfig that "
                "declares the tool being chosen."
            )

        BedrockClient._reject_invalid_tool_property_names(tool_config)

        if tool_choice is None:
            return tool_config

        # Shallow copy: callers are expected to reuse one deterministic,
        # per-class toolConfig object (see the prompt-cache note in
        # invoke_model), so never mutate what we were handed.
        merged = dict(tool_config)
        existing_choice = merged.get("toolChoice")
        if existing_choice is not None and existing_choice != tool_choice:
            logger.warning(
                "Both tool_config['toolChoice'] (%s) and the tool_choice "
                "parameter (%s) were provided; the tool_choice parameter wins.",
                existing_choice,
                tool_choice,
            )
        merged["toolChoice"] = tool_choice
        return merged

    def invoke_model(
        self,
        model_id: str,
        system_prompt: Union[str, List[Dict[str, str]]],
        content: List[Dict[str, Any]],
        temperature: Union[float, str] = 0.0,
        top_k: Optional[Union[float, str]] = 5,
        top_p: Optional[Union[float, str]] = 0.1,
        max_tokens: Optional[Union[int, str]] = None,
        max_retries: Optional[int] = None,
        context: str = "Unspecified",
        service_tier: Optional[str] = None,
        model_lambda_hook_arn: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        tool_config: Optional[Dict[str, Any]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Invoke a Bedrock model or custom Lambda hook with retry logic.

        When model_id is 'LambdaHook', the request is routed to the specified
        Lambda function instead of Bedrock. The Lambda receives a Converse API-compatible
        payload and must return a Converse API-compatible response.

        Args:
            model_id: The Bedrock model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')
                      Use 'LambdaHook' to invoke a custom Lambda function instead.
            system_prompt: The system prompt as string or list of content objects
            content: The content for the user message (can include text and images)
            temperature: The temperature parameter for model inference (float or string)
            top_k: Optional top_k parameter (float or string)
            top_p: Optional top_p parameter (float or string)
            max_tokens: Optional max_tokens parameter (int or string)
            max_retries: Optional override for the instance's max_retries setting
            service_tier: Optional service tier (priority, standard, flex)
            model_lambda_hook_arn: Lambda function ARN (required when model_id is 'LambdaHook')
            reasoning_effort: Reasoning effort for OpenAI Responses models
                (minimal/low/medium/high). Ignored by other model families.
            tool_config: Optional Converse ``toolConfig`` dict, passed through
                verbatim, e.g.::

                    {"tools": [{"toolSpec": {
                        "name": "extract_fields",
                        "description": "...",
                        "inputSchema": {"json": {...JSON Schema...}},
                    }}]}

                Omitted from the request entirely when None (the default), so the
                request is byte-identical to a call that never passed it.
            tool_choice: Optional Converse ``toolChoice`` dict, e.g.
                ``{"tool": {"name": "extract_fields"}}``, ``{"any": {}}`` or
                ``{"auto": {}}``. This is a *member of* ``toolConfig`` in the
                Converse API; it is accepted separately here purely as a
                convenience and is merged into a shallow copy of ``tool_config``
                (the caller's dict is never mutated). If ``tool_config`` already
                carries a ``toolChoice``, this parameter wins and a warning is
                logged. Requires ``tool_config``: passing ``tool_choice`` alone
                raises ValueError, because Converse rejects a ``toolChoice``
                with no tools.

        Returns:
            Response object with metering information (same format for both Bedrock and Lambda)

        Raises:
            ValueError: If ``tool_config``/``tool_choice`` is passed for a model
                that does not reach the Converse API — the ``LambdaHook``
                sentinel or an OpenAI GPT-5.x Responses model. The schema is
                never silently dropped; use ``supports_tool_config(model_id)``
                to branch beforehand if you need a text fallback.

        Tool-use notes (read before adding a caller):
            * **Response may still be text.** Even with a forced ``toolChoice``,
              treat a text answer as possible: use
              ``extract_tool_use_from_response()`` and fall back to
              ``extract_text_from_response()`` when it returns None.
            * **No strict mode exists on bedrock-runtime.** ``toolSpec.strict``,
              ``output_config.format`` and ``response_format`` are all rejected
              ("Extra inputs are not permitted") on both Converse and
              InvokeModel. A forced ``toolChoice`` is best-effort constrained
              decoding, not a grammar guarantee — validate the result.
            * **Property names must be sanitized by the caller.** Converse
              rejects *top-level* ``inputSchema.json.properties`` keys that do
              not match ``^[a-zA-Z0-9_.-]{1,64}$`` — so a key like
              ``"Account Number"`` fails with
              ``Property keys should match pattern ...``. Several shipped class
              schemas contain such names. This method does NOT sanitize; the
              caller must rename and map back (a shared sanitizer is planned).
              Sanitize recursively even though only the top level is enforced
              today.
            * **Keep the toolConfig deterministic per class.** Tools render
              *before* ``system`` in the prompt-cache prefix, so changing the
              tool schema invalidates the entire cached prefix (system prompt
              included). A per-class, stable ``toolConfig`` caches fine; a
              per-request one destroys cache hits silently.
        """
        # Resolve tool-use plumbing BEFORE any routing, so that a caller who
        # passes a schema to a non-Converse route fails loudly here instead of
        # getting a plausible-looking free-text answer with no schema applied.
        effective_tool_config = self._resolve_tool_config(
            model_id=model_id, tool_config=tool_config, tool_choice=tool_choice
        )

        # Route to Lambda hook if model_id is LambdaHook
        if model_id == LAMBDA_HOOK_MODEL_ID:
            return self._invoke_lambda_hook(
                lambda_arn=model_lambda_hook_arn,
                system_prompt=system_prompt,
                content=content,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                max_tokens=max_tokens,
                max_retries=max_retries,
                context=context,
            )

        # Use instance max_retries if not overridden
        effective_max_retries = (
            max_retries if max_retries is not None else self.max_retries
        )

        # Route OpenAI GPT-5.x models to the bedrock-mantle Responses API. These
        # models do NOT support the Converse API used below; they are served via
        # an OpenAI-compatible REST endpoint. The backend translates the same
        # (system_prompt, content) inputs and returns the identical
        # {"response": ..., "metering": ...} structure, so callers are unaffected.
        # Prompt caching is handled inside openai_responses.py: GPT-5.4/5.5 cache
        # automatically (markers stripped); GPT-5.6 translates <<CACHEPOINT>>
        # markers into explicit prompt_cache_breakpoint fields. (This is separate
        # from CACHEPOINT_SUPPORTED_MODELS, which governs the Converse path only.)
        if is_openai_responses_model(model_id):
            return invoke_responses_api(
                client=self,
                model_id=model_id,
                system_prompt=system_prompt,
                content=content,
                max_tokens=max_tokens,
                max_retries=effective_max_retries,
                context=context,
                reasoning_effort=reasoning_effort,
            )

        # Track total requests
        self._put_metric("BedrockRequestsTotal", 1)

        # Format system prompt if needed
        if isinstance(system_prompt, str):
            formatted_system_prompt = [{"text": system_prompt}]
        else:
            formatted_system_prompt = system_prompt

        # Check for cachePoint tags in content
        has_cachepoint_tags = any(
            "text" in item
            and isinstance(item["text"], str)
            and "<<CACHEPOINT>>" in item["text"]
            for item in content
        )

        if has_cachepoint_tags:
            if self._is_model_cachepoint_supported(model_id):
                # Process content for cachePoint tags with supported model
                processed_content = self._preprocess_content_for_cachepoint(content)
                logger.info(
                    f"Applied cachePoint processing for supported model: {model_id}"
                )
            else:
                # For unsupported models, just remove the <<CACHEPOINT>> tags but keep content intact
                processed_content = []
                for item in content:
                    if (
                        "text" in item
                        and isinstance(item["text"], str)
                        and "<<CACHEPOINT>>" in item["text"]
                    ):
                        # Remove the cachepoint tags but keep the text
                        clean_text = item["text"].replace("<<CACHEPOINT>>", "")
                        processed_content.append({"text": clean_text})
                        logger.warning(
                            f"Removed <<CACHEPOINT>> tags for unsupported model: {model_id}. "
                            "CachePoint is supported for standard cross-region inference profiles "
                            "and application inference profiles that wrap supported foundation models."
                        )
                    else:
                        # Pass through unchanged
                        processed_content.append(item)
        else:
            # No cachepoint tags, use content as is
            processed_content = content

        # Build message
        message = {"role": "user", "content": processed_content}
        messages = [message]

        # Convert temperature to float if it's a string
        if isinstance(temperature, str):
            try:
                temperature = float(temperature)
            except ValueError:
                logger.warning(
                    f"Failed to convert temperature value '{temperature}' to float. Using default 0.0"
                )
                temperature = 0.0

        # Claude 4.7+ and xAI Grok don't support temperature, top_k, or top_p:
        # deprecated on Claude, hard-rejected with a 400 on Grok.
        is_claude_4_7 = _is_claude_4_7_model(model_id)
        if strips_sampling_params(model_id):
            inference_config = {}
            logger.info(
                f"Skipping temperature/top_p for {model_id} "
                "(these parameters are rejected or deprecated for this model)"
            )
        else:
            # Initialize inference config with temperature
            inference_config = {"temperature": temperature}

            # Handle top_p parameter - use top_p if it's positive, otherwise use temperature
            # Some models don't allow both temperature and top_p to be specified
            # This allows temperature=0.0 for deterministic output (recommended by Anthropic)
            if top_p is not None:
                # Convert top_p to float if it's a string
                if isinstance(top_p, str):
                    try:
                        top_p = float(top_p)
                    except ValueError:
                        logger.warning(
                            f"Failed to convert top_p value '{top_p}' to float. Not using top_p."
                        )
                        top_p = None

                # Only use top_p if it's positive (greater than 0)
                if top_p is not None and top_p > 0:
                    inference_config["topP"] = top_p
                    # Remove temperature when using top_p to avoid conflicts
                    del inference_config["temperature"]
                    logger.debug(
                        f"Using top_p={top_p} for inference (temperature ignored)"
                    )
                else:
                    logger.debug(
                        f"Using temperature={temperature} for inference (top_p is 0 or None)"
                    )

        # Handle max_tokens parameter
        if max_tokens is not None:
            # Convert max_tokens to int if it's a string
            if isinstance(max_tokens, str):
                try:
                    max_tokens = int(max_tokens)
                except ValueError:
                    logger.warning(
                        f"Failed to convert max_tokens value '{max_tokens}' to int. Not using max_tokens."
                    )
                    max_tokens = None

        # Always request the model's maximum output when no explicit value is
        # given. Bedrock's default-when-omitted is a small truncating value
        # (measured 4096 for Claude, 2000 for Nova) — there is no "use model max"
        # sentinel in the Converse API, so we set it explicitly from the single
        # source of truth (model_config_limits.yaml). Completeness matters more
        # than an output cap for this accelerator's extraction/confidence passes.
        if max_tokens is None:
            try:
                max_tokens = get_model_max_output_tokens(model_id)
            except (ValueError, FileNotFoundError):
                # Unknown model, or limits file unavailable in this runtime: leave
                # max_tokens unset. If Bedrock truncates or rejects, the over-limit
                # retry below recovers the real cap.
                logger.warning(
                    "Could not resolve maxTokens for %s (missing entry or "
                    "model_config_limits.yaml unavailable); Bedrock default "
                    "applies.",
                    model_id,
                )

        # Place maxTokens on the carrier the model actually honors.
        #
        # ``inferenceConfig.maxTokens`` is the Converse-standard field and is the
        # DEFAULT for every family, so a newly added model caps correctly without
        # a code change. Claude is the exception: it takes the value via
        # ``additionalModelRequestFields.max_tokens`` (set further below), which
        # is long-verified behavior and left untouched.
        #
        # Getting this wrong fails OPEN, not loudly: Grok accepts
        # ``additionalModelRequestFields.max_tokens`` and silently ignores it
        # (verified live 2026-09-02 — a request capped at 100 there returned 135
        # output tokens), so an unsupported carrier means uncapped output rather
        # than an error.
        if max_tokens is not None and "anthropic" not in model_id.lower():
            inference_config["maxTokens"] = max_tokens

        # Add additional model fields if needed
        additional_model_fields = {}

        # Handle top_k parameter
        if top_k is not None:
            # Convert top_k to float if it's a string
            if isinstance(top_k, str):
                try:
                    top_k = float(top_k)
                except ValueError:
                    logger.warning(
                        f"Failed to convert top_k value '{top_k}' to float. Not using top_k."
                    )
                    top_k = None

        # Handle model-specific parameters
        if "anthropic" in model_id.lower():
            # Add parameters to additionalModelRequestFields for Claude (snake_case)
            # Skip top_k for Claude 4.7+ models (deprecated parameter)
            if top_k is not None and not is_claude_4_7:
                additional_model_fields["top_k"] = int(top_k)
            elif top_k is not None and is_claude_4_7:
                logger.info(
                    f"Skipping top_k for Claude 4.7+ model: {model_id} "
                    "(this parameter is deprecated for this model)"
                )

            if max_tokens is not None:
                additional_model_fields["max_tokens"] = max_tokens

            # Reasoning effort (output_config.effort) for effort-capable Claude
            # models. Controls thinking depth / output-token spend
            # (low|medium|high|xhigh|max). Ignored/omitted for models that don't
            # support it (Sonnet 4.5, Haiku 4.5) to avoid a 400.
            if reasoning_effort and is_claude_effort_model(model_id):
                effort = str(reasoning_effort).lower().strip()
                if effort in CLAUDE_EFFORT_LEVELS:
                    additional_model_fields["output_config"] = {"effort": effort}
                    logger.info("Using reasoning effort '%s' for %s", effort, model_id)
                else:
                    logger.warning(
                        "Ignoring unsupported Claude reasoning effort '%s' for %s "
                        "(valid: %s)",
                        reasoning_effort,
                        model_id,
                        ", ".join(CLAUDE_EFFORT_LEVELS),
                    )
            elif reasoning_effort and not is_claude_effort_model(model_id):
                logger.debug(
                    "Model %s does not support reasoning effort; ignoring '%s'",
                    model_id,
                    reasoning_effort,
                )

        # Handle Nova-specific parameters
        elif "amazon" in model_id.lower():
            # For Nova models, topK should be in additionalModelRequestFields.inferenceConfig
            if top_k is not None:
                if additional_model_fields is None:
                    additional_model_fields = {}
                if "inferenceConfig" not in additional_model_fields:
                    additional_model_fields["inferenceConfig"] = {}
                additional_model_fields["inferenceConfig"]["topK"] = int(top_k)

        # Handle xAI Grok-specific parameters
        elif is_grok_model(model_id):
            # Reasoning is always on for Grok; effort rides in
            # additionalModelRequestFields.reasoning.effort. Claude's
            # output_config.effort is silently ignored here, and so is any other
            # unrecognized key — so an unsupported effort value must be dropped
            # rather than passed through, or the request quietly loses the
            # setting instead of failing. top_k is deliberately not forwarded
            # (Grok rejects the sampling group).
            if reasoning_effort:
                effort = str(reasoning_effort).lower().strip()
                if effort in GROK_EFFORT_LEVELS:
                    additional_model_fields["reasoning"] = {"effort": effort}
                    logger.info("Using reasoning effort '%s' for %s", effort, model_id)
                else:
                    logger.warning(
                        "Ignoring unsupported Grok reasoning effort '%s' for %s "
                        "(valid: %s)",
                        reasoning_effort,
                        model_id,
                        ", ".join(GROK_EFFORT_LEVELS),
                    )

        # Add 1M context headers if needed
        use_model_id = model_id
        if model_id and model_id.endswith(":1m"):
            use_model_id = model_id[:-3]  # Remove ':1m'
            if additional_model_fields is None:
                additional_model_fields = {}
            additional_model_fields["anthropic_beta"] = ["context-1m-2025-08-07"]

        # Parse model ID to extract service tier from suffix
        base_model_id, tier_from_suffix = parse_model_id(use_model_id)

        # Use tier from model ID suffix if present, otherwise use service_tier parameter
        effective_service_tier = tier_from_suffix or service_tier

        # Update use_model_id to base model ID (without tier suffix)
        if tier_from_suffix:
            use_model_id = base_model_id
            logger.info(
                f"Extracted service tier '{tier_from_suffix}' from model ID. Using base model ID: {base_model_id}"
            )

        # If no additional model fields were added, set to None
        if not additional_model_fields:
            additional_model_fields = None

        # Normalize and validate service tier
        normalized_service_tier = None
        if effective_service_tier:
            tier_lower = effective_service_tier.lower().strip()
            if tier_lower in ["priority", "flex"]:
                normalized_service_tier = tier_lower
            elif tier_lower in ["standard", "default"]:
                normalized_service_tier = "default"
            else:
                logger.warning(
                    f"Invalid service_tier value '{effective_service_tier}'. "
                    f"Valid values are: priority, standard, flex. Using default tier."
                )

        # Get guardrail configuration if available
        guardrail_config = self.get_guardrail_config()

        # Build converse parameters
        converse_params: Dict[str, Any] = {
            "modelId": use_model_id,
            "messages": messages,
            "system": formatted_system_prompt,
            "inferenceConfig": inference_config,
            "additionalModelRequestFields": additional_model_fields,
        }

        # Add the tool configuration if one was supplied. Left absent otherwise,
        # so requests without tool use are unchanged.
        if effective_tool_config is not None:
            converse_params["toolConfig"] = effective_tool_config

        # Add service tier if specified
        if normalized_service_tier:
            converse_params["serviceTier"] = {"type": normalized_service_tier}
            logger.info(f"Using service tier: {normalized_service_tier}")

        # Add guardrail config if available
        if guardrail_config:
            converse_params["guardrailConfig"] = guardrail_config

        # Start timing the entire request
        request_start_time = time.time()

        # Call the recursive retry function
        result = self._invoke_with_retry(
            model_id=model_id,
            converse_params=converse_params,
            retry_count=0,
            max_retries=effective_max_retries,
            request_start_time=request_start_time,
            context=context,
        )

        return result

    @staticmethod
    def _apply_max_tokens_limit(converse_params: Dict[str, Any], limit: int) -> bool:
        """Clamp the request's maxTokens to `limit` in place, if it exceeds it.

        Handles both carriers: the Converse-standard ``inferenceConfig.maxTokens``
        (Nova, xAI Grok, and any other non-Claude family) and Claude's
        ``additionalModelRequestFields.max_tokens``. Returns True if a value was
        actually lowered (so a retry is warranted), False otherwise — the False
        case prevents an infinite retry when no maxTokens was set or it's already
        at/below the limit.
        """
        changed = False
        inference_config = converse_params.get("inferenceConfig")
        if isinstance(inference_config, dict):
            current = inference_config.get("maxTokens")
            if isinstance(current, int) and current > limit:
                inference_config["maxTokens"] = limit
                changed = True
        amrf = converse_params.get("additionalModelRequestFields")
        if isinstance(amrf, dict):
            current = amrf.get("max_tokens")
            if isinstance(current, int) and current > limit:
                amrf["max_tokens"] = limit
                changed = True
        return changed

    def _invoke_with_retry(
        self,
        model_id: str,
        converse_params: Dict[str, Any],
        retry_count: int,
        max_retries: int,
        request_start_time: float,
        last_exception: Optional[Exception] = None,
        context: str = "Unspecified",
    ) -> Dict[str, Any]:
        """
        Recursive helper method to handle retries for Bedrock invocation.

        Args:
            converse_params: Parameters for the Bedrock converse API call
            retry_count: Current retry attempt (0-based)
            max_retries: Maximum number of retry attempts
            request_start_time: Time when the original request started
            last_exception: The last exception encountered (for final error reporting)

        Returns:
            Bedrock response object with metering information

        Raises:
            Exception: The last exception encountered if max retries are exceeded
        """
        try:
            # Create a copy of the messages to sanitize for logging
            sanitized_params = copy.deepcopy(converse_params)
            if "messages" in sanitized_params:
                sanitized_params["messages"] = self._sanitize_messages_for_logging(
                    sanitized_params["messages"]
                )

            # Log detailed request parameters
            logger.info(f"Bedrock request attempt {retry_count + 1}/{max_retries}:")
            logger.info(f"  - model: {converse_params['modelId']}")
            logger.info(f"  - inferenceConfig: {converse_params['inferenceConfig']}")
            logger.info(f"  - system: {converse_params['system']}")
            logger.info(f"  - messages: {sanitized_params['messages']}")
            logger.info(
                f"  - additionalModelRequestFields: {converse_params['additionalModelRequestFields']}"
            )

            # Log the tool configuration when tool use is in play. Only emitted
            # for tool-enabled calls, so existing log output is unchanged.
            if "toolConfig" in converse_params:
                logger.info(f"  - toolConfig: {converse_params['toolConfig']}")

            # Log guardrail usage if configured
            if "guardrailConfig" in converse_params:
                logger.debug(
                    f"  - guardrailConfig: {converse_params['guardrailConfig']}"
                )

            # Start timing this attempt
            attempt_start_time = time.time()

            # Make the API call
            response = self.client.converse(**converse_params)

            # Calculate duration
            duration = time.time() - attempt_start_time

            # Log response details, but sanitize large content
            sanitized_response = self._sanitize_response_for_logging(response)
            logger.info(
                f"Bedrock request successful after {retry_count + 1} attempts. Duration: {duration:.2f}s"
            )
            logger.debug(f"Response: {sanitized_response}")
            logger.info(f"Token Usage: {response.get('usage')}")
            # Track successful requests and latency
            self._put_metric("BedrockRequestsSucceeded", 1)
            self._put_metric("BedrockRequestLatency", duration * 1000, "Milliseconds")
            if retry_count > 0:
                self._put_metric("BedrockRetrySuccess", 1)

            # Track token usage
            if "usage" in response:
                inputTokens = response["usage"].get("inputTokens", 0)
                outputTokens = response["usage"].get("outputTokens", 0)
                total_tokens = response["usage"].get("totalTokens", 0)
                cacheReadInputTokens = response["usage"].get("cacheReadInputTokens", 0)
                cacheWriteInputTokens = response["usage"].get(
                    "cacheWriteInputTokens", 0
                )
                self._put_metric("InputTokens", inputTokens)
                self._put_metric("OutputTokens", outputTokens)
                self._put_metric("TotalTokens", total_tokens)
                self._put_metric("CacheReadInputTokens", cacheReadInputTokens)
                self._put_metric("CacheWriteInputTokens", cacheWriteInputTokens)

            # Calculate total duration
            total_duration = time.time() - request_start_time
            self._put_metric(
                "BedrockTotalLatency", total_duration * 1000, "Milliseconds"
            )

            # Create metering data
            usage = response.get("usage", {})
            response_with_metering = {
                "response": response,
                "metering": {f"{context}/bedrock/{model_id}": {**usage, "requests": 1}},
            }

            return response_with_metering

        except ClientError as e:
            # Handle boto3/botocore client errors (have response structure)
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]

            retryable_errors = [
                "ThrottlingException",
                "ServiceQuotaExceededException",
                "RequestLimitExceeded",
                "TooManyRequestsException",
                "ServiceUnavailableException",
                "ModelErrorException",
                "InternalServerException",
                "InternalServerError",
                "RequestTimeout",
                "RequestTimeoutException",
            ]

            if error_code in retryable_errors:
                self._put_metric("BedrockThrottles", 1)

                # Emit circuit-breaker specific metrics by error category.
                # BedrockThrottling is a combined generation+embedding signal
                # that feeds BedrockServiceOutageAlarm (the circuit breaker
                # trigger). For per-path counts use BedrockThrottles
                # (generation, above) or BedrockEmbeddingThrottles (embedding).
                if error_code == "ServiceUnavailableException":
                    self._put_metric("BedrockServiceUnavailable", 1)
                elif error_code in (
                    "ThrottlingException",
                    "TooManyRequestsException",
                    "RequestLimitExceeded",
                ):
                    self._put_metric("BedrockThrottling", 1)
                elif error_code == "ServiceQuotaExceededException":
                    self._put_metric("BedrockQuotaLimit", 1)

                # Check if we've reached max retries
                if retry_count >= max_retries:
                    logger.error(
                        f"Max retries ({max_retries}) exceeded. Last error: {error_message}"
                    )
                    self._put_metric("BedrockRequestsFailed", 1)
                    self._put_metric("BedrockMaxRetriesExceeded", 1)
                    raise

                # Calculate backoff time
                backoff = self._calculate_backoff(retry_count)
                logger.warning(
                    f"Bedrock throttling occurred (attempt {retry_count + 1}/{max_retries}). "
                    f"Error: {error_message}. "
                    f"Backing off for {backoff:.2f}s"
                )

                # Sleep for backoff period
                time.sleep(backoff)

                # Recursive call with incremented retry count
                return self._invoke_with_retry(
                    model_id=model_id,
                    converse_params=converse_params,
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    request_start_time=request_start_time,
                    last_exception=e,
                    context=context,
                )
            else:
                # Self-heal an over-limit maxTokens request. There is no AWS API
                # for the per-model output cap, so when model_config_limits.yaml
                # is stale/missing for a model we requested too many tokens for,
                # Bedrock's ValidationException states the true limit — parse it,
                # clamp, and retry once. This keeps a newly-added model working
                # before its YAML entry is corrected.
                discovered_limit = (
                    parse_max_tokens_limit_from_error(error_message)
                    if error_code == "ValidationException"
                    else None
                )
                if discovered_limit is not None and self._apply_max_tokens_limit(
                    converse_params, discovered_limit
                ):
                    logger.warning(
                        "maxTokens exceeded model limit for %s; Bedrock reports "
                        "%d. Clamping and retrying. Update model_config_limits.yaml.",
                        model_id,
                        discovered_limit,
                    )
                    return self._invoke_with_retry(
                        model_id=model_id,
                        converse_params=converse_params,
                        retry_count=retry_count,
                        max_retries=max_retries,
                        request_start_time=request_start_time,
                        last_exception=e,
                        context=context,
                    )

                # Include model_id: errors like ResourceNotFoundException
                # ("model version has reached the end of its life") do not name
                # the model, so logging it here makes the offending model
                # explicit in the function logs for troubleshooting.
                logger.error(
                    f"Non-retryable Bedrock error for model {model_id}: "
                    f"{error_code} - {error_message}"
                )
                self._put_metric("BedrockRequestsFailed", 1)
                self._put_metric("BedrockNonRetryableErrors", 1)
                raise

        except (
            ReadTimeoutError,
            ConnectTimeoutError,
            EndpointConnectionError,
            Urllib3ReadTimeoutError,
            RequestsReadTimeout,
            RequestsConnectTimeout,
        ) as e:
            # Handle timeout and connection errors (these are retryable)
            error_message = str(e)

            self._put_metric("BedrockTimeouts", 1)

            # Check if we've reached max retries
            if retry_count >= max_retries:
                logger.error(
                    f"Max retries ({max_retries}) exceeded. Last timeout error: {error_message}"
                )
                self._put_metric("BedrockRequestsFailed", 1)
                self._put_metric("BedrockMaxRetriesExceeded", 1)
                raise

            # Calculate backoff time
            backoff = self._calculate_backoff(retry_count)
            logger.warning(
                f"Bedrock timeout occurred (attempt {retry_count + 1}/{max_retries}). "
                f"Error: {error_message}. "
                f"Backing off for {backoff:.2f}s"
            )

            # Sleep for backoff period
            time.sleep(backoff)

            # Recursive call with incremented retry count
            return self._invoke_with_retry(
                model_id=model_id,
                converse_params=converse_params,
                retry_count=retry_count + 1,
                max_retries=max_retries,
                request_start_time=request_start_time,
                last_exception=e,
                context=context,
            )

        except Exception as e:
            # Handle unexpected errors (not retryable)
            error_message = str(e)
            logger.error(f"Unexpected Bedrock error: {error_message}", exc_info=True)
            self._put_metric("BedrockRequestsFailed", 1)
            self._put_metric("BedrockUnexpectedErrors", 1)
            raise

    def get_guardrail_config(self) -> Optional[Dict[str, str]]:
        """
        Get guardrail configuration from environment if available.

        Returns:
            Optional guardrail configuration dict with id and version
        """
        guardrail_env = os.environ.get("GUARDRAIL_ID_AND_VERSION", "")
        if not guardrail_env:
            return None

        try:
            guardrail_id, guardrail_version = guardrail_env.split(":")
            if guardrail_id and guardrail_version:
                logger.debug(
                    f"Using Bedrock Guardrail ID: {guardrail_id}, Version: {guardrail_version}"
                )
                return {
                    "guardrailIdentifier": guardrail_id,
                    "guardrailVersion": guardrail_version,
                    "trace": "enabled",  # Enable tracing for guardrail violations
                }
        except ValueError:
            logger.warning(
                f"Invalid GUARDRAIL_ID_AND_VERSION format: {guardrail_env}. Expected format: 'id:version'"
            )

        return None

    def generate_embedding(
        self,
        text: Optional[str] = None,
        model_id: str = "amazon.titan-embed-text-v1",
        max_retries: Optional[int] = None,
        image_bytes: Optional[bytes] = None,
        input_type: Optional[str] = "search_document",
    ) -> List[float]:
        """
        Generate an embedding vector for text and/or image using Amazon Bedrock.

        Supports multiple embedding models:
        - Amazon Titan Embed Text (text only)
        - Amazon Titan Multimodal Embedding (text + image)
        - Cohere Embed v3/v4 (text + image, multimodal)

        Args:
            text: The text to generate embeddings for (optional if image_bytes provided)
            model_id: The embedding model ID to use (default: amazon.titan-embed-text-v1)
            max_retries: Optional override for the instance's max_retries setting
            image_bytes: Optional image bytes for multimodal embedding models
            input_type: Input type for Cohere models (search_document, search_query,
                       classification, clustering). Defaults to search_document.

        Returns:
            List of floats representing the embedding vector
        """
        if not text and image_bytes is None:
            # Return an empty vector for empty input
            return []

        # Use instance max_retries if not overridden
        effective_max_retries = (
            max_retries if max_retries is not None else self.max_retries
        )

        # Track total embedding requests
        self._put_metric("BedrockEmbeddingRequestsTotal", 1)

        # Normalize whitespace if text provided
        normalized_text = " ".join(text.split()) if text else None

        # Prepare the request body based on the model
        request_body = self._build_embedding_request_body(
            model_id=model_id,
            text=normalized_text,
            image_bytes=image_bytes,
            input_type=input_type,
        )

        # Call the recursive embedding function
        return self._generate_embedding_with_retry(
            model_id=model_id,
            request_body=request_body,
            normalized_text=normalized_text or "(image-only)",
            retry_count=0,
            max_retries=effective_max_retries,
        )

    def generate_embeddings_batch(
        self,
        items: List[Dict[str, Any]],
        model_id: str = "amazon.titan-embed-text-v1",
        max_retries: Optional[int] = None,
        max_concurrent: int = 5,
        input_type: Optional[str] = "search_document",
        progress_callback: Optional[Any] = None,
    ) -> List[Optional[List[float]]]:
        """
        Generate embeddings for a batch of items with concurrency control.

        Each item in the batch can contain text, image_bytes, or both.

        Args:
            items: List of dicts with optional 'text' and 'image_bytes' keys
            model_id: The embedding model ID to use
            max_retries: Optional override for retry count
            max_concurrent: Maximum concurrent embedding requests
            input_type: Input type for Cohere models
            progress_callback: Optional callable(completed, total) for progress updates

        Returns:
            List of embedding vectors (None for failed items)
        """
        import concurrent.futures

        total = len(items)
        results: List[Optional[List[float]]] = [None] * total
        completed = 0

        def _embed_single(index: int, item: Dict[str, Any]) -> tuple:
            """Embed a single item and return (index, embedding)."""
            try:
                embedding = self.generate_embedding(
                    text=item.get("text"),
                    model_id=model_id,
                    max_retries=max_retries,
                    image_bytes=item.get("image_bytes"),
                    input_type=input_type,
                )
                return (index, embedding)
            except Exception as e:
                logger.warning(f"Failed to generate embedding for item {index}: {e}")
                return (index, None)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrent
        ) as executor:
            futures = {
                executor.submit(_embed_single, i, item): i
                for i, item in enumerate(items)
            }
            for future in concurrent.futures.as_completed(futures):
                idx, embedding = future.result()
                results[idx] = embedding
                completed += 1
                if progress_callback:
                    try:
                        progress_callback(completed, total)
                    except Exception:
                        pass

        return results

    def _build_embedding_request_body(
        self,
        model_id: str,
        text: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        input_type: Optional[str] = "search_document",
    ) -> str:
        """
        Build the JSON request body for an embedding model.

        Supports:
        - Amazon Titan Embed Text v1/v2: text-only via inputText
        - Amazon Titan Multimodal Embedding: text + image via inputText/inputImage
        - Cohere Embed v3/v4: text + image via texts/images arrays

        Args:
            model_id: The embedding model ID
            text: Optional text input
            image_bytes: Optional image bytes
            input_type: Input type for Cohere models

        Returns:
            JSON string for the request body
        """
        import base64

        model_lower = model_id.lower()

        if "cohere" in model_lower:
            # Cohere Embed v3/v4 format
            body: Dict[str, Any] = {
                "input_type": input_type or "search_document",
            }
            # Detect v4 models (embed-v4) vs v3 (embed-english-v3, embed-multilingual-v3)
            is_v4 = "embed-v4" in model_lower
            if is_v4:
                # Cohere v4 requires explicit embedding type and supports output_dimension
                body["embedding_types"] = ["float"]
                body["output_dimension"] = 1024
            if text:
                body["texts"] = [text]
            if image_bytes is not None:
                img_b64 = base64.b64encode(image_bytes).decode("utf-8")
                if is_v4:
                    # Cohere v4 requires data URI format for images
                    body["images"] = [f"data:image/png;base64,{img_b64}"]
                else:
                    # Cohere v3 uses raw base64
                    body["images"] = [img_b64]
            return json.dumps(body)

        elif "titan-embed-image" in model_lower or (
            "titan-embed" in model_lower and image_bytes is not None
        ):
            # Amazon Titan Multimodal Embedding G1 format
            body = {}
            if text:
                body["inputText"] = text
            if image_bytes is not None:
                img_b64 = base64.b64encode(image_bytes).decode("utf-8")
                body["inputImage"] = img_b64
            return json.dumps(body)

        elif "titan-embed" in model_lower:
            # Amazon Titan Embed Text v1/v2 (text-only)
            return json.dumps({"inputText": text or ""})

        else:
            # Default format
            body = {}
            if text:
                body["text"] = text
            return json.dumps(body)

    def _generate_embedding_with_retry(
        self,
        model_id: str,
        request_body: str,
        normalized_text: str,
        retry_count: int,
        max_retries: int,
        last_exception: Optional[Exception] = None,
    ) -> List[float]:
        """
        Recursive helper method to handle retries for embedding generation.

        Args:
            model_id: The embedding model ID
            request_body: JSON request body for the API call
            normalized_text: Normalized input text (for logging)
            retry_count: Current retry attempt (0-based)
            max_retries: Maximum number of retry attempts
            last_exception: The last exception encountered (for final error reporting)

        Returns:
            List of floats representing the embedding vector

        Raises:
            Exception: The last exception encountered if max retries are exceeded
        """
        try:
            logger.info(
                f"Bedrock embedding request attempt {retry_count + 1}/{max_retries}:"
            )
            logger.debug(f"  - model: {model_id}")
            logger.debug(f"  - input text length: {len(normalized_text)} characters")

            attempt_start_time = time.time()
            response = self.client.invoke_model(
                modelId=model_id,
                contentType="application/json",
                accept="application/json",
                body=request_body,
            )
            duration = time.time() - attempt_start_time

            # Extract the embedding vector from response
            response_body = json.loads(response["body"].read())

            # Handle different response formats based on the model
            if "amazon.titan-embed" in model_id:
                embedding = response_body.get("embedding", [])
            elif "cohere" in model_id.lower() and "embed-v4" in model_id.lower():
                # Cohere Embed v4 returns {"embeddings": {"float": [[...]]}}
                embeddings_obj = response_body.get("embeddings", {})
                if isinstance(embeddings_obj, dict):
                    float_embeddings = embeddings_obj.get("float", [])
                    embedding = float_embeddings[0] if float_embeddings else []
                else:
                    # Fallback for unexpected format
                    embedding = embeddings_obj[0] if embeddings_obj else []
            else:
                # Default extraction format (Cohere v3 and others)
                embedding = response_body.get("embedding", [])

            # Track successful requests and latency
            self._put_metric("BedrockEmbeddingRequestsSucceeded", 1)
            self._put_metric(
                "BedrockEmbeddingRequestLatency", duration * 1000, "Milliseconds"
            )

            logger.debug(f"Generated embedding with {len(embedding)} dimensions")
            return embedding

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]

            retryable_errors = [
                "ThrottlingException",
                "ServiceQuotaExceededException",
                "RequestLimitExceeded",
                "TooManyRequestsException",
                "ServiceUnavailableException",
                "InternalServerException",
                "InternalServerError",
                "RequestTimeout",
                "ReadTimeout",
                "TimeoutError",
                "RequestTimeoutException",
            ]

            if error_code in retryable_errors:
                self._put_metric("BedrockEmbeddingThrottles", 1)

                # Emit circuit-breaker specific metrics by error category.
                # BedrockThrottling is a combined generation+embedding signal
                # that feeds BedrockServiceOutageAlarm (the circuit breaker
                # trigger). For per-path counts use BedrockEmbeddingThrottles
                # (embedding, above) or BedrockThrottles (generation).
                if error_code == "ServiceUnavailableException":
                    self._put_metric("BedrockServiceUnavailable", 1)
                elif error_code in (
                    "ThrottlingException",
                    "TooManyRequestsException",
                    "RequestLimitExceeded",
                ):
                    self._put_metric("BedrockThrottling", 1)
                elif error_code == "ServiceQuotaExceededException":
                    self._put_metric("BedrockQuotaLimit", 1)

                # Check if we've reached max retries
                if retry_count >= max_retries:
                    logger.error(
                        f"Max retries ({max_retries}) exceeded for embedding. Last error: {error_message}"
                    )
                    self._put_metric("BedrockEmbeddingRequestsFailed", 1)
                    self._put_metric("BedrockEmbeddingMaxRetriesExceeded", 1)
                    raise

                # Calculate backoff time
                backoff = self._calculate_backoff(retry_count)
                logger.warning(
                    f"Bedrock throttling occurred (attempt {retry_count + 1}/{max_retries}). "
                    f"Error: {error_message}. "
                    f"Backing off for {backoff:.2f}s"
                )

                # Sleep for backoff period
                time.sleep(backoff)

                # Recursive call with incremented retry count
                return self._generate_embedding_with_retry(
                    model_id=model_id,
                    request_body=request_body,
                    normalized_text=normalized_text,
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    last_exception=e,
                )
            else:
                logger.error(
                    f"Non-retryable Bedrock error for embedding model {model_id}: "
                    f"{error_code} - {error_message}"
                )
                self._put_metric("BedrockEmbeddingRequestsFailed", 1)
                self._put_metric("BedrockEmbeddingNonRetryableErrors", 1)
                raise

        except Exception as e:
            logger.error(
                f"Unexpected error generating embedding: {str(e)}", exc_info=True
            )
            self._put_metric("BedrockEmbeddingRequestsFailed", 1)
            self._put_metric("BedrockEmbeddingUnexpectedErrors", 1)
            raise

    def extract_text_from_response(self, response: Dict[str, Any]) -> str:
        """
        Extract text from a Bedrock response.

        Args:
            response: Bedrock response object

        Returns:
            Extracted text content
        """
        response_obj = response.get("response", response)
        content = response_obj["output"]["message"].get("content", [])
        if not content or len(content) == 0:
            logger = logging.getLogger(__name__)
            logger.error(
                "LLM returned empty content array",
                extra={"response": response_obj},
            )
            return ""
        # Reasoning models (Claude Sonnet 5 / 4.6+, and any model with extended
        # thinking on) prepend one or more `reasoningContent` blocks BEFORE the
        # answer `text` block, so content[0] is often not the text. Concatenate
        # every `text` block (skipping reasoningContent) so we return the actual
        # answer regardless of block ordering. Falls back to the old behavior for
        # single-block responses.
        text_parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(text_parts)

    def extract_tool_use_from_response(
        self, response: Dict[str, Any], tool_name: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Extract the structured ``toolUse.input`` dict from a Bedrock response.

        Accepts either the metering-wrapped response returned by
        ``invoke_model`` or a raw Converse response, matching
        ``extract_text_from_response``.

        Like that method, this scans **every** content block rather than
        indexing ``content[0]``: reasoning models (Claude Sonnet 5 / 4.6+, and
        any model with extended thinking on) emit one or more
        ``reasoningContent`` blocks BEFORE the answer block, so the ``toolUse``
        block is frequently not first.

        A None return is a normal, expected outcome — a model can accept a
        ``toolConfig`` and still answer in prose (``stopReason`` ``end_turn``
        rather than ``tool_use``), so callers should fall back to
        ``extract_text_from_response()`` and parse/validate from there.

        Args:
            response: Bedrock response object (wrapped or raw).
            tool_name: Optional tool name to match. When given, ``toolUse``
                blocks for other tools are skipped — useful when the request
                declared more than one tool.

        Returns:
            The tool's ``input`` dict, or None if the response contains no
            matching ``toolUse`` block with a dict input.
        """
        response_obj = response.get("response", response)
        try:
            content = response_obj["output"]["message"].get("content", []) or []
        except (KeyError, TypeError):
            logger.warning(
                "Response has no output.message.content; cannot extract toolUse"
            )
            return None

        for item in content:
            if not isinstance(item, dict):
                continue
            tool_use = item.get("toolUse")
            if not isinstance(tool_use, dict):
                continue
            if tool_name is not None and tool_use.get("name") != tool_name:
                continue
            tool_input = tool_use.get("input")
            if isinstance(tool_input, dict):
                return tool_input
            logger.warning(
                "toolUse block for '%s' had a non-dict input (%s); skipping",
                tool_use.get("name"),
                type(tool_input).__name__,
            )

        return None

    def format_prompt(
        self,
        prompt_template: str,
        substitutions: dict[str, str],
        required_placeholders: list[str] | None = None,
    ) -> str:
        """
        Prepare prompt from template by replacing placeholders with values.

        Args:
            prompt_template: The prompt template with placeholders in {PLACEHOLDER} format
            substitutions: Dictionary of placeholder values
            required_placeholders: List of placeholder names that must be present in the template

        Returns:
            String with placeholders replaced by values

        Raises:
            ValueError: If a required placeholder is missing from the template
        """
        # Validate required placeholders if specified
        if required_placeholders:
            missing_placeholders = [
                p for p in required_placeholders if f"{{{p}}}" not in prompt_template
            ]
            if missing_placeholders:
                raise ValueError(
                    f"Prompt template must contain the following placeholders: {', '.join([f'{{{p}}}' for p in missing_placeholders])}"
                )

        # Check if template uses {PLACEHOLDER} format and convert to %(PLACEHOLDER)s for secure replacement
        if any(f"{{{key}}}" in prompt_template for key in substitutions):
            for key in substitutions:
                placeholder = f"{{{key}}}"
                if placeholder in prompt_template:
                    prompt_template = prompt_template.replace(placeholder, f"%({key})s")

        # Apply substitutions using % operator which is safer than .format()
        return prompt_template % substitutions

    def _invoke_lambda_hook(
        self,
        lambda_arn: Optional[str],
        system_prompt: Union[str, List[Dict[str, str]]],
        content: List[Dict[str, Any]],
        temperature: Union[float, str] = 0.0,
        top_k: Optional[Union[float, str]] = None,
        top_p: Optional[Union[float, str]] = None,
        max_tokens: Optional[Union[int, str]] = None,
        max_retries: Optional[int] = None,
        context: str = "Unspecified",
    ) -> Dict[str, Any]:
        """
        Invoke a custom Lambda function instead of Bedrock for LLM inference.

        The Lambda receives a Converse API-compatible payload with images converted
        to S3 references (to avoid Lambda's 6MB payload limit). The Lambda must
        return a Converse API-compatible response.

        Args:
            lambda_arn: ARN of the Lambda function to invoke
            system_prompt: The system prompt as string or list of content objects
            content: The content for the user message (can include text and images)
            temperature: The temperature parameter for model inference
            top_k: Optional top_k parameter
            top_p: Optional top_p parameter
            max_tokens: Optional max_tokens parameter
            max_retries: Optional override for retry count
            context: Context prefix for metering key

        Returns:
            Response object with metering information (same format as Bedrock responses)

        Raises:
            ValueError: If lambda_arn is not provided or invalid
            Exception: If Lambda invocation fails after retries
        """
        if not lambda_arn:
            raise ValueError(
                "model_lambda_hook_arn is required when model is 'LambdaHook'. "
                "Configure the Lambda function ARN in the configuration."
            )

        # Validate Lambda function name starts with GENAIIDP-
        # Extract function name from ARN (last segment after ':function:')
        if ":function:" in lambda_arn:
            func_name_part = lambda_arn.split(":function:")[-1]
            # Handle alias/version suffix (function:name:alias)
            func_name = func_name_part.split(":")[0]
            if not func_name.startswith("GENAIIDP-"):
                raise ValueError(
                    f"Lambda function name must start with 'GENAIIDP-'. "
                    f"Got function name: '{func_name}' from ARN: '{lambda_arn}'"
                )

        self._put_metric("LambdaHookRequestsTotal", 1)

        # Format system prompt
        if isinstance(system_prompt, str):
            formatted_system_prompt = [{"text": system_prompt}]
        else:
            formatted_system_prompt = system_prompt

        # Strip <<CACHEPOINT>> tags from content (not applicable to custom Lambda)
        processed_content = []
        for item in content:
            if (
                "text" in item
                and isinstance(item["text"], str)
                and "<<CACHEPOINT>>" in item["text"]
            ):
                clean_text = item["text"].replace("<<CACHEPOINT>>", "")
                processed_content.append({"text": clean_text})
            else:
                processed_content.append(item)

        # Convert inline image bytes to S3 references to avoid 6MB Lambda payload limit
        processed_content = self._convert_images_to_s3_refs(processed_content)

        # Convert temperature to float
        if isinstance(temperature, str):
            try:
                temperature = float(temperature)
            except ValueError:
                temperature = 0.0

        # Build inference config
        inference_config: Dict[str, Any] = {"temperature": temperature}

        if top_p is not None:
            if isinstance(top_p, str):
                try:
                    top_p = float(top_p)
                except ValueError:
                    top_p = None
            if top_p is not None and top_p > 0:
                inference_config["topP"] = top_p

        if top_k is not None:
            if isinstance(top_k, str):
                try:
                    top_k = float(top_k)
                except ValueError:
                    top_k = None
            if top_k is not None:
                inference_config["topK"] = int(top_k)

        if max_tokens is not None:
            if isinstance(max_tokens, str):
                try:
                    max_tokens = int(max_tokens)
                except ValueError:
                    max_tokens = None
            if max_tokens is not None:
                inference_config["maxTokens"] = max_tokens

        # Build the Converse API-compatible payload for the Lambda
        message = {"role": "user", "content": processed_content}
        lambda_payload = {
            "modelId": LAMBDA_HOOK_MODEL_ID,
            "messages": [message],
            "system": formatted_system_prompt,
            "inferenceConfig": inference_config,
            "context": context,
        }

        # Invoke Lambda with retry logic
        effective_max_retries = (
            max_retries if max_retries is not None else self.max_retries
        )
        request_start_time = time.time()

        return self._invoke_lambda_hook_with_retry(
            lambda_arn=lambda_arn,
            lambda_payload=lambda_payload,
            retry_count=0,
            max_retries=effective_max_retries,
            request_start_time=request_start_time,
            context=context,
        )

    def _convert_images_to_s3_refs(
        self, content: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Convert inline image bytes in content to S3 references.

        This prevents hitting the Lambda 6MB payload limit when documents
        contain multiple page images. Images are uploaded to the working
        bucket under a temp/lambdahook/ prefix.

        Images that already use s3Location references are passed through unchanged.

        Args:
            content: Content list with potential inline image bytes

        Returns:
            Content list with images converted to S3 references
        """
        import base64
        import uuid

        working_bucket = os.environ.get("WORKING_BUCKET")
        if not working_bucket:
            logger.warning(
                "WORKING_BUCKET not set - cannot convert images to S3 refs for LambdaHook. "
                "Images will be sent inline (may hit 6MB Lambda payload limit)."
            )
            return content

        converted_content = []
        image_count = 0

        for item in content:
            if "image" not in item:
                converted_content.append(item)
                continue

            image_data = item["image"]
            source = image_data.get("source", {})
            img_format = image_data.get("format", "jpeg")

            # Skip if already an S3 reference
            if "s3Location" in source:
                converted_content.append(item)
                continue

            # Get the inline bytes
            img_bytes = source.get("bytes")
            if img_bytes is None:
                converted_content.append(item)
                continue

            # Handle base64-encoded strings (from serialized payloads)
            if isinstance(img_bytes, str):
                try:
                    img_bytes = base64.b64decode(img_bytes)
                except Exception:
                    logger.warning(
                        "Failed to decode base64 image data, passing through"
                    )
                    converted_content.append(item)
                    continue

            # Upload to S3
            image_key = f"temp/lambdahook/{uuid.uuid4().hex}.{img_format}"
            try:
                content_type_map = {
                    "jpeg": "image/jpeg",
                    "png": "image/png",
                    "gif": "image/gif",
                    "webp": "image/webp",
                }
                content_type = content_type_map.get(
                    img_format, "application/octet-stream"
                )

                self.s3_client.put_object(
                    Bucket=working_bucket,
                    Key=image_key,
                    Body=img_bytes,
                    ContentType=content_type,
                )

                s3_uri = f"s3://{working_bucket}/{image_key}"
                image_count += 1

                # Replace inline bytes with S3 reference
                s3_location = {"uri": s3_uri}
                # Only include bucketOwner if AWS_ACCOUNT_ID is set (Bedrock requires valid 12-digit ID or omit)
                account_id = os.environ.get("AWS_ACCOUNT_ID", "")
                if account_id and len(account_id) == 12:
                    s3_location["bucketOwner"] = account_id

                converted_content.append(
                    {
                        "image": {
                            "format": img_format,
                            "source": {
                                "s3Location": s3_location,
                            },
                        }
                    }
                )

                logger.debug(f"Uploaded image to S3: {s3_uri} ({len(img_bytes)} bytes)")

            except Exception as e:
                logger.warning(
                    f"Failed to upload image to S3 for LambdaHook: {str(e)}. "
                    "Falling back to inline bytes (may hit payload limit)."
                )
                converted_content.append(item)

        if image_count > 0:
            logger.info(
                f"Converted {image_count} inline image(s) to S3 references for LambdaHook"
            )

        return converted_content

    def _invoke_lambda_hook_with_retry(
        self,
        lambda_arn: str,
        lambda_payload: Dict[str, Any],
        retry_count: int,
        max_retries: int,
        request_start_time: float,
        last_exception: Optional[Exception] = None,
        context: str = "Unspecified",
    ) -> Dict[str, Any]:
        """
        Invoke Lambda hook with retry logic for transient failures.

        Args:
            lambda_arn: ARN of the Lambda function
            lambda_payload: Converse API-compatible payload
            retry_count: Current retry attempt
            max_retries: Maximum retry attempts
            request_start_time: When the original request started
            last_exception: Last exception for error reporting
            context: Context for metering

        Returns:
            Response with metering information

        Raises:
            Exception: If invocation fails after all retries
        """
        try:
            logger.info(
                f"LambdaHook request attempt {retry_count + 1}/{max_retries}: "
                f"ARN={lambda_arn}, context={context}"
            )

            attempt_start_time = time.time()

            # Serialize payload - handle bytes by converting to base64
            payload_json = json.dumps(lambda_payload, default=str)

            # Invoke Lambda synchronously
            response = self.lambda_client.invoke(
                FunctionName=lambda_arn,
                InvocationType="RequestResponse",
                Payload=payload_json.encode("utf-8"),
            )

            duration = time.time() - attempt_start_time

            # Check for Lambda-level errors
            function_error = response.get("FunctionError")

            if function_error:
                error_payload = json.loads(response["Payload"].read().decode("utf-8"))
                error_message = error_payload.get("errorMessage", str(error_payload))
                logger.error(
                    f"LambdaHook function error ({function_error}): {error_message}"
                )

                # Retry on unhandled errors (transient issues)
                if function_error == "Unhandled" and retry_count < max_retries:
                    backoff = self._calculate_backoff(retry_count)
                    logger.warning(
                        f"LambdaHook transient error, retrying in {backoff:.2f}s"
                    )
                    time.sleep(backoff)
                    return self._invoke_lambda_hook_with_retry(
                        lambda_arn=lambda_arn,
                        lambda_payload=lambda_payload,
                        retry_count=retry_count + 1,
                        max_retries=max_retries,
                        request_start_time=request_start_time,
                        context=context,
                    )

                self._put_metric("LambdaHookRequestsFailed", 1)
                raise RuntimeError(f"LambdaHook function error: {error_message}")

            # Parse response payload
            response_payload = json.loads(response["Payload"].read().decode("utf-8"))

            logger.info(
                f"LambdaHook request successful after {retry_count + 1} attempts. "
                f"Duration: {duration:.2f}s"
            )

            # Extract usage/metering from the Lambda response
            usage = response_payload.get("usage", {})
            if not usage:
                # Provide default metering if Lambda doesn't return usage
                usage = {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "totalTokens": 0,
                }

            # Track metrics
            self._put_metric("LambdaHookRequestsSucceeded", 1)
            self._put_metric(
                "LambdaHookRequestLatency", duration * 1000, "Milliseconds"
            )

            total_duration = time.time() - request_start_time
            self._put_metric(
                "LambdaHookTotalLatency", total_duration * 1000, "Milliseconds"
            )

            # Build response in the same format as Bedrock responses
            response_with_metering = {
                "response": response_payload,
                "metering": {
                    f"{context}/lambda_hook/{lambda_arn}": {
                        **usage,
                        "requests": 1,
                    }
                },
            }

            return response_with_metering

        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]

            retryable_errors = [
                "TooManyRequestsException",
                "ServiceException",
                "EC2ThrottledException",
                "ResourceConflictException",
            ]

            if error_code in retryable_errors and retry_count < max_retries:
                self._put_metric("LambdaHookThrottles", 1)
                backoff = self._calculate_backoff(retry_count)
                logger.warning(
                    f"LambdaHook throttled ({error_code}), retrying in {backoff:.2f}s"
                )
                time.sleep(backoff)
                return self._invoke_lambda_hook_with_retry(
                    lambda_arn=lambda_arn,
                    lambda_payload=lambda_payload,
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    request_start_time=request_start_time,
                    last_exception=e,
                    context=context,
                )
            else:
                logger.error(f"LambdaHook error: {error_code} - {error_message}")
                self._put_metric("LambdaHookRequestsFailed", 1)
                raise

        except Exception as e:
            error_message = str(e)
            if "RuntimeError" not in type(e).__name__ and retry_count < max_retries:
                backoff = self._calculate_backoff(retry_count)
                logger.warning(
                    f"LambdaHook unexpected error, retrying in {backoff:.2f}s: {error_message}"
                )
                time.sleep(backoff)
                return self._invoke_lambda_hook_with_retry(
                    lambda_arn=lambda_arn,
                    lambda_payload=lambda_payload,
                    retry_count=retry_count + 1,
                    max_retries=max_retries,
                    request_start_time=request_start_time,
                    last_exception=e,
                    context=context,
                )

            logger.error(f"LambdaHook failed: {error_message}", exc_info=True)
            self._put_metric("LambdaHookRequestsFailed", 1)
            raise

    def _calculate_backoff(self, retry_count: int) -> float:
        """
        Calculate exponential backoff time with jitter.

        Args:
            retry_count: Current retry attempt (0-based)

        Returns:
            Backoff time in seconds
        """
        # Exponential backoff with base of 2
        backoff_seconds = min(self.max_backoff, self.initial_backoff * (2**retry_count))

        # Add jitter (random value between 0 and 1 second)
        jitter = random.random()  # nosec B311 - retry jitter

        return backoff_seconds + jitter

    def _put_metric(
        self, metric_name: str, value: Union[int, float], unit: str = "Count"
    ):
        """
        Publish a metric if metrics are enabled.

        Args:
            metric_name: Name of the metric
            value: Metric value
            unit: Metric unit (default: Count)
        """
        if self.metrics_enabled:
            try:
                from ..metrics import put_metric

                put_metric(metric_name, value, unit)
            except Exception as e:
                logger.warning(f"Failed to publish metric {metric_name}: {str(e)}")

    def _sanitize_messages_for_logging(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Create a copy of messages with image content replaced for logging.

        Args:
            messages: List of message objects for Bedrock API

        Returns:
            Sanitized message objects suitable for logging
        """
        sanitized = copy.deepcopy(messages)

        for message in sanitized:
            if "content" in message and isinstance(message["content"], list):
                for content_item in message["content"]:
                    # Check for image type content
                    if (
                        isinstance(content_item, dict)
                        and content_item.get("type") == "image"
                    ):
                        # Replace actual image data with placeholder
                        if "source" in content_item:
                            content_item["source"] = {"data": "[image_data]"}
                    elif isinstance(content_item, dict) and "image" in content_item:
                        # Handle different image format used by some models
                        content_item["image"] = "[image_data]"
                    elif isinstance(content_item, dict) and "bytes" in content_item:
                        # Handle raw binary format
                        content_item["bytes"] = "[binary_data]"
                    elif isinstance(content_item, dict) and "document" in content_item:
                        # Handle different image format used by some models
                        content_item["document"] = "[document_data]"

        return sanitized

    def _sanitize_response_for_logging(
        self, response: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Create a sanitized copy of the response suitable for logging.

        Args:
            response: Response from Bedrock API

        Returns:
            Sanitized response suitable for logging
        """
        # Create a deep copy to avoid modifying the original
        sanitized = copy.deepcopy(response)

        # For very large responses, limit the content for logging
        if "output" in sanitized and "message" in sanitized["output"]:
            message = sanitized["output"]["message"]
            if "content" in message:
                content = message["content"]

                # Handle list of content items (multimodal responses)
                if isinstance(content, list):
                    for i, item in enumerate(content):
                        if isinstance(item, dict):
                            # Truncate text content if too long
                            if (
                                "text" in item
                                and isinstance(item["text"], str)
                                and len(item["text"]) > 500
                            ):
                                item["text"] = item["text"][:500] + "... [truncated]"
                            # Replace image data with placeholder
                            if "image" in item:
                                item["image"] = "[image_data]"
                # Handle string content
                elif isinstance(content, str) and len(content) > 500:
                    message["content"] = content[:500] + "... [truncated]"

        return sanitized


# Create a default client instance
default_client = BedrockClient()

# Export the default client as invoke_model for backward compatibility
invoke_model = default_client

# Add docstring to the exported function for better IDE support
invoke_model.__doc__ = """
Invoke a Bedrock model with retry logic.

Args:
    model_id: The Bedrock model ID (e.g., 'anthropic.claude-3-sonnet-20240229-v1:0')
    system_prompt: The system prompt as string or list of content objects
    content: The content for the user message (can include text and images)
    temperature: The temperature parameter for model inference (float or string)
    top_k: Optional top_k parameter (float or string)
    top_p: Optional top_p parameter (float or string)
    max_tokens: Optional max_tokens parameter (int or string)
    max_retries: Optional override for the instance's max_retries setting
    context: Context prefix for metering key (default: "Unspecified")
    tool_config: Optional Converse toolConfig dict (see BedrockClient.invoke_model)
    tool_choice: Optional Converse toolChoice dict (see BedrockClient.invoke_model)

Returns:
    Bedrock response object with metering information
"""
