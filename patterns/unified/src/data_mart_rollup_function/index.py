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
import os
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

    # nosec B608 — target_date/target_hour are derived from datetime, not user input
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_hourly"
        SELECT
            date_trunc('hour', "timestamp") AS hour_ts,
            config_version,
            service_api,
            unit,
            COUNT(DISTINCT document_id) AS n_docs,
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
    """Rollup the previous fully-sealed UTC day."""
    target_date = _previous_day()
    logger.info(f"Daily rollup targeting date={target_date}")
    if _partition_already_written(table="metering_daily", date=target_date):
        logger.info(
            f"metering_daily partition date={target_date} already exists — "
            f"skipping (idempotent)"
        )
        return {"mode": "daily", "target_date": target_date, "skipped": True}

    # nosec B608 — target_date is derived from datetime, not user input
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_daily"
        SELECT
            date '{target_date}' AS day,
            config_version,
            service_api,
            unit,
            SUM(n_docs) AS n_docs,
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


# ---------------------------------------------------------------------------
# CloudWatch metric fetching for control-plane Lambdas
# ---------------------------------------------------------------------------


def _discover_control_plane_lambdas() -> List[str]:
    """Return control-plane Lambda ARNs (all IDP Lambdas minus data-plane).

    "IDP Lambdas" = anything CloudFormation created in this stack. CFN
    auto-tags every resource with ``aws:cloudformation:stack-name`` — we
    filter on that instead of maintaining a custom ``idp:stack`` tag. Data
    plane is the small explicit whitelist tagged ``idp:plane=data``.
    Everything else is implicitly control plane. See §10.3.
    """
    if not STACK_NAME:
        logger.warning("STACK_NAME env var not set; cannot discover Lambdas")
        return []

    all_idp = _get_resources_by_tag({"aws:cloudformation:stack-name": [STACK_NAME]})
    data_plane = set(_get_resources_by_tag({"idp:plane": ["data"]}))

    control_plane = [arn for arn in all_idp if arn not in data_plane]

    # Emit a WARN log for any Lambda under `patterns/unified/` (data-plane
    # code home by convention) that lacks the tag — drift detector for the
    # location-based linter's blind spots.
    unified_prefix_hint = [
        "ocr",
        "classification",
        "extraction",
        "assessment",
        "summarization",
        "evaluation",
        "workflow",
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
) -> Dict[str, float]:
    """Aggregate the hour's CloudWatch metrics for one Lambda.

    Emits a single ``get_metric_data`` call with all four metrics batched:
    Duration (native), Invocations (native), AthenaBytesScanned (custom
    from ``IDPControlPlane`` namespace, if the Lambda emits it),
    BedrockInputTokens + BedrockOutputTokens (custom, same namespace).
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
        {
            "Id": "bedrock_in",
            "MetricStat": {
                "Metric": {
                    "Namespace": "IDPControlPlane",
                    "MetricName": "BedrockInputTokens",
                    "Dimensions": [{"Name": "Component", "Value": component}],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
        },
        {
            "Id": "bedrock_out",
            "MetricStat": {
                "Metric": {
                    "Namespace": "IDPControlPlane",
                    "MetricName": "BedrockOutputTokens",
                    "Dimensions": [{"Name": "Component", "Value": component}],
                },
                "Period": 3600,
                "Stat": "Sum",
            },
        },
    ]
    try:
        response = cloudwatch_client.get_metric_data(
            MetricDataQueries=query,
            StartTime=hour_start,
            EndTime=hour_end,
        )
    except Exception as e:
        logger.warning(f"get_metric_data failed for {function_name}: {e}. Skipping.")
        return {}
    return _flatten_cw_response(response)


def _flatten_cw_response(response: Dict[str, Any]) -> Dict[str, float]:
    """Turn ``get_metric_data`` output into a flat ``{id: sum}`` dict.

    A metric with no data (Lambda didn't hit that stat this hour) has an
    empty ``Values`` list — treat as 0.0. All queries in this Lambda use
    ``Period=3600``, so there's at most one value per metric per query.
    """
    result: Dict[str, float] = {}
    for r in response.get("MetricDataResults", []):
        values = r.get("Values") or []
        result[r["Id"]] = float(sum(values))
    return result


def _build_control_plane_rows(
    function_name: str,
    component: str,
    hour_ts: datetime,
    metrics: Dict[str, float],
) -> List[Dict[str, Any]]:
    """Compose one output row for the given function. Skips writing if
    the function had zero activity this hour — the row would be all
    zeros and just adds noise to Athena scans.
    """
    duration_ms = metrics.get("duration", 0.0)
    invocations = int(metrics.get("invocations", 0))
    if duration_ms == 0 and invocations == 0:
        # Function didn't run this hour — omit the row entirely.
        return []

    athena_bytes = int(metrics.get("athena_bytes", 0))
    tokens_in = int(metrics.get("bedrock_in", 0))
    tokens_out = int(metrics.get("bedrock_out", 0))

    # Duration is in milliseconds. Assume 512 MB Lambda memory for the
    # cost estimate (dashboard is best-effort, not billing-grade).
    lambda_gb_seconds = (duration_ms / 1000.0) * 0.512
    est_lambda_cost = lambda_gb_seconds * LAMBDA_ARM64_GB_SECOND_PRICE
    est_athena_cost = (athena_bytes / (1024**4)) * ATHENA_PRICE_PER_TB
    # For control_plane_hourly, bedrock_model is null at rollup time —
    # per-model breakdown would require iterating models within the
    # component (deferred; see §10.5).
    price = _bedrock_price_for_model(None)
    est_bedrock_cost = (
        tokens_in * price["in"] / 1000.0 + tokens_out * price["out"] / 1000.0
    )

    return [
        {
            "hour_ts": hour_ts,
            "function_name": function_name,
            "component": component,
            "bedrock_model": None,
            "invocations": invocations,
            "duration_ms_sum": int(duration_ms),
            "athena_bytes_sum": athena_bytes,
            "bedrock_tokens_in": tokens_in,
            "bedrock_tokens_out": tokens_out,
            "est_lambda_cost": est_lambda_cost,
            "est_athena_cost": est_athena_cost,
            "est_bedrock_cost": est_bedrock_cost,
        }
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


def _component_for_function(function_name: str) -> str:
    """Best-effort mapping from Lambda name → ``component`` label.

    Uses substring matching against the function's CloudFormation logical
    ID (which is embedded in the deployed function name). See §10.4 for
    the canonical set of component labels.
    """
    name = function_name.lower()
    if "monitoringmetrics" in name or "dashboardresolver" in name:
        return "monitor-dashboard"
    if "monitor" in name and "agent" in name:
        return "monitor-agent"
    if "analyticsagent" in name or "agentchat" in name or "agentprocessor" in name:
        return "analytics-agent"
    # User-driven chat with a document (not the analytics agent chat).
    if "chatwithdocument" in name or "chatstream" in name:
        return "doc-chat"
    if "testset" in name:
        return "test-set-mgmt"
    if "testrunner" in name or "filecopy" in name or "filecopier" in name:
        return "test-runner"
    if "testresults" in name or "testexecutionaggregation" in name or "mlflow" in name:
        return "test-results"
    if "config" in name and "resolver" in name:
        return "config-mgmt"
    if "capacity" in name:
        return "capacity-planner"
    if "discovery" in name or "policydiscovery" in name:
        return "policy-discovery"
    if "finetuning" in name:
        return "finetuning"
    if "datamartrollup" in name or "rollup" in name:
        return "rollup-lambda"
    # Cognito / user directory management (user CRUD, group sync).
    if "usermanagement" in name or "usersync" in name:
        return "user-mgmt"
    # Document status lookup + main HTTP API dispatcher — every UI page load.
    if "lookupfunction" in name or "apihandler" in name or "httpapidispatcher" in name:
        return "api-dispatch"
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
    """Poll get_query_execution until the query terminates."""
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
