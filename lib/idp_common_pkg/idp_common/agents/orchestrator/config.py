# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration management for orchestrator agents.
"""

import logging
from typing import Any, Dict

from ..common.config import (
    DEFAULT_AGENT_MODEL_ID,
    configure_logging,
    get_environment_config,
)

logger = logging.getLogger(__name__)


def get_orchestrator_config() -> Dict[str, Any]:
    """
    Get orchestrator-specific configuration from environment variables and configuration table.

    Returns:
        Dict containing orchestrator configuration values

    Raises:
        ValueError: If required environment variables are missing
    """
    # Get base configuration
    config = get_environment_config()

    # Add orchestrator-specific defaults
    config.setdefault("default_model_id", DEFAULT_AGENT_MODEL_ID)

    # Configure logging based on the configuration
    configure_logging(
        log_level=config.get("log_level"),
        strands_log_level=config.get("strands_log_level"),
    )

    logger.info("Orchestrator configuration loaded successfully")
    return config


def get_chat_companion_model_id() -> str:
    """
    Get the chat companion model ID from configuration.

    Uses the modern configuration system that reads user-changed values from DynamoDB.

    Returns:
        Model ID string
    """
    try:
        from ...config import get_config

        # Use the modern configuration system that reads from DynamoDB
        config = get_config(as_model=True)

        # Get model ID from configuration with type safety
        model_id = config.agents.chat_companion.model_id

        logger.info(f"Using chat companion model ID from configuration: {model_id}")
        return model_id

    except Exception as e:
        logger.warning(f"Failed to load model ID from configuration: {e}")

        # Final fallback. Matches `agents.chat_companion.model_id` in
        # config/system_defaults/base-agents.yaml, which is what a deployed stack
        # resolves — so the path taken when configuration cannot be read runs the
        # same model, and at the same price, as the path taken when it can. A
        # Sonnet here would be ~3x the per-token cost of the configured default.
        default_model_id = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        logger.info(f"Using default chat companion model ID: {default_model_id}")
        return default_model_id
