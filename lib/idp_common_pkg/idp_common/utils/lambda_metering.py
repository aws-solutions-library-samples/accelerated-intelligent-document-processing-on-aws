# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Lambda metering utilities for tracking execution costs in document processing.

This module provides utilities to calculate and track Lambda function execution
costs including invocation counts and GB-seconds for duration-based pricing.
The metering data integrates with the existing document metering system.
"""

import time
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def calculate_lambda_metering(
    context_name: str, lambda_context: Any, start_time: float
) -> Dict[str, Any]:
    """
    Calculate Lambda execution metering data for core processing functions.

    This function calculates both invocation-based and duration-based metrics
    that align with AWS Lambda pricing:
    - Requests: Number of invocations ($0.20 per 1M requests)
    - Duration: GB-seconds of execution time ($16.67 per 1M GB-seconds)

    Args:
        context_name: Processing context (e.g., "OCR", "Classification", "Extraction", "Assessment", "Summarization")
        lambda_context: AWS Lambda context object containing function metadata
        start_time: Function start time from time.time()

    Returns:
        Dictionary with Lambda metering data in the standard format:
        {
            "{context}/lambda/requests": {"requests": 1},
            "{context}/lambda/duration": {"gb_seconds": calculated_value}
        }
    """
    try:
        # Calculate execution duration
        end_time = time.time()
        duration_seconds = float(end_time - start_time)

        # Get allocated memory in MB from Lambda context - handle string/int types
        memory_mb_raw = lambda_context.memory_limit_in_mb

        # Convert memory to float, handling both string and numeric types
        if isinstance(memory_mb_raw, str):
            try:
                memory_mb = float(memory_mb_raw)
            except (ValueError, TypeError):
                logger.warning(
                    f"Could not convert memory_limit_in_mb '{memory_mb_raw}' to float, using default 128MB"
                )
                memory_mb = 128.0  # Default Lambda memory allocation
        else:
            memory_mb = float(memory_mb_raw)

        # Convert to GB-seconds (AWS pricing unit)
        # AWS charges based on allocated memory, not actual usage
        memory_gb = memory_mb / 1024.0
        gb_seconds_raw = memory_gb * duration_seconds

        # Round GB-seconds to 1 decimal places for cleaner output
        gb_seconds = round(gb_seconds_raw, 1)

        # Log the calculated metrics for visibility
        logger.info(
            f"Lambda metering for {context_name}: "
            f"duration={duration_seconds:.3f}s, "
            f"memory={memory_mb}MB, "
            f"gb_seconds={gb_seconds}"
        )

        # Return metering data in the standard format used by other services
        return {
            # Unit name must be "requests", matching the `requests` unit of the
            # `lambda/requests` pricing entry in config_library/pricing.yaml.
            # This emitted "invocations" until #952: the pricing KEY matched but
            # the UNIT did not, so every Lambda invocation was costed at $0.00.
            # Reconciled on the emitter side rather than in pricing.yaml because
            # pricing is user-overridable config — already-deployed and custom
            # pricing configs all say "requests", so fixing the emitter fixes
            # them too, whereas renaming the shipped unit would only fix
            # deployments that adopt the new default.
            f"{context_name}/lambda/requests": {"requests": 1},
            f"{context_name}/lambda/duration": {"gb_seconds": gb_seconds},
        }

    except Exception as e:
        logger.warning(
            f"Error calculating Lambda metering for {context_name}: {str(e)}"
        )
        # Return empty metering data on error to avoid breaking the document processing pipeline
        return {}
