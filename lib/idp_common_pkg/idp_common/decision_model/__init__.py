# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Decision models: small models that return a probability for each option.

See ``README.md`` beside this file, and ``docs/decision-models.md``.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from idp_common.config.models import DECISION_MODEL_INPUTS
from idp_common.decision_model.invoker import (
    DEFAULT_FUNCTION_MEMORY_MB,
    FUNCTION_NAME_PREFIX,
    SUPPORTED_BACKENDS,
    DecisionModelClient,
    DecisionModelError,
    DecisionModelRefused,
    DecisionModelUnavailable,
    DecisionResult,
    function_name,
    merge_metering,
)
from idp_common.decision_model.wire import (
    IMAGE_MAX_SIDE,
    MAX_CHOICE_OPTIONS,
    MAX_REQUEST_IMAGES,
    MAX_SCORE_LEVELS,
    MIN_SCORE_LEVELS,
    QUESTION_TYPES,
    ChoiceAnswer,
    ChoiceQuestion,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    SystemOneRequest,
    SystemOneResponse,
)

#: The values ``ExternalModelConfig.input`` takes; defined once, with the model.
INPUT_MODES = DECISION_MODEL_INPUTS


def resolve_endpoint(configured: Optional[str]) -> str:
    """The configured endpoint. Required: there is no default decision model.

    Raises:
        ValueError: it is empty.
    """
    endpoint = (configured or "").strip()
    if not endpoint:
        raise ValueError(
            "decision_model.endpoint is empty: set it to the decision model's "
            f"Lambda function ARN (the function name must start {FUNCTION_NAME_PREFIX})"
        )
    return endpoint


def client_from_config(
    config: Any, region: Optional[str] = None
) -> DecisionModelClient:
    """A client for an ``ExternalModelConfig``-shaped block."""
    return DecisionModelClient(
        endpoint=resolve_endpoint(getattr(config, "endpoint", "")),
        backend=getattr(config, "backend", "") or "lambda",
        timeout_ms=getattr(config, "timeout_ms", 30000),
        max_concurrency=getattr(config, "max_concurrency", 4),
        function_memory_mb=getattr(
            config, "function_memory_mb", DEFAULT_FUNCTION_MEMORY_MB
        )
        or DEFAULT_FUNCTION_MEMORY_MB,
        region=region,
    )


def encode_image(image_bytes: bytes) -> str:
    """A page image as the base64 a request carries, at the size the model reads.

    Scaled so its longer side is at most :data:`IMAGE_MAX_SIDE`: the model reads
    no more than that, and a Lambda request is capped at 6 MB.
    """
    from idp_common.image import resize_image

    small = resize_image(image_bytes, IMAGE_MAX_SIDE, IMAGE_MAX_SIDE)
    return base64.b64encode(small).decode("ascii")


__all__ = [
    "DEFAULT_FUNCTION_MEMORY_MB",
    "FUNCTION_NAME_PREFIX",
    "IMAGE_MAX_SIDE",
    "INPUT_MODES",
    "MAX_CHOICE_OPTIONS",
    "MAX_REQUEST_IMAGES",
    "MAX_SCORE_LEVELS",
    "MIN_SCORE_LEVELS",
    "QUESTION_TYPES",
    "SUPPORTED_BACKENDS",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "DecisionModelClient",
    "DecisionModelError",
    "DecisionModelRefused",
    "DecisionModelUnavailable",
    "DecisionResult",
    "NoulAnswer",
    "NoulQuestion",
    "ScoreAnswer",
    "ScoreQuestion",
    "SystemOneRequest",
    "SystemOneResponse",
    "client_from_config",
    "encode_image",
    "function_name",
    "merge_metering",
    "resolve_endpoint",
]
