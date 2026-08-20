# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Data Mart Rollup Lambda — populates metering_hourly, metering_daily,
and control_plane_hourly.

Two EventBridge schedules dispatch based on the ``mode`` field in the
event payload:

- ``{"mode": "hourly"}`` — every hour at :05 UTC. Writes ``metering_hourly``
  and ``control_plane_hourly`` for the previous fully-sealed hour.
- ``{"mode": "daily"}`` — every day at 00:15 UTC. Writes ``metering_daily``
  for the previous fully-sealed day, reading from ``metering_hourly``.

**Append-only.** Each partition is written once and never rewritten.
The ``metering`` table is partitioned by write time (= completion time,
see save_reporting_data.py + docs/reporting-sql-layer.md §2.3),
so metering rows never land in past partitions — no re-materialization
window needed.

Idempotency: the handler checks whether the target partition already has
data before writing (Athena queries with ``LIMIT 1``). If the partition
already exists, the handler skips the INSERT. This means a duplicate
EventBridge fire is safe.

See docs/reporting-sql-layer.md for the full design.
"""

import io
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Athena / Glue configuration passed in via env vars from CloudFormation.
DATABASE = os.environ.get("REPORTING_DATABASE", "")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
QUERY_OUTPUT_LOCATION = os.environ.get("ATHENA_QUERY_OUTPUT_LOCATION", "")
REPORTING_BUCKET = os.environ.get("REPORTING_BUCKET", "")
STACK_NAME = os.environ.get("STACK_NAME", "")

# Pricing constants — US-East-1 defaults. Sub-cent precision doesn't
# matter; these are best-effort estimates surfaced on the dashboard's
# Control Plane KPI, not billing-grade numbers.
ATHENA_PRICE_PER_TB = 5.0  # $5 per TB scanned
LAMBDA_ARM64_GB_SECOND_PRICE = 0.0000133334  # per GB-second
# Bedrock per-1K-token prices, keyed by model ID prefix. Fallback to
# Claude Sonnet pricing for unknown models — best-effort.
BEDROCK_PRICING = {
    "us.anthropic.claude-opus": {"in": 15.0, "out": 75.0},
    "us.anthropic.claude-sonnet": {"in": 3.0, "out": 15.0},
    "us.anthropic.claude-haiku": {"in": 0.80, "out": 4.0},
    "anthropic.claude-3-5-haiku": {"in": 0.80, "out": 4.0},
    "amazon.nova-pro": {"in": 0.80, "out": 3.20},
    "amazon.nova-lite": {"in": 0.06, "out": 0.24},
    "amazon.nova-micro": {"in": 0.035, "out": 0.14},
}
DEFAULT_BEDROCK_PRICE = {"in": 3.0, "out": 15.0}

athena_client = boto3.client("athena")
cloudwatch_client = boto3.client("cloudwatch")
tagging_client = boto3.client("resourcegroupstaggingapi")
s3_client = boto3.client("s3")
lambda_client = boto3.client("lambda")

# Cache Lambda MemorySize lookups within a single rollup invocation to avoid
# re-issuing get_function_configuration per (function, hour) call.
_lambda_memory_cache: Dict[str, int] = {}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Route between hourly and daily rollup modes.

    Default mode is ``hourly`` — makes ad-hoc invocations (e.g., a manual
    console test) do the more common thing without needing to remember
    the payload shape.
    """
    mode = event.get("mode", "hourly")
    logger.info(f"Rollup Lambda invoked with mode={mode!r}")

    if mode == "hourly":
        return _run_hourly()
    if mode == "daily":
        return _run_daily()
    raise ValueError(f"Unknown rollup mode: {mode!r} (expected 'hourly' or 'daily')")


# ---------------------------------------------------------------------------
# Hourly rollup — writes ``metering_hourly`` + ``control_plane_hourly``
# ---------------------------------------------------------------------------


def _run_hourly() -> Dict[str, Any]:
    """Rollup the previous fully-sealed UTC hour."""
    target_date, target_hour = _previous_hour()
    logger.info(f"Hourly rollup targeting date={target_date} hour={target_hour}")
    results = {
        "mode": "hourly",
        "target_date": target_date,
        "target_hour": target_hour,
        "metering_hourly": _rollup_metering_hourly(target_date, target_hour),
        "control_plane_hourly": _rollup_control_plane_hourly(target_date, target_hour),
    }
    logger.info(f"Hourly rollup complete: {results}")
    return results


def _rollup_metering_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Write ``metering_hourly`` for the given hour if not already written.

    Rollup dimensions: ``(hour_ts, document_class, config_version,
    service_api)``. Note that ``metering`` today has no ``document_class``
    column — Phase 1 uses ``config_version`` and ``service_api`` as the
    grouping dimensions, and adds ``document_class`` in a follow-up when
    the classification service starts emitting it into metering rows.
    """
    if _partition_already_written(
        table="metering_hourly", date=target_date, hour=target_hour
    ):
        logger.info(
            f"metering_hourly partition date={target_date} hour={target_hour} "
            f"already exists — skipping (idempotent)"
        )
        return {"skipped": True, "reason": "partition_exists"}

    # nosec B608 — target_date/target_hour are derived from datetime, not user input.
    # `n_doc_events` (not `n_docs`) — a doc reprocessed lands multiple
    # metering rows, and COUNT(DISTINCT document_id) at hour grain
    # de-dupes only within the hour. Consumers who need cross-hour
    # unique-doc counts should query raw metering.
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_hourly"
        SELECT
            date_trunc('hour', "timestamp") AS hour_ts,
            config_version,
            service_api,
            unit,
            COUNT(DISTINCT document_id) AS n_doc_events,
            SUM(value) AS sum_value,
            SUM(estimated_cost) AS sum_cost,
            SUM(number_of_pages) AS sum_pages,
            '{target_date}' AS date,
            '{target_hour}' AS hour
        FROM "{DATABASE}"."metering"
        WHERE date = '{target_date}' AND hour = '{target_hour}'
        GROUP BY 1, 2, 3, 4
    """  # nosec B608
    query_id = _run_athena(sql)
    return {"query_execution_id": query_id, "skipped": False}


def _rollup_control_plane_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Query CloudWatch for the previous hour's control-plane metrics
    and write one Parquet row per (function, component, model) to S3.

    Control-plane Lambdas are discovered via the CFN-native
    ``aws:cloudformation:stack-name`` tag (all IDP Lambdas carry it)
    minus those with ``idp:plane=data`` (the whitelisted per-doc
    processors). Everything else is implicitly control plane — see
    docs/reporting-sql-layer.md §10.3.
    """
    if _s3_object_exists(
        f"control_plane/date={target_date}/hour={target_hour}/data.parquet"
    ):
        logger.info(
            f"control_plane_hourly partition date={target_date} "
            f"hour={target_hour} already exists — skipping"
        )
        return {"skipped": True, "reason": "partition_exists"}

    control_arns = _discover_control_plane_lambdas()
    if not control_arns:
        logger.warning(
            "No control-plane Lambdas discovered (expected at least the "
            "rollup Lambda itself + others). Check that the stack's Lambdas "
            "carry the CFN-native aws:cloudformation:stack-name tag."
        )
        return {"skipped": True, "reason": "no_control_lambdas"}

    hour_start, hour_end = _hour_window(target_date, target_hour)
    rows: List[Dict[str, Any]] = []
    for function_arn in control_arns:
        function_name = function_arn.rsplit(":", 1)[-1]
        component = _component_for_function(function_name)
        metrics = _get_cw_metrics_for_function(
            function_name=function_name,
            component=component,
            hour_start=hour_start,
            hour_end=hour_end,
        )
        rows.extend(
            _build_control_plane_rows(
                function_name=function_name,
                component=component,
                hour_ts=hour_start,
                metrics=metrics,
            )
        )

    if not rows:
        logger.info(f"No control-plane activity for {target_date} hour={target_hour}")
        return {"skipped": True, "reason": "no_activity"}

    key = f"control_plane/date={target_date}/hour={target_hour}/data.parquet"
    _write_parquet(rows, key)
    return {"skipped": False, "rows": len(rows), "s3_key": key}


# ---------------------------------------------------------------------------
# Daily rollup — writes ``metering_daily`` from ``metering_hourly``
# ---------------------------------------------------------------------------


def _run_daily() -> Dict[str, Any]:
    """Rollup the previous fully-sealed UTC day.

    Before writing, verify that ``metering_hourly`` has all 24 hour
    partitions present for the target date. Writing an incomplete daily
    would be permanent — the per-partition idempotency skip means the
    row never gets recomputed even if the missing hourly arrives later.
    On incomplete input, raise so Lambda async-retry can replay after
    the hourly rollup catches up.
    """
    target_date = _previous_day()
    logger.info(f"Daily rollup targeting date={target_date}")
    if _partition_already_written(table="metering_daily", date=target_date):
        logger.info(
            f"metering_daily partition date={target_date} already exists — "
            f"skipping (idempotent)"
        )
        return {"mode": "daily", "target_date": target_date, "skipped": True}

    _require_all_24_hours_present(target_date)

    # nosec B608 — target_date is derived from datetime, not user input.
    # `n_doc_events` (not `n_docs`) — a doc reprocessed in a different hour
    # is counted once per hour, so summing across hours may exceed unique
    # doc count. Consumers who need unique-doc counts should query raw
    # `metering` with COUNT(DISTINCT document_id) — see §2 in the doc.
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_daily"
        SELECT
            date '{target_date}' AS day,
            config_version,
            service_api,
            unit,
            SUM(n_doc_events) AS n_doc_events,
            SUM(sum_value) AS sum_value,
            SUM(sum_cost) AS sum_cost,
            SUM(sum_pages) AS sum_pages,
            '{target_date}' AS date
        FROM "{DATABASE}"."metering_hourly"
        WHERE date = '{target_date}'
        GROUP BY 1, 2, 3, 4
    """  # nosec B608
    query_id = _run_athena(sql)
    return {
        "mode": "daily",
        "target_date": target_date,
        "query_execution_id": query_id,
        "skipped": False,
    }


def _require_all_24_hours_present(target_date: str) -> None:
    """Fail loudly if metering_hourly is missing any hour that raw metering
    has data for.

    We compare metering_hourly against RAW metering rather than "all 24
    hours" — a day may legitimately have fewer than 24 hours of data (deploy
    day, offline period, low-volume weekend) and demanding 24 would block
    the daily rollup forever for those days. The guard's real purpose is to
    catch the "transient outage caused an hourly rollup to fail while raw
    metering does have data for that hour" case — an actual data hole that
    the async retry can fix once the hourly rollup catches up. If both sets
    match, the day is faithfully rolled up; write metering_daily.
    """
    # nosec B608 — target_date is from datetime.strftime, not user input
    raw_sql = (
        f'SELECT DISTINCT hour FROM "{DATABASE}"."metering" '  # nosec B608
        f"WHERE date = '{target_date}'"  # nosec B608
    )
    hourly_sql = (
        f'SELECT DISTINCT hour FROM "{DATABASE}"."metering_hourly" '  # nosec B608
        f"WHERE date = '{target_date}'"  # nosec B608
    )
    raw_rows = _run_athena_query_with_results(raw_sql)
    hourly_rows = _run_athena_query_with_results(hourly_sql)
    raw_hours = {r[0] for r in raw_rows if r and r[0]}
    hourly_hours = {r[0] for r in hourly_rows if r and r[0]}
    missing = raw_hours - hourly_hours
    if missing:
        raise RuntimeError(
            f"metering_hourly for date={target_date} is missing hours "
            f"{sorted(missing)}. Refusing to write incomplete metering_daily; "
            f"async retry will replay once the hourly rollup catches up."
        )


# ---------------------------------------------------------------------------
# CloudWatch metric fetching for control-plane Lambdas
# ---------------------------------------------------------------------------


def _discover_control_plane_lambdas() -> List[str]:
    """Return control-plane Lambda ARNs (all IDP Lambdas minus data-plane).

    "IDP Lambdas" = anything CloudFormation created in this stack **tree**
    (root + nested). CFN auto-tags every resource with
    ``aws:cloudformation:stack-name`` set to the *immediate* stack that
    owns it — so a Lambda in a nested stack carries the nested stack's
    name, NOT the root. Filtering by root name alone misses everything
    in nested stacks (57 of 68 Lambdas on this repo's live topology).

    Fix: enumerate the full stack tree via ``cloudformation:ListStackResources``
    starting from the root stack, then pass every discovered stack
    name in the ``Values=[...]`` filter of the tag query.

    Data plane is the small explicit whitelist tagged ``idp:plane=data``.
    Everything else in the tree is implicitly control plane. See §10.3.
    """
    if not STACK_NAME:
        logger.warning("STACK_NAME env var not set; cannot discover Lambdas")
        return []

    stack_tree = _enumerate_stack_tree(STACK_NAME)
    logger.info(
        f"Stack tree from root {STACK_NAME!r}: {len(stack_tree)} stack(s) — {stack_tree}"
    )

    all_idp = _get_resources_by_tag(
        {"aws:cloudformation:stack-name": stack_tree}
    )
    # Scope the data-plane query to the SAME tree — a shared account with
    # multiple IDP stacks would otherwise cross-contaminate.
    data_plane = set(
        _get_resources_by_tag(
            {"aws:cloudformation:stack-name": stack_tree, "idp:plane": ["data"]}
        )
    )

    control_plane = [arn for arn in all_idp if arn not in data_plane]

    # Emit a WARN log for any Lambda that looks like a known data-plane
    # processor but lacks the tag — drift detector for the whitelist linter's
    # blind spots (e.g., a rename that didn't update DATA_PLANE_WHITELIST).
    unified_prefix_hint = [
        "ocr",
        "classification",
        "extraction",
        "assessment",
        "summarization",
        "evaluation",
        "workflowtracker",
    ]
    for arn in control_plane:
        function_name = arn.rsplit(":", 1)[-1].lower()
        if any(hint in function_name for hint in unified_prefix_hint):
            logger.warning(
                f"Possible untagged data-plane Lambda in control-plane set: "
                f"{arn} — expected idp:plane=data tag"
            )
    return control_plane


def _enumerate_stack_tree(root_stack_name: str) -> List[str]:
    """Walk the CFN stack tree BFS from the root, returning every
    stack name (root + all nested, at any depth).

    Uses ``cloudformation:ListStackResources`` — for each stack, any
    resource of type ``AWS::CloudFormation::Stack`` is a nested stack
    whose ``PhysicalResourceId`` is the child's ARN. Extract the child
    stack name from the ARN, recurse.
    """
    cfn = boto3.client("cloudformation")
    result: List[str] = [root_stack_name]
    to_visit = [root_stack_name]
    visited = {root_stack_name}
    while to_visit:
        current = to_visit.pop(0)
        try:
            paginator = cfn.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=current):
                for r in page.get("StackResourceSummaries", []):
                    if r.get("ResourceType") != "AWS::CloudFormation::Stack":
                        continue
                    # PhysicalResourceId is the nested stack's ARN:
                    #   arn:aws:cloudformation:region:acct:stack/<name>/<uuid>
                    arn = r.get("PhysicalResourceId") or ""
                    if not arn or "/" not in arn:
                        continue
                    nested_name = arn.split("/", 2)[1]
                    if nested_name in visited:
                        continue
                    visited.add(nested_name)
                    result.append(nested_name)
                    to_visit.append(nested_name)
        except Exception as e:
            # A single nested-stack listing failure shouldn't tank
            # discovery — log and continue with what we have.
            logger.warning(f"Failed to list resources of stack {current!r}: {e}")
    return result


def _get_resources_by_tag(tags: Dict[str, List[str]]) -> List[str]:
    """Fetch all Lambda ARNs matching the given tag filter. Paginated."""
    tag_filters = [{"Key": key, "Values": values} for key, values in tags.items()]
    arns: List[str] = []
    next_page: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {
            "TagFilters": tag_filters,
            "ResourceTypeFilters": ["lambda:function"],
        }
        if next_page:
            kwargs["PaginationToken"] = next_page
        response = tagging_client.get_resources(**kwargs)
        for mapping in response.get("ResourceTagMappingList", []):
            arns.append(mapping["ResourceARN"])
        next_page = response.get("PaginationToken") or None
        if not next_page:
            break
    return arns


def _get_cw_metrics_for_function(
    function_name: str,
    component: str,
    hour_start: datetime,
    hour_end: datetime,
) -> Dict[str, Any]:
    """Aggregate the hour's CloudWatch metrics for one Lambda.

    Returns a dict with:
      - ``duration_ms``, ``invocations`` (Lambda-scoped, native)
      - ``athena_bytes`` (Component-scoped, custom)
      - ``bedrock_by_model``: {model_id: {"in": tokens, "out": tokens}}
        Empty when the component didn't call Bedrock this hour.

    Bedrock metrics carry a ``Model`` dimension. GetMetricData requires
    exact dimension sets, so we ListMetrics first to discover which
    (Component, Model) pairs exist for this hour's namespace, then
    batch-query each. The helper (idp_common.metrics.emit_control_plane_cost_metric)
    is the sole emitter, so the dimension shape is contractual.

    Transient CloudWatch errors (Throttling, ServiceUnavailable) are
    re-raised so the caller can abort the whole rollup and let Lambda's
    async retry replay the hour — dropping a Lambda's row silently would
    hide the outage forever behind the per-partition idempotency skip.
    """
    query = [
        {
            "Id": "duration",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Duration",
                    "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
        },
        {
            "Id": "invocations",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": "Invocations",
                    "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
        },
        {
            "Id": "athena_bytes",
            "MetricStat": {
                "Metric": {
                    "Namespace": "IDPControlPlane",
                    "MetricName": "AthenaBytesScanned",
                    "Dimensions": [{"Name": "Component", "Value": component}],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
        },
    ]
    response = cloudwatch_client.get_metric_data(
        MetricDataQueries=query,
        StartTime=hour_start,
        EndTime=hour_end,
    )
    flat = _flatten_cw_response(response)
    return {
        "duration_ms": flat.get("duration", 0.0),
        "invocations": flat.get("invocations", 0.0),
        "athena_bytes": flat.get("athena_bytes", 0.0),
        "bedrock_by_model": _get_bedrock_tokens_by_model(
            component, hour_start, hour_end
        ),
    }


def _get_bedrock_tokens_by_model(
    component: str, hour_start: datetime, hour_end: datetime
) -> Dict[str, Dict[str, float]]:
    """List Bedrock token metrics for this component and return
    {model_id: {"in": tokens, "out": tokens}}.

    The emitter (``emit_control_plane_cost_metric``) writes metrics with
    dims ``[Component, Model]`` — GetMetricData needs an exact dimension
    set, so we ListMetrics first to discover the Model values that
    actually exist for this component, then query one aggregate per
    Model. See §10.5 in docs/reporting-sql-layer.md.
    """
    models: List[str] = []
    seen: set = set()
    for metric_name in ("BedrockInputTokens", "BedrockOutputTokens"):
        next_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {
                "Namespace": "IDPControlPlane",
                "MetricName": metric_name,
                "Dimensions": [
                    {"Name": "Component", "Value": component},
                ],
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cloudwatch_client.list_metrics(**kwargs)
            for m in resp.get("Metrics", []):
                for d in m.get("Dimensions", []):
                    if d.get("Name") == "Model" and d.get("Value") not in seen:
                        seen.add(d["Value"])
                        models.append(d["Value"])
            next_token = resp.get("NextToken")
            if not next_token:
                break
    if not models:
        return {}

    # Batch queries for all (model, direction) pairs into a single
    # GetMetricData call. IDs must be alphanumeric — sanitize model IDs.
    queries: List[Dict[str, Any]] = []
    id_to_key: Dict[str, Tuple[str, str]] = {}
    for i, model in enumerate(models):
        for direction, metric_name in (
            ("in", "BedrockInputTokens"),
            ("out", "BedrockOutputTokens"),
        ):
            qid = f"m{i}_{direction}"
            id_to_key[qid] = (model, direction)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "IDPControlPlane",
                            "MetricName": metric_name,
                            "Dimensions": [
                                {"Name": "Component", "Value": component},
                                {"Name": "Model", "Value": model},
                            ],
                        },
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                }
            )
    # GetMetricData caps at 500 queries per call. With ~few models per
    # component we're nowhere near it, but chunk just in case.
    result: Dict[str, Dict[str, float]] = {m: {"in": 0.0, "out": 0.0} for m in models}
    for chunk_start in range(0, len(queries), 500):
        chunk = queries[chunk_start : chunk_start + 500]
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=chunk,
            StartTime=hour_start,
            EndTime=hour_end,
        )
        flat = _flatten_cw_response(response)
        for qid, value in flat.items():
            model, direction = id_to_key[qid]
            result[model][direction] = value
    return result


def _flatten_cw_response(response: Dict[str, Any]) -> Dict[str, float]:
    """Turn ``get_metric_data`` output into a flat ``{id: sum}`` dict.

    Filters NaN values before summing — a broken metric occasionally
    yields NaN, which would poison an int() cast downstream. Empty
    Values (Lambda didn't hit that stat this hour) collapse to 0.0.
    All queries use ``Period=3600``, so at most one value per query.
    """
    result: Dict[str, float] = {}
    for r in response.get("MetricDataResults", []):
        values = [v for v in (r.get("Values") or []) if not math.isnan(v)]
        result[r["Id"]] = float(math.fsum(values))
    return result


def _get_lambda_memory_mb(function_name: str) -> int:
    """Return the Lambda's configured MemorySize in MB.

    Cached per invocation so we don't spam get_function_configuration —
    memory is a static property of the deployed function. Falls back to
    128 MB (the AWS default) on lookup failure so cost estimates are
    conservative rather than zero.
    """
    cached = _lambda_memory_cache.get(function_name)
    if cached is not None:
        return cached
    try:
        response = lambda_client.get_function_configuration(FunctionName=function_name)
        memory_mb = int(response.get("MemorySize", 128))
    except Exception as e:
        logger.warning(
            f"get_function_configuration failed for {function_name}: {e}. "
            f"Assuming default 128 MB — cost estimate may be low."
        )
        memory_mb = 128
    _lambda_memory_cache[function_name] = memory_mb
    return memory_mb


def _build_control_plane_rows(
    function_name: str,
    component: str,
    hour_ts: datetime,
    metrics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compose output rows for the given function.

    Emits one row per (function, model) — the ``bedrock_by_model`` dict
    can have zero, one, or many entries. When zero, emits a single row
    with ``bedrock_model=None`` capturing Lambda+Athena cost only.

    Skips writing if the function had zero activity this hour — an
    all-zero row just adds noise to Athena scans.
    """
    duration_ms = float(metrics.get("duration_ms", 0.0))
    invocations = int(metrics.get("invocations", 0.0))
    if duration_ms == 0 and invocations == 0:
        return []

    athena_bytes = int(metrics.get("athena_bytes", 0.0))
    memory_mb = _get_lambda_memory_mb(function_name)
    lambda_gb_seconds = (duration_ms / 1000.0) * (memory_mb / 1024.0)
    est_lambda_cost = lambda_gb_seconds * LAMBDA_ARM64_GB_SECOND_PRICE
    est_athena_cost = (athena_bytes / (1024**4)) * ATHENA_PRICE_PER_TB

    bedrock_by_model = metrics.get("bedrock_by_model") or {}

    # Common (Lambda + Athena) fields shared across per-model rows.
    def _row(model: Optional[str], tokens_in: int, tokens_out: int) -> Dict[str, Any]:
        price = _bedrock_price_for_model(model)
        est_bedrock_cost = (
            tokens_in * price["in"] / 1000.0 + tokens_out * price["out"] / 1000.0
        )
        return {
            "hour_ts": hour_ts,
            "function_name": function_name,
            "component": component,
            "bedrock_model": model,
            "invocations": invocations,
            "duration_ms_sum": int(duration_ms),
            "athena_bytes_sum": athena_bytes,
            "bedrock_tokens_in": tokens_in,
            "bedrock_tokens_out": tokens_out,
            "est_lambda_cost": est_lambda_cost,
            "est_athena_cost": est_athena_cost,
            "est_bedrock_cost": est_bedrock_cost,
        }

    if not bedrock_by_model:
        # Component didn't call Bedrock this hour — one row without a model.
        return [_row(None, 0, 0)]
    # One row per Bedrock model. Cost bookkeeping is straightforward this way,
    # and consumers can group by ``bedrock_model`` for per-model drill-down.
    return [
        _row(model, int(tokens["in"]), int(tokens["out"]))
        for model, tokens in bedrock_by_model.items()
    ]


def _bedrock_price_for_model(model: Optional[str]) -> Dict[str, float]:
    """Return per-1K-token pricing for a Bedrock model, falling back to
    Claude Sonnet defaults if the model is unknown.
    """
    if model:
        for prefix, price in BEDROCK_PRICING.items():
            if model.startswith(prefix):
                return price
    return DEFAULT_BEDROCK_PRICE


# Component-mapping rules — ORDER MATTERS. First match wins. Rules are
# regexes compiled against the lower-cased function name. Ordering is
# from most-specific to least-specific so a broad rule (e.g. ``config``)
# doesn't accidentally catch a Lambda a more-specific rule would claim.
# See §10.2 in docs/reporting-sql-layer.md for the canonical label set.
_COMPONENT_RULES: List[Tuple[re.Pattern, str]] = [
    # Monitor (marketplace) dashboard resolver + AI-summary agent.
    (re.compile(r"monitoringmetrics|dashboardresolver"), "monitor-dashboard"),
    (re.compile(r"monitor.*agent"), "monitor-agent"),
    # Rollup Lambda itself — matched before generic 'rollup' catch-alls
    # so a future 'rollup-*' Lambda doesn't get grabbed.
    (re.compile(r"datamartrollup|rollup"), "rollup-lambda"),
    # Test infrastructure — all matched here so 'testresults' / 'testrunner'
    # don't fall through to 'test-set-mgmt' via the 'testset' rule.
    (re.compile(r"testresults|testexecutionaggregation|mlflow"), "test-results"),
    (re.compile(r"testrunner|filecopy|filecopier"), "test-runner"),
    (re.compile(r"testset"), "test-set-mgmt"),
    # Analytics agents (SQL-driven) and doc-chat processors — matched
    # before broader user/agent patterns.
    (
        re.compile(r"analyticsagent|agentchat|agentprocessor"),
        "analytics-agent",
    ),
    (re.compile(r"chatwithdocument|chatstream"), "doc-chat"),
    # Policy discovery (more specific than 'config').
    (re.compile(r"policydiscovery|discovery"), "policy-discovery"),
    # Config CRUD — narrower than 'config' alone, requires 'resolver' suffix.
    (re.compile(r"config.*resolver"), "config-mgmt"),
    (re.compile(r"capacity"), "capacity-planner"),
    (re.compile(r"finetuning"), "finetuning"),
    # Cognito / user-directory management.
    (re.compile(r"usermanagement|usersync"), "user-mgmt"),
    # UI-facing dispatchers (every page load hits these).
    (
        re.compile(r"lookupfunction|apihandler|httpapidispatcher"),
        "api-dispatch",
    ),
]


def _component_for_function(function_name: str) -> str:
    """Best-effort mapping from Lambda name → ``component`` label.

    Uses regex matching against the lower-cased function name. Rules are
    ordered from most-specific to least-specific in ``_COMPONENT_RULES``
    (see comment above the list). Unmatched Lambdas fall through to
    ``other-control`` — an explicit fallback the dashboard can flag so
    operators know to extend the rules or investigate a new feature.
    """
    name = function_name.lower()
    for pattern, label in _COMPONENT_RULES:
        if pattern.search(name):
            return label
    return "other-control"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _previous_hour() -> Tuple[str, str]:
    """Return (YYYY-MM-DD, HH) for the most recently-sealed UTC hour."""
    now = datetime.now(timezone.utc)
    prev = now - timedelta(hours=1)
    return prev.strftime("%Y-%m-%d"), prev.strftime("%H")


def _previous_day() -> str:
    """Return YYYY-MM-DD for the most recently-sealed UTC day."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


def _hour_window(date_str: str, hour_str: str) -> Tuple[datetime, datetime]:
    """UTC datetime bounds of the (date, hour) partition."""
    start = datetime.strptime(
        f"{date_str} {hour_str}:00:00", "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    return start, start + timedelta(hours=1)


def _partition_already_written(
    table: str, date: str, hour: Optional[str] = None
) -> bool:
    """Cheap idempotency check — does the target partition already have
    at least one row?
    """
    where = f"date = '{date}'"
    if hour is not None:
        where += f" AND hour = '{hour}'"
    sql = f'SELECT 1 FROM "{DATABASE}"."{table}" WHERE {where} LIMIT 1'  # nosec B608
    try:
        rows = _run_athena_query_with_results(sql)
        return bool(rows)
    except Exception as e:
        # If the query fails (e.g., table doesn't exist yet on first
        # invocation after deploy), treat as "not written" and let the
        # INSERT proceed. First INSERT to a table on Athena creates
        # partitions on demand.
        logger.info(
            f"Idempotency check failed for {table} — assuming not written. ({e})"
        )
        return False


def _run_athena(sql: str) -> str:
    """Start an Athena query and wait for completion. Returns QueryExecutionId."""
    if not DATABASE:
        raise RuntimeError("REPORTING_DATABASE env var not set")
    response = athena_client.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": DATABASE},
        WorkGroup=WORKGROUP,
        ResultConfiguration={"OutputLocation": QUERY_OUTPUT_LOCATION}
        if QUERY_OUTPUT_LOCATION
        else {},
    )
    query_id = response["QueryExecutionId"]
    _wait_for_athena(query_id)
    return query_id


def _run_athena_query_with_results(sql: str) -> List[List[str]]:
    """Run a query and return result rows (as string lists)."""
    query_id = _run_athena(sql)
    result = athena_client.get_query_results(QueryExecutionId=query_id)
    # Skip header row.
    return [
        [c.get("VarCharValue", "") for c in r.get("Data", [])]
        for r in result.get("ResultSet", {}).get("Rows", [])[1:]
    ]


def _wait_for_athena(query_id: str, timeout_sec: int = 300) -> None:
    """Poll get_query_execution until the query terminates.

    On timeout, call StopQueryExecution before raising — otherwise an
    orphaned Athena query keeps scanning (and billing) after we've
    given up on it, and a retry starts a fresh one on top.
    """
    started = time.time()
    while True:
        response = athena_client.get_query_execution(QueryExecutionId=query_id)
        state = response["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return
        if state in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get(
                "StateChangeReason", "unknown"
            )
            raise RuntimeError(
                f"Athena query {query_id} ended in state {state}: {reason}"
            )
        if time.time() - started > timeout_sec:
            try:
                athena_client.stop_query_execution(QueryExecutionId=query_id)
                logger.warning(
                    f"Athena query {query_id} timed out — stop_query_execution issued."
                )
            except Exception as stop_err:
                logger.warning(
                    f"stop_query_execution({query_id}) failed: {stop_err}"
                )
            raise TimeoutError(
                f"Athena query {query_id} did not complete in {timeout_sec}s"
            )
        time.sleep(1)


def _s3_object_exists(key: str) -> bool:
    """Return True if a bucket key already exists."""
    if not REPORTING_BUCKET:
        return False
    try:
        s3_client.head_object(Bucket=REPORTING_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _write_parquet(rows: List[Dict[str, Any]], key: str) -> None:
    """Serialize rows to Parquet and upload to the reporting bucket."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            ("hour_ts", pa.timestamp("ms")),
            ("function_name", pa.string()),
            ("component", pa.string()),
            ("bedrock_model", pa.string()),
            ("invocations", pa.int64()),
            ("duration_ms_sum", pa.int64()),
            ("athena_bytes_sum", pa.int64()),
            ("bedrock_tokens_in", pa.int64()),
            ("bedrock_tokens_out", pa.int64()),
            ("est_lambda_cost", pa.float64()),
            ("est_athena_cost", pa.float64()),
            ("est_bedrock_cost", pa.float64()),
        ]
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)
    s3_client.put_object(
        Bucket=REPORTING_BUCKET,
        Key=key,
        Body=buf.getvalue(),
        ContentType="application/octet-stream",
    )
    logger.info(f"Wrote {len(rows)} rows to s3://{REPORTING_BUCKET}/{key}")
