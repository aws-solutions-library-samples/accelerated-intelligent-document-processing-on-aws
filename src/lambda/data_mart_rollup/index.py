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
# Lambda Duration is billed in GB-seconds; the rate depends on the function's
# architecture. Missing invocation request pricing before → ~20% under-count
# on any control-plane Lambda that isn't ARM64 (most of the root-stack
# Lambdas don't set Architectures and default to x86_64).
LAMBDA_ARM64_GB_SECOND_PRICE = 0.0000133334  # per GB-second on arm64
LAMBDA_X86_64_GB_SECOND_PRICE = 0.0000166667  # per GB-second on x86_64
LAMBDA_REQUEST_PRICE = 0.20 / 1_000_000  # $0.20 per 1M requests, both archs
# Bedrock pricing is the single source of truth at ``config_library/pricing.yaml``
# (deployed into the ConfigurationTable in DynamoDB and used by every data-plane
# Lambda that emits ``estimated_cost``). This rollup Lambda reads the same
# source at cold start so its cost columns can never drift from data-plane
# math. Prices there are **per-token USD** (e.g. ``3.0E-7`` = $0.30 / million).
# See ``_load_bedrock_pricing_from_config`` below.
#
# Small hardcoded fallback for the case where the DynamoDB read itself fails
# (throttling, table missing during initial deploy) — Sonnet defaults at the
# per-token scale. Kept small on purpose; drift is not silent because
# `_bedrock_price_for_model` logs which path answered.
DEFAULT_BEDROCK_PRICE_PER_TOKEN = {"in": 3.0e-6, "out": 15.0e-6}

# Module-level pricing cache. Populated lazily on first Bedrock cost lookup;
# survives across warm invocations of the same Lambda container.
_bedrock_pricing_map: Optional[Dict[str, Dict[str, float]]] = None
CONFIGURATION_TABLE_NAME = os.environ.get("CONFIGURATION_TABLE_NAME", "")

athena_client = boto3.client("athena")
cloudwatch_client = boto3.client("cloudwatch")
tagging_client = boto3.client("resourcegroupstaggingapi")
s3_client = boto3.client("s3")
lambda_client = boto3.client("lambda")

# Cache Lambda config lookups within a single rollup invocation to avoid
# re-issuing get_function_configuration per (function, hour) call.
# Cache: function_name -> (memory_mb, architecture). Architecture defaults
# to "x86_64" when the SDK doesn't return it or the lookup fails.
_lambda_memory_cache: Dict[str, Tuple[int, str]] = {}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Route between hourly and daily rollup modes.

    Default mode is ``hourly`` — makes ad-hoc invocations (e.g., a manual
    console test) do the more common thing without needing to remember
    the payload shape.
    """
    mode = event.get("mode", "hourly")
    # Anchor the target hour/day to the EventBridge trigger time (`time`
    # field on scheduled events) rather than wall-clock. This matters on
    # async retries that cross an hour or day boundary: without it, a
    # retry silently rolls up the NEXT partition and abandons the failed
    # one forever. Falls back to now() for ad-hoc invocations that don't
    # include a time field (manual `aws lambda invoke`).
    anchor = _parse_anchor_time(event)
    logger.info(f"Rollup Lambda invoked with mode={mode!r} anchor={anchor.isoformat()}")

    if mode == "hourly":
        return _run_hourly(anchor)
    if mode == "daily":
        return _run_daily(anchor)
    raise ValueError(f"Unknown rollup mode: {mode!r} (expected 'hourly' or 'daily')")


def _parse_anchor_time(event: Dict[str, Any]) -> datetime:
    """Return the UTC anchor time for ``previous_hour``/``previous_day``.

    Prefers ``event["time"]`` (EventBridge sets this to ISO 8601 UTC on
    scheduled events) so async retries pin to the ORIGINAL trigger time,
    not wall-clock — a retry that crossed a boundary would otherwise
    silently target the wrong partition. Falls back to
    ``datetime.now(UTC)`` for manual invokes that don't include a time.
    """
    raw = event.get("time")
    if raw:
        try:
            # EventBridge uses ISO 8601 with a trailing "Z"; normalize
            # to "+00:00" so fromisoformat handles it on all Python 3.11+.
            normalized = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (ValueError, TypeError) as e:
            logger.warning(
                f"Failed to parse event['time']={raw!r} ({e}); falling back to now()"
            )
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Hourly rollup — writes ``metering_hourly`` + ``control_plane_hourly``
# ---------------------------------------------------------------------------


def _run_hourly(anchor: Optional[datetime] = None) -> Dict[str, Any]:
    """Rollup the previous fully-sealed UTC hour relative to ``anchor``
    (defaults to now — see ``_parse_anchor_time`` for the retry-safe path)."""
    target_date, target_hour = _previous_hour(anchor)
    logger.info(f"Hourly rollup targeting date={target_date} hour={target_hour}")
    results = {
        "mode": "hourly",
        "target_date": target_date,
        "target_hour": target_hour,
        "metering_hourly": _rollup_metering_hourly(target_date, target_hour),
        "metering_docs_hourly": _rollup_metering_docs_hourly(target_date, target_hour),
        "control_plane_hourly": _rollup_control_plane_hourly(target_date, target_hour),
    }
    logger.info(f"Hourly rollup complete: {results}")
    return results


def _rollup_metering_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Write ``metering_hourly`` (cost per service/unit) for the given hour
    if not already written.

    Rollup dimensions: ``(hour_ts, config_version, service_api, unit)``.
    Cost-only columns: sum_value, sum_cost. Document-level metrics
    (n_docs, sum_pages) live in a separate table ``metering_docs_hourly``
    because pages and unique-doc counts fan out across (service_api, unit)
    — including them here would produce a 6× overcount for a doc with 6
    service rows.
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
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_hourly"
        SELECT
            date_trunc('hour', "timestamp") AS hour_ts,
            config_version,
            service_api,
            unit,
            SUM(value) AS sum_value,
            SUM(estimated_cost) AS sum_cost,
            '{target_date}' AS date,
            '{target_hour}' AS hour
        FROM "{DATABASE}"."metering"
        WHERE date = '{target_date}' AND hour = '{target_hour}'
        GROUP BY 1, 2, 3, 4
    """  # nosec B608
    query_id = _run_athena(sql)
    return {"query_execution_id": query_id, "skipped": False}


def _rollup_metering_docs_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Write ``metering_docs_hourly`` (doc-grain volume + pages) for the
    given hour if not already written.

    Grain: ``(hour_ts, config_version)`` — one row per config_version per
    hour, NOT per service_api. ``number_of_pages`` is a document-level
    value stamped identically on every metering row for that doc, so
    grouping by service_api would fan out the page count by the number
    of (service_api, unit) combinations a doc touched.

    SQL: outer aggregate over a doc-grain subquery that MAX()-collapses
    the per-doc fan-out first.
    """
    if _partition_already_written(
        table="metering_docs_hourly", date=target_date, hour=target_hour
    ):
        logger.info(
            f"metering_docs_hourly partition date={target_date} "
            f"hour={target_hour} already exists — skipping (idempotent)"
        )
        return {"skipped": True, "reason": "partition_exists"}

    # nosec B608 — target_date/target_hour are derived from datetime, not user input.
    # Inner subquery: one row per (hour_ts, config_version, document_id)
    # with MAX(number_of_pages) — pages is stamped identically across
    # every metering row for a doc, so MAX is the doc's page count.
    # Outer aggregate: COUNT(*) of docs, SUM of the MAX-per-doc pages.
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_docs_hourly"
        SELECT
            hour_ts,
            config_version,
            COUNT(*) AS n_docs,
            SUM(max_pages) AS sum_pages,
            '{target_date}' AS date,
            '{target_hour}' AS hour
        FROM (
            SELECT
                date_trunc('hour', "timestamp") AS hour_ts,
                config_version,
                document_id,
                MAX(number_of_pages) AS max_pages
            FROM "{DATABASE}"."metering"
            WHERE date = '{target_date}' AND hour = '{target_hour}'
            GROUP BY 1, 2, 3
        )
        GROUP BY 1, 2
    """  # nosec B608
    query_id = _run_athena(sql)
    return {"query_execution_id": query_id, "skipped": False}


def _rollup_control_plane_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Query CloudWatch for the previous hour's control-plane metrics
    and write one Parquet row per (function, component, model) to S3.

    Control-plane Lambdas are discovered via the CFN-native
    ``aws:cloudformation:stack-name`` tag (all IDP Lambdas carry it)
    minus those with ``idp:plane=data`` (the allowlisted per-doc
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


def _run_daily(anchor: Optional[datetime] = None) -> Dict[str, Any]:
    """Rollup the previous fully-sealed UTC day relative to ``anchor``
    — writes both ``metering_daily`` (cost) and ``metering_docs_daily``
    (doc-grain volume/pages).

    Before writing, verify that every hour present in raw metering is
    also present in ``metering_hourly`` for the target date (see
    ``_require_hourly_matches_raw_metering``). Writing an incomplete
    daily would be permanent — the per-partition idempotency skip means
    the row never gets recomputed even if the missing hourly arrives
    later. On incomplete input, raise so Lambda async-retry can replay
    after the hourly rollup catches up.
    """
    target_date = _previous_day(anchor)
    logger.info(f"Daily rollup targeting date={target_date}")

    result: Dict[str, Any] = {"mode": "daily", "target_date": target_date}

    _require_hourly_matches_raw_metering(target_date)

    # --- metering_daily (cost per service/unit) ---
    if _partition_already_written(table="metering_daily", date=target_date):
        logger.info(
            f"metering_daily partition date={target_date} already exists — "
            f"skipping (idempotent)"
        )
        result["metering_daily"] = {"skipped": True}
    else:
        # nosec B608 — target_date is derived from datetime, not user input.
        sql = f"""
            INSERT INTO "{DATABASE}"."metering_daily"
            SELECT
                date '{target_date}' AS day,
                config_version,
                service_api,
                unit,
                SUM(sum_value) AS sum_value,
                SUM(sum_cost) AS sum_cost,
                '{target_date}' AS date
            FROM "{DATABASE}"."metering_hourly"
            WHERE date = '{target_date}'
            GROUP BY 1, 2, 3, 4
        """  # nosec B608
        result["metering_daily"] = {
            "query_execution_id": _run_athena(sql),
            "skipped": False,
        }

    # --- metering_docs_daily (doc-grain volume/pages) ---
    if _partition_already_written(table="metering_docs_daily", date=target_date):
        logger.info(
            f"metering_docs_daily partition date={target_date} already exists — "
            f"skipping (idempotent)"
        )
        result["metering_docs_daily"] = {"skipped": True}
    else:
        # Sums the hourly doc-grain rollups. A doc reprocessed across
        # multiple hours is counted once per hour (a "doc-hour"), same
        # for its pages. For strict cross-day unique-doc counts, query
        # raw metering with COUNT(DISTINCT document_id). See §2 in the doc.
        # nosec B608 — target_date is derived from datetime, not user input.
        sql = f"""
            INSERT INTO "{DATABASE}"."metering_docs_daily"
            SELECT
                date '{target_date}' AS day,
                config_version,
                SUM(n_docs) AS n_docs,
                SUM(sum_pages) AS sum_pages,
                '{target_date}' AS date
            FROM "{DATABASE}"."metering_docs_hourly"
            WHERE date = '{target_date}'
            GROUP BY 1, 2
        """  # nosec B608
        result["metering_docs_daily"] = {
            "query_execution_id": _run_athena(sql),
            "skipped": False,
        }

    # Legacy top-level keys for backward-compat with the existing test
    # + operator invocation shape (skipped/query_execution_id flags).
    result["skipped"] = bool(result["metering_daily"].get("skipped"))
    if "query_execution_id" in result["metering_daily"]:
        result["query_execution_id"] = result["metering_daily"]["query_execution_id"]
    return result


def _require_hourly_matches_raw_metering(target_date: str) -> None:
    """Fail loudly if either hourly rollup is missing any hour that raw
    metering has data for (deploy-day exception below).

    Guards **both** ``metering_hourly`` and ``metering_docs_hourly`` —
    the rollup writes them sequentially, so a transient Athena outage
    could leave one populated and the other empty for the same hour.
    Checking only ``metering_hourly`` would let an incomplete
    ``metering_docs_daily`` land and become idempotently locked, silently
    under-counting ``n_docs``/``sum_pages`` for that day forever.

    We compare each hourly against RAW metering rather than "all 24
    hours" — a day may legitimately have fewer than 24 hours of data (deploy
    day, offline period, low-volume weekend) and demanding 24 would block
    the daily rollup forever for those days. The guard's real purpose is to
    catch the "transient outage caused an hourly rollup to fail while raw
    metering does have data for that hour" case — an actual data hole that
    the async retry can fix once the hourly rollup catches up.

    Deploy-day exception: raw ``metering`` predates this rollup Lambda by
    however long the stack has been up, so on the first daily invocation
    after deploy raw will have hours the hourly rollup will *never*
    backfill — the hourly cron only ever targets ``previous_hour(anchor)``,
    never a historical hour. Blocking daily forever on this would poison
    the first-ever daily rollup and every subsequent one (idempotency
    skip means no re-attempt). We treat "hourly is completely empty for
    the target date" as the deploy-day case (per-table) and skip that
    table's guard; and otherwise only require raw hours ≥ the earliest
    hourly-written hour to be present. Real transient-outage misses in
    the go-forward hourly window still fail loudly and get replayed by
    async retry.
    """
    # nosec B608 — target_date is from datetime.strftime, not user input
    raw_sql = (
        f'SELECT DISTINCT hour FROM "{DATABASE}"."metering" '  # nosec B608
        f"WHERE date = '{target_date}'"  # nosec B608
    )
    raw_rows = _run_athena_query_with_results(raw_sql)
    raw_hours = {r[0] for r in raw_rows if r and r[0]}
    if not raw_hours:
        # No raw data for the day → nothing to check either hourly against.
        return
    for hourly_table in ("metering_hourly", "metering_docs_hourly"):
        hourly_sql = (
            f'SELECT DISTINCT hour FROM "{DATABASE}"."{hourly_table}" '  # nosec B608
            f"WHERE date = '{target_date}'"  # nosec B608
        )
        hourly_rows = _run_athena_query_with_results(hourly_sql)
        hourly_hours = {r[0] for r in hourly_rows if r and r[0]}
        if not hourly_hours:
            logger.info(
                f"{hourly_table} for date={target_date} is empty — treating "
                f"as first-run/deploy-day and skipping raw-vs-hourly guard "
                f"for this table. raw hours present: {sorted(raw_hours)}"
            )
            continue
        earliest_hourly = min(hourly_hours)
        in_window_raw = {h for h in raw_hours if h >= earliest_hourly}
        missing = in_window_raw - hourly_hours
        if missing:
            raise RuntimeError(
                f"{hourly_table} for date={target_date} is missing hours "
                f"{sorted(missing)} within the hourly-rollup window "
                f"(earliest hourly-written hour = {earliest_hourly!r}). "
                f"Refusing to write incomplete daily rollups; async retry "
                f"will replay once the hourly rollup catches up."
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

    Data plane is the small explicit allowlist tagged ``idp:plane=data``.
    Everything else in the tree is implicitly control plane. See §10.3.
    """
    if not STACK_NAME:
        logger.warning("STACK_NAME env var not set; cannot discover Lambdas")
        return []

    stack_tree = _enumerate_stack_tree(STACK_NAME)
    logger.info(
        f"Stack tree from root {STACK_NAME!r}: {len(stack_tree)} stack(s) — {stack_tree}"
    )

    all_idp = _get_resources_by_tag({"aws:cloudformation:stack-name": stack_tree})
    # Scope the data-plane query to the SAME tree — a shared account with
    # multiple IDP stacks would otherwise cross-contaminate.
    data_plane = set(
        _get_resources_by_tag(
            {"aws:cloudformation:stack-name": stack_tree, "idp:plane": ["data"]}
        )
    )

    control_plane = [arn for arn in all_idp if arn not in data_plane]

    # Emit a WARN log for any Lambda that looks like a known data-plane
    # processor but lacks the tag — drift detector for the allowlist linter's
    # blind spots (e.g., a rename that didn't update DATA_PLANE_ALLOWLIST).
    unified_prefix_hint = [
        "ocr",
        "classification",
        "extraction",
        "assessment",
        "summarization",
        "evaluation",
        "workflowtracker",
        # BDA + Rule Validation + result-stitcher — all per-doc, all should
        # carry idp:plane=data. Missing here previously meant a rename to
        # e.g. RuleValidationFunctionV2 wouldn't have been surfaced.
        "rulevalidation",
        "bda",
        "processresults",
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
        "athena_bytes": _get_athena_bytes_sum(function_name, hour_start, hour_end),
        "bedrock_by_model": _get_bedrock_tokens_by_model(
            function_name, hour_start, hour_end
        ),
    }


def _get_athena_bytes_sum(
    function_name: str,
    hour_start: datetime,
    hour_end: datetime,
) -> float:
    """Sum ``IDPControlPlane/AthenaBytesScanned`` for this function over the
    hour.

    CloudWatch identifies metrics by their **full** dimension set — a
    GetMetricData query with a *subset* of the emitted dims (e.g. only
    ``FunctionName``) matches no metric at all and returns 0 datapoints
    silently. The emitter (``idp_common.metrics.emit_control_plane_cost_metric``)
    always publishes AthenaBytesScanned with dims
    ``[Component, FunctionName]``. To read those back reliably, we
    ``ListMetrics`` first — filtered by ``FunctionName`` (subset filter is
    fine on ListMetrics) — to discover the full dim signatures the
    emitter actually used for this function, then ``GetMetricData`` with
    each signature's dim set verbatim.
    """
    signatures = _list_ipdcp_metric_signatures(
        metric_name="AthenaBytesScanned",
        function_name=function_name,
    )
    if not signatures:
        return 0.0
    queries: List[Dict[str, Any]] = []
    for i, dims in enumerate(signatures):
        queries.append(
            {
                "Id": f"a{i}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "IDPControlPlane",
                        "MetricName": "AthenaBytesScanned",
                        "Dimensions": dims,
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
            }
        )
    total = 0.0
    for chunk_start in range(0, len(queries), 500):
        chunk = queries[chunk_start : chunk_start + 500]
        resp = cloudwatch_client.get_metric_data(
            MetricDataQueries=chunk,
            StartTime=hour_start,
            EndTime=hour_end,
        )
        for r in resp.get("MetricDataResults", []):
            # Filter NaN before summing — a broken metric occasionally yields
            # NaN, which would poison the int() cast at the athena_bytes_sum
            # cast site downstream (raises ValueError, aborts the rollup,
            # lands in the DLQ). Sibling paths (_flatten_cw_response,
            # _get_bedrock_tokens_by_model) filter — this one must too.
            values = [v for v in (r.get("Values") or []) if not math.isnan(v)]
            total += float(math.fsum(values))
    return total


def _list_ipdcp_metric_signatures(
    metric_name: str, function_name: str
) -> List[List[Dict[str, str]]]:
    """Return the full dim signatures emitted for
    ``IDPControlPlane/<metric_name>`` by ``function_name``.

    ListMetrics with a ``Dimensions=[{FunctionName}]`` filter returns
    every metric whose dim set *contains* FunctionName — i.e. the exact
    metrics we want to read back. Each returned metric's ``Dimensions``
    field is the full dim set as published, which we pass verbatim to
    GetMetricData so the identity match hits.
    """
    signatures: List[List[Dict[str, str]]] = []
    seen: set = set()
    next_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {
            "Namespace": "IDPControlPlane",
            "MetricName": metric_name,
            "Dimensions": [{"Name": "FunctionName", "Value": function_name}],
        }
        if next_token:
            kwargs["NextToken"] = next_token
        resp = cloudwatch_client.list_metrics(**kwargs)
        for m in resp.get("Metrics", []):
            dims = m.get("Dimensions", []) or []
            # De-dupe by canonical (sorted) dim tuple.
            key = tuple(sorted((d["Name"], d["Value"]) for d in dims))
            if key in seen:
                continue
            seen.add(key)
            signatures.append(dims)
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return signatures


def _get_bedrock_tokens_by_model(
    function_name: str,
    hour_start: datetime,
    hour_end: datetime,
) -> Dict[str, Dict[str, float]]:
    """List Bedrock token metrics for this function and return
    ``{model_id: {"in": tokens, "out": tokens}}``.

    CloudWatch identifies metrics by their **full** dimension set — a
    GetMetricData with a *subset* of the emitted dims returns 0 datapoints
    silently. The emitter publishes BedrockInput/OutputTokens with
    ``[Component, FunctionName, Model]``. We ListMetrics with a
    FunctionName filter (subset filter is fine on ListMetrics) to
    discover each emitted metric's full dim signature, then GetMetricData
    with that signature verbatim. See §10.5 in docs/reporting-sql-layer.md.
    """
    result: Dict[str, Dict[str, float]] = {}
    for direction, metric_name in (
        ("in", "BedrockInputTokens"),
        ("out", "BedrockOutputTokens"),
    ):
        signatures = _list_ipdcp_metric_signatures(
            metric_name=metric_name,
            function_name=function_name,
        )
        if not signatures:
            continue
        queries: List[Dict[str, Any]] = []
        id_to_model: Dict[str, str] = {}
        for i, dims in enumerate(signatures):
            model = next((d["Value"] for d in dims if d.get("Name") == "Model"), None)
            if model is None:
                # Emitter guarantees Model for bedrock metrics; a
                # signature without one is malformed — skip loudly.
                logger.warning(
                    f"Bedrock metric signature missing Model dim for "
                    f"{function_name!r}: {dims!r}"
                )
                continue
            qid = f"b{i}"
            id_to_model[qid] = model
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "IDPControlPlane",
                            "MetricName": metric_name,
                            "Dimensions": dims,
                        },
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                }
            )
        # GetMetricData caps at 500 queries per call.
        for chunk_start in range(0, len(queries), 500):
            chunk = queries[chunk_start : chunk_start + 500]
            resp = cloudwatch_client.get_metric_data(
                MetricDataQueries=chunk,
                StartTime=hour_start,
                EndTime=hour_end,
            )
            for r in resp.get("MetricDataResults", []):
                model = id_to_model.get(r["Id"])
                if model is None:
                    continue
                values = [v for v in (r.get("Values") or []) if not math.isnan(v)]
                total = float(math.fsum(values))
                bucket = result.setdefault(model, {"in": 0.0, "out": 0.0})
                bucket[direction] += total
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


def _get_lambda_memory_mb(function_name: str) -> Tuple[int, str]:
    """Return the Lambda's configured (MemorySize MB, architecture).

    Cached per invocation so we don't spam get_function_configuration —
    both properties are static per deployed function. Falls back to
    (128 MB, "x86_64") on lookup failure — x86_64 is the AWS default
    architecture, so this errs on the side of *slightly higher* per-GB
    -second cost (safer than under-estimating).
    """
    cached = _lambda_memory_cache.get(function_name)
    if cached is not None:
        return cached
    memory_mb = 128
    architecture = "x86_64"
    try:
        response = lambda_client.get_function_configuration(FunctionName=function_name)
        memory_mb = int(response.get("MemorySize", 128))
        archs = response.get("Architectures") or ["x86_64"]
        architecture = archs[0] if archs else "x86_64"
    except Exception as e:
        logger.warning(
            f"get_function_configuration failed for {function_name}: {e}. "
            f"Assuming default 128 MB x86_64 — cost estimate may be low."
        )
    result = (memory_mb, architecture)
    _lambda_memory_cache[function_name] = result
    return result


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
    memory_mb, architecture = _get_lambda_memory_mb(function_name)
    # GB-second rate depends on architecture: arm64 is ~20% cheaper than x86_64.
    gb_second_rate = (
        LAMBDA_ARM64_GB_SECOND_PRICE
        if architecture == "arm64"
        else LAMBDA_X86_64_GB_SECOND_PRICE
    )
    lambda_gb_seconds = (duration_ms / 1000.0) * (memory_mb / 1024.0)
    # Duration cost + per-request cost — request price is arch-independent.
    est_lambda_cost = (
        lambda_gb_seconds * gb_second_rate + invocations * LAMBDA_REQUEST_PRICE
    )
    est_athena_cost = (athena_bytes / (1024**4)) * ATHENA_PRICE_PER_TB

    bedrock_by_model = metrics.get("bedrock_by_model") or {}

    # Row shape: shared function-hour columns (invocations, duration_ms_sum,
    # athena_bytes_sum, est_lambda_cost, est_athena_cost) are stamped on ONE
    # row per (function, hour) — the first one — and zeroed on subsequent
    # per-model rows. Otherwise a
    # ``SELECT SUM(invocations) FROM control_plane_hourly GROUP BY function_name``
    # would over-count by the number of Bedrock models the function touched
    # (fan-out class, same shape as the round-2 sum_pages blocker). Bedrock
    # columns (bedrock_tokens_in/out, est_bedrock_cost) stay per-model on
    # each row. Round-5 review fix.
    def _row(
        model: Optional[str],
        tokens_in: int,
        tokens_out: int,
        include_shared: bool,
    ) -> Dict[str, Any]:
        price = _bedrock_price_for_model(model)
        # Prices are per-TOKEN USD (matches config_library/pricing.yaml scale,
        # e.g. 3.0E-7 for Nova-2 Lite input = $0.30/M). No divisor needed.
        est_bedrock_cost = tokens_in * price["in"] + tokens_out * price["out"]
        return {
            "hour_ts": hour_ts,
            "function_name": function_name,
            "component": component,
            "bedrock_model": model,
            # Shared function-hour columns — stamped once, zeroed on siblings.
            "invocations": invocations if include_shared else 0,
            "duration_ms_sum": int(duration_ms) if include_shared else 0,
            "athena_bytes_sum": athena_bytes if include_shared else 0,
            "est_lambda_cost": est_lambda_cost if include_shared else 0.0,
            "est_athena_cost": est_athena_cost if include_shared else 0.0,
            # Per-model columns — carry their own value on every row.
            "bedrock_tokens_in": tokens_in,
            "bedrock_tokens_out": tokens_out,
            "est_bedrock_cost": est_bedrock_cost,
        }

    if not bedrock_by_model:
        # Component didn't call Bedrock this hour — one row without a model.
        return [_row(None, 0, 0, include_shared=True)]
    # One row per Bedrock model, but shared columns only on the FIRST.
    # Ordering: dict insertion order is deterministic (Python 3.7+), so
    # the row assigned the shared columns is stable across runs of the
    # same input — consumers that filter to `bedrock_model = X` and rely
    # on the shared columns being non-zero on any specific model row are
    # doing the wrong query and would need to aggregate anyway.
    rows: List[Dict[str, Any]] = []
    for i, (model, tokens) in enumerate(bedrock_by_model.items()):
        rows.append(
            _row(model, int(tokens["in"]), int(tokens["out"]), include_shared=(i == 0))
        )
    return rows


def _bedrock_price_for_model(model: Optional[str]) -> Dict[str, float]:
    """Return per-TOKEN USD pricing for a Bedrock model, loaded from the
    ConfigurationTable (same source as data-plane cost math).

    Lookup key is ``bedrock/<model>`` — matches the ``pricing[].name`` shape
    in ``config_library/pricing.yaml`` and the ``service_api`` written by
    ``save_reporting_data.save_metering_data``. If the model is missing
    from the config or the config load itself failed, falls back to
    ``DEFAULT_BEDROCK_PRICE_PER_TOKEN`` (Sonnet defaults) with a WARNING —
    surfaces drift instead of silently pricing at $0.
    """
    if not model:
        return DEFAULT_BEDROCK_PRICE_PER_TOKEN
    pricing_map = _load_bedrock_pricing_from_config()
    key = f"bedrock/{model}"
    entry = pricing_map.get(key)
    if entry:
        return {
            "in": entry.get("inputTokens", DEFAULT_BEDROCK_PRICE_PER_TOKEN["in"]),
            "out": entry.get("outputTokens", DEFAULT_BEDROCK_PRICE_PER_TOKEN["out"]),
        }
    logger.warning(
        f"No pricing entry for {key!r} in ConfigurationTable; falling back "
        f"to Sonnet default. Add an entry to config_library/pricing.yaml "
        f"to remove this warning."
    )
    return DEFAULT_BEDROCK_PRICE_PER_TOKEN


def _load_bedrock_pricing_from_config() -> Dict[str, Dict[str, float]]:
    """Load per-token Bedrock pricing from the ConfigurationTable, once per
    Lambda container.

    Uses ``idp_common.config.ConfigurationManager.get_merged_pricing()`` —
    the same helper every data-plane cost-writer uses. Returns
    ``{service_name: {unit_name: price_per_token_usd}}``; on any failure
    (missing env var, DynamoDB throttling, malformed config), returns an
    empty dict — callers see the DEFAULT_BEDROCK_PRICE_PER_TOKEN fallback.
    """
    global _bedrock_pricing_map
    if _bedrock_pricing_map is not None:
        return _bedrock_pricing_map
    _bedrock_pricing_map = {}
    if not CONFIGURATION_TABLE_NAME:
        logger.warning(
            "CONFIGURATION_TABLE_NAME env var not set; Bedrock cost columns "
            "will use hardcoded default pricing."
        )
        return _bedrock_pricing_map
    try:
        from idp_common.config import ConfigurationManager

        manager = ConfigurationManager(table_name=CONFIGURATION_TABLE_NAME)
        merged = manager.get_merged_pricing()
        for service in getattr(merged, "pricing", None) or []:
            units: Dict[str, float] = {}
            for unit in getattr(service, "units", None) or []:
                try:
                    units[unit.name] = float(unit.price)
                except (TypeError, ValueError):
                    continue
            if units:
                _bedrock_pricing_map[service.name] = units
        logger.info(
            f"Loaded {len(_bedrock_pricing_map)} pricing entries from "
            f"ConfigurationTable."
        )
    except Exception as e:
        # Never crash the rollup over pricing telemetry — degrade to default.
        logger.warning(
            f"Failed to load pricing from ConfigurationTable "
            f"({CONFIGURATION_TABLE_NAME!r}): {e}. Falling back to hardcoded "
            f"default pricing for Bedrock cost columns."
        )
    return _bedrock_pricing_map


# Component-mapping rules — ORDER MATTERS. First match wins. Rules are
# regexes compiled against the lower-cased function name. Ordering is
# from most-specific to least-specific so a broad rule (e.g. ``config``)
# doesn't accidentally catch a Lambda a more-specific rule would claim.
# See §10.2 in docs/reporting-sql-layer.md for the canonical label set.
_COMPONENT_RULES: List[Tuple[re.Pattern, str]] = [
    # Monitor (marketplace) dashboard resolver + AI-summary agent.
    (re.compile(r"monitoringmetrics|dashboardresolver"), "monitor-dashboard"),
    (re.compile(r"monitor.*agent"), "monitor-agent"),
    # Rollup Lambda itself. Note: this rule DOES also match any future
    # Lambda whose logical ID contains "rollup" — intentional, because
    # any future rollup Lambda is by definition still control-plane
    # scheduled aggregation. If a genuinely-different `rollup-*` Lambda
    # gets added (e.g. a per-doc pipeline stage that happens to be
    # named `rollup_scores`), add a more-specific rule ABOVE this one.
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
    # Multi-doc discovery — an admin batch tool, distinct from schema
    # (policy) discovery. Must precede the generic 'discovery' rule so
    # MultiDocDiscoveryPrepareFunction / MultiDocDiscoveryEmbedFunction
    # / etc. don't get lumped into policy-discovery.
    (re.compile(r"multidocdiscovery"), "multi-doc-discovery"),
    # Policy (schema) discovery (more specific than 'config').
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


def _previous_hour(anchor: Optional[datetime] = None) -> Tuple[str, str]:
    """Return (YYYY-MM-DD, HH) for the most recently-sealed UTC hour
    relative to ``anchor`` (default: now). Anchoring to the EventBridge
    trigger time (via ``_parse_anchor_time``) keeps async retries from
    silently rolling up the wrong partition after crossing a boundary."""
    base = anchor or datetime.now(timezone.utc)
    prev = base - timedelta(hours=1)
    return prev.strftime("%Y-%m-%d"), prev.strftime("%H")


def _previous_day(anchor: Optional[datetime] = None) -> str:
    """Return YYYY-MM-DD for the most recently-sealed UTC day, anchored
    to ``anchor`` (default: now). See ``_previous_hour`` for the retry
    rationale."""
    base = anchor or datetime.now(timezone.utc)
    return (base - timedelta(days=1)).strftime("%Y-%m-%d")


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

    Narrow fail-open policy: ONLY treats "table does not exist" as
    not-yet-written (the first-invocation-after-deploy case). Any other
    error — throttle, permission blip, malformed response — RE-RAISES.
    Fail-open on transient errors lets an INSERT run against an
    already-populated partition and permanently double-counts cost;
    re-raising lets the caller's DLQ + async retry recover.
    """
    where = f"date = '{date}'"
    if hour is not None:
        where += f" AND hour = '{hour}'"
    sql = f'SELECT 1 FROM "{DATABASE}"."{table}" WHERE {where} LIMIT 1'  # nosec B608
    try:
        rows = _run_athena_query_with_results(sql)
        return bool(rows)
    except Exception as e:
        # Only "table does not exist" is safe to swallow — Athena reports
        # this as TABLE_NOT_FOUND, EntityNotFoundException, or (from Glue)
        # "does not exist" in the message. First INSERT to a table on
        # Athena creates partitions on demand, so treating this as
        # not-written is correct on the first-invocation-after-deploy path.
        msg = str(e).lower()
        table_missing_markers = (
            "table_not_found",
            "entitynotfoundexception",
            "does not exist",
            "table not found",
        )
        if any(m in msg for m in table_missing_markers):
            logger.info(
                f"Idempotency check for {table}: table does not exist yet — "
                f"assuming not written. ({e})"
            )
            return False
        # Anything else — throttle, timeout, permission blip — must NOT be
        # papered over. Re-raise so async retry + DLQ can recover; a
        # fail-open here would let an INSERT run against a populated
        # partition and permanently double-count.
        logger.warning(
            f"Idempotency check for {table} failed with a non-table-missing "
            f"error; re-raising so the rollup aborts and Lambda's async retry "
            f"can replay: {e}"
        )
        raise


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

    On success, emit the query's ``DataScannedInBytes`` under
    component=``rollup-lambda`` so the rollup's own INSERT-INTO cost
    shows up in ``control_plane_hourly``. The rollup is likely the
    single largest control-plane Athena consumer — leaving it out
    would understate its own cost line to zero.

    On timeout, call StopQueryExecution before raising — otherwise an
    orphaned Athena query keeps scanning (and billing) after we've
    given up on it, and a retry starts a fresh one on top.
    """
    started = time.time()
    while True:
        response = athena_client.get_query_execution(QueryExecutionId=query_id)
        state = response["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            _emit_self_athena_cost(response)
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
                logger.warning(f"stop_query_execution({query_id}) failed: {stop_err}")
            raise TimeoutError(
                f"Athena query {query_id} did not complete in {timeout_sec}s"
            )
        time.sleep(1)


def _emit_self_athena_cost(query_execution_response: Dict[str, Any]) -> None:
    """Emit AthenaBytesScanned for the rollup Lambda's own query, so its
    Athena spend shows up under component=``rollup-lambda`` in
    ``control_plane_hourly``. Fire-and-forget — never blocks the rollup.
    """
    try:
        from idp_common.metrics import emit_control_plane_cost_metric

        bytes_scanned = (
            query_execution_response.get("QueryExecution", {})
            .get("Statistics", {})
            .get("DataScannedInBytes")
        )
        if bytes_scanned is not None:
            emit_control_plane_cost_metric(
                component="rollup-lambda",
                athena_bytes=int(bytes_scanned),
            )
    except Exception as e:  # nosec — cost telemetry must not affect the rollup
        # WARNING (not silent) so a future layer/packaging regression that
        # revives the round-3 "idp_common not on sys.path" blocker is
        # visible in the log instead of returning invisible zeros in
        # control_plane_hourly forever. Round-5 review fix.
        logger.warning(
            f"Failed to emit self-athena-cost metric: {e!r} — "
            f"control_plane_hourly's rollup-lambda row will under-count."
        )


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
