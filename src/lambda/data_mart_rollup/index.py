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
import random
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
# Round-20 review fix (#1720): tracks whether the invocation's pricing load
# hit a real failure (empty result or exception) vs was never attempted /
# succeeded. When True AND control-plane rows have bedrock activity, the
# rollup MUST raise so Lambda async-retry replays; otherwise S3
# idempotency locks est_bedrock_cost=0 for the hour forever.
_bedrock_pricing_unavailable: bool = False
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

# Per-invocation cache of the CFN stack tree and the data-plane ARN set.
# ``_rollup_control_plane_hourly`` and ``_rollup_data_plane_lambda_hourly`` both
# need them, and each used to re-walk the tree (``ListStackResources`` across
# root + every nested stack) and re-run the ``idp:plane=data`` tag query
# independently — twice the CFN and Tagging API calls per hourly fire for
# results that cannot change mid-invocation. Cleared alongside the other caches
# in ``handler`` so a stack update between fires is still picked up.
_stack_tree_cache: Optional[List[str]] = None
_data_plane_arn_cache: Optional[List[str]] = None


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
    global _bedrock_pricing_map, _bedrock_pricing_unavailable
    global _stack_tree_cache, _data_plane_arn_cache
    _lambda_memory_cache.clear()
    _bedrock_pricing_map = None
    _bedrock_pricing_unavailable = False
    _stack_tree_cache = None
    _data_plane_arn_cache = None

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
    # Round-18 review fix (#209): preserve permanent-vs-retryable
    # classification. ``_wait_for_athena`` raises ValueError for permanent
    # failures and RuntimeError for retryable — but blindly re-raising
    # RuntimeError from this aggregator downgraded permanent failures to
    # retryable and burned Lambda async-retry attempts on truly permanent
    # errors. Track whether ANY sub-failure was a ValueError and re-raise
    # ValueError from the aggregator if so, so async-retry recognizes
    # the permanent class.
    any_permanent = False
    for label, fn in (
        ("metering_hourly", _rollup_metering_hourly),
        ("metering_docs_hourly", _rollup_metering_docs_hourly),
        ("control_plane_hourly", _rollup_control_plane_hourly),
        # Round-22 sibling of control_plane_hourly for consistency —
        # data-plane Lambdas' compute cost had no home in the reporting
        # layer (metering_hourly captures per-doc API service costs but
        # not the Lambda compute time hosting those calls).
        ("data_plane_lambda_hourly", _rollup_data_plane_lambda_hourly),
    ):
        try:
            results[label] = fn(target_date, target_hour)
        except ValueError as e:
            logger.exception(
                f"{label} PERMANENT rollup failure for {target_date} hour={target_hour}"
            )
            results[label] = {"skipped": False, "error": str(e)}
            failures.append(f"{label}(permanent): {e}")
            any_permanent = True
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
        msg = (
            f"Hourly rollup for {target_date} hour={target_hour} had "
            f"{len(failures)} of 4 sub-rollups fail: {'; '.join(failures)}"
        )
        raise (ValueError if any_permanent else RuntimeError)(msg)
    return results


# Athena's ClientRequestToken requires 32-128 characters. The natural
# per-partition key (e.g. ``metering_hourly-2026-08-27-13``) is only 29
# chars, which boto3 rejects client-side before the query is even sent —
# round-17 review fix. This helper prepends a stable stack-scoped
# prefix so every generated token clears the 32-char floor regardless
# of the (table, date, hour) triple.
#
# The prefix MUST be stable across Lambda invocations (both the initial
# and any async-retry attempts must produce the exact same token for
# Athena to dedupe them) so it is derived from the stack name only —
# NOT invocation-scoped state like time or random.
_IDEMPOTENCY_KEY_PREFIX = f"idp-rollup-{STACK_NAME or 'unknown'}"


# Round-18 review fix (finding #1985): single source of truth for
# "is this Athena/Glue error a table-not-found error?". Round 6/7/8/11/
# 15/16/17 each edited one of three drifted copies of this logic (in
# ``_partition_already_written``, ``_hourly_ever_written``, and
# ``_wait_for_athena``'s permanent classifier). Consolidating removes
# the drift and lets us bind the "does not exist" phrase to the SPECIFIC
# table name so unrelated column/bucket/database/role/view errors
# don't false-positive.
def _is_athena_table_missing(exc_or_msg: Any, table: Optional[str] = None) -> bool:
    """Return True iff the Athena/Glue error indicates the given table
    doesn't exist. Accepts either an Exception or a raw message string.

    If ``table`` is supplied, the phrase ``does not exist`` must appear
    bound to that table name (backtick / quoted / catalog-qualified
    forms). If ``table`` is None, only unambiguous shape markers
    (``TABLE_NOT_FOUND``, ``EntityNotFoundException``) match — falling
    back to bare ``does not exist`` here would false-positive on column
    or bucket errors.
    """
    msg = str(exc_or_msg).lower()
    unambiguous_markers = (
        "table_not_found",
        "entitynotfoundexception",
        "table not found",
    )
    has_unambiguous = any(m in msg for m in unambiguous_markers)
    if not table:
        # No table binding requested — unambiguous markers alone.
        return has_unambiguous
    tbl = table.lower()
    # Round-23 review fix (#295): round-20 tightened this to require
    # the specific table name to appear in the message when the caller
    # supplied a ``table`` argument, but that misclassifies bare
    # ``TABLE_NOT_FOUND`` / ``EntityNotFoundException`` errors (which
    # some Athena error variants emit without a fully-qualified name)
    # as retryable — reproducing the round-6-era retry loop on
    # permanent Glue conditions.
    #
    # Correct behavior: if the message HAS an unambiguous marker AND
    # ALSO mentions the specific table → match. If it has an
    # unambiguous marker but NO table name at all → also match (bare
    # shape; we can't tell but the failure is genuinely a
    # table-missing shape). Only if the message names a DIFFERENT
    # table explicitly should we return False.
    if has_unambiguous:
        if tbl in msg:
            return True
        # Round-23 review fix (#295): distinguish "bare marker with no
        # table name at all" (default-True: safer to treat as permanent
        # per round-6) from "marker with a DIFFERENT table's name"
        # (return False: not our table's error). Look for any
        # ``<identifier> does not exist`` / ``TABLE_NOT_FOUND: <ident>``
        # pattern; if an identifier appears and it isn't ours, it's a
        # different table's error.
        other_name = re.search(
            r"(?:table[_\s]not[_\s]found:?\s*|entitynotfoundexception:?\s*|table\s+[`'\"]?)"
            r"([a-z_][a-z0-9_.]*)"
            r"(?:[`'\"]?\s+does not exist|[`'\"]?\s*$)",
            msg,
        )
        if other_name:
            ident = other_name.group(1)
            # Strip catalog/db prefix if present ("catalog.db.table" → "table").
            ident = ident.rsplit(".", 1)[-1]
            if ident != tbl:
                return False
        # Bare unambiguous marker (no parseable identifier) — assume
        # it's for our table (safer than treating a permanent Glue
        # failure as retryable, per round-6).
        return True
    return any(
        marker in msg
        for marker in (
            # Backtick / double-quote / single-quote table forms.
            f"table `{tbl}` does not exist",
            f'table "{tbl}" does not exist',
            f"table '{tbl}' does not exist",
            # Fully-qualified variants Athena/Trino emit — catalog.db.table
            # segment ending in the specific table name.
            f".{tbl}' does not exist",
            f".{tbl}` does not exist",
            f'.{tbl}" does not exist',
            f'."{tbl}" does not exist',
            f".`{tbl}` does not exist",
        )
    )


def _idempotency_key(table: str, date: str, hour: Optional[str] = None) -> str:
    """Build a per-partition Athena ClientRequestToken.

    Guarantees:
    - 32–128 chars (Athena hard limit).
    - Deterministic on (table, date, hour) so async retry dedupes.
    - Contains only letters/digits/dash/underscore.
    - Distinct (table, date, hour) tuples NEVER collide, even when
      ``STACK_NAME`` is long enough to force truncation. Round-18
      review fix — the previous ordering ``prefix-<discriminator>``
      then ``[:128]`` chopped the trailing discriminator, so two
      partitions from a long-stack-name deployment could hash to the
      same token and the second INSERT silently no-oped (Athena's
      dedup returns the earlier QueryExecutionId).
    """
    core = f"{table}-{date}"
    if hour:
        core = f"{core}-{hour}"
    # Round-18 fix: put the DISCRIMINATOR FIRST, then the stack-scoped
    # prefix. If the total exceeds 128 chars, truncation lops off the
    # stack-name suffix (bloat), not the (table, date, hour) tuple that
    # actually distinguishes partitions. Even the shortest core value
    # ("metering_daily-2026-08-27" = 25 chars) still needs SOME prefix
    # to clear the 32-char floor, so we place the prefix after and let
    # truncation eat into it if necessary — never into the core.
    sanitized_core = re.sub(r"[^A-Za-z0-9_-]", "-", core)
    sanitized_prefix = re.sub(r"[^A-Za-z0-9_-]", "-", _IDEMPOTENCY_KEY_PREFIX)
    key = f"{sanitized_core}-{sanitized_prefix}"
    # Truncate the TAIL (prefix side) at 128; the discriminator survives.
    key = key[:128]
    # Pad short cores (empty stack name in local tests) so we still
    # clear Athena's 32-char floor by appending fixed padding.
    if len(key) < 32:
        key = (key + "-idempotency-pad-idempotency-pad")[:64]
    return key


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
    # Round-16 review fix: stable idempotency key per (table, date, hour).
    # An async retry that fires while the first INSERT is still in flight
    # (before Glue metadata propagates the new partition rows) will get
    # the SAME QueryExecutionId back from Athena instead of starting a
    # second, double-writing INSERT.
    query_id = _run_athena(
        sql,
        idempotency_key=_idempotency_key("metering_hourly", target_date, target_hour),
    )
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
    # Round-16 review fix: idempotency key — see metering_hourly above.
    query_id = _run_athena(
        sql,
        idempotency_key=_idempotency_key(
            "metering_docs_hourly", target_date, target_hour
        ),
    )
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
    from concurrent.futures import ThreadPoolExecutor, as_completed

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

    # Round-13 review fix: `pool.map` raises on the FIRST exception and
    # skips every subsequent function — a single throttled CW call would
    # blank out control_plane_hourly for the whole stack. Switch to
    # `submit` + `as_completed` so each per-function fetch is isolated:
    # a failure logs a warning and drops that function's rows, the rest
    # of the fleet still lands in the parquet. Deterministic order is
    # restored by sorting rows on function_name after collection (the
    # arns list was sorted upstream, so this reproduces the prior order).
    rows: List[Dict[str, Any]] = []
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, arn): arn for arn in control_arns}
        for future in as_completed(futures):
            arn = futures[future]
            try:
                rows.extend(future.result())
            except Exception as e:
                function_name = arn.rsplit(":", 1)[-1]
                failed.append(function_name)
                logger.warning(
                    f"control-plane fetch failed for {function_name}: "
                    f"{type(e).__name__}: {e} — dropping this function's "
                    f"row(s), rollup continues for the rest of the fleet"
                )
    if failed:
        logger.warning(
            f"control_plane_hourly partition {target_date}/{target_hour}: "
            f"{len(failed)} function(s) failed to fetch metrics: "
            f"{sorted(failed)[:10]}{'...' if len(failed) > 10 else ''}"
        )
    # Restore deterministic order (function_name is the natural sort key —
    # component/hour are shared across rows within this partition). Round-14
    # review fix: sort key is ``bedrock_model``, not ``model`` — the latter
    # is always None and silently collapsed the sort to function-name-only.
    # This is currently masked by ``_build_control_plane_rows`` iterating
    # ``sorted(bedrock_by_model.keys())`` internally so per-function rows
    # already come out in model order, but the round-8
    # shared-columns-on-first-model invariant would silently break if that
    # inner iteration ever changed.
    rows.sort(
        key=lambda r: (r.get("function_name") or "", r.get("bedrock_model") or "")
    )

    # Round-14 review fix: if EVERY function's fetch failed, ``rows`` is
    # empty and the "no_activity" skip path masks the total outage as a
    # legitimate zero-activity hour — the idempotency guard then locks the
    # empty partition forever and no async retry / DLQ ever fires. Raise
    # so Lambda's async-retry policy can replay the hour; the DLQ alarm
    # eventually surfaces the outage to oncall. We DO tolerate partial
    # failure (some functions succeeded → still write a partial parquet):
    # only the "0 successes + N failures" case is treated as fatal.
    if failed and not rows:
        raise RuntimeError(
            f"control_plane_hourly {target_date}/{target_hour}: all "
            f"{len(failed)} control-plane function fetches failed and no "
            f"rows were produced. Refusing to write an empty parquet — the "
            f"idempotency skip would lock this hour into a permanent hole. "
            f"Sample failures: {sorted(failed)[:5]}"
        )

    if not rows:
        logger.info(f"No control-plane activity for {target_date} hour={target_hour}")
        return {"skipped": True, "reason": "no_activity"}

    # Round-20 review fix (#1720): if pricing was unavailable this
    # invocation AND any row has bedrock activity, DO NOT write a
    # parquet with zero est_bedrock_cost — the S3 idempotency skip
    # would then permanently lock the wrong cost for the hour. Raise
    # so Lambda's async retry replays with a fresh pricing-load attempt
    # instead. If no row has bedrock activity, empty pricing is fine
    # (nothing to price) and we proceed to write.
    if _bedrock_pricing_unavailable and any(
        r.get("bedrock_model") is not None for r in rows
    ):
        raise RuntimeError(
            f"control_plane_hourly {target_date}/{target_hour}: pricing "
            f"map was unavailable this invocation AND at least one row "
            f"has bedrock activity — refusing to write zero-cost partition "
            f"that S3 idempotency would lock. Async retry will replay "
            f"with a fresh pricing load."
        )

    key = f"control_plane/date={target_date}/hour={target_hour}/data.parquet"
    _write_parquet(rows, key)
    return {"skipped": False, "rows": len(rows), "s3_key": key}


def _rollup_data_plane_lambda_hourly(
    target_date: str, target_hour: str
) -> Dict[str, Any]:
    """Query CloudWatch for the previous hour's data-plane Lambda
    compute metrics and write one Parquet row per (function, hour) to
    S3 under ``data_plane_lambda/date=<D>/hour=<H>/data.parquet``.

    Sibling of ``_rollup_control_plane_hourly`` — same Duration/
    Invocations math, but scoped to Lambdas tagged ``idp:plane=data``
    and with a minimal Lambda-only schema (no Bedrock/Athena columns).
    Data-plane Bedrock/Textract API costs already flow through
    ``metering_hourly`` via ``save_metering_data``'s per-doc metering
    counters — this table closes the gap for the Lambda compute cost
    hosting those API calls.

    Idempotency: partition-write skip identical to control_plane_hourly.
    Isolation: per-function try/except (same round-13 pattern) so a
    single throttled CW call doesn't blank the whole hour.
    """
    if _s3_object_exists(
        f"data_plane_lambda/date={target_date}/hour={target_hour}/data.parquet"
    ):
        logger.info(
            f"data_plane_lambda_hourly partition date={target_date} "
            f"hour={target_hour} already exists — skipping"
        )
        return {"skipped": True, "reason": "partition_exists"}

    data_arns = _discover_data_plane_lambdas()
    if not data_arns:
        logger.info(
            "No data-plane Lambdas discovered (fresh stack or all untagged). "
            "Nothing to roll up for data_plane_lambda_hourly this hour."
        )
        return {"skipped": True, "reason": "no_data_lambdas"}

    hour_start, hour_end = _hour_window(target_date, target_hour)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch_one_data(function_arn: str) -> Optional[Dict[str, Any]]:
        function_name = function_arn.rsplit(":", 1)[-1]
        component = _component_for_function(function_name)
        # ONLY Duration + Invocations for data-plane — skip the Bedrock/
        # Athena metric fetches that control-plane needs. Bedrock spend
        # for data-plane is already captured in metering_hourly via
        # save_metering_data's per-doc counters.
        queries = [
            {
                "Id": "d",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Duration",
                        "Dimensions": [
                            {"Name": "FunctionName", "Value": function_name}
                        ],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
            },
            {
                "Id": "i",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/Lambda",
                        "MetricName": "Invocations",
                        "Dimensions": [
                            {"Name": "FunctionName", "Value": function_name}
                        ],
                    },
                    "Period": 3600,
                    "Stat": "Sum",
                },
            },
        ]
        # Round-23 review fix (#659): loop on NextToken for defensive
        # consistency with the sibling paths in ``_get_athena_bytes_sum``
        # / ``_get_bedrock_tokens_by_model`` (round-19 pagination fix).
        # In practice this call returns 2 datapoints and cannot paginate,
        # but the accumulate-safe ``_flatten_cw_response`` handles multi-
        # page responses correctly if CW ever changes behavior.
        flat: Dict[str, float] = {}
        next_token: Optional[str] = None
        while True:
            call_kwargs: Dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": hour_start,
                "EndTime": hour_end,
            }
            if next_token:
                call_kwargs["NextToken"] = next_token
            raw = cloudwatch_client.get_metric_data(**call_kwargs)
            page = _flatten_cw_response(raw)
            for k, v in page.items():
                flat[k] = flat.get(k, 0.0) + v
            next_token = raw.get("NextToken")
            if not next_token:
                break
        duration_ms = flat.get("d", 0.0)
        invocations = flat.get("i", 0.0)
        if invocations <= 0.0 and duration_ms <= 0.0:
            return None  # no activity this hour — drop the row
        mem_mb, arch = _get_lambda_memory_mb(function_name)
        gb_second_price = (
            LAMBDA_ARM64_GB_SECOND_PRICE
            if arch == "arm64"
            else LAMBDA_X86_64_GB_SECOND_PRICE
        )
        gb_seconds = (duration_ms / 1000.0) * (mem_mb / 1024.0)
        est_lambda_cost = (
            gb_seconds * gb_second_price + invocations * LAMBDA_REQUEST_PRICE
        )
        return {
            "hour_ts": hour_start,
            "function_name": function_name,
            "component": component,
            "invocations": int(invocations),
            "duration_ms_sum": int(duration_ms),
            "est_lambda_cost": float(est_lambda_cost),
        }

    rows: List[Dict[str, Any]] = []
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one_data, arn): arn for arn in data_arns}
        for future in as_completed(futures):
            arn = futures[future]
            try:
                row = future.result()
                if row is not None:
                    rows.append(row)
            except Exception as e:
                function_name = arn.rsplit(":", 1)[-1]
                failed.append(function_name)
                logger.warning(
                    f"data-plane lambda-cost fetch failed for {function_name}: "
                    f"{type(e).__name__}: {e} — dropping this function's "
                    f"row, rollup continues for the rest of the fleet"
                )

    if failed:
        logger.warning(
            f"data_plane_lambda_hourly partition {target_date}/{target_hour}: "
            f"{len(failed)} function(s) failed to fetch metrics: "
            f"{sorted(failed)[:10]}{'...' if len(failed) > 10 else ''}"
        )
    rows.sort(key=lambda r: r.get("function_name") or "")

    # Same total-outage guard as control-plane rollup — refuse to lock
    # an empty partition when every function failed.
    if failed and not rows:
        raise RuntimeError(
            f"data_plane_lambda_hourly {target_date}/{target_hour}: all "
            f"{len(failed)} data-plane function fetches failed and no "
            f"rows were produced. Refusing to write an empty parquet — "
            f"the idempotency skip would lock this hour into a permanent "
            f"hole. Sample failures: {sorted(failed)[:5]}"
        )

    if not rows:
        logger.info(
            f"No data-plane Lambda activity for {target_date} hour={target_hour}"
        )
        return {"skipped": True, "reason": "no_activity"}

    key = f"data_plane_lambda/date={target_date}/hour={target_hour}/data.parquet"
    _write_parquet(rows, key, schema_name="data_plane_lambda")
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
    # Round-15 review fix: only guard the sub-hourlies whose daily
    # partition is about to be written. An already-committed daily is
    # idempotency-locked, so its input hourly's gaps can never affect
    # what we write here — blocking on them would only wedge the OTHER
    # daily forever.
    guard_tables: List[str] = []
    if not daily_exists:
        guard_tables.append("metering_hourly")
    if not docs_daily_exists:
        guard_tables.append("metering_docs_hourly")
    if guard_tables:
        _require_hourly_matches_raw_metering(
            target_date, tables_to_guard=tuple(guard_tables)
        )

    # --- metering_daily (cost per service/unit) ---
    # Round-13 review fix: per-INSERT try/except isolation to match
    # ``_run_hourly``. Before this, a transient Athena failure on the
    # first INSERT would raise and skip the second one entirely; the
    # async-retry would then re-run the succeeded one (idempotent skip)
    # AND retry the failed one. That's fine per-day, but on a
    # both-failed run the caller would only see the first error.
    # Isolating each INSERT records BOTH errors in the result dict so
    # CloudWatch logs and the (re-raised) final exception carry the
    # union.
    errors: List[str] = []
    # Round-18 fix (#639): track whether ANY sub-INSERT was permanent
    # (ValueError) so the aggregator re-raise preserves the class.
    any_permanent = False
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
        try:
            # Round-16 idempotency key — same pattern as the hourly INSERTs.
            result["metering_daily"] = {
                "query_execution_id": _run_athena(
                    sql,
                    idempotency_key=_idempotency_key("metering_daily", target_date),
                ),
                "skipped": False,
            }
        except ValueError as e:
            logger.exception("metering_daily PERMANENT INSERT failure")
            errors.append(f"metering_daily(permanent): {type(e).__name__}: {e}")
            result["metering_daily"] = {"error": str(e), "skipped": False}
            any_permanent = True
        except Exception as e:
            logger.exception("metering_daily INSERT failed")
            errors.append(f"metering_daily: {type(e).__name__}: {e}")
            result["metering_daily"] = {"error": str(e), "skipped": False}

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
        try:
            # Round-16 idempotency key.
            result["metering_docs_daily"] = {
                "query_execution_id": _run_athena(
                    sql,
                    idempotency_key=_idempotency_key(
                        "metering_docs_daily", target_date
                    ),
                ),
                "skipped": False,
            }
        except ValueError as e:
            logger.exception("metering_docs_daily PERMANENT INSERT failure")
            errors.append(f"metering_docs_daily(permanent): {type(e).__name__}: {e}")
            result["metering_docs_daily"] = {"error": str(e), "skipped": False}
            any_permanent = True
        except Exception as e:
            logger.exception("metering_docs_daily INSERT failed")
            errors.append(f"metering_docs_daily: {type(e).__name__}: {e}")
            result["metering_docs_daily"] = {"error": str(e), "skipped": False}

    # Legacy top-level keys for backward-compat with the existing test
    # + operator invocation shape. Round-18 fix (#631): the aggregate
    # ``skipped`` and ``query_execution_id`` used to reflect only
    # ``metering_daily``, so a caller reading the legacy top-level shape
    # would see ``skipped=True`` when metering_daily was idempotently
    # skipped even though metering_docs_daily actually wrote. Now:
    # ``skipped`` is True iff BOTH sub-dailies skipped, and
    # ``query_execution_id`` prefers a real ID from either sub-daily.
    d_daily = result["metering_daily"]
    d_docs = result["metering_docs_daily"]
    result["skipped"] = bool(d_daily.get("skipped")) and bool(d_docs.get("skipped"))
    for sub in (d_daily, d_docs):
        if "query_execution_id" in sub:
            result["query_execution_id"] = sub["query_execution_id"]
            break
    # Raise AFTER both INSERTs have had a chance to run so a partial
    # success is recorded in the result dict and the async-retry only
    # replays the truly-failed table (idempotency skip on the succeeded
    # one). Round-18 fix (#639): preserve permanent-vs-retryable
    # classification — ValueError from _wait_for_athena means don't
    # burn async retries.
    if errors:
        msg = (
            f"Daily rollup for date={target_date} failed on "
            f"{len(errors)} table(s): {'; '.join(errors)}"
        )
        raise (ValueError if any_permanent else RuntimeError)(msg)
    return result


def _hourly_ever_written(before_date: str) -> bool:
    """Return True if ``metering_hourly`` OR raw ``metering`` has ANY row
    on a date strictly before ``before_date``.

    Distinguishes true deploy-day (never rolled up anything AND no raw
    data older than today) from a total-outage day (hourly rows exist
    on prior dates, or raw metering shows the stack was actively
    processing documents on prior dates so an empty hourly-for-target
    isn't day-1).

    Round-11 fix used only prior-hourly-row existence. Round-13 review
    fix: a multi-day hourly outage would leave ``metering_hourly`` empty
    on every prior date even though raw metering shows the stack
    processed documents on those days — ``is_deploy_day`` would still
    return True and skip the guard, letting a legitimately incomplete
    daily write and lock idempotently. Extend the probe to also check
    raw metering on prior dates; either signal proves we're past day-1.

    Fast SELECTs: LIMIT 1 with partition-pruned WHERE date < '{X}'.
    ``emit_self_cost=False`` — bookkeeping probe, not a genuine
    cost-attribution query.
    """
    # nosec B608 — before_date is from datetime.strftime, not user input.
    hourly_sql = (
        f'SELECT 1 FROM "{DATABASE}"."metering_hourly" '  # nosec B608
        f"WHERE date < '{before_date}' LIMIT 1"  # nosec B608
    )
    raw_sql = (
        f'SELECT 1 FROM "{DATABASE}"."metering" '  # nosec B608
        f"WHERE date < '{before_date}' LIMIT 1"  # nosec B608
    )
    # Retry a couple times before the defensive default-True — round-12
    # review fix. On the first-daily-after-deploy path, an Athena
    # throttle would otherwise mis-classify a legitimate deploy-day as
    # "hourly-has-been-written", spuriously firing the guard. Two
    # retries survive a single-transient throttle without moving to
    # the default. Falls back to True on persistent failure so we
    # don't accidentally write and lock a zero daily.
    # Round-15/16 review fix: TABLE_NOT_FOUND on the hourly probe alone
    # doesn't prove day-1 — the operator could have dropped/renamed
    # metering_hourly on a stack whose raw ``metering`` table still holds
    # historical rows. Round-15 mistakenly returned False on hourly
    # TABLE_NOT_FOUND without consulting raw. This fix falls through to
    # the raw probe when hourly is missing; only if BOTH tables are
    # missing (or hourly is missing AND raw is empty on prior dates) do
    # we return False and let the caller treat it as day-1.
    # Round-19 review fix (#806): use the shared
    # ``_is_athena_table_missing`` helper introduced in round-18
    # instead of the hand-copied marker set that lived here. The
    # helper unifies the drift class that rounds 6/7/8/11/15/16/17
    # each edited a different copy of.

    def _probe_raw() -> Optional[bool]:
        """Return True if raw metering has prior-date rows, False if it's
        empty, None if the probe itself failed or the table is missing.
        """
        try:
            rows = _run_athena_query_with_results(raw_sql, emit_self_cost=False)
            return bool(rows)
        except Exception as e:
            if _is_athena_table_missing(e, "metering"):
                logger.info(
                    f"_hourly_ever_written: raw metering table also missing "
                    f"({e}); no prior-date data possible."
                )
                return False  # missing raw ⇒ no prior data ⇒ day-1 signal
            logger.warning(f"_hourly_ever_written: raw metering probe failed ({e})")
            return None

    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            hourly_rows = _run_athena_query_with_results(
                hourly_sql, emit_self_cost=False
            )
            if hourly_rows:
                return True
            # Hourly is present but has no prior rows. Consult raw.
            raw_rows = _run_athena_query_with_results(raw_sql, emit_self_cost=False)
            return bool(raw_rows)
        except Exception as e:
            last_error = e
            if _is_athena_table_missing(e, "metering_hourly"):
                # Hourly table missing. STILL consult raw before deciding —
                # raw metering may hold historical rows even if hourly was
                # dropped, and that means we're NOT day-1.
                logger.info(
                    f"_hourly_ever_written: hourly table missing ({e}); "
                    f"falling through to raw-metering probe."
                )
                raw = _probe_raw()
                if raw is None:
                    # Raw probe failed too — safest default is True
                    # (guard will fire on empty hourly rather than
                    # silently writing a zero daily).
                    return True
                # raw is True → prior data → not day-1
                # raw is False → no prior data anywhere → day-1
                return raw
            if attempt < 2:
                time.sleep(1 + attempt)  # 1s, 2s
    logger.warning(
        f"_hourly_ever_written probe failed after 3 attempts ({last_error!r}); "
        f"defaulting to True (guard will fire on empty hourly for target "
        f"date rather than silently writing a 0-doc daily)."
    )
    return True


def _require_hourly_matches_raw_metering(
    target_date: str,
    tables_to_guard: Optional[Tuple[str, ...]] = None,
) -> None:
    """Fail loudly if either hourly rollup is missing any hour that raw
    metering has data for (deploy-day exception below).

    Guards **both** ``metering_hourly`` and ``metering_docs_hourly`` by
    default — the rollup writes them sequentially, so a transient Athena
    outage could leave one populated and the other empty for the same
    hour. Checking only ``metering_hourly`` would let an incomplete
    ``metering_docs_daily`` land and become idempotently locked, silently
    under-counting ``n_docs``/``sum_pages`` for that day forever.

    Round-15 review fix: callers can pass ``tables_to_guard`` to restrict
    the check to only the sub-hourlies whose corresponding daily
    partitions are ACTUALLY about to be written. Otherwise, a case where
    ``metering_daily`` is already committed but only
    ``metering_docs_daily`` is pending would be blocked forever if
    ``metering_hourly`` had an unrelated gap — the gap can never affect
    metering_daily (already committed, idempotency skip), yet it holds
    up the docs-daily we could safely write.

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

    guard_tables = tables_to_guard or ("metering_hourly", "metering_docs_hourly")
    for hourly_table in guard_tables:
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


def _cached_stack_tree() -> List[str]:
    """The CFN stack tree (root + nested), walked at most once per invocation.

    Both hourly plane rollups need it. The walk is a ``ListStackResources``
    paginate per stack in the tree, and the topology cannot change while a
    single rollup runs, so doing it twice was pure duplicate API load.
    ``handler`` clears the cache so a stack update between fires is picked up.
    """
    global _stack_tree_cache
    if _stack_tree_cache is None:
        _stack_tree_cache = _enumerate_stack_tree(STACK_NAME)
        logger.info(
            f"Stack tree from root {STACK_NAME!r}: "
            f"{len(_stack_tree_cache)} stack(s) — {_stack_tree_cache}"
        )
    return _stack_tree_cache


def _cached_data_plane_arns() -> List[str]:
    """Lambda ARNs tagged ``idp:plane=data`` in this stack tree, fetched at most
    once per invocation.

    ``_discover_control_plane_lambdas`` subtracts this set and
    ``_discover_data_plane_lambdas`` returns it, so the same tag query used to
    run twice per hourly fire.
    """
    global _data_plane_arn_cache
    if _data_plane_arn_cache is None:
        _data_plane_arn_cache = _get_resources_by_tag(
            {
                "aws:cloudformation:stack-name": _cached_stack_tree(),
                "idp:plane": ["data"],
            }
        )
    return _data_plane_arn_cache


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

    stack_tree = _cached_stack_tree()

    all_idp = _get_resources_by_tag({"aws:cloudformation:stack-name": stack_tree})
    # Scope the data-plane query to the SAME tree — a shared account with
    # multiple IDP stacks would otherwise cross-contaminate.
    data_plane = set(_cached_data_plane_arns())

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


def _discover_data_plane_lambdas() -> List[str]:
    """Return data-plane Lambda ARNs — the opposite side of the split
    from ``_discover_control_plane_lambdas``. Same tag-based query,
    inverted: everything with ``idp:plane=data`` in the stack tree.

    Data-plane Bedrock/Textract API COSTS already flow through the raw
    ``metering`` table (per-doc). What's missing is the Lambda compute
    cost of the OCR/Classification/Extraction/etc. Lambdas themselves —
    that's what ``data_plane_lambda_hourly`` closes. No Bedrock/Athena
    metric read here; only Duration/Invocations.
    """
    if not STACK_NAME:
        logger.warning("STACK_NAME env var not set; cannot discover Lambdas")
        return []
    return list(_cached_data_plane_arns())


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
    re-raised from this helper; the caller (`_rollup_control_plane_hourly`)
    catches per-function exceptions and drops that Lambda's rows from the
    hour rather than aborting the whole rollup — round-13 review fix,
    per-function isolation so one throttled Lambda doesn't hide the
    entire fleet's hour. Per-function failures are logged as
    ``logger.warning`` (log-only, no custom metric). The DLQ signal
    fires ONLY when *every* function fails AND zero rows are produced
    (see the ``RuntimeError`` raise in ``_rollup_control_plane_hourly``
    below) — Lambda's async retry then delivers to the rollup DLQ,
    so a full CloudWatch outage doesn't hide silently behind the
    per-partition idempotency skip.
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
    # Round-16 review fix: get_metric_data response is paginated via
    # NextToken. Chunking QUERIES at 500 per-call handles the per-request
    # query limit, but each response can still return NextToken if the
    # datapoint set spills past the response-size ceiling. Loop on
    # NextToken so we don't silently drop tail datapoints.
    for chunk_start in range(0, len(queries), 500):
        chunk = queries[chunk_start : chunk_start + 500]
        next_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {
                "MetricDataQueries": chunk,
                "StartTime": hour_start,
                "EndTime": hour_end,
            }
            if next_token:
                kwargs["NextToken"] = next_token
            resp = cloudwatch_client.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                # Filter NaN before summing — a broken metric occasionally yields
                # NaN, which would poison the int() cast at the athena_bytes_sum
                # cast site downstream (raises ValueError, aborts the rollup,
                # lands in the DLQ). Sibling paths (_flatten_cw_response,
                # _get_bedrock_tokens_by_model) filter — this one must too.
                values = [
                    v
                    for v in (r.get("Values") or [])
                    if v is not None and math.isfinite(v)
                ]
                total += float(math.fsum(values))
            next_token = resp.get("NextToken")
            if not next_token:
                break
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
        # GetMetricData caps at 500 queries per call AND per-response can
        # paginate via NextToken — round-16 review fix: loop on NextToken
        # so tail datapoints aren't silently dropped.
        for chunk_start in range(0, len(queries), 500):
            chunk = queries[chunk_start : chunk_start + 500]
            next_token: Optional[str] = None
            while True:
                kwargs: Dict[str, Any] = {
                    "MetricDataQueries": chunk,
                    "StartTime": hour_start,
                    "EndTime": hour_end,
                }
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = cloudwatch_client.get_metric_data(**kwargs)
                for r in resp.get("MetricDataResults", []):
                    model = id_to_model.get(r["Id"])
                    if model is None:
                        continue
                    values = [
                        v
                        for v in (r.get("Values") or [])
                        if v is not None and math.isfinite(v)
                    ]
                    total = float(math.fsum(values))
                    bucket = result.setdefault(model, {"in": 0.0, "out": 0.0})
                    bucket[direction] += total
                next_token = resp.get("NextToken")
                if not next_token:
                    break
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
            v for v in (r.get("Values") or []) if v is not None and math.isfinite(v)
        ]
        result[r["Id"]] = result.get(r["Id"], 0.0) + float(math.fsum(values))
    return result


def _get_lambda_memory_mb(function_name: str) -> Tuple[int, str]:
    """Return the Lambda's configured (MemorySize MB, architecture).

    Cached across warm-container invocations so we don't spam
    get_function_configuration — both properties are static per deployed
    function. Falls back to (512 MB, "x86_64") on lookup failure: 512 is the
    median across this stack's Lambdas (see the rationale at the ``except``
    below — 128, the AWS floor, was up to ~24x under-count on a 3008 MB
    function), and x86_64 is the AWS default architecture, so the arch half
    errs toward *slightly higher* per-GB-second cost rather than under-
    estimating.

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

    # Round-15 review fix: drop empty-string / falsy model keys entirely.
    # A malformed CW dimension can emit ``Model=""`` — that used to sort
    # FIRST under ``sorted(bedrock_by_model.keys())`` and steal the
    # shared-columns row from the real model, so a downstream
    # ``WHERE bedrock_model = 'us.anthropic.claude-opus-4-1'`` query
    # would see 0 invocations / duration / athena_bytes for the real
    # model. Filtering them out here means their tokens are lost, which
    # is the lesser evil vs. mis-attributing shared columns to a
    # non-identifiable model — the emitter-side WARN (see
    # ``_get_bedrock_tokens_by_model``) already surfaces the malformed
    # dim.
    filtered = {m: v for m, v in bedrock_by_model.items() if m}
    if len(filtered) != len(bedrock_by_model):
        logger.warning(
            f"Dropped {len(bedrock_by_model) - len(filtered)} bedrock model "
            f"row(s) with empty-string Model dim for {function_name}: their "
            f"shared-column values would otherwise be mis-attributed."
        )

    if not filtered:
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
    for i, model in enumerate(sorted(filtered.keys())):
        tokens = filtered[model]
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
    # Round-13 review fix: distinguish "no Bedrock activity" (model is
    # None) from "empty-string model name from a malformed metric
    # dimension". `if not model:` treated both the same and let empty
    # strings sneak the DEFAULT price. Now: None returns the neutral
    # default; a non-None empty string falls through to the pricing-map
    # lookup, misses (no `bedrock/` entry with empty tail), and emits
    # the same ERROR + zero-cost path any unknown model gets.
    if model is None:
        return dict(DEFAULT_BEDROCK_PRICE_PER_TOKEN)
    pricing_map = _load_bedrock_pricing_from_config()
    key = f"bedrock/{model}"
    entry = pricing_map.get(key)
    if entry:
        # Round-19 review fix (#1637): don't hardcode
        # ``inputTokens``/``outputTokens`` key names — the map is
        # populated from the config's ``unit.name`` which is whatever
        # the operator wrote in pricing.yaml (could be ``input_tokens``,
        # ``input-tokens``, ``inputToken`` singular, etc.). Match any
        # case- and separator-insensitive variant so a config-side
        # rename doesn't silently produce zero cost.
        def _pick(entry: Dict[str, float], *candidates: str) -> float:
            # Try exact match first (fast path for the current standard).
            for c in candidates:
                if c in entry:
                    return entry[c]
            # Fallback: normalize keys and candidates for match.
            norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
            normalized_entry = {norm(k): v for k, v in entry.items()}
            for c in candidates:
                v = normalized_entry.get(norm(c))
                if v is not None:
                    return v
            return 0.0

        return {
            "in": _pick(entry, "inputTokens", "input_tokens", "inputToken"),
            "out": _pick(entry, "outputTokens", "output_tokens", "outputToken"),
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
    global _bedrock_pricing_map, _bedrock_pricing_unavailable
    if _bedrock_pricing_map is not None:
        return _bedrock_pricing_map
    if not CONFIGURATION_TABLE_NAME:
        logger.warning(
            "CONFIGURATION_TABLE_NAME env var not set; Bedrock cost columns "
            "will use hardcoded default pricing."
        )
        # Env var never appears mid-lifetime — this IS a "cache the empty"
        # result: no retry will help.
        # Round-23 review fix (#1889): the round-20 pricing-unavailable
        # flag was set on the empty-result and exception paths but NOT
        # here — a stack with the env var missing would write
        # est_bedrock_cost=0 for rows with real bedrock activity and
        # let S3 idempotency lock those zeros forever. Set the flag
        # so ``_rollup_control_plane_hourly`` raises on bedrock rows
        # instead of writing zeros.
        _bedrock_pricing_unavailable = True
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
        # Round-15 review fix: cache the empty result WITHIN this
        # invocation so the CW fan-out's 10 worker threads don't each
        # race back to _load_bedrock_pricing_from_config() and pile
        # duplicate ConfigurationManager.get_merged_pricing() reads on
        # the config DynamoDB table. Cross-invocation retry semantics
        # are preserved because handler() resets
        # ``_bedrock_pricing_map = None`` at the start of every fire.
        # Round-20 review fix (#1720): mark the empty state as
        # "pricing unavailable" (not merely "no entries known") so the
        # caller can distinguish and raise if bedrock activity is
        # present — prevents the S3 idempotency skip from locking
        # est_bedrock_cost=0 for the hour when pricing was transiently
        # broken. Round-23 (#1889): ``global _bedrock_pricing_unavailable``
        # now declared at the top of the function alongside
        # ``_bedrock_pricing_map`` so all three assignments (env-var-unset,
        # empty-result, exception) share one declaration and Python
        # doesn't SyntaxError on assign-before-global.
        logger.warning(
            "ConfigurationTable returned 0 pricing entries; caching empty "
            "within THIS invocation so workers don't stampede DynamoDB. "
            "Next invocation resets the cache and retries."
        )
        _bedrock_pricing_map = {}
        _bedrock_pricing_unavailable = True
        return _bedrock_pricing_map
    except Exception as e:
        # Same reasoning as the empty-result path above: cache the empty
        # result within this invocation to avoid worker-thread stampede,
        # but the handler-level reset guarantees cross-invocation retry.
        logger.warning(
            f"Failed to load pricing from ConfigurationTable "
            f"({CONFIGURATION_TABLE_NAME!r}): {e}. Falling back to hardcoded "
            f"default pricing for Bedrock cost columns THIS INVOCATION; "
            f"next invocation will retry."
        )
        _bedrock_pricing_map = {}
        _bedrock_pricing_unavailable = True
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
    # AgentCore — MCP-based agent runtime and its gateway manager.
    # Placed BEFORE the analytics/chat agent rules so ``agentcore`` wins
    # for AgentCoreMCPHandler / AgentCoreGatewayManager Lambdas (they
    # would otherwise fall through to ``other-control`` because they
    # don't match ``analyticsagent`` / ``agentchat`` / ``agentprocessor``).
    (re.compile(r"agentcore"), "agent-core"),
    # Analytics agents (SQL-driven) and doc-chat processors — matched
    # before broader user/agent patterns.
    (
        re.compile(r"analyticsagent|agentchat|agentprocessor"),
        "analytics-agent",
    ),
    (re.compile(r"chatwithdocument|chatstream"), "doc-chat"),
    # Blueprint (schema) optimization — an LLM-driven admin tool that
    # tunes discovery blueprints. Sibling of policy-discovery, kept
    # separate so its cost is visible when a user runs an optimize pass.
    (re.compile(r"blueprintoptimization"), "blueprint-optimization"),
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
    # Circuit breaker manages backpressure to Bedrock — invoked per
    # throttle event, kept in its own bucket so throttle-driven spend
    # is visible separately from steady capacity planning.
    (re.compile(r"circuitbreaker"), "circuit-breaker"),
    # Version-check resolver is hit on every UI page load — high-
    # frequency, worth its own bucket rather than being lumped into
    # ``other-control``.
    (re.compile(r"versioncheck"), "version-check"),
    (re.compile(r"finetuning"), "finetuning"),
    # Cognito / user-directory management.
    (re.compile(r"usermanagement|usersync"), "user-mgmt"),
    # UI-facing dispatchers (every page load hits these).
    (
        re.compile(r"lookupfunction|apihandler|httpapidispatcher"),
        "api-dispatch",
    ),
    # Data-plane pipeline stages — round-24 UI polish. These Lambdas
    # carry ``idp:plane=data`` so they only ever land in
    # ``data_plane_lambda_hourly`` (never in ``control_plane_hourly``).
    # Without these rules, every data-plane row's component fell
    # through to "other-control" — cost math was correct but the label
    # was misleading. CFN names for these look like
    # ``PATTERNSTACK-2UBGW8A18HIT-OCRFunction-xxxx`` etc. Match on the
    # bare stage name embedded in the middle of the CFN-generated ID.
    #
    # BDA rules come FIRST among the data-plane stages, and name each BDA
    # Lambda explicitly. Two bugs in the round-24 version, both fixed here:
    #
    # 1. The rule was ``(^|[^a-z])bda`` — a boundary guard so ``lambda``
    #    (``GetDomainLambda`` etc.) wouldn't match. But it ALSO failed on
    #    ``InvokeBDAFunction``: lowercased, ``invoke*bda*function`` has the
    #    letter ``e`` before ``bda``, so the BDA-mode invoke Lambda — the
    #    most expensive one on that path — silently fell through to
    #    ``other-control``.
    # 2. The rule sat BELOW ``processresultsfunction``, so
    #    ``BDAProcessResultsFunction`` was claimed by the pipeline
    #    ``process-results`` rule instead.
    #
    # An explicit list fixes both without a boundary guard, and can't
    # false-positive on a CFN random suffix that happens to contain ``bda``.
    # The first three are data plane; BDAOCRProject is a control-plane CFN
    # custom resource that still belongs in the ``bda`` bucket. (One more
    # BDA-ish Lambda exists — ``SyncBdaIdpResolverFunction`` in
    # nested/api-resolvers — but it is a UI resolver for BDA *blueprint sync*,
    # not a BDA invocation, so it stays in ``other-control`` as it was before
    # this change.)
    (
        re.compile(r"invokebda|bdaprocessresults|bdacompletion|bdaocrproject"),
        "bda",
    ),
    # ``rulevalidation`` must precede ``classificationfunction``:
    # ``RuleValidationPolicyClassificationFunction`` contains
    # ``classificationfunction`` and was being labelled ``classification``.
    (re.compile(r"rulevalidation"), "rule-validation"),
    (re.compile(r"ocrfunction"), "ocr"),
    (re.compile(r"classificationfunction"), "classification"),
    (re.compile(r"extractionfunction"), "extraction"),
    (re.compile(r"assessmentfunction"), "assessment"),
    (re.compile(r"summarizationfunction"), "summarization"),
    (re.compile(r"evaluationfunction"), "evaluation"),
    (re.compile(r"processresultsfunction"), "process-results"),
    (re.compile(r"shardruntimefunction"), "shard-runtime"),
    (re.compile(r"savereportingdata"), "save-reporting"),
    (re.compile(r"workflowtracker"), "workflow-tracker"),
    (re.compile(r"queueprocessor"), "queue-processor"),
    (re.compile(r"queuesender"), "queue-sender"),
    (re.compile(r"pipelinehooks"), "pipeline-hooks"),
    # The remaining four data-plane Lambdas on DATA_PLANE_ALLOWLIST. Round-24
    # added rules for the pipeline stages but missed these, so their
    # ``data_plane_lambda_hourly`` rows carried ``component='other-control'``
    # — a label whose name says "control" appearing in the data-plane table.
    # ``scripts/tests/test_data_plane_component_labels.py`` now pins the
    # allowlist ↔ label mapping so a future addition can't be forgotten.
    #
    # KEEP EVERY LITERAL AT OR UNDER 24 CHARACTERS. Lambda function names cap at
    # 64 chars and CloudFormation truncates the *logical-ID* segment to fit, so
    # what arrives here is a PREFIX of the logical ID, not the whole thing. Live
    # examples from the deployment account, all cut to exactly 24:
    #   IDP1-PATTERNSTACK-170UXCO-BDAProcessResultsFunctio-05Xr5A5hn8po
    #   IDP1-PATTERNSTACK-170UXCO-RuleValidationPolicyClas-QmIjulE8f33f
    #   IDP1-APIRESOLVERSTACK-LI3-SyncBdaIdpResolverFuncti-P3kLHJldG4DK
    # ``postprocessingdecompressor`` (26) was over that budget and matched
    # nothing on any stack whose name is long enough to force truncation —
    # i.e. it silently failed to fix the very row it was added for. The
    # truncated-name shape in the test above now covers this.
    (re.compile(r"batchpreprocessor"), "batch-ingest"),
    (re.compile(r"jobtracker"), "job-tracker"),
    (re.compile(r"postprocessingdecomp"), "post-processing"),
    (re.compile(r"completesectionreview"), "hitl-review"),
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
        # Round-19 review fix (#806): use the shared
        # ``_is_athena_table_missing`` helper — the marker set here
        # used to be hand-copied and drifted independently over rounds
        # 6/7/8/11/15/16/17.
        if _is_athena_table_missing(e, table):
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


def _query_wrote_manifest(query_id: str) -> bool:
    """Positive-only success signal for an Athena INSERT INTO query.

    Called from ``_run_athena``'s cached-failure probe branch when
    ``get_query_execution`` has been throttled for all three probe
    attempts. Athena writes ``<query_id>-manifest.txt`` to the
    workgroup's ``OutputLocation`` ONLY on successful INSERT INTO /
    CTAS runs — presence of that key is a definitive success signal
    even when the control-plane API is unavailable.

    Returns True only on POSITIVE confirmation via ``HeadObject``.
    Any error, 404, missing/malformed ``OutputLocation`` returns
    False so the caller defaults to the safer restart branch — this
    helper MUST NEVER fail-open (i.e. never claim success without
    positive evidence), because a false-positive here would suppress
    the restart of a genuinely-cached-failure query and permanently
    lose that partition's data.
    """
    if not QUERY_OUTPUT_LOCATION or not QUERY_OUTPUT_LOCATION.startswith("s3://"):
        return False
    try:
        bucket_and_prefix = QUERY_OUTPUT_LOCATION[len("s3://") :]
        parts = bucket_and_prefix.split("/", 1)
        bucket = parts[0]
        prefix = parts[1] if len(parts) > 1 else ""
        if prefix and not prefix.endswith("/"):
            prefix = f"{prefix}/"
        key = f"{prefix}{query_id}-manifest.txt"
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as e:  # nosec — best-effort positive signal only
        logger.info(f"S3 manifest probe for {query_id} did not confirm success: {e}")
        return False


def _run_athena(
    sql: str,
    emit_self_cost: bool = True,
    idempotency_key: Optional[str] = None,
) -> str:
    """Start an Athena query and wait for completion. Returns QueryExecutionId.

    ``emit_self_cost=False`` skips the self-attribution CloudWatch metric
    for the query's ``DataScannedInBytes``. Idempotency-check SELECTs
    (LIMIT 1 partition probes) use this to avoid emitting per-partition
    ``AthenaBytesScanned`` metrics for every rollup fire, which was noise
    on the ``rollup-lambda`` component. Round-9 review fix.

    ``idempotency_key`` (round-16 review fix): if provided, passed to
    Athena as ``ClientRequestToken``. On Lambda async retry, the same
    token guarantees Athena returns the SAME QueryExecutionId instead
    of starting a new query — otherwise the check-then-INSERT is not
    atomic against Glue metadata propagation lag, and a slow first
    INSERT + fast retry could double-write a partition. Only apply
    to write queries (INSERT INTO ...); read queries don't need it
    because they're safely re-runnable.
    """
    if not DATABASE:
        raise RuntimeError("REPORTING_DATABASE env var not set")
    kwargs: Dict[str, Any] = {
        "QueryString": sql,
        "QueryExecutionContext": {"Database": DATABASE},
        "WorkGroup": WORKGROUP,
        "ResultConfiguration": (
            {"OutputLocation": QUERY_OUTPUT_LOCATION} if QUERY_OUTPUT_LOCATION else {}
        ),
    }
    if idempotency_key:
        # Athena requires ClientRequestToken be 32-128 chars, letters/digits/dash.
        # Truncate defensively; callers should already meet this.
        # Round-23 review fix (#2208): reserve space so the fresh-salt
        # restart below can append ``-r<10 digits>`` (12 chars) without
        # the salt being chopped off by the 128-char cap. Effective
        # user-controlled budget = 128 - 12 = 116 chars.
        kwargs["ClientRequestToken"] = idempotency_key[:116]
    response = athena_client.start_query_execution(**kwargs)
    query_id = response["QueryExecutionId"]
    # Round-19 review fix (#1948): Athena's ClientRequestToken idempotency
    # caches ALL prior QueryExecutionIds for a given token — including
    # FAILED and CANCELLED ones. On Lambda async retry (same anchor time
    # → same token), Athena returns the previously-FAILED QueryExecutionId
    # and our _wait_for_athena raises the same failure again → next retry
    # returns the same FAILED QID → the retry loop is defeated forever.
    # Fix: after start_query_execution, if the returned execution is
    # already in a terminal-failure state, we know Athena served us a
    # cached failure; start a FRESH query without the token so the
    # retry actually retries.
    if idempotency_key:
        # Round-20 review fix (#1937): retry the probe on transient
        # errors instead of swallowing → None → falling through. If the
        # probe itself keeps failing, DEFAULT TO RESTART (safer to
        # re-execute than to trust an unknown state and hit the cached-
        # failure loop the round-19 fix was meant to break).
        initial_state: Optional[str] = None
        _AthenaBotoCoreError: Any
        _AthenaClientError: Any
        from botocore.exceptions import (  # noqa: PLC0415
            BotoCoreError as _AthenaBotoCoreError,
        )
        from botocore.exceptions import (
            ClientError as _AthenaClientError,
        )

        for probe_attempt in range(3):
            try:
                initial = athena_client.get_query_execution(QueryExecutionId=query_id)
                initial_state = initial["QueryExecution"]["Status"]["State"]
                break
            except (_AthenaClientError, _AthenaBotoCoreError) as e:
                logger.warning(
                    f"Cached-failure probe for {query_id} attempt "
                    f"{probe_attempt + 1}/3 failed ({e})"
                )
                if probe_attempt < 2:
                    time.sleep(1 + probe_attempt)  # 1s, 2s
                else:
                    # Probe genuinely can't determine state. Before
                    # defaulting to the restart branch (which double-
                    # writes if the original query actually succeeded),
                    # look for a positive success signal in S3: Athena
                    # writes ``<query_id>-manifest.txt`` under the
                    # workgroup OutputLocation ONLY on successful
                    # INSERT INTO. If that key exists, the original
                    # already committed — skip restart and let round-
                    # 23's ``cached_success`` path suppress the
                    # AthenaBytesScanned re-emit.
                    if _query_wrote_manifest(query_id):
                        logger.info(
                            f"Probe for {query_id} exhausted, but S3 "
                            f"manifest confirms SUCCEEDED — skipping "
                            f"restart to avoid double-write."
                        )
                        initial_state = "SUCCEEDED"
                    else:
                        logger.warning(
                            f"Probe for {query_id} exhausted AND S3 "
                            f"manifest absent — defaulting to cached-"
                            f"failure restart branch (fail-safe)."
                        )
                        initial_state = "FAILED"  # trigger the restart

        # Round-23 review fix (#2374): track whether the RETURNED query
        # is a cached SUCCEEDED result — in that case ``_wait_for_athena``
        # will see SUCCEEDED at the first poll and emit
        # AthenaBytesScanned AGAIN, double-counting the cost that the
        # ORIGINAL query already emitted. Skip the emit on the cached-
        # success path.
        cached_success = initial_state == "SUCCEEDED"

        if initial_state in ("FAILED", "CANCELLED"):
            logger.warning(
                f"Athena returned cached {initial_state} QueryExecutionId "
                f"{query_id!r} for idempotency token — retry would loop "
                f"forever. Starting a FRESH query with a fresh token."
            )
            # Round-23 review fix (#2210): the cached-failure was
            # detected on a probe that could ALSO happen while the
            # original was still RUNNING (probe-exhausted default-to-
            # FAILED path). Stop the original before starting the fresh
            # one so we don't run two salted executions in parallel,
            # billing twice and racing writes to Glue metadata.
            try:
                athena_client.stop_query_execution(QueryExecutionId=query_id)
                logger.info(f"Stopped original query {query_id} before restarting.")
            except Exception as stop_err:  # nosec — best-effort
                logger.warning(
                    f"stop_query_execution({query_id}) failed before "
                    f"restart: {stop_err}"
                )
            # Round-20 review fix (#1949): don't DROP ClientRequestToken
            # on the restart — that forfeits dedup for the logical
            # write, so a Lambda hard-timeout race could double-INSERT.
            # Instead, append a fresh salt so Athena treats it as a NEW
            # logical write (breaks the cached-failure lock) while
            # still deduping any concurrent retry of THIS attempt.
            #
            # Salt is a UTC-second timestamp — high enough resolution
            # that two concurrent retries of THIS invocation share it
            # (dedup wins), but every subsequent async-retry gets a
            # different one (breaks lock).
            #
            # This is the ONE window ``ClientRequestToken`` can't close, and
            # it's why ``DataMartRollupFunction`` sets
            # ``ReservedConcurrentExecutions: 1`` in template.yaml — do not
            # remove that property. Two concurrent restarts of the same
            # partition landing in DIFFERENT wall-clock seconds get different
            # tokens and would both INSERT.
            #
            # The salt can't be derived from the event anchor time instead:
            # the anchor is identical across async retries by design (that's
            # what makes retries target the right partition), so an
            # anchor-derived salt would be stable across retries and
            # reinstate exactly the cached-failure lock this branch exists to
            # break. The two requirements — differ across sequential retries,
            # match across concurrent duplicates — have no single-token
            # solution, hence the concurrency pin.
            fresh_salt = str(int(time.time()))
            # Round-23 (#2208): input token was truncated to 116 chars
            # above so the "-r<10-digit-timestamp>" suffix (12 chars)
            # fits under the 128-char cap without being chopped.
            restart_key = f"{idempotency_key[:116]}-r{fresh_salt}"[:128]
            kwargs["ClientRequestToken"] = restart_key
            response = athena_client.start_query_execution(**kwargs)
            query_id = response["QueryExecutionId"]
            cached_success = False  # fresh execution, real emit is expected
    else:
        cached_success = False
    # Round-23 review fix (#2374): pass ``emit_self_cost=False`` when the
    # returned QueryExecutionId is a cached SUCCEEDED — the ORIGINAL
    # attempt already emitted AthenaBytesScanned for this query. Emitting
    # again would double-count in the next control_plane_hourly rollup.
    effective_emit = emit_self_cost and not cached_success
    _wait_for_athena(query_id, emit_self_cost=effective_emit)
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
    # Round-16 review fix: back-off + jitter + throttle handling on the
    # poll itself. Previously ``get_query_execution`` was called with no
    # try/except — a ThrottlingException on the poll (Athena's poll rate
    # limits are aggressive under high concurrency) was fatal to the
    # rollup. Fixed 1s sleep exacerbated concurrent throttle. Now:
    # exponential backoff capped at 5s + up to 500ms jitter, and
    # ThrottlingException / RequestLimitExceeded / InternalServerError
    # are retried in-place until the outer timeout_sec is exceeded.
    from botocore.exceptions import (
        BotoCoreError as _AthenaBotoCoreError,
    )
    from botocore.exceptions import (
        ClientError as _AthenaClientError,
    )

    _consecutive_throttles = 0
    _RETRYABLE_POLL_CODES = (  # noqa: N806
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "InternalServerException",
    )

    def _stop_orphan(qid: str) -> None:
        """Round-18 review fix (#1941, #1963): stop the orphan Athena
        query before raising TimeoutError from a poll-retry timeout.
        Without this, the query keeps scanning (and billing) up to
        Athena's own ceiling while Lambda has already given up. Called
        from both the BotoCoreError and ClientError retry-timeout
        branches so the two additional exit paths match the sibling
        terminal-state timeout below.
        """
        try:
            athena_client.stop_query_execution(QueryExecutionId=qid)
            logger.warning(
                f"Athena query {qid} orphan-stopped after poll-retry timeout."
            )
        except Exception as stop_err:  # nosec — best-effort telemetry.
            logger.warning(f"stop_query_execution({qid}) failed: {stop_err}")

    while True:
        try:
            response = athena_client.get_query_execution(QueryExecutionId=query_id)
        except _AthenaBotoCoreError as poll_err:
            # Round-17 review fix: BotoCoreError subclasses
            # (EndpointConnectionError, ReadTimeoutError,
            # ConnectTimeoutError) bypass the ClientError handler and
            # used to be fatal. Same in-place backoff+retry as the
            # throttle path — connection resets to Athena are the
            # exact case async retry can heal.
            _consecutive_throttles += 1
            backoff = min(2 ** (_consecutive_throttles - 1), 5.0)
            jitter = random.uniform(
                0, 0.5
            )  # decorrelates concurrent-Lambda retry (round-18)
            sleep_for = backoff + jitter
            logger.warning(
                f"Athena poll for {query_id} threw BotoCoreError "
                f"({type(poll_err).__name__}: {poll_err}) — sleeping "
                f"{sleep_for:.2f}s before retry."
            )
            time.sleep(sleep_for)
            if time.time() - started > timeout_sec:
                _stop_orphan(query_id)
                raise TimeoutError(
                    f"Athena poll for {query_id} exceeded {timeout_sec}s "
                    f"of BotoCoreError retries; last: {poll_err}"
                ) from poll_err
            continue
        except _AthenaClientError as poll_err:
            code = poll_err.response.get("Error", {}).get("Code", "")
            if code in _RETRYABLE_POLL_CODES:
                _consecutive_throttles += 1
                # 1s, 2s, 4s, capped at 5s + 0-500ms jitter.
                backoff = min(2 ** (_consecutive_throttles - 1), 5.0)
                # Time-derived jitter (no random module — deterministic
                # under test) — take fractional seconds mod 1.
                jitter = random.uniform(
                    0, 0.5
                )  # decorrelates concurrent-Lambda retry (round-18)
                sleep_for = backoff + jitter
                logger.warning(
                    f"Athena poll for {query_id} threw {code} "
                    f"(consecutive={_consecutive_throttles}); sleeping "
                    f"{sleep_for:.2f}s before retry."
                )
                time.sleep(sleep_for)
                if time.time() - started > timeout_sec:
                    _stop_orphan(query_id)
                    raise TimeoutError(
                        f"Athena poll for {query_id} exceeded {timeout_sec}s "
                        f"of poll throttle; last error: {poll_err}"
                    ) from poll_err
                continue
            # Non-throttle client error — propagate. Round-19 review
            # fix (#2103): stop the orphan query first so it doesn't
            # keep scanning + billing while Lambda gives up. Matches
            # the throttle/BotoCoreError timeout paths above.
            _stop_orphan(query_id)
            raise
        _consecutive_throttles = 0
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
            # Round-18 review fix (#1997): the bare ``does not exist``
            # marker used to false-positive on column/bucket/database/
            # role/view "does not exist" errors and misclassify them as
            # PERMANENT (which is fine for a permanent classification —
            # those never succeed on retry either — but muddies the DLQ
            # message). Column-specific and view-specific markers stay
            # as their own dedicated shapes; table-shape now goes
            # through the shared helper.
            permanent_markers = (
                "syntax_error",
                "syntax error",
                "semantic_error",
                "column_not_found",
                "no viable alternative",
                "hive_metastore_error",  # schema mismatch
                "invalid_view",
            )
            retryable_markers = (
                "throttling",
                "internal_error_query_engine",
                "internal_error",
                "service_unavailable",
                "resource_exhausted",
                "network_error",
            )
            # Table-missing is permanent (needs CFN/operator fix). Use
            # the shared helper — matches TABLE_NOT_FOUND /
            # EntityNotFoundException unconditionally, PLUS
            # "does not exist" only when bound to a real table name
            # via the fully-qualified segment. Prevents unrelated
            # "column does not exist" from stealing the permanent path.
            if _is_athena_table_missing(reason_lc):
                raise ValueError(
                    f"Athena query {query_id} PERMANENT (table missing) "
                    f"({state}): {reason}"
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


def _write_parquet(
    rows: List[Dict[str, Any]],
    key: str,
    schema_name: str = "control_plane",
) -> None:
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

    ``schema_name`` selects which per-table schema to use. Round-22:
    added ``"data_plane_lambda"`` for the sibling ``data_plane_lambda_hourly``
    table — its rows have only Lambda-cost columns (no Bedrock/Athena),
    so writing with the control-plane schema would fill the missing
    columns with null junk that Athena would then read back as
    permanent zeros in Bedrock/Athena cost columns.
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

    if schema_name == "control_plane":
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
    elif schema_name == "data_plane_lambda":
        # Data-plane Lambda cost: minimal Lambda-only columns. Bedrock
        # and Textract costs already live in metering_hourly via
        # save_metering_data's per-doc counters, so this table
        # deliberately omits them to avoid double-counting and to keep
        # the schema focused on the gap it closes.
        schema = pa.schema(
            [
                ("hour_ts", pa.timestamp("ms", tz="UTC")),
                ("function_name", pa.string()),
                ("component", pa.string()),
                ("invocations", pa.int64()),
                ("duration_ms_sum", pa.int64()),
                ("est_lambda_cost", pa.float64()),
            ]
        )
    else:
        raise ValueError(
            f"_write_parquet: unknown schema_name={schema_name!r} "
            f"(expected 'control_plane' or 'data_plane_lambda')"
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
