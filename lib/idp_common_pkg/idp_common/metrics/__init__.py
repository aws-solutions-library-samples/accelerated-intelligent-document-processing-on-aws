# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os
import threading
from typing import Any, Dict, List, Optional

import boto3

logger = logging.getLogger(__name__)

# Initialize clients
_cloudwatch_client = None
_client_lock = threading.Lock()
_metric_lock = threading.Lock()


def get_cloudwatch_client():
    """
    Get or initialize the CloudWatch client in a thread-safe manner

    Returns:
        boto3 CloudWatch client
    """
    global _cloudwatch_client
    with _client_lock:
        if _cloudwatch_client is None:
            _cloudwatch_client = boto3.client("cloudwatch")
        return _cloudwatch_client


def put_metric(
    name: str,
    value: float,
    unit: str = "Count",
    dimensions: Optional[List[Dict[str, str]]] = None,
    namespace: Optional[str] = None,
) -> None:
    """
    Publish a metric to CloudWatch in a thread-safe manner

    Args:
        name: The name of the metric
        value: The value of the metric
        unit: The unit of the metric
        dimensions: Optional list of dimensions
        namespace: Optional metric namespace, defaults to environment variable
    """
    dimensions = dimensions or []

    # Get namespace from environment if not provided
    if namespace is None:
        namespace = os.environ.get("METRIC_NAMESPACE", "GENAIDP")

    # Use thread lock to ensure thread safety when publishing metrics
    with _metric_lock:
        logger.debug(f"Publishing metric {name}: {value}")
        try:
            cloudwatch = get_cloudwatch_client()
            cloudwatch.put_metric_data(
                Namespace=namespace,
                MetricData=[
                    {
                        "MetricName": name,
                        "Value": value,
                        "Unit": unit,
                        "Dimensions": dimensions,
                    }
                ],
            )
        except Exception as e:
            logger.error(f"Error publishing metric {name}: {e}")


def emit_control_plane_cost_metric(
    component: str,
    athena_bytes: Optional[int] = None,
    bedrock_tokens_in: Optional[int] = None,
    bedrock_tokens_out: Optional[int] = None,
    bedrock_model: Optional[str] = None,
) -> None:
    """Emit control-plane cost metrics for a single Lambda invocation.

    Meant to be called at the end of every control-plane Lambda invocation
    that hits Athena or Bedrock. Lambda ``Duration`` is emitted by AWS
    natively — do not call this for duration.

    Metric shape (namespace ``IDPControlPlane``):
      - ``AthenaBytesScanned`` (dim: ``Component``)
      - ``BedrockInputTokens`` (dim: ``Component``, ``Model``)
      - ``BedrockOutputTokens`` (dim: ``Component``, ``Model``)

    The hourly rollup Lambda (main IDP stack) reads these via
    ``cloudwatch:GetMetricData`` and writes rows to ``control_plane_hourly``
    for the dashboard's Control Plane Cost KPI. See
    ``docs/planning/monitor-data-mart.md`` §10 for the design.

    Args:
        component: Short label identifying the feature area
            (``monitor-dashboard``, ``monitor-agent``, ``test-runner``,
            etc.). See §10.2 for the fixed set of values.
        athena_bytes: Bytes scanned by an Athena query in this invocation.
            Emit the ``QueryExecution.Statistics.DataScannedInBytes``
            value from ``get_query_execution``.
        bedrock_tokens_in: Input tokens consumed by a Bedrock call in
            this invocation.
        bedrock_tokens_out: Output tokens produced by a Bedrock call in
            this invocation.
        bedrock_model: Bedrock model ID (e.g., ``us.anthropic.claude-...``).
            Required whenever ``bedrock_tokens_in`` or
            ``bedrock_tokens_out`` is set — priced-per-token per-model.

    Failure mode is fire-and-forget: if CloudWatch is unreachable, log a
    warning and continue. Cost visibility is best-effort — never a
    reason to fail an invocation.
    """
    if bedrock_tokens_in is not None or bedrock_tokens_out is not None:
        if not bedrock_model:
            logger.warning(
                "emit_control_plane_cost_metric: bedrock tokens supplied "
                "without bedrock_model; skipping bedrock metrics"
            )
            bedrock_tokens_in = None
            bedrock_tokens_out = None

    metric_data: List[Dict[str, Any]] = []
    if athena_bytes is not None:
        metric_data.append(
            {
                "MetricName": "AthenaBytesScanned",
                "Value": float(athena_bytes),
                "Unit": "Bytes",
                "Dimensions": [{"Name": "Component", "Value": component}],
            }
        )
    if bedrock_tokens_in is not None:
        metric_data.append(
            {
                "MetricName": "BedrockInputTokens",
                "Value": float(bedrock_tokens_in),
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "Component", "Value": component},
                    {"Name": "Model", "Value": bedrock_model or "unknown"},
                ],
            }
        )
    if bedrock_tokens_out is not None:
        metric_data.append(
            {
                "MetricName": "BedrockOutputTokens",
                "Value": float(bedrock_tokens_out),
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "Component", "Value": component},
                    {"Name": "Model", "Value": bedrock_model or "unknown"},
                ],
            }
        )

    if not metric_data:
        return

    with _metric_lock:
        try:
            cloudwatch = get_cloudwatch_client()
            cloudwatch.put_metric_data(
                Namespace="IDPControlPlane",
                MetricData=metric_data,
            )
        except Exception as e:
            # Best-effort — cost telemetry never fails an invocation.
            logger.warning(
                f"Failed to emit control-plane cost metrics for "
                f"component={component!r}: {e}"
            )


def create_client_performance_metrics(
    name: str,
    duration_ms: float,
    is_success: bool = True,
    error_type: Optional[str] = None,
) -> None:
    """
    Helper to publish standardized client performance metrics in a thread-safe manner

    Args:
        name: Base name for the metric group
        duration_ms: Duration in milliseconds
        is_success: Whether the operation succeeded
        error_type: Optional error type for failures
    """
    # Use a single lock for all metrics to ensure they are published as a group
    with _metric_lock:
        # Get namespace from environment
        namespace = os.environ.get("METRIC_NAMESPACE", "GENAIDP")
        dimensions = []
        cloudwatch = get_cloudwatch_client()

        # Build metric data array for all metrics we want to publish
        metric_data = [
            {
                "MetricName": f"{name}Latency",
                "Value": duration_ms,
                "Unit": "Milliseconds",
                "Dimensions": dimensions,
            }
        ]

        # Add success/failure metrics
        if is_success:
            metric_data.append(
                {
                    "MetricName": f"{name}Success",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": dimensions,
                }
            )
        else:
            metric_data.append(
                {
                    "MetricName": f"{name}Failure",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": dimensions,
                }
            )
            if error_type:
                metric_data.append(
                    {
                        "MetricName": f"{name}Error.{error_type}",
                        "Value": 1,
                        "Unit": "Count",
                        "Dimensions": dimensions,
                    }
                )

        # Publish all metrics in a single API call for efficiency
        try:
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=metric_data)
            logger.debug(f"Published {len(metric_data)} metrics for {name}")
        except Exception as e:
            logger.error(f"Error publishing performance metrics for {name}: {e}")
