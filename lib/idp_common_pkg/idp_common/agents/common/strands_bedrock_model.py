# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Helper function for creating BedrockModel instances with automatic guardrail support and retry logic.
"""

import logging
import os
from typing import Dict

from botocore.config import Config
from strands.models import BedrockModel

from idp_common.bedrock.guardrail import guardrail_id_and_version_from_env

logger = logging.getLogger(__name__)


def strands_guardrail_kwargs() -> Dict[str, str]:
    """``BedrockModel`` keyword arguments that apply the configured guardrail.

    Parsed by the same function ``BedrockClient`` uses, so a guardrail set in
    ``GUARDRAIL_ID_AND_VERSION`` reaches a Strands model exactly when it reaches
    a ``converse`` call. Returns an empty dict when none is configured, so a
    caller that builds ``BedrockModel(...)`` itself can splat it unconditionally
    and leave the model unchanged in that case.
    """
    parsed = guardrail_id_and_version_from_env()
    if parsed is None:
        return {}
    guardrail_id, guardrail_version = parsed
    return {
        "guardrail_id": guardrail_id,
        "guardrail_version": guardrail_version,
        "guardrail_trace": "enabled",
    }


def create_strands_bedrock_model(
    model_id: str, boto_session=None, **kwargs
) -> BedrockModel:
    """
    Create a BedrockModel with automatic guardrail configuration and retry logic.

    This function creates a BedrockModel with:
    - Automatic retry configuration with exponential backoff (adaptive mode)
    - Guardrail support from environment variables
    - Configurable timeouts for connection and read operations

    The retry configuration handles Bedrock transient errors including:
    - serviceUnavailableException
    - ThrottlingException
    - ModelThrottledException
    - And other transient errors

    Args:
        model_id: The Bedrock model ID to use
        boto_session: Optional boto3 session
        **kwargs: Additional arguments to pass to BedrockModel

    Returns:
        BedrockModel instance with guardrails and retry logic applied

    Environment Variables:
        GUARDRAIL_ID_AND_VERSION: Format "guardrail_id:version" for guardrail configuration
        BEDROCK_MAX_RETRIES: Maximum retry attempts (default: 5)
        BEDROCK_CONNECT_TIMEOUT: Connection timeout in seconds (default: 10)
        BEDROCK_READ_TIMEOUT: Read timeout in seconds (default: 300)
    """
    # Get retry configuration from environment
    max_retries = int(os.environ.get("BEDROCK_MAX_RETRIES", "5"))
    connect_timeout = float(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10.0"))
    read_timeout = float(os.environ.get("BEDROCK_READ_TIMEOUT", "300.0"))

    # Configure boto3 retry behavior with exponential backoff
    # This applies to all Bedrock API calls made through this model
    boto_config = Config(
        retries={
            "max_attempts": max_retries,
            "mode": "adaptive",  # Uses exponential backoff with adaptive retry mode
        },
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )

    logger.debug(
        "Creating BedrockModel with retry configuration",
        extra={
            "model_id": model_id,
            "max_retries": max_retries,
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
        },
    )

    # Apply boto config to kwargs if not already provided
    if "boto_client_config" not in kwargs:
        kwargs["boto_client_config"] = boto_config

    # Route Bedrock through the cross-account session factory when
    # BEDROCK_ASSUME_ROLE_ARN is set. The Strands BedrockModel's
    # boto_session is used **only** for Bedrock invocations, so it is safe
    # to override any explicitly-passed session here — non-Bedrock AWS
    # calls in the same Lambda continue to use the calling-account
    # credentials.
    #
    # When BEDROCK_ASSUME_ROLE_ARN is unset, get_bedrock_session() returns
    # a vanilla session, preserving prior behavior.
    if os.environ.get("BEDROCK_ASSUME_ROLE_ARN", "").strip():
        try:
            from idp_common.bedrock.session import get_bedrock_session

            boto_session = get_bedrock_session()
        except Exception as e:
            logger.warning(
                "Failed to build Bedrock hub-account session, falling back to default (%s)",
                e,
            )
    elif boto_session is None:
        try:
            from idp_common.bedrock.session import get_bedrock_session

            boto_session = get_bedrock_session()
        except Exception as e:
            logger.debug("Falling back to default Bedrock session (%s)", e)

    # Apply the guardrail from GUARDRAIL_ID_AND_VERSION when configured.
    guardrail_kwargs = strands_guardrail_kwargs()
    if guardrail_kwargs:
        kwargs.update(guardrail_kwargs)
        logger.debug(
            "Guardrails enabled for BedrockModel",
            extra={"guardrail_id": guardrail_kwargs["guardrail_id"]},
        )

    return BedrockModel(model_id=model_id, boto_session=boto_session, **kwargs)
