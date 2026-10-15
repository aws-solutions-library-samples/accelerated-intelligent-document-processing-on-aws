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
    function_name: Optional[str] = None,
) -> None:
    """Emit control-plane cost metrics for a single Lambda invocation.

    Meant to be called at the end of every control-plane Lambda invocation
    that hits Athena or Bedrock. Lambda ``Duration`` is emitted by AWS
    natively — do not call this for duration.

    Metric shape (namespace ``IDPControlPlane``):
      - ``AthenaBytesScanned`` (dims: ``Component``, ``FunctionName``)
      - ``BedrockInputTokens`` (dims: ``Component``, ``FunctionName``, ``Model``)
      - ``BedrockOutputTokens`` (dims: ``Component``, ``FunctionName``, ``Model``)

    Why the ``FunctionName`` dimension: the rollup Lambda reads these
    per-function so a component's Athena/Bedrock cost is attributed to
    the specific Lambda that spent it. Without this dimension, a
    component-scoped query (dim: ``Component`` only) returns ONE
    aggregated stream and the rollup would over-count by the number of
    Lambdas in the component (each row picks up the whole component's
    total). See ``docs/reporting-sql-layer.md`` §10.5.

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
        function_name: The emitting Lambda's function name. Defaults to
            the ``AWS_LAMBDA_FUNCTION_NAME`` env var — set by the Lambda
            runtime. Override only in tests or non-Lambda callers.

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

    # FunctionName pins each metric stream to the emitter — prevents the
    # per-Lambda over-counting that a component-only dimension would produce.
    fn_name = function_name or os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "unknown")
    base_dims = [
        {"Name": "Component", "Value": component},
        {"Name": "FunctionName", "Value": fn_name},
    ]

    metric_data: List[Dict[str, Any]] = []
    if athena_bytes is not None:
        metric_data.append(
            {
                "MetricName": "AthenaBytesScanned",
                "Value": float(athena_bytes),
                "Unit": "Bytes",
                "Dimensions": list(base_dims),
            }
        )
    if bedrock_tokens_in is not None:
        metric_data.append(
            {
                "MetricName": "BedrockInputTokens",
                "Value": float(bedrock_tokens_in),
                "Unit": "Count",
                "Dimensions": [
                    *base_dims,
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
                    *base_dims,
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


def emit_document_cost_metrics(
    estimated_cost: Optional[float],
    unpriced_row_count: int,
    namespace: Optional[str] = None,
) -> None:
    """Emit one document's estimated cost to CloudWatch.

    Called once per document by ``SaveReportingData.save_metering_data``, which
    is the single place that already turns metering rows into dollars. ``cost``
    is **passed in, never recomputed here**: the caller hands over the sum of the
    per-row ``estimated_cost`` values it has just written to the metering parquet,
    so the metric and the Athena ledger are the same number by construction
    rather than by two implementations agreeing.

    Metric shape (namespace from ``METRIC_NAMESPACE``, which the stack sets to the
    stack name — so stacks are separated by namespace and need no dimension for
    it):

      - ``DocumentEstimatedCost`` (USD, no dimensions)
      - ``UnpricedMeteringRows`` (count, no dimensions)

    **No dimensions, deliberately.** CloudWatch bills per metric per distinct
    dimension combination, and every candidate dimension here is unbounded or
    grows with use: ``DocumentId`` is one metric per document; ``DocumentClass``
    is a customer-defined taxonomy; ``ServiceApi``/``Model`` grows with every
    model and processing step a deployment touches, and a deployment that
    exercises the whole pipeline already has tens of distinct values. A metric
    that costs more than the spend it reports is not a cost control. This shape
    is two metric streams per stack, flat in document volume — and the
    per-document, per-model, per-class breakdown already exists for free in the
    metering parquet, the Athena rollups and the UI's per-document cost panel.
    What those cannot do is drive an alarm, which is the only thing this adds.

    The per-document datapoint is the useful unit rather than a per-row one:
    ``Sum`` over a period is spend in that period, ``Average`` is cost per
    document, ``Maximum`` is the most expensive single document and
    ``SampleCount`` is documents completed. A per-row datapoint would make three
    of those four meaningless.

    Args:
        estimated_cost: The document's total estimated cost in USD, already
            computed by the caller, or ``None`` when **no** metering row for the
            document had a price at all. ``None`` emits no cost datapoint —
            a zero would be indistinguishable from a document that genuinely
            cost nothing, and would drag an average down with a number nobody
            measured. This mirrors the metering table, which writes NULL rather
            than 0.00 for the same reason. A cost of ``0.0`` is a *measured*
            zero and IS emitted.
        unpriced_row_count: How many of the document's metering rows had no
            price. Emitted unconditionally, zero included, so that "the cost
            metric is complete" is an observable fact with a datapoint behind it
            instead of an absence. Without it, a pricing configuration missing
            the model actually in use makes ``DocumentEstimatedCost`` read low,
            or vanish, with nothing on any dashboard to say why.
        namespace: Override the namespace. Defaults to ``METRIC_NAMESPACE``,
            matching :func:`put_metric`. Note the stack grants this Lambda
            ``cloudwatch:PutMetricData`` under a
            ``StringEquals cloudwatch:namespace = <stack name>`` condition, so a
            hardcoded namespace would be denied — read it from the environment.

    Failure mode is fire-and-forget, like the rest of this module: cost
    visibility is never a reason to fail a document's reporting write.
    """
    if namespace is None:
        namespace = os.environ.get("METRIC_NAMESPACE", "GENAIDP")

    metric_data: List[Dict[str, Any]] = [
        {
            "MetricName": "UnpricedMeteringRows",
            "Value": float(unpriced_row_count),
            "Unit": "Count",
            "Dimensions": [],
        }
    ]
    if estimated_cost is not None:
        # CloudWatch has no currency unit; "None" is its explicit dimensionless
        # unit, and is what makes the datapoint render without a bogus suffix.
        metric_data.insert(
            0,
            {
                "MetricName": "DocumentEstimatedCost",
                "Value": float(estimated_cost),
                "Unit": "None",
                "Dimensions": [],
            },
        )

    with _metric_lock:
        try:
            cloudwatch = get_cloudwatch_client()
            cloudwatch.put_metric_data(Namespace=namespace, MetricData=metric_data)
        except Exception as e:
            logger.warning(f"Failed to emit document cost metrics: {e}")


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
