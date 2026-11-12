# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Bedrock guardrail named by ``GUARDRAIL_ID_AND_VERSION``, in each API's shape.

One parser for every model-call path, so a configured guardrail means the same
thing whichever API a component calls: ``BedrockClient`` (``converse``), the
Strands model factory, agentic extraction, the chat processors' direct
``converse_stream`` and the rule translator's direct ``invoke_model``.

Standard library only, so a Lambda test that stubs the rest of ``idp_common`` can
load this module for real.
"""

import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)
GUARDRAIL_ENV_VAR = "GUARDRAIL_ID_AND_VERSION"


def guardrail_id_and_version_from_env() -> Optional[Tuple[str, str]]:
    """Return ``(guardrail_id, version)`` from ``GUARDRAIL_ID_AND_VERSION``, or None.

    The one parser every model-call path shares, so a configured guardrail
    means the same thing whichever API a component calls. The value is
    ``"<id>:<version>"``; an unset or empty variable, an empty half, or a value
    that does not split into exactly two parts yields None (the last with a
    warning), which leaves the call unchanged.
    """
    guardrail_env = os.environ.get(GUARDRAIL_ENV_VAR, "")
    if not guardrail_env:
        return None
    try:
        guardrail_id, guardrail_version = guardrail_env.split(":")
    except ValueError:
        logger.warning(
            f"Invalid {GUARDRAIL_ENV_VAR} format: {guardrail_env}. "
            "Expected format: 'id:version'"
        )
        return None
    if guardrail_id and guardrail_version:
        return guardrail_id, guardrail_version
    return None


def guardrail_config_from_env() -> Optional[Dict[str, str]]:
    """The ``guardrailConfig`` value for ``converse`` / ``converse_stream``, or None.

    The same dict is valid for both operations: ``GuardrailStreamConfiguration``
    adds only the optional ``streamProcessingMode``.
    """
    parsed = guardrail_id_and_version_from_env()
    if parsed is None:
        return None
    guardrail_id, guardrail_version = parsed
    logger.debug(
        f"Using Bedrock Guardrail ID: {guardrail_id}, Version: {guardrail_version}"
    )
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": guardrail_version,
        "trace": "enabled",  # Enable tracing for guardrail violations
    }


def guardrail_invoke_model_params() -> Dict[str, str]:
    """Keyword arguments that apply the guardrail to a raw ``invoke_model`` call.

    ``InvokeModel`` takes the guardrail as top-level members rather than a
    ``guardrailConfig`` dict, and spells its trace enum in upper case. Returns
    an empty dict when no guardrail is configured, so ``**`` splatting it into
    the call leaves the request unchanged.
    """
    parsed = guardrail_id_and_version_from_env()
    if parsed is None:
        return {}
    guardrail_id, guardrail_version = parsed
    return {
        "guardrailIdentifier": guardrail_id,
        "guardrailVersion": guardrail_version,
        "trace": "ENABLED",
    }
