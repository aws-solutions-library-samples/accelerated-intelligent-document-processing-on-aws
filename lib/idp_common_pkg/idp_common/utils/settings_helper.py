"""
Settings helper for runtime configuration via SSM Parameter Store.

Provides cached access to stack settings stored in SSM, enabling
Lambda functions to retrieve configuration values without deployment-time
dependencies on other nested stacks.
"""

import json
import os
import threading
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

# Module-level cache
_settings_cache: Optional[Dict[str, Any]] = None
_cache_timestamp: float = 0
_CACHE_TTL_SECONDS = 300  # 5 minute cache

# The SSM client is built on first use rather than at import time. This module is
# re-exported by ``idp_common.utils``, which ``idp_common.s3`` imports, which in
# turn is imported by most of this repository's Lambda handlers — so a module-scope
# ``boto3.client("ssm")`` here made *importing the library at all* require a
# resolvable AWS region. The Lambda runtime always sets one, so production was
# never affected; a unit test run on a machine with no region is not, and botocore
# raised ``NoRegionError`` while pytest was still collecting. That is how the
# ``save_reporting_data`` suite came to pass only on developer machines, whose
# region comes from the shared AWS config file, and fail on a CI runner (#988).
#
# Deferring construction also matches the sibling
# ``idp_common.monitoring.settings_cache``, which has always built its SSM client
# lazily, and costs nothing at run time: the client is created once on the first
# ``get_settings`` call and cached for the life of the process, exactly as before.
_ssm_client: Optional[Any] = None

# ``boto3.client()`` is not documented as thread-safe. This is library code reachable
# from any caller, so the lock makes construction once-only by construction rather
# than by auditing every present and future call site; today's two callers are both
# single-threaded. The read outside the lock is the usual double-checked pattern and
# is safe because the only transition is None -> client.
_ssm_client_lock = threading.Lock()


def _get_ssm_client() -> Any:
    """Return the process-wide SSM client, constructing it on first use.

    Tests that need to intercept the call can assign a double to
    ``settings_helper._ssm_client`` instead of patching ``boto3.client``.
    """
    global _ssm_client
    if _ssm_client is None:
        with _ssm_client_lock:
            if _ssm_client is None:
                _ssm_client = boto3.client("ssm")
    return _ssm_client


def get_settings(
    parameter_name: Optional[str] = None, force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Get settings from SSM Parameter Store with caching.

    Args:
        parameter_name: SSM parameter name. Defaults to SETTINGS_PARAMETER env var.
        force_refresh: If True, bypass cache and fetch fresh values.

    Returns:
        Dict containing settings key-value pairs.

    Raises:
        ValueError: If parameter_name not provided and SETTINGS_PARAMETER not set.
        ClientError: If SSM parameter cannot be retrieved.

    Example:
        >>> settings = get_settings()
        >>> state_machine_arn = settings.get('StateMachineArn')
    """
    global _settings_cache, _cache_timestamp

    param_name = parameter_name or os.environ.get("SETTINGS_PARAMETER")
    if not param_name:
        raise ValueError(
            "Settings parameter name not provided and SETTINGS_PARAMETER "
            "environment variable not set"
        )

    current_time = time.time()
    cache_valid = (
        _settings_cache is not None
        and not force_refresh
        and (current_time - _cache_timestamp) < _CACHE_TTL_SECONDS
    )

    if cache_valid:
        return _settings_cache  # type: ignore[return-value]

    try:
        response = _get_ssm_client().get_parameter(Name=param_name)
        # Bound to a local first so the return type is the declared dict rather
        # than the module global's Optional.
        settings: Dict[str, Any] = json.loads(response["Parameter"]["Value"])
        _settings_cache = settings
        _cache_timestamp = current_time
        return settings
    except ClientError as e:
        # If parameter doesn't exist yet (during initial deployment), return empty dict
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return {}
        raise


def get_setting(
    key: str, default: Any = None, parameter_name: Optional[str] = None
) -> Any:
    """
    Get a specific setting value.

    Args:
        key: The setting key to retrieve.
        default: Default value if key not found.
        parameter_name: SSM parameter name (optional).

    Returns:
        The setting value, or default if not found.

    Example:
        >>> state_machine_arn = get_setting('StateMachineArn')
        >>> kb_id = get_setting('KnowledgeBaseId', default='')
    """
    settings = get_settings(parameter_name=parameter_name)
    return settings.get(key, default)


def clear_cache() -> None:
    """Clear the settings cache. Useful for testing."""
    global _settings_cache, _cache_timestamp
    _settings_cache = None
    _cache_timestamp = 0
