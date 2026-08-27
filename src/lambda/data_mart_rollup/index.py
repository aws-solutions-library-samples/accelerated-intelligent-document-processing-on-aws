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
# $5 per TB scanned. AWS Athena bills per DECIMAL TB (10**12 bytes),
# not per binary TiB (1024**4 = 1.0995e12). Under-counted every cost
# row by ~9.05% pre-round-10; the ``_BYTES_PER_TB`` constant makes the
# unit explicit at the callsite.
ATHENA_PRICE_PER_TB = 5.0
_BYTES_PER_TB = 10**12  # decimal TB — matches AWS billing.
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
#
# Cleared at the start of every ``handler`` invocation so a CFN update
# that changes a function's MemorySize/Architecture between rollups is
# picked up on the next fire — see the ``_lambda_memory_cache.clear()``
# call in ``handler``.
_lambda_memory_cache: Dict[str, Tuple[int, str]] = {}


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Route between hourly and daily rollup modes.

    Default mode is ``hourly`` — makes ad-hoc invocations (e.g., a manual
    console test) do the more common thing without needing to remember
    the payload shape.
    """
    # Reset per-invocation caches — round-7+round-8 review fixes. Both
    # module-scope caches persist within a rollup (dedup dozens of
    # GetFunction / DynamoDB reads across N Lambdas × 1 hour) but MUST
    # NOT survive across invocations: a CFN update between rollup fires
    # can change a Lambda's MemorySize/Architecture, and an operator
    # editing pricing.yaml must see the change on the next rollup, not
    # after the container recycles.
    global _bedrock_pricing_map
    _lambda_memory_cache.clear()
    _bedrock_pricing_map = None

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
    (defaults to now — see ``_parse_anchor_time`` for the retry-safe path).

    Round-8 review fix: each of the three rollups runs independently in
    its own try/except so a transient failure on one (e.g. Athena
    partial-region outage affecting the metering table) doesn't couple
    the fates of the others. ``control_plane_hourly`` in particular
    reads CloudWatch (not the metering table) and was previously killed
    by any metering-side raise. If ANY rollup raises, this function
    re-raises AFTER all three have been attempted, so async retry can
    replay whichever ones failed — the successful writes are idempotent.
    """
    target_date, target_hour = _previous_hour(anchor)
    logger.info(f"Hourly rollup targeting date={target_date} hour={target_hour}")

    results: Dict[str, Any] = {
        "mode": "hourly",
        "target_date": target_date,
        "target_hour": target_hour,
    }
    failures: List[str] = []
    for label, fn in (
        ("metering_hourly", _rollup_metering_hourly),
        ("metering_docs_hourly", _rollup_metering_docs_hourly),
        ("control_plane_hourly", _rollup_control_plane_hourly),
    ):
        try:
            results[label] = fn(target_date, target_hour)
        except Exception as e:
            logger.exception(
                f"{label} rollup failed for {target_date} hour={target_hour}"
            )
            results[label] = {"skipped": False, "error": str(e)}
            failures.append(f"{label}: {e}")

    logger.info(f"Hourly rollup complete: {results}")
    if failures:
        # Raise AFTER the two independent siblings have run. The
        # successful ones' partitions are idempotency-locked, so async
        # retry only replays the failed ones.
        raise RuntimeError(
            f"Hourly rollup for {target_date} hour={target_hour} had "
            f"{len(failures)} of 3 sub-rollups fail: {'; '.join(failures)}"
        )
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
    # with MAX(number_of_pages). Round-8 note: the invariant assumes
    # number_of_pages is stamped identically across every metering row
    # for the same doc — true in practice because OCR sets it once,
    # and a same-hour reprocess re-runs OCR on the same PDF (same page
    # count). If a doc were somehow reprocessed within the same hour
    # against a materially different file (different page count), MAX
    # picks the LARGER value — a slight over-count but bounded to that
    # doc, not systematic. MIN/AVG/ANY_VALUE have equally-defensible
    # semantics; MAX chosen so the count is not silently rounded down.
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

    # Warm the pricing cache in the main thread BEFORE fan-out. Round-12
    # review fix: without this, the first 10 worker threads all see
    # `_bedrock_pricing_map is None` and race to load, doing up to 10
    # duplicate ConfigurationManager.get_merged_pricing() calls. A single
    # main-thread load populates the cache before the pool starts.
    _load_bedrock_pricing_from_config()

    # Parallelize CW fetches — round-10 review fix. Each per-function
    # call round-trips 5+ CloudWatch APIs (Duration, Invocations,
    # AthenaBytes ListMetrics + GetMetricData, BedrockTokens ×2). At
    # ~68 stack Lambdas that was ~340 blocking calls per rollup and
    # dominated wall time (~19-20s observed). 10 workers cuts that to
    # ~2-3s while staying well under CW's per-account TPS ceiling.
    from concurrent.futures import ThreadPoolExecutor

    def _fetch_one(function_arn: str) -> List[Dict[str, Any]]:
        function_name = function_arn.rsplit(":", 1)[-1]
        component = _component_for_function(function_name)
        metrics = _get_cw_metrics_for_function(
            function_name=function_name,
            hour_start=hour_start,
            hour_end=hour_end,
        )
        return _build_control_plane_rows(
            function_name=function_name,
            component=component,
            hour_ts=hour_start,
            metrics=metrics,
        )

    rows: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        # Order is deterministic (control_arns is sorted upstream); the
        # ordered map preserves that across the parallel fan-out so the
        # written parquet's row order is stable across reruns.
        for per_fn_rows in pool.map(_fetch_one, control_arns):
            rows.extend(per_fn_rows)

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

    # Check idempotency FIRST so the guard doesn't fire on an already-
    # committed partition (round-6 review fix — an operator emptying
    # metering_hourly to reset a bad rollup while metering_daily is
    # already written would previously raise unnecessarily). Only run
    # the guard when we're actually about to write.
    daily_exists = _partition_already_written(table="metering_daily", date=target_date)
    docs_daily_exists = _partition_already_written(
        table="metering_docs_daily", date=target_date
    )
    if not (daily_exists and docs_daily_exists):
        _require_hourly_matches_raw_metering(target_date)

    # --- metering_daily (cost per service/unit) ---
    if daily_exists:
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
    if docs_daily_exists:
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


def _hourly_ever_written(before_date: str) -> bool:
    """Return True if ``metering_hourly`` has ANY row on a date strictly
    before ``before_date``.

    Distinguishes true deploy-day (never rolled up anything, anywhere)
    from a total-outage day (metering_hourly empty for THIS date but
    healthy on prior dates). Round-11 review fix — using "metering_hourly
    empty for target_date" as the sole deploy-day signal mis-classified
    an outage day and silently wrote a 0-doc daily that idempotency then
    locked in forever.

    Fast SELECT: LIMIT 1 with partition-pruned WHERE date < '{X}'.
    ``emit_self_cost=False`` — this is a bookkeeping probe, not a
    genuine cost-attribution query.
    """
    # nosec B608 — before_date is from datetime.strftime, not user input.
    sql = (
        f'SELECT 1 FROM "{DATABASE}"."metering_hourly" '  # nosec B608
        f"WHERE date < '{before_date}' LIMIT 1"  # nosec B608
    )
    # Retry a couple times before the defensive default-True — round-12
    # review fix. On the first-daily-after-deploy path, an Athena
    # throttle would otherwise mis-classify a legitimate deploy-day as
    # "hourly-has-been-written", spuriously firing the guard. Two
    # retries survive a single-transient throttle without moving to
    # the default. Falls back to True on persistent failure so we
    # don't accidentally write and lock a zero daily.
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            rows = _run_athena_query_with_results(sql, emit_self_cost=False)
            return bool(rows)
        except Exception as e:
            last_error = e
            if attempt < 2:
                time.sleep(1 + attempt)  # 1s, 2s
    logger.warning(
        f"_hourly_ever_written probe failed after 3 attempts ({last_error!r}); "
        f"defaulting to True (guard will fire on empty hourly for target "
        f"date rather than silently writing a 0-doc daily)."
    )
    return True


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
    # Determine deploy-day baseline from the PRIMARY hourly table.
    # metering_hourly and metering_docs_hourly are written by the SAME
    # rollup invocation — if one is empty for the date and the other has
    # data, that's a systematic failure (not deploy-day), and we must
    # NOT skip the guard on the empty one. Round-6 review fix.
    primary_hourly_rows = _run_athena_query_with_results(
        f'SELECT DISTINCT hour FROM "{DATABASE}"."metering_hourly" '  # nosec B608
        f"WHERE date = '{target_date}'"  # nosec B608
    )
    primary_hourly_hours = {r[0] for r in primary_hourly_rows if r and r[0]}
    # Round-11 review fix: a "deploy-day" signal for THIS date isn't
    # sufficient — a day where every hour's rollup failed (Athena outage,
    # DLQ episode) also has metering_hourly empty for the date. To
    # distinguish, look for ANY metering_hourly row on a PRIOR date. If
    # any exist, the hourly rollup has been running before — an empty
    # target-date is a real outage, not deploy-day. If none exist across
    # any prior date, this really is the first day the rollup has
    # attempted to write.
    is_deploy_day = not primary_hourly_hours and not _hourly_ever_written(
        before_date=target_date
    )

    for hourly_table in ("metering_hourly", "metering_docs_hourly"):
        hourly_sql = (
            f'SELECT DISTINCT hour FROM "{DATABASE}"."{hourly_table}" '  # nosec B608
            f"WHERE date = '{target_date}'"  # nosec B608
        )
        hourly_rows = _run_athena_query_with_results(hourly_sql)
        hourly_hours = {r[0] for r in hourly_rows if r and r[0]}
        if not hourly_hours:
            if is_deploy_day:
                logger.info(
                    f"{hourly_table} for date={target_date} is empty AND "
                    f"no prior date has hourly rows either — deploy-day, "
                    f"skipping raw-vs-hourly guard for this table. raw "
                    f"hours: {sorted(raw_hours)}"
                )
                continue
            # Either metering_hourly has rows for THIS date, or hourly
            # has data on some PRIOR date → this isn't deploy-day. An
            # empty hourly for this date means every hour's rollup
            # failed. Fail loudly so async-retry can replay before the
            # daily locks in zero forever.
            raise RuntimeError(
                f"{hourly_table} for date={target_date} is empty but "
                f"hourly rollups have run before (primary_hourly this date "
                f"= {len(primary_hourly_hours)}) — systematic failure of "
                f"{hourly_table} INSERTs for the day. Refusing to write "
                f"an incomplete daily; async retry will replay once the "
                f"hourly rollup catches up. raw hours: {sorted(raw_hours)}"
            )
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
        except cfn.exceptions.ClientError as e:
            # Distinguish retryable errors (Throttling, InternalError,
            # ServiceUnavailable) from expected non-fatal ones (stack
            # deleted between discovery and listing → ValidationError
            # "Stack ... does not exist"). Round-10 review fix: the
            # previous ``except Exception`` swallowed retryable throttles
            # too, silently dropping nested-stack Lambdas from the
            # control-plane discovery set — the rollup would then miss
            # ~57 of 68 Lambdas.
            code = e.response.get("Error", {}).get("Code", "")
            msg = str(e).lower()
            is_retryable = code in (
                "Throttling",
                "ThrottlingException",
                "TooManyRequestsException",
                "RequestLimitExceeded",
                "InternalError",
                "InternalFailure",
                "ServiceUnavailable",
            )
            is_stack_gone = code == "ValidationError" and "does not exist" in msg
            if is_stack_gone:
                logger.info(
                    f"Skipping {current!r} — stack no longer exists (deleted "
                    f"between discovery hops)."
                )
                continue
            if is_retryable:
                # Re-raise so Lambda's async retry replays the whole
                # rollup after a back-off; a partial tree = a partial
                # control-plane row set = under-count.
                raise
            logger.warning(
                f"Failed to list resources of stack {current!r} "
                f"({code}): {e}. Continuing with partial tree."
            )
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
            values = [
                v
                for v in (r.get("Values") or [])
                if v is not None and not math.isnan(v)
            ]
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
                values = [
                    v
                    for v in (r.get("Values") or [])
                    if v is not None and not math.isnan(v)
                ]
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

    Round-12 review fix: ACCUMULATES on same Id rather than overwriting.
    We don't paginate GetMetricData today so duplicates don't happen in
    practice, but if pagination is added later, splitting one query's
    values across pages would silently drop everything but the last
    page under the previous overwrite semantic.
    """
    result: Dict[str, float] = {}
    for r in response.get("MetricDataResults", []):
        values = [
            v for v in (r.get("Values") or []) if v is not None and not math.isnan(v)
        ]
        result[r["Id"]] = result.get(r["Id"], 0.0) + float(math.fsum(values))
    return result


def _get_lambda_memory_mb(function_name: str) -> Tuple[int, str]:
    """Return the Lambda's configured (MemorySize MB, architecture).

    Cached across warm-container invocations so we don't spam
    get_function_configuration — both properties are static per deployed
    function. Falls back to (128 MB, "x86_64") on lookup failure — x86_64
    is the AWS default architecture, so this errs on the side of
    *slightly higher* per-GB-second cost (safer than under-estimating).

    On lookup FAILURE the fallback is used for this call but NOT cached
    — a transient throttle should not poison the warm container for its
    entire life. Round-6 review fix.
    """
    cached = _lambda_memory_cache.get(function_name)
    if cached is not None:
        return cached
    try:
        response = lambda_client.get_function_configuration(FunctionName=function_name)
        memory_mb = int(response.get("MemorySize", 128))
        archs = response.get("Architectures") or ["x86_64"]
        architecture = archs[0] if archs else "x86_64"
    except Exception as e:
        # Fallback tuned to median of this stack's Lambdas (512 MB), not
        # the AWS floor (128 MB). Round-7 review fix — 128 was up to
        # ~24× under-count when a transient throttle hit a 3008 MB
        # function; 512 is closer to typical and errs less. Still
        # imperfect (exact memory varies), but bounded within ~2-3×
        # rather than an order of magnitude.
        logger.warning(
            f"get_function_configuration failed for {function_name}: {e}. "
            f"Assuming default 512 MB x86_64 — cost estimate approximate. "
            f"Not caching; next call retries."
        )
        # DO NOT cache the fallback — a transient throttle would else
        # lock this Lambda's cost estimate wrong for the container's life.
        return (512, "x86_64")
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
    est_athena_cost = (athena_bytes / _BYTES_PER_TB) * ATHENA_PRICE_PER_TB

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
    # Round-8 review fix: sort by model name so the shared-column row is
    # the same one every time regardless of the (undocumented)
    # ListMetrics traversal order — otherwise a re-run of the same hour
    # could put shared columns on a different row and (if unlucky) a
    # consumer's LEFT JOIN could pick up different values across
    # rebuilds of the same partition.
    rows: List[Dict[str, Any]] = []
    for i, model in enumerate(sorted(bedrock_by_model.keys())):
        tokens = bedrock_by_model[model]
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
    from the config, returns ``{in: 0.0, out: 0.0}`` and emits an ERROR
    log — round-7 review fix (previously fell back to Sonnet defaults
    3e-6 / 15e-6, which silently OVER-counted Nova-Lite by ~50× and
    UNDER-counted Opus by ~5×). 0.0 is a deliberate under-count so the
    dashboard's cost KPI is never inflated by an unknown model — the
    ERROR log + zero-cost row surfaces the config gap without misleading
    the dashboard.
    """
    # Return a fresh dict each time — the module-level default is mutable,
    # and a callee accidentally mutating `price["in"] = ...` would poison
    # every subsequent lookup for the container's lifetime. Round-9
    # review fix.
    if not model:
        return dict(DEFAULT_BEDROCK_PRICE_PER_TOKEN)
    pricing_map = _load_bedrock_pricing_from_config()
    key = f"bedrock/{model}"
    entry = pricing_map.get(key)
    if entry:
        return {
            "in": entry.get("inputTokens", 0.0),
            "out": entry.get("outputTokens", 0.0),
        }
    logger.error(
        f"No pricing entry for {key!r} in ConfigurationTable. "
        f"control_plane_hourly will under-count this model's cost by the "
        f"actual per-token rate. Add an entry to config_library/pricing.yaml "
        f"and redeploy to fix."
    )
    return {"in": 0.0, "out": 0.0}


def _load_bedrock_pricing_from_config() -> Dict[str, Dict[str, float]]:
    """Load per-token Bedrock pricing from the ConfigurationTable, once per
    Lambda container.

    Uses ``idp_common.config.ConfigurationManager.get_merged_pricing()`` —
    the same helper every data-plane cost-writer uses. Returns
    ``{service_name: {unit_name: price_per_token_usd}}`` populated on
    success. On failure (missing env var, DynamoDB throttling, malformed
    config), returns an empty dict for THIS invocation but does NOT cache
    that empty dict — the next invocation retries. Round-6 review fix
    for the "empty dict cached on failure poisons the warm container"
    class.
    """
    global _bedrock_pricing_map
    if _bedrock_pricing_map is not None:
        return _bedrock_pricing_map
    if not CONFIGURATION_TABLE_NAME:
        logger.warning(
            "CONFIGURATION_TABLE_NAME env var not set; Bedrock cost columns "
            "will use hardcoded default pricing."
        )
        # Env var never appears mid-lifetime — this IS a "cache the empty"
        # result: no retry will help.
        _bedrock_pricing_map = {}
        return _bedrock_pricing_map
    try:
        from idp_common.config import ConfigurationManager

        manager = ConfigurationManager(table_name=CONFIGURATION_TABLE_NAME)
        merged = manager.get_merged_pricing()
        loaded: Dict[str, Dict[str, float]] = {}
        for service in getattr(merged, "pricing", None) or []:
            units: Dict[str, float] = {}
            for unit in getattr(service, "units", None) or []:
                try:
                    units[unit.name] = float(unit.price)
                except (TypeError, ValueError):
                    continue
            if units:
                loaded[service.name] = units
        # ONLY cache on success WITH content — if the DynamoDB read
        # returned zero entries (eventual consistency, empty custom
        # config), don't lock the container into $0/undefined pricing.
        # Next invocation retries. Round-7 review fix — the earlier
        # comment said "only on success" but the code assigned
        # unconditionally.
        if loaded:
            _bedrock_pricing_map = loaded
            logger.info(
                f"Loaded {len(loaded)} pricing entries from ConfigurationTable."
            )
            return _bedrock_pricing_map
        logger.warning(
            "ConfigurationTable returned 0 pricing entries; not caching. "
            "Next invocation will retry. This invocation falls back to "
            "hardcoded default pricing."
        )
        return {}
    except Exception as e:
        # DO NOT set _bedrock_pricing_map on failure — leave it as None
        # so the NEXT invocation retries. Only this invocation degrades
        # to the hardcoded default.
        logger.warning(
            f"Failed to load pricing from ConfigurationTable "
            f"({CONFIGURATION_TABLE_NAME!r}): {e}. Falling back to hardcoded "
            f"default pricing for Bedrock cost columns THIS INVOCATION; "
            f"next invocation will retry."
        )
        return {}


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
    # Multi-doc discovery — an admin batch tool.
    (re.compile(r"multidocdiscovery"), "multi-doc-discovery"),
    # Policy (schema) discovery. Round-7 review fix: tightened to match
    # ONLY the specific ``policydiscovery`` / ``discoveryprocessor``
    # shapes this codebase actually uses. The earlier bare ``discovery``
    # fallback was a silent trap — any future Lambda with "discovery"
    # in its logical ID (a doc-discovery agent, a resource-discovery
    # cron, etc.) would get mis-labeled and have its cost attributed to
    # policy-discovery. Add a more-specific rule ABOVE this one when a
    # new discovery Lambda appears.
    (re.compile(r"policydiscovery|discoveryprocessor"), "policy-discovery"),
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
        # emit_self_cost=False — these idempotency probes are tiny
        # LIMIT-1 partition-pruned SELECTs and would otherwise emit one
        # AthenaBytesScanned metric per rollup fire per table, drowning
        # the rollup-lambda component's real Athena cost signal in noise.
        # Round-9 review fix.
        rows = _run_athena_query_with_results(sql, emit_self_cost=False)
        return bool(rows)
    except Exception as e:
        # Only "table does not exist" is safe to swallow — Athena reports
        # this as TABLE_NOT_FOUND, EntityNotFoundException, or (from Glue)
        # "does not exist" in the message. First INSERT to a table on
        # Athena creates partitions on demand, so treating this as
        # not-written is correct on the first-invocation-after-deploy path.
        msg = str(e).lower()
        # Match ONLY the specific table-missing error shapes Athena/Glue
        # actually emit. Round-7 fix: added the single-quote
        # fully-qualified form Athena uses in practice
        # (`Table 'awsdatacatalog.<db>.<name>' does not exist`) —
        # the round-6 narrowing only had backtick/double-quote forms,
        # which never matched real Athena output. A broader "does not
        # exist" substring would fail-open on missing bucket / database /
        # column / role, so we still bind the phrase to the table name.
        tbl = table.lower()
        table_missing_markers = (
            "table_not_found",
            "entitynotfoundexception",
            "table not found",
            f"table `{tbl}` does not exist",
            f'table "{tbl}" does not exist',
            f"table '{tbl}' does not exist",
            # Athena's real form includes the catalog+db prefix:
            f".{tbl}' does not exist",
            f".{tbl}` does not exist",
            f'.{tbl}" does not exist',
            # Trino/Athena engine v3 also emits the fully-quoted-per-
            # segment form: Table "catalog"."db"."tbl" does not exist —
            # the segment BEFORE `tbl` ends in `"."` and the segment
            # AROUND tbl is `"tbl"`. Round-8 review fix.
            f'."{tbl}" does not exist',
            # Hive/backtick fully-qualified form:
            # `catalog`.`db`.`tbl` — the segment before tbl ends in
            # `` `.` `` (backtick-dot-backtick). Round-11 review fix.
            f".`{tbl}` does not exist",
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


def _run_athena(sql: str, emit_self_cost: bool = True) -> str:
    """Start an Athena query and wait for completion. Returns QueryExecutionId.

    ``emit_self_cost=False`` skips the self-attribution CloudWatch metric
    for the query's ``DataScannedInBytes``. Idempotency-check SELECTs
    (LIMIT 1 partition probes) use this to avoid emitting per-partition
    ``AthenaBytesScanned`` metrics for every rollup fire, which was noise
    on the ``rollup-lambda`` component. Round-9 review fix.
    """
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
    _wait_for_athena(query_id, emit_self_cost=emit_self_cost)
    return query_id


def _run_athena_query_with_results(
    sql: str, emit_self_cost: bool = True
) -> List[List[str]]:
    """Run a query and return result rows (as string lists).

    See ``_run_athena`` for the ``emit_self_cost`` flag.

    Paginates ``get_query_results`` — Athena caps a single response at
    ~1000 rows. Header row is only on the FIRST page; paginating naively
    while always stripping ``Rows[0]`` would drop the first data row of
    every page ≥2 (round-6 review fix — silent truncation + naive
    pagination retrofit hazard).
    """
    query_id = _run_athena(sql, emit_self_cost=emit_self_cost)
    all_rows: List[List[str]] = []
    next_token: Optional[str] = None
    first_page = True
    while True:
        kwargs: Dict[str, Any] = {"QueryExecutionId": query_id}
        if next_token:
            kwargs["NextToken"] = next_token
        result = athena_client.get_query_results(**kwargs)
        page_rows = result.get("ResultSet", {}).get("Rows", [])
        if first_page:
            page_rows = page_rows[1:]  # strip header on first page only
            first_page = False
        all_rows.extend(
            [c.get("VarCharValue", "") for c in r.get("Data", [])] for r in page_rows
        )
        next_token = result.get("NextToken")
        if not next_token:
            break
    return all_rows


def _wait_for_athena(
    query_id: str, timeout_sec: int = 300, emit_self_cost: bool = True
) -> None:
    """Poll get_query_execution until the query terminates.

    On success, emit the query's ``DataScannedInBytes`` under
    component=``rollup-lambda`` so the rollup's own INSERT-INTO cost
    shows up in ``control_plane_hourly``. The rollup is likely the
    single largest control-plane Athena consumer — leaving it out
    would understate its own cost line to zero.

    ``emit_self_cost=False`` opts out — used by
    ``_partition_already_written`` so a per-partition LIMIT-1 probe
    doesn't emit one AthenaBytesScanned metric per idempotency check.

    On timeout, call StopQueryExecution before raising — otherwise an
    orphaned Athena query keeps scanning (and billing) after we've
    given up on it, and a retry starts a fresh one on top.
    """
    started = time.time()
    while True:
        response = athena_client.get_query_execution(QueryExecutionId=query_id)
        state = response["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            if emit_self_cost:
                _emit_self_athena_cost(response)
            return
        if state in ("FAILED", "CANCELLED"):
            reason = response["QueryExecution"]["Status"].get(
                "StateChangeReason", "unknown"
            )
            # Route by reason so Lambda's async retry doesn't burn its
            # two attempts on a permanent syntax error. Round-10 review
            # fix. Athena's error text is well-known (documented at
            # https://docs.aws.amazon.com/athena/latest/ug/error-reference.html).
            reason_lc = reason.lower()
            permanent_markers = (
                "syntax_error",
                "syntax error",
                "semantic_error",
                "column_not_found",
                "no viable alternative",
                "hive_metastore_error",  # schema mismatch
                "invalid_view",
                # Target table missing → CFN dependency ordering issue,
                # not a transient failure. DLQ immediately instead of
                # burning both async retries. Round-12 review fix.
                "table_not_found",
                "does not exist",
                "entitynotfoundexception",
            )
            retryable_markers = (
                "throttling",
                "internal_error_query_engine",
                "internal_error",
                "service_unavailable",
                "resource_exhausted",
                "network_error",
            )
            if any(m in reason_lc for m in permanent_markers):
                # Permanent → operator intervention needed; async retry
                # is wasted budget. Raise ValueError so DLQ sees a
                # distinctly non-retryable class.
                raise ValueError(
                    f"Athena query {query_id} PERMANENT failure ({state}): {reason}"
                )
            if any(m in reason_lc for m in retryable_markers):
                # Retryable → RuntimeError, async retry will replay
                # after back-off.
                raise RuntimeError(
                    f"Athena query {query_id} TRANSIENT failure "
                    f"({state}, will retry): {reason}"
                )
            # Unknown reason → default to retryable (safer than
            # skipping an hour). Log for the operator to classify.
            logger.warning(
                f"Athena query {query_id} failed with unclassified reason: "
                f"{reason!r}. Treating as retryable. Consider adding this "
                f"reason to permanent/retryable_markers if you see it "
                f"repeatedly."
            )
            raise RuntimeError(
                f"Athena query {query_id} UNCLASSIFIED failure ({state}): {reason}"
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
    """Return True if a bucket key already exists.

    Only treats a real 404 as "not present". Any other error (KMS blip,
    throttling, transient network) is re-raised so the rollup aborts
    and Lambda's async retry can replay — a bare "return False" on
    everything defeats the idempotency guard: a transient error would
    let us overwrite an already-committed control_plane_hourly partition.
    Round-6 review fix.
    """
    if not REPORTING_BUCKET:
        return False
    try:
        s3_client.head_object(Bucket=REPORTING_BUCKET, Key=key)
        return True
    except s3_client.exceptions.ClientError as e:
        # boto3 exception classes vary by service; head_object raises
        # ClientError with 404 Not Found for a missing key.
        code = e.response.get("Error", {}).get("Code")
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return False
        # Everything else — propagate so async-retry can recover.
        raise


def _write_parquet(rows: List[Dict[str, Any]], key: str) -> None:
    """Serialize rows to Parquet and upload to the reporting bucket.

    Round-8 review fix: re-checks target-key existence immediately
    before PUT, so a manual invoke concurrent with an in-flight async
    retry can't double-write (belt-and-braces on top of the caller's
    earlier ``_s3_object_exists`` check plus the function's
    ``ReservedConcurrentExecutions: 1``). The check + PUT still isn't
    strictly atomic — S3 has no conditional-put on this write path —
    but the second-writer window shrinks to the PUT itself, which is
    orders of magnitude tighter than the previous "check at start of
    handler, PUT at end".
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    if _s3_object_exists(key):
        logger.info(
            f"_write_parquet: s3://{REPORTING_BUCKET}/{key} already exists "
            f"(race: idempotency check passed at start of rollup but a "
            f"concurrent writer landed the partition first). Skipping PUT."
        )
        return

    schema = pa.schema(
        [
            # Explicit UTC tz — round-10 review fix. hour_ts values are
            # tz-aware datetimes from ``_hour_window`` (timezone.utc); the
            # previous ``pa.timestamp("ms")`` (naive) silently stripped
            # the tz on write. Newer pyarrow versions raise ArrowInvalid
            # on the mismatch, so declaring tz explicitly future-proofs
            # the write and preserves UTC in the parquet metadata for
            # non-Athena readers.
            ("hour_ts", pa.timestamp("ms", tz="UTC")),
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
