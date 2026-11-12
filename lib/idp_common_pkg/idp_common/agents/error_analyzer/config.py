# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Configuration management for error analyzer agents.
"""

import logging
from typing import Any, Dict

from ..common.config import configure_logging, get_environment_config

logger = logging.getLogger(__name__)


def get_error_analyzer_config() -> Dict[str, Any]:
    """
    Get error analyzer-specific configuration from environment variables.

    Returns:
        Dict containing error analyzer configuration values

    Raises:
        ValueError: If required environment variables are missing
    """
    # Get base configuration
    config = get_environment_config()

    # Add error analyzer-specific defaults
    config.setdefault("max_log_events", 10)
    config.setdefault("lookback_hours", 24)
    # Keep legacy alias for any code that still reads time_range_hours_default
    config.setdefault("time_range_hours_default", config["lookback_hours"])

    # Configure logging based on the configuration
    configure_logging(
        log_level=config.get("log_level"),
        strands_log_level=config.get("strands_log_level"),
    )

    logger.info("Error analyzer configuration loaded successfully")
    return config


def get_error_analyzer_model_id() -> str:
    """
    Get the error analyzer model ID from configuration.

    Uses the modern configuration system that reads user-changed values from DynamoDB.

    Returns:
        Model ID string
    """
    try:
        from ...config import get_config

        # Use the modern configuration system that reads from DynamoDB
        config = get_config(as_model=True)

        # Get model ID from configuration with type safety
        model_id = config.agents.error_analyzer.model_id

        logger.info(f"Using error analyzer model ID from configuration: {model_id}")
        return model_id

    except Exception as e:
        logger.warning(f"Failed to load model ID from configuration: {e}")

        default_model_id = default_error_analyzer_model_id()
        logger.info(f"Using default error analyzer model ID: {default_model_id}")
        return default_model_id


#: What :func:`default_error_analyzer_model_id` returns when the field default
#: cannot be read: the same model through the same profile.
ERROR_ANALYZER_MODEL_LAST_RESORT = "global.anthropic.claude-sonnet-4-6"


def default_error_analyzer_model_id() -> str:
    """The error analyzer's model when its configuration cannot be read.

    ``ErrorAnalyzerConfig.model_id``'s declared default, which equals what
    ``base-agents.yaml`` ships, served through its ``global.`` profile because a
    ``us.`` fallback cannot be invoked from an EU or Canadian stack (see
    :func:`idp_common.agents.common.config.portable_profile`). The import is
    guarded: this runs because configuration could not be loaded.
    """
    from ..common.config import portable_profile

    try:
        from ...config.models import ErrorAnalyzerConfig

        default = ErrorAnalyzerConfig.model_fields["model_id"].default
        if isinstance(default, str) and default:
            return portable_profile(default)
    except Exception:  # defensive, see docstring
        pass
    return ERROR_ANALYZER_MODEL_LAST_RESORT


def get_aws_service_capabilities() -> Dict[str, Any]:
    """Returns AWS service integration metadata and descriptions."""
    return {
        "cloudwatch_logs": {
            "description": "CloudWatch Logs integration for error analysis",
            "capabilities": [
                "search_log_events",
                "get_log_groups",
                "filter_log_events",
            ],
            "implementation": "Native AWS SDK integration",
        },
        "dynamodb": {
            "description": "DynamoDB integration for document tracking",
            "capabilities": ["scan_table", "query_table", "get_item"],
            "implementation": "Native AWS SDK integration",
        },
        "benefits": [
            "No external dependencies",
            "Native Lambda integration",
            "Optimal performance",
        ],
    }


# Utility functions
def decimal_to_float(obj: Any) -> Any:
    """Recursively converts DynamoDB Decimal objects to JSON-compatible floats."""
    if hasattr(obj, "__class__") and obj.__class__.__name__ == "Decimal":
        return float(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_float(v) for v in obj]
    return obj


def get_ea_param(field: str, default: Any) -> Any:
    """
    Safely retrieve a single ``ErrorAnalyzerParameters`` field from IDPConfig.

    All tools share this helper so they never need to write bare ``getattr``
    chains or duplicate the try/except fallback boilerplate.

    Args:
        field:   Attribute name on ``ErrorAnalyzerParameters``
                 (e.g. ``"max_stepfunction_timeline_events"``).
        default: Value returned when config is unavailable or the field
                 does not exist.

    Returns:
        The configured value, or *default* on any error.
    """
    try:
        from ...config import get_config

        config = get_config(as_model=True)
        return getattr(config.agents.error_analyzer.parameters, field, default)
    except Exception:  # noqa: BLE001
        return default


def create_error_response(error: str, **kwargs) -> Dict[str, Any]:
    """Creates standardized error response with consistent format."""
    response = {"error": str(error), "success": False}
    response.update(kwargs)
    return response


def create_response(data: Dict[str, Any]) -> Dict[str, Any]:
    """Creates standardized response with consistent format."""
    return data


def safe_int_conversion(value: Any, default: int = 0) -> int:
    """Safely converts values to integers with fallback handling."""
    try:
        return int(float(value)) if value is not None else default
    except (ValueError, TypeError):
        return default


def truncate_message(message: str, max_length: int = 200) -> str:
    """Truncates messages to specified length with ellipsis indicator."""
    if len(message) <= max_length:
        return message
    return message[:max_length] + "... [truncated]"
