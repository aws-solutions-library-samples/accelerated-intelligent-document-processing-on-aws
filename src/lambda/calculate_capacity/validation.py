# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Environment variable and input validation for capacity planning Lambda.

This module provides validation functions to ensure all required configuration
is present and valid before processing capacity calculations.
"""

import json
import os
from typing import Dict, Any, List, Optional


class ValidationError(Exception):
    """Raised when validation fails."""
    pass


def validate_required_env_vars() -> Dict[str, Any]:
    """
    Validate all required environment variables at Lambda startup.

    Returns:
        Dict containing parsed and validated environment variables

    Raises:
        ValidationError: If any required environment variable is missing or invalid
    """
    errors = []
    validated = {}

    # Required DynamoDB tables
    tracking_table = os.environ.get('TRACKING_TABLE')
    if not tracking_table:
        errors.append('TRACKING_TABLE environment variable is required')
    else:
        validated['tracking_table'] = tracking_table

    metering_table = os.environ.get('METERING_TABLE_NAME')
    if not metering_table:
        errors.append('METERING_TABLE_NAME environment variable is required')
    else:
        validated['metering_table'] = metering_table

    # Required Lambda memory configuration
    lambda_memory_gb = os.environ.get('LAMBDA_MEMORY_GB')
    if not lambda_memory_gb:
        errors.append('LAMBDA_MEMORY_GB environment variable is required')
    else:
        try:
            validated['lambda_memory_gb'] = float(lambda_memory_gb)
            if validated['lambda_memory_gb'] <= 0:
                errors.append('LAMBDA_MEMORY_GB must be a positive number')
        except ValueError:
            errors.append(f'LAMBDA_MEMORY_GB must be a valid number, got: {lambda_memory_gb}')

    # Required recommendation thresholds
    threshold_vars = {
        'RECOMMENDATION_HIGH_COMPLEXITY_THRESHOLD': float,
        'RECOMMENDATION_MEDIUM_COMPLEXITY_THRESHOLD': float,
        'RECOMMENDATION_HIGH_LOAD_THRESHOLD': float,
        'RECOMMENDATION_MEDIUM_LOAD_THRESHOLD': float,
        'RECOMMENDATION_HIGH_LATENCY_THRESHOLD': int,
        'RECOMMENDATION_LARGE_DOC_THRESHOLD': int,
        'RECOMMENDATION_HIGH_PAGE_THRESHOLD': int,
    }

    for var_name, var_type in threshold_vars.items():
        value = os.environ.get(var_name)
        if not value:
            errors.append(f'{var_name} environment variable is required')
        else:
            try:
                validated[var_name.lower()] = var_type(value)
            except ValueError:
                errors.append(f'{var_name} must be a valid {var_type.__name__}, got: {value}')

    # Required document complexity thresholds
    complexity_vars = {
        'MEDIUM_COMPLEXITY_THRESHOLD': int,
        'HIGH_COMPLEXITY_THRESHOLD': int,
        'PAGE_COMPLEXITY_FACTOR': float,
        'HIGH_COMPLEXITY_MULTIPLIER': float,
        'MEDIUM_COMPLEXITY_MULTIPLIER': float,
    }

    for var_name, var_type in complexity_vars.items():
        value = os.environ.get(var_name)
        if not value:
            errors.append(f'{var_name} environment variable is required')
        else:
            try:
                validated[var_name.lower()] = var_type(value)
            except ValueError:
                errors.append(f'{var_name} must be a valid {var_type.__name__}, got: {value}')

    # Required token calculation
    min_tokens = os.environ.get('MIN_TOKENS_PER_REQUEST')
    if not min_tokens:
        errors.append('MIN_TOKENS_PER_REQUEST environment variable is required')
    else:
        try:
            validated['min_tokens_per_request'] = int(min_tokens)
            if validated['min_tokens_per_request'] <= 0:
                errors.append('MIN_TOKENS_PER_REQUEST must be a positive integer')
        except ValueError:
            errors.append(f'MIN_TOKENS_PER_REQUEST must be a valid integer, got: {min_tokens}')

    # Required Bedrock model quota codes (JSON)
    tpm_codes = os.environ.get('BEDROCK_MODEL_QUOTA_CODES')
    if not tpm_codes:
        errors.append('BEDROCK_MODEL_QUOTA_CODES environment variable is required')
    else:
        try:
            validated['bedrock_model_quota_codes'] = json.loads(tpm_codes)
            if not isinstance(validated['bedrock_model_quota_codes'], dict):
                errors.append('BEDROCK_MODEL_QUOTA_CODES must be a JSON object')
            elif not validated['bedrock_model_quota_codes']:
                errors.append('BEDROCK_MODEL_QUOTA_CODES cannot be empty')
        except json.JSONDecodeError as e:
            errors.append(f'BEDROCK_MODEL_QUOTA_CODES must be valid JSON: {e}')

    # Required Bedrock model RPM quota codes (JSON)
    rpm_codes = os.environ.get('BEDROCK_MODEL_RPM_QUOTA_CODES')
    if not rpm_codes:
        errors.append('BEDROCK_MODEL_RPM_QUOTA_CODES environment variable is required')
    else:
        try:
            validated['bedrock_model_rpm_quota_codes'] = json.loads(rpm_codes)
            if not isinstance(validated['bedrock_model_rpm_quota_codes'], dict):
                errors.append('BEDROCK_MODEL_RPM_QUOTA_CODES must be a JSON object')
            elif not validated['bedrock_model_rpm_quota_codes']:
                errors.append('BEDROCK_MODEL_RPM_QUOTA_CODES cannot be empty')
        except json.JSONDecodeError as e:
            errors.append(f'BEDROCK_MODEL_RPM_QUOTA_CODES must be valid JSON: {e}')

    if errors:
        error_msg = 'Environment variable validation failed:\n' + '\n'.join(f'  - {e}' for e in errors)
        raise ValidationError(error_msg)

    return validated


def sanitize_json_input(input_str: str, max_size_bytes: int = 1_000_000) -> Dict[str, Any]:
    """
    Safely parse and validate JSON input with size limits.

    Args:
        input_str: JSON string to parse
        max_size_bytes: Maximum allowed size in bytes (default 1MB)

    Returns:
        Parsed JSON object

    Raises:
        ValidationError: If input is invalid or exceeds size limit
    """
    if not isinstance(input_str, str):
        raise ValidationError(f'Input must be a string, got {type(input_str).__name__}')

    # Check size limit
    if len(input_str.encode('utf-8')) > max_size_bytes:
        raise ValidationError(f'Input exceeds maximum size of {max_size_bytes} bytes')

    # Parse JSON with error handling
    try:
        data = json.loads(input_str)
    except json.JSONDecodeError as e:
        raise ValidationError(f'Invalid JSON format: {e}')

    if not isinstance(data, dict):
        raise ValidationError(f'Input must be a JSON object, got {type(data).__name__}')

    return data


def validate_capacity_input(input_data: Dict[str, Any]) -> None:
    """
    Validate capacity calculation input parameters.

    Args:
        input_data: Parsed input data dictionary

    Raises:
        ValidationError: If input parameters are invalid
    """
    errors = []

    # Validate pattern
    pattern = input_data.get('pattern')
    if not pattern:
        errors.append('pattern is required')
    elif pattern not in ('pattern-2', 'unified'):
        errors.append(f'Only pattern-2 and unified patterns are supported, got: {pattern}')

    # Validate maxAllowedLatency. The two spellings are alternatives, and which one
    # supplied the value is decided on whether it is present rather than on whether
    # it is truthy: `0` is a value an operator can type, and reading it as absent
    # reported a field they did fill in as missing and put the "must be positive"
    # check below out of reach of the one value that can reach it. An empty string
    # counts as not supplied, as it does for the numeric document fields further
    # down, so clearing the field still reads as a missing value rather than as a
    # malformed number.
    max_latency = None
    for latency_key in ('maxAllowedLatency', 'max_allowed_latency'):
        candidate = input_data.get(latency_key)
        if candidate is not None and candidate != '':
            max_latency = candidate
            break
    if max_latency is None:
        errors.append('maxAllowedLatency or max_allowed_latency is required')
    else:
        try:
            max_latency_float = float(max_latency)
            if max_latency_float <= 0:
                errors.append('maxAllowedLatency must be positive')
            if max_latency_float > 3600:
                errors.append('maxAllowedLatency cannot exceed 3600 seconds (1 hour)')
        except (ValueError, TypeError):
            errors.append(f'maxAllowedLatency must be a number, got: {max_latency}')

    # Validate documentConfigs
    doc_configs = input_data.get('documentConfigs', [])
    if not doc_configs:
        errors.append('documentConfigs is required and cannot be empty')
    elif not isinstance(doc_configs, list):
        errors.append(f'documentConfigs must be a list, got {type(doc_configs).__name__}')
    else:
        for idx, config in enumerate(doc_configs):
            if not isinstance(config, dict):
                errors.append(f'documentConfigs[{idx}] must be an object')
                continue

            # Validate required fields
            if not config.get('type'):
                errors.append(f'documentConfigs[{idx}].type is required')

            # Validate numeric fields
            numeric_fields = ['avgPages', 'ocrTokens', 'classificationTokens',
                            'extractionTokens', 'assessmentTokens', 'summarizationTokens']
            for field in numeric_fields:
                if field in config and config[field] != '':
                    try:
                        value = float(config[field])
                        if value < 0:
                            errors.append(f'documentConfigs[{idx}].{field} cannot be negative')
                    except (ValueError, TypeError):
                        errors.append(f'documentConfigs[{idx}].{field} must be a number')

    # Validate timeSlots if present
    time_slots = input_data.get('timeSlots', [])
    if isinstance(time_slots, str):
        try:
            time_slots = json.loads(time_slots)
        except json.JSONDecodeError:
            errors.append('timeSlots must be valid JSON if provided as string')
            time_slots = []

    if time_slots and isinstance(time_slots, list):
        for idx, slot in enumerate(time_slots):
            if not isinstance(slot, dict):
                errors.append(f'timeSlots[{idx}] must be an object')
                continue

            # Validate hour
            if 'hour' in slot:
                try:
                    hour = int(slot['hour'])
                    if hour < 0 or hour > 23:
                        errors.append(f'timeSlots[{idx}].hour must be between 0 and 23')
                except (ValueError, TypeError):
                    errors.append(f'timeSlots[{idx}].hour must be an integer')

            # Validate docsPerHour
            if 'docsPerHour' in slot:
                try:
                    docs = int(slot['docsPerHour'])
                    if docs < 0:
                        errors.append(f'timeSlots[{idx}].docsPerHour cannot be negative')
                except (ValueError, TypeError):
                    errors.append(f'timeSlots[{idx}].docsPerHour must be an integer')

    # Validate userConfig if present
    user_config = input_data.get('userConfig')
    if user_config and isinstance(user_config, str):
        if user_config.strip() and user_config.strip() != '{}':
            try:
                json.loads(user_config)
            except json.JSONDecodeError as e:
                errors.append(f'userConfig must be valid JSON: {e}')

    if errors:
        error_msg = 'Input validation failed:\n' + '\n'.join(f'  - {e}' for e in errors)
        raise ValidationError(error_msg)


# Global variable to store validated environment variables (loaded once at cold start)
_validated_env_vars: Optional[Dict[str, Any]] = None


def get_validated_env_vars() -> Dict[str, Any]:
    """
    Get validated environment variables (cached after first call).

    Returns:
        Dictionary of validated environment variables
    """
    global _validated_env_vars
    if _validated_env_vars is None:
        _validated_env_vars = validate_required_env_vars()
    return _validated_env_vars
