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
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Athena / Glue configuration passed in via env vars from CloudFormation.
DATABASE = os.environ.get("REPORTING_DATABASE", "")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
QUERY_OUTPUT_LOCATION = os.environ.get("ATHENA_QUERY_OUTPUT_LOCATION", "")
REPORTING_BUCKET = os.environ.get("REPORTING_BUCKET", "")
STACK_NAME = os.environ.get("STACK_NAME", "")

# Load-time guard. Every stack embeds its ``StackName`` into the SSM
# parameter path so the marker is per-stack. An empty ``STACK_NAME``
# would collapse the path to ``/idp//data-mart-rollup/…`` which every
# stack in the account then shares — one stack's ``state=completed``
# marker would short-circuit every other stack's migration and route
# it away from InitialPurge, leaving the fresh stack's rollup tables
# empty and untouched. Fail fast at import so a misconfigured deployment
# surfaces in the first invocation's cold-start rather than in a silent
# data hole. Placed at the TOP of the module (before any use of
# STACK_NAME in a module-level constant expression) so no downstream
# fallback like ``STACK_NAME or 'unknown'`` can ever take effect — a
# future refactor moving the guard down would resurrect the fallback
# constant silently.
if not STACK_NAME:
    raise RuntimeError(
        "STACK_NAME environment variable is empty; refusing to compute the SSM "
        "migration marker path because '/idp//data-mart-rollup/migration-complete' "
        "would collide across every stack in the account. Ensure the Lambda's "
        "Environment sets STACK_NAME to !Ref AWS::StackName."
    )

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
# S3 client with adaptive retry so bulk DeleteObjects calls in the
# migration's InitialPurge phase can outwait S3 SlowDown (503) bursts.
# Standard mode only retries 4 times with limited backoff, which the
# migration hit on 2026-09-22 when purging the 94-partition legacy
# metering_hourly prefix — a burst delete against a single S3 prefix
# blew past the standard retry budget. Adaptive mode adjusts its retry
# rate based on service responses and comfortably clears typical
# SlowDown windows.
_s3_config = boto3.session.Config(retries={"mode": "adaptive", "max_attempts": 10})
s3_client = boto3.client("s3", config=_s3_config)
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

# Per-invocation cache of the ``document_sections_*`` table list. Both
# ``_rollup_metering_hourly`` and ``_rollup_metering_docs_hourly`` need it to
# derive ``document_class`` for historical rows whose raw-metering column is
# NULL. Discovered once per invocation via ``information_schema.columns`` —
# same query as the read-side ``discover_document_sections_tables`` in
# ``analytics_document_service.py`` — and cleared alongside the other caches in
# ``handler`` so a Glue-crawler-added new class shows up on the next fire.
_document_sections_tables_cache: Optional[List[str]] = None

# Safe character set for double-quoting ``document_sections_*`` table names in
# the CTE UNION. Matches the read-side widget's guard at
# ``analytics_cost_service.py:1807-1817`` — hyphens (e.g.
# ``document_sections_1099-int``) and spaces are legal Glue table names but
# would parse as arithmetic without quoting; anything outside this set is
# skipped as a defense against SQL injection via Glue table names.
_SAFE_TABLE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. "
)


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
    global _document_sections_tables_cache
    _lambda_memory_cache.clear()
    _bedrock_pricing_map = None
    _bedrock_pricing_unavailable = False
    _stack_tree_cache = None
    _data_plane_arn_cache = None
    _document_sections_tables_cache = None

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
    if mode == "backfill":
        # Iterate a caller-supplied hour range and re-run the per-document
        # hourly rollups for each hour. Used by the reconciler Lambda to
        # fill in gaps left by missed schedules (all 4 arms — no ``arms``
        # in payload), and by DataMartMigrationStateMachine's BackfillChunk
        # task (which passes ``arms=["metering_hourly",
        # "metering_docs_hourly"]`` to skip the arms the migration doesn't
        # own — see the arms-restriction note in _run_backfill's docstring). See
        # docs/reporting-sql-layer.md §10 Track D.
        start_raw = event.get("start")
        end_raw = event.get("end")
        if not (start_raw and end_raw):
            raise ValueError(
                "backfill mode requires 'start' and 'end' ISO 8601 UTC timestamps"
            )
        arms = event.get("arms")
        if arms is not None and not isinstance(arms, list):
            raise ValueError(
                f"backfill 'arms' must be a list of arm-label strings; got {arms!r}"
            )
        return _run_backfill(start_raw, end_raw, arms=arms)
    if mode == "backfill_migrate":
        # DEPRECATED — migration now runs via the DataMartMigrationStateMachine
        # (AWS::StepFunctions::StateMachine in template.yaml). Kept here as
        # a no-op so any queued async retries from the pre-SFN Lambda-only
        # design succeed cleanly (rather than DLQ-firing an alarm the
        # operator has to interpret). The state machine is invoked by the
        # DataMartMigrationDispatcherFunction on CFN CustomResource fire.
        logger.warning(
            "mode='backfill_migrate' is deprecated — migration now runs via "
            "DataMartMigrationStateMachine. This event is a no-op. If you "
            "meant to run a migration, invoke the state machine directly "
            "(or bump ForceFresh on the CustomResource)."
        )
        return {
            "mode": "backfill_migrate",
            "deprecated": True,
            "action": "no-op (migration moved to Step Functions)",
        }
    if mode == "reconcile":
        # Fill in gaps left by missed schedules over the trailing 24 h.
        # Same idempotency as any other rollup — already-written partitions
        # are no-ops via the ``HeadObject``-skip guard, so this is
        # cheap when nothing is missing and self-heals when something is.
        # Wired to a separate EventBridge rule that fires at :35 each
        # hour (30 min after the :05 scheduled hourly) so the reconciler
        # doesn't chase a rollup that is still in flight.
        return _run_reconcile(anchor)
    # ── DataMartMigrationStateMachine task modes ──────────────────────
    # These modes are invoked by the state machine's Task states. Each
    # is a small, focused unit — the state machine orchestrates them.
    # See template.yaml::DataMartMigrationStateMachine for the ASL.
    if mode == "check_marker_state":
        # Read the SSM migration marker, parse its state, and return a
        # routing decision the state machine's Choice state consumes.
        days = int(event.get("days", 30))
        version = event.get("version")  # e.g. "v1" — see _check_marker_state
        return _check_marker_state(days, version=version)
    if mode == "write_marker":
        # Write the SSM migration marker to a specified state.
        # State must be either 'in_progress' or 'completed'.
        state = event.get("state")
        days = int(event.get("days", 30))
        version = event.get("version")  # e.g. "v1" — see _write_marker
        if state not in ("in_progress", "completed"):
            raise ValueError(
                f"write_marker state must be 'in_progress' or 'completed'; got {state!r}"
            )
        return _write_marker(state, days, version=version)
    if mode == "check_lake_state":
        # Cheap emptiness probe used by the state machine on the
        # full-flow branch (marker absent / mismatched version). On a
        # fresh install with no documents yet processed, raw
        # ``metering/`` is empty; running the full migration then does
        # roughly 720+ chunk Athena queries plus the daily backfill,
        # all against zero rows, taking ~20 min of workgroup time and
        # producing no useful state. The state machine reads
        # ``is_empty`` and, when true, routes directly to
        # WriteCompletedMarker so subsequent invocations short-circuit.
        # The check itself is one S3 ListObjectsV2 with MaxKeys=1 —
        # sub-second, ~0 IAM added (rollup Lambda already has
        # s3:ListBucket on the reporting bucket).
        return _check_lake_state()
    if mode == "purge_rollup_prefixes":
        # Delete parquet under the four per-document rollup prefixes so
        # the state machine can repopulate at the widened
        # (document_class) grain. Scoped to date= partitions inside
        # ``[anchor - days, anchor)`` — the same window the migration
        # is about to repopulate — so a stack that has been running
        # longer than the migration window (default 30 d, retention
        # default 365 d) keeps its older rollup data intact. Before
        # this scope, the purge deleted every object under the four
        # prefixes and only the last ``days`` were rebuilt, silently
        # destroying up to 335 days of aggregates on a year-old stack.
        days = int(event.get("days", 30))
        return _purge_rollup_prefixes_task(anchor, days)
    if mode == "plan_migration_chunks":
        # Compute the list of (start, end) time ranges the state
        # machine's Map state iterates. Each chunk covers ``chunk_hours``
        # of the retention window.
        days = int(event.get("days", 30))
        chunk_hours = int(event.get("chunk_hours", 24))
        return _plan_migration_chunks(anchor, days, chunk_hours)
    if mode == "check_hours_failed":
        # Aggregate the Map state's chunk results AND (optionally) the
        # daily-backfill result to decide whether the migration finished
        # cleanly. Returns {all_hours_clean: bool, total_failed: int,
        # total_partial: int, total_succeeded: int}. Daily result is
        # optional so this mode can be invoked in either the hourly-only
        # aggregation shape (legacy tests) or the full aggregation shape
        # from the state machine.
        chunk_results = event.get("chunk_results") or []
        daily_result = event.get("daily_result")
        return _check_hours_failed(chunk_results, daily_result)
    if mode == "backfill_daily_range":
        # Iterate each day in the retention window and invoke the
        # daily-rollup INSERTs (metering_daily + metering_docs_daily).
        # State-machine invokes this AFTER MigrateChunks (hourly) so
        # metering_hourly / metering_docs_hourly are fully populated;
        # the daily rollups read from those.
        days = int(event.get("days", 30))
        return _run_backfill_daily_range(anchor, days)
    raise ValueError(
        f"Unknown rollup mode: {mode!r} "
        "(expected 'hourly' | 'daily' | 'backfill' | 'reconcile' | "
        "'check_marker_state' | 'write_marker' | 'purge_rollup_prefixes' | "
        "'plan_migration_chunks' | 'check_hours_failed' | 'backfill_daily_range')"
    )


def _parse_anchor_time(event: Dict[str, Any]) -> datetime:
    """Return the UTC anchor time for ``previous_hour``/``previous_day``.

    Preference order:
      1. ``event["anchor"]`` — stamped ONCE by the migration state
         machine's dispatcher at execution-start time and threaded
         through every Task's Payload (``anchor.$: $.anchor``) so
         ``InitialPurge``, ``PlanChunks`` and ``BackfillDailyRange``
         all share the same reference time. This closes the
         cross-midnight anchor-drift race where each task otherwise
         resolved its own ``now()`` and the resulting purge and rebuild
         windows disagreed by one day.
      2. ``event["time"]`` — EventBridge sets this to ISO 8601 UTC on
         scheduled events, so async retries of the hourly / reconciler
         / daily crons pin to the ORIGINAL trigger time (not wall-clock).
      3. ``datetime.now(UTC)`` — fallback for manual invokes that don't
         include either field.
    """
    raw = event.get("anchor") or event.get("time")
    if raw:
        try:
            # EventBridge / dispatcher use ISO 8601 with a trailing "Z";
            # normalize to "+00:00" so fromisoformat handles it on
            # Python 3.11+.
            normalized = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (ValueError, TypeError) as e:
            logger.warning(
                "Failed to parse anchor/time=%r (%s); falling back to now()",
                raw,
                e,
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
_IDEMPOTENCY_KEY_PREFIX = f"idp-rollup-{STACK_NAME}"


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


def _discover_document_sections_tables() -> List[str]:
    """Discover ``document_sections_*`` tables in the reporting database.

    Uses the Glue ``GetTables`` API rather than an Athena
    ``information_schema.columns`` query. Two reasons the previous
    Athena-based discovery was worse: (a) it took ~5-10 s cold and
    added an Athena query per invocation, (b) a missing
    ``glue:GetTables`` grant made information_schema silently return
    zero rows, so a permissions regression looked identical to "no
    document_sections_* tables exist" — the failure mode the template
    comment on the grant explicitly documents. Direct Glue GetTables
    is sub-second, needs no additional IAM (the grant is already
    present on this Lambda's role — required by the previous Athena
    path anyway) and raises loudly on a missing permission.

    Only tables that actually have a ``date`` partition column are
    returned — the Glue crawler sometimes creates malformed variants
    (e.g. ``document_sections_date_2026_03_19``, ``..._parquet``) that
    lack it; a subsequent query filtering on ``"date"`` against those
    fails with COLUMN_NOT_FOUND and blanks the whole rollup for the
    hour.

    Cached per-invocation via ``_document_sections_tables_cache`` —
    cleared in ``handler`` so a new class the Glue crawler added since
    the previous fire is picked up on the next hourly.
    """
    global _document_sections_tables_cache
    if _document_sections_tables_cache is not None:
        return _document_sections_tables_cache

    glue_client = boto3.client("glue")
    names: List[str] = []
    try:
        paginator = glue_client.get_paginator("get_tables")
        for page in paginator.paginate(
            DatabaseName=DATABASE, Expression="document_sections_*"
        ):
            for table in page.get("TableList", []) or []:
                name = table.get("Name") or ""
                if not name.startswith("document_sections_"):
                    continue
                if not all(c in _SAFE_TABLE_CHARS for c in name):
                    logger.warning(
                        "Skipping document_sections table with unsafe characters: %r",
                        name,
                    )
                    continue
                # Confirm the ``date`` column exists as either a
                # partition key OR a regular column. The crawler's
                # canonical shape lists ``date`` (and ``hour``) as
                # partition keys, but a defensive check on both catches
                # any variant the crawler emits.
                partition_keys = {
                    (p.get("Name") or "") for p in table.get("PartitionKeys") or []
                }
                storage_columns = {
                    (c.get("Name") or "")
                    for c in (table.get("StorageDescriptor") or {}).get("Columns") or []
                }
                if "date" not in partition_keys and "date" not in storage_columns:
                    continue
                names.append(name)
    except Exception as exc:  # noqa: BLE001
        # Discovery must not fail the rollup — falling back to an
        # empty list means the CTE has no rows, LEFT JOIN produces
        # NULL for dc.document_class, and COALESCE picks
        # metering.document_class (populated for post-widening
        # writers) or 'unknown'. Raising here would take the whole
        # hourly rollup down over one operator-visible Glue hiccup.
        logger.warning(
            "Failed to discover document_sections_* tables via "
            "glue:GetTables (%s); falling back to empty list. Historical "
            "rows without raw metering.document_class will bucket as "
            "'unknown' for this rollup fire.",
            exc,
        )
        _document_sections_tables_cache = []
        return _document_sections_tables_cache

    names.sort()
    logger.info("Discovered %d document_sections_* tables for rollup", len(names))
    _document_sections_tables_cache = names
    return _document_sections_tables_cache


def _build_doc_class_cte(target_date: Optional[str] = None) -> str:
    """Build the ``doc_class`` CTE that derives ``document_class`` per
    document from ``document_sections_*``.

    Mirrors the read-side widget's 0/1/N rule
    (``analytics_cost_service.py:1836-1867``): 0 distinct classes → 'unknown',
    1 → that class, >1 → 'mixed'. Excluded sections are NOT filtered
    here — ``Section.excluded`` is not persisted to ``document_sections_*``
    tables, so this CTE is a *fallback* for historical rows only, whose
    ``metering.document_class`` (which does apply the filter) is NULL.
    Post-widening writers write ``metering.document_class`` directly and
    the ``COALESCE(m.document_class, dc.document_class, 'unknown')`` in
    each rollup query prefers that over this CTE's output.

    ``target_date`` — partition-pruning knob. When provided, each UNION arm gets
    ``WHERE date BETWEEN <target-1d> AND <target+1d>`` so Athena
    partition-prunes ``document_sections_*`` scans down to the ~3 date
    partitions per table that could actually contain rows for the
    metering hour being rolled up. Without this, the CTE scanned every
    partition of every table (20 tables × 30-day retention = 600
    partitions per rollup INSERT), which crushed S3 with
    ``HIVE_S3_THROTTLING`` on any real-volume stack — the CTE fanned
    out ~600 parallel S3 GETs per query and Athena's worker fleet blew
    past the per-prefix 5500 GET/s cap. Root cause of the 2026-09-22
    migration failure. The ±1 day slop covers the timing gap between
    per-pipeline-step metering rows (written throughout processing) and
    the ``document_sections_*`` write (at pipeline end), which can span
    a UTC-day boundary. Callers that don't know the target date (none
    today; here for defensive symmetry) get the old unfiltered CTE.

    Returns a CTE fragment ready to be embedded in a ``WITH ... INSERT``
    query. If no ``document_sections_*`` tables exist (fresh stack, no
    docs processed yet), returns a CTE that yields no rows — the LEFT
    JOIN in each rollup query then produces NULL for ``dc.document_class``
    and the outer COALESCE falls through correctly.
    """
    tables = _discover_document_sections_tables()
    if not tables:
        return (
            "doc_class AS (\n"
            "    SELECT CAST(NULL AS varchar) AS document_id,\n"
            "           CAST(NULL AS varchar) AS document_class\n"
            "    WHERE 1 = 0\n"
            ")"
        )
    date_filter = ""
    if target_date:
        # ISO YYYY-MM-DD strings sort lexicographically = chronologically,
        # and the ``date`` partition column on ``document_sections_*`` is
        # ``string`` (confirmed via Glue schema), so a string BETWEEN is
        # both correct and partition-prunable.
        target = datetime.strptime(target_date, "%Y-%m-%d")
        prev_day = (target - timedelta(days=1)).strftime("%Y-%m-%d")
        next_day = (target + timedelta(days=1)).strftime("%Y-%m-%d")
        date_filter = f" WHERE date BETWEEN '{prev_day}' AND '{next_day}'"
    union_arms = "\n            UNION ALL\n".join(
        f'            SELECT document_id, "document_class.type" AS doc_type FROM "{t}"{date_filter}'
        for t in tables
    )
    return (
        "doc_class AS (\n"
        "    SELECT document_id,\n"
        "           CASE\n"
        "               WHEN COUNT(DISTINCT doc_type) = 0 THEN 'unknown'\n"
        "               WHEN COUNT(DISTINCT doc_type) = 1 THEN MIN(doc_type)\n"
        "               ELSE 'mixed'\n"
        "           END AS document_class\n"
        "      FROM (\n"
        f"{union_arms}\n"
        "      )\n"
        "     WHERE doc_type IS NOT NULL\n"
        "     GROUP BY document_id\n"
        ")"
    )


def _rollup_metering_hourly(target_date: str, target_hour: str) -> Dict[str, Any]:
    """Write ``metering_hourly`` (cost per service/unit) for the given hour
    if not already written.

    Rollup dimensions: ``(hour_ts, config_version, service_api, unit)``.
    Cost-only columns: sum_value, sum_cost. Document-level metrics
    (n_docs, sum_pages) live in a separate table ``metering_docs_hourly``
    because pages and unique-doc counts fan out across (service_api, unit)
    — including them here would produce a 6× overcount for a doc with 6
    service rows.

    ``sum_cost`` is deliberately left NULLABLE. ``estimated_cost`` on the raw
    ``metering`` table is NULL when the service has no pricing entry at all
    (cost unknown, as distinct from 0.0 for a metered-but-not-chargeable unit),
    and ``SUM`` skips NULLs. That does not corrupt this rollup, because the
    grouping key ``(config_version, service_api, unit)`` is exactly the key
    pricing is resolved by: every row in a group is priced or none is, so an
    unpriced group yields ``sum_cost = NULL`` for the whole group rather than a
    silently short total. Coercing it to 0 here would instead destroy the
    distinction between "free" and "unknown" for every downstream reader. The
    obligation therefore falls on queries that aggregate ACROSS groups, which
    must ``COALESCE(sum_cost, 0)`` and surface the NULL count — see the
    ``metering_hourly`` notes in
    ``idp_common/agents/analytics/schema_provider.py``.
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
    # Grain widened with ``document_class`` — see docs/reporting-sql-layer.md §10.
    # ``doc_class`` CTE derives per-doc classification from ``document_sections_*``
    # UNION with the 0/1/N rule (fallback for historical rows). Post-widening
    # writers populate ``metering.document_class`` directly and the COALESCE
    # prefers that; the CTE only fills in for rows where m.document_class IS NULL.
    # Pass target_date so the CTE partition-prunes doc_sections scans
    # to ±1 day — prevents HIVE_S3_THROTTLING on the migration.
    doc_class_cte = _build_doc_class_cte(target_date=target_date)
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_hourly"
        WITH {doc_class_cte}
        SELECT
            date_trunc('hour', m."timestamp") AS hour_ts,
            m.config_version,
            COALESCE(m.document_class, dc.document_class, 'unknown') AS document_class,
            m.service_api,
            m.unit,
            SUM(m.value) AS sum_value,
            SUM(m.estimated_cost) AS sum_cost,
            '{target_date}' AS date,
            '{target_hour}' AS hour
        FROM "{DATABASE}"."metering" m
        LEFT JOIN doc_class dc ON m.document_id = dc.document_id
        WHERE m.date = '{target_date}' AND m.hour = '{target_hour}'
        GROUP BY 1, 2, 3, 4, 5
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
    # Empty-hour sentinel — if the INSERT produced no parquet, mark
    # this partition as "known empty" so a subsequent reconciler pass
    # over the same hour doesn't re-run the same empty INSERT for the
    # next 24 h. See _partition_marked_empty for the TTL rationale.
    if not _partition_produced_rows("metering_hourly", target_date, target_hour):
        _mark_partition_empty("metering_hourly", target_date, target_hour)
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
    # Grain widened with ``document_class`` — see docs/reporting-sql-layer.md §10.
    # Inner subquery: one row per (hour_ts, config_version, document_class,
    # document_id) with MAX(number_of_pages). Round-8 note: the invariant
    # assumes number_of_pages is stamped identically across every metering row
    # for the same doc — true in practice because OCR sets it once, and a
    # same-hour reprocess re-runs OCR on the same PDF (same page count). If a
    # doc were somehow reprocessed within the same hour against a materially
    # different file (different page count), MAX picks the LARGER value — a
    # slight over-count but bounded to that doc, not systematic.
    # MIN/AVG/ANY_VALUE have equally-defensible semantics; MAX chosen so the
    # count is not silently rounded down. Outer aggregate: COUNT(*) of docs,
    # SUM of the MAX-per-doc pages.
    # Pass target_date so the CTE partition-prunes doc_sections scans
    # to ±1 day — prevents HIVE_S3_THROTTLING on the migration.
    doc_class_cte = _build_doc_class_cte(target_date=target_date)
    sql = f"""
        INSERT INTO "{DATABASE}"."metering_docs_hourly"
        WITH {doc_class_cte}
        SELECT
            hour_ts,
            config_version,
            document_class,
            COUNT(*) AS n_docs,
            SUM(max_pages) AS sum_pages,
            '{target_date}' AS date,
            '{target_hour}' AS hour
        FROM (
            SELECT
                date_trunc('hour', m."timestamp") AS hour_ts,
                m.config_version,
                COALESCE(m.document_class, dc.document_class, 'unknown') AS document_class,
                m.document_id,
                MAX(m.number_of_pages) AS max_pages
            FROM "{DATABASE}"."metering" m
            LEFT JOIN doc_class dc ON m.document_id = dc.document_id
            WHERE m.date = '{target_date}' AND m.hour = '{target_hour}'
            GROUP BY 1, 2, 3, 4
        )
        GROUP BY 1, 2, 3
    """  # nosec B608
    # Round-16 review fix: idempotency key — see metering_hourly above.
    query_id = _run_athena(
        sql,
        idempotency_key=_idempotency_key(
            "metering_docs_hourly", target_date, target_hour
        ),
    )
    # Empty-hour sentinel — see _rollup_metering_hourly's comment above.
    if not _partition_produced_rows("metering_docs_hourly", target_date, target_hour):
        _mark_partition_empty("metering_docs_hourly", target_date, target_hour)
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
        # Grain widened with ``document_class`` — reads the (already widened)
        # metering_hourly rollup, so no CTE needed here. See §10.
        sql = f"""
            INSERT INTO "{DATABASE}"."metering_daily"
            SELECT
                date '{target_date}' AS day,
                config_version,
                document_class,
                service_api,
                unit,
                SUM(sum_value) AS sum_value,
                SUM(sum_cost) AS sum_cost,
                '{target_date}' AS date
            FROM "{DATABASE}"."metering_hourly"
            WHERE date = '{target_date}'
            GROUP BY 1, 2, 3, 4, 5
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
            # Empty-day sentinel — see _rollup_metering_hourly for the
            # rationale. Prevents the reconciler / manual re-run from
            # firing an empty daily INSERT every day on idle stacks.
            if not _partition_produced_rows("metering_daily", target_date):
                _mark_partition_empty("metering_daily", target_date)
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
        # Grain widened with ``document_class`` — reads the (already widened)
        # metering_docs_hourly rollup, so no CTE needed here.
        # nosec B608 — target_date is derived from datetime, not user input.
        sql = f"""
            INSERT INTO "{DATABASE}"."metering_docs_daily"
            SELECT
                date '{target_date}' AS day,
                config_version,
                document_class,
                SUM(n_docs) AS n_docs,
                SUM(sum_pages) AS sum_pages,
                '{target_date}' AS date
            FROM "{DATABASE}"."metering_docs_hourly"
            WHERE date = '{target_date}'
            GROUP BY 1, 2, 3
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
            # Empty-day sentinel — see _rollup_metering_hourly for the
            # rationale.
            if not _partition_produced_rows("metering_docs_daily", target_date):
                _mark_partition_empty("metering_docs_daily", target_date)
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


# ---------------------------------------------------------------------------
# Backfill mode — repopulate rollup partitions for a caller-supplied window
# ---------------------------------------------------------------------------


def _parse_backfill_bound(raw: Any, field: str) -> datetime:
    """Parse a ``start``/``end`` field on a backfill event to a UTC-aware
    ``datetime``. Accepts ISO 8601 with a trailing ``Z`` (EventBridge shape)
    or an explicit offset. Naive datetimes are treated as UTC to match the
    rest of the pipeline. Raises ValueError with the field name so a
    malformed payload is diagnosable.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            f"backfill event {field!r} must be a non-empty ISO 8601 UTC string; got {raw!r}"
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"backfill event {field!r} is not parseable as ISO 8601 UTC: {raw!r} ({e})"
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_backfill(
    start_raw: str,
    end_raw: str,
    arms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Iterate ``[start, end)`` at hourly granularity and re-run the
    per-document hourly rollups for each hour.

    Idempotent — the existing partition-write-skip guard in each rollup
    fn means already-written partitions are no-ops. So a caller can:

    - Fill in gaps left by missed schedules (reconciler use case).
    - Repopulate freshly-emptied tables after a schema widening
      (backfill_migrate use case).

    ``arms`` — optional list of arm labels to run this pass
    (out of ``metering_hourly``, ``metering_docs_hourly``,
    ``control_plane_hourly``, ``data_plane_lambda_hourly``). When
    None (default), runs all 4 — matches the reconciler's contract of
    replaying every gap. When set, restricts the pass to those arms —
    this is what the DataMartMigrationStateMachine uses so it only
    replays the two tables that had their schema widened with
    ``document_class`` (metering_hourly + metering_docs_hourly). The
    control-plane / data-plane arms don't have that column, are never
    purged, and are already populated by the routine :05 hourly cron —
    including them in the migration is scope creep that (a) doubles
    per-chunk time, and (b) surfaces empty-CloudWatch-hour "no rows"
    failures from control_plane_hourly's defensive refuse-to-write-
    empty-parquet path, which then get counted as ``hours_partial`` and
    block WriteCompletedMarker. Root cause of the 2026-09-22 execution
    partial-hour blockers observed on a development stack.

    Runs the arms PER-HOUR (not all hours of one arm, then all hours of
    the next) so a transient partition failure doesn't leave one table
    months ahead of another. Sequential rather than parallel — the two
    metering arms within one hour share the ``doc_class`` CTE's
    ``document_sections_*`` scan, and Athena's per-workgroup concurrency
    cap on the ``primary`` workgroup is 5 by default; parallelising
    hours would need explicit throttling that adds complexity for a
    one-shot job.

    Reports per-hour status in a compact accumulator so a partial
    failure can be diagnosed from the return value or CloudWatch log
    tail; does not raise on individual-hour failures because a
    reconciler re-run picks them up.
    """
    start_dt = _parse_backfill_bound(start_raw, "start")
    end_dt = _parse_backfill_bound(end_raw, "end")
    if not (start_dt < end_dt):
        raise ValueError(
            f"backfill start ({start_dt.isoformat()}) must be strictly before "
            f"end ({end_dt.isoformat()})"
        )
    # Round each bound to the top of its hour so the iteration is
    # partition-aligned. Metering rows are partitioned by (date, hour) so
    # a start of 14:30 would otherwise silently include only 14:30-14:59
    # of that hour on the first rollup, matching the same partition as a
    # start of 14:00 — better to be explicit.
    start_hour = start_dt.replace(minute=0, second=0, microsecond=0)
    end_hour = end_dt.replace(minute=0, second=0, microsecond=0)
    # Post-truncation validation: a sub-hour input range (e.g.
    # T00:00Z → T00:30Z) passes the raw ``start_dt < end_dt`` check but
    # truncates to ``start_hour == end_hour``, so the while-loop below
    # iterates zero times, ``hours_attempted`` stays at 0, and
    # ``_check_hours_failed`` reports ``all_hours_clean=True`` — the
    # state machine's WriteCompletedMarker task then writes a marker
    # over rollup tables that received zero data. Reject the range
    # explicitly so the state machine surfaces the caller's mistake
    # instead of silently completing.
    if not (start_hour < end_hour):
        raise ValueError(
            f"backfill hour-aligned range ({start_hour.isoformat()} → "
            f"{end_hour.isoformat()}) covers less than one full hour after "
            "truncation; supply a range spanning at least one whole hour."
        )

    results: Dict[str, Any] = {
        "mode": "backfill",
        "start": start_hour.isoformat(),
        "end": end_hour.isoformat(),
        "hours_attempted": 0,
        "hours_succeeded": 0,
        "hours_partial": 0,
        "hours_failed": 0,
        "failures": [],  # list of {date, hour, table, error}
    }

    all_arms = (
        ("metering_hourly", _rollup_metering_hourly),
        ("metering_docs_hourly", _rollup_metering_docs_hourly),
        ("control_plane_hourly", _rollup_control_plane_hourly),
        ("data_plane_lambda_hourly", _rollup_data_plane_lambda_hourly),
    )
    if arms is None:
        selected_arms = all_arms
    else:
        arms_set = set(arms)
        # An explicit empty list is a programming error, not "run no
        # work quietly". The inner arm-loop would iterate zero times
        # per hour, hour_ok/hour_fail would both stay 0, and the
        # ``if hour_fail == 0`` branch would tick ``hours_succeeded``
        # for every hour — leading to a false ``all_hours_clean=True``
        # and a state=completed marker over rollup tables that
        # received zero writes. Reject explicitly.
        if not arms_set:
            raise ValueError(
                "backfill arms=[] is invalid — pass None to run every arm, "
                "or a non-empty list of arm labels."
            )
        unknown = arms_set - {label for label, _ in all_arms}
        if unknown:
            raise ValueError(
                f"backfill arms={sorted(arms_set)} contains unknown labels "
                f"{sorted(unknown)}; valid: {[label for label, _ in all_arms]}"
            )
        selected_arms = tuple(
            (label, fn) for label, fn in all_arms if label in arms_set
        )
    results["arms"] = [label for label, _ in selected_arms]

    cursor = start_hour
    one_hour = timedelta(hours=1)
    while cursor < end_hour:
        target_date = cursor.strftime("%Y-%m-%d")
        target_hour = cursor.strftime("%H")
        results["hours_attempted"] += 1
        hour_ok = 0
        hour_fail = 0
        for label, fn in selected_arms:
            try:
                fn(target_date, target_hour)
                hour_ok += 1
            except Exception as e:  # noqa: BLE001
                hour_fail += 1
                logger.warning(
                    "backfill %s %s/%s FAILED: %s",
                    label,
                    target_date,
                    target_hour,
                    e,
                )
                results["failures"].append(
                    {
                        "date": target_date,
                        "hour": target_hour,
                        "table": label,
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
        if hour_fail == 0:
            results["hours_succeeded"] += 1
        elif hour_ok == 0:
            results["hours_failed"] += 1
        else:
            results["hours_partial"] += 1
        cursor += one_hour

    logger.info(
        "Backfill %s → %s: attempted=%d succeeded=%d partial=%d failed=%d",
        results["start"],
        results["end"],
        results["hours_attempted"],
        results["hours_succeeded"],
        results["hours_partial"],
        results["hours_failed"],
    )
    return results


# ---------------------------------------------------------------------------
# DataMartMigrationStateMachine task-mode helpers
# ---------------------------------------------------------------------------
# The migration is orchestrated by an AWS::StepFunctions::StateMachine
# (see template.yaml::DataMartMigrationStateMachine). The state machine
# invokes THIS Lambda as a Task for each unit of work below. Each helper
# is small, focused, and returns structured JSON the state machine's
# Choice / Map / Task states consume.
#
# Why a state machine (not Lambda-only): a real-volume customer stack
# has 720 hours × 4 rollup arms at ~5-10 s each = 1-2 h of serialized
# work, well past Lambda's 900 s timeout. Async-retry-on-Lambda gave us
# only ~45 min budget across 3 attempts. The state machine's Map state
# chunks the range into fine-grained slices (production default:
# ``ChunkHours=1`` from the CFN CustomResource, so 720 one-hour chunks
# on a 30 d migration — a tight per-chunk retry blast radius), each
# fitting in one 900 s Lambda invocation, with SFN-native per-chunk
# retry replacing async retries. ``plan_migration_chunks`` accepts
# ``chunk_hours`` up to 168 as its own default fallback of 24 h, but
# that fallback only applies when the caller omits the field — which
# the dispatcher does not do. See the CHANGELOG entry for the retry-safe
# purge that this design supersedes.

_MIGRATION_MARKER_NAME = f"/idp/{STACK_NAME}/data-mart-rollup/migration-complete"


def _check_marker_state(days: int, version: Optional[str] = None) -> Dict[str, Any]:
    """Read the SSM migration marker and return a routing decision the
    state machine's ``Choice`` state consumes.

    Compares the marker against BOTH ``days`` and ``version``:
      * ``state=completed`` matching ``days`` AND ``version`` → short-circuit.
      * ``state=in_progress`` matching ``days`` AND ``version`` → skip purge, resume.
      * Any mismatch on ``days`` or ``version`` → full flow.

    ``version`` is the ``MigrationVersion`` CFN property (e.g. ``"v1"``)
    passed through from the CustomResource. Without it in the marker, a
    future MigrationVersion bump that keeps ``Days`` the same would
    short-circuit here even though the schema demands a fresh
    migration.

    A marker that PRE-DATES version support (has no ``version=`` segment)
    is treated as MISMATCH — routes to full flow. This is deliberate:
    silently accepting a legacy marker would reintroduce the original
    footgun (a future MigrationVersion bump for a new schema change
    short-circuiting against a pre-versioning ``state=completed``). The
    one-time cost is a single re-migration on any stack that has an
    unversioned marker — for customer prod, this class is limited to
    stacks that ran an intermediate build of the migration on develop
    (none in the wild at ship time). New markers written from this
    build onward always include ``version=``.

    Returns:
        {
          "state": "completed" | "in_progress" | "absent" | "unrecognised",
          "days": <int|None>,
          "version": <str|None>,
          "value": <raw marker string|None>,
          "should_short_circuit": <bool>,
          "should_skip_purge": <bool>,
        }
    """
    ssm_client = boto3.client("ssm")
    try:
        response = ssm_client.get_parameter(Name=_MIGRATION_MARKER_NAME)
        value = response.get("Parameter", {}).get("Value", "")
    except ssm_client.exceptions.ParameterNotFound:
        return {
            "state": "absent",
            "days": None,
            "version": None,
            "value": None,
            "should_short_circuit": False,
            "should_skip_purge": False,
        }
    # Non-``ParameterNotFound`` errors (SSM throttling, IAM denial,
    # transient service errors) MUST propagate. Previously this handler
    # coerced any read failure to ``state="absent"`` and returned it —
    # ``absent`` routes the state machine's ``RouteOnMarkerState`` Choice
    # to the DEFAULT branch (``InitialPurge``), which deletes every S3
    # object under the four rollup prefixes. On a stack whose migration
    # was already completed, a transient SSM 5xx or a bad-window
    # ``ssm:GetParameter`` throttle would silently destroy the customer's
    # populated rollup data on a routine CustomResource re-fire. The
    # state machine has ``Retry`` configured for Lambda.* errors so a
    # bubbled exception is retried up to 6 times with 30 s / backoff — a
    # persistent read failure fails the whole execution visibly instead
    # of masquerading as "no marker → clean slate".

    # Parse the marker as ``;``-delimited ``key=value`` segments rather
    # than doing substring matches against the whole payload. Substring
    # matches are delimiter-unsafe:
    #   * ``f"days={days}" in value`` treats ``days=3`` as a match
    #     against a marker written with ``days=30`` or ``300`` — a
    #     shorter-window migration would then short-circuit against a
    #     longer-window completed marker even though the operator asked
    #     for a fresh smaller window.
    #   * ``"state=completed" in value`` would match a future state
    #     name that has ``completed`` as a prefix (e.g. a hypothetical
    #     ``state=completed_pending`` intermediate state). Safe today,
    #     unsafe as soon as any future state name shares a prefix.
    # Structured segment parsing eliminates both.
    marker_segments: Dict[str, str] = {}
    for segment in value.split(";"):
        if "=" in segment:
            key, _, val = segment.partition("=")
            marker_segments[key.strip()] = val.strip()
    marker_days = marker_segments.get("days")
    marker_state = marker_segments.get("state")
    marker_version: Optional[str] = marker_segments.get("version")

    days_match = marker_days == str(days)
    if version is None:
        # Caller didn't supply a version — no comparison to do. Preserves
        # the pre-versioning invocation shape (any direct SFN start_execution
        # invocation that doesn't set input.version still routes correctly on
        # days alone).
        version_match = True
    else:
        # Strict: a marker with no version= segment (pre-versioning) or a
        # different version string is a mismatch → full flow. See docstring
        # for why silent-accept was rejected.
        #
        # ``.strip()`` on the CALLER side too — the parsed marker side
        # has already been stripped (see the ``val.strip()`` in the
        # segment loop above). Without symmetric normalisation, a
        # ``MigrationVersion="v1 "`` (trailing whitespace, easy to
        # introduce via a CFN parameter default or a template edit)
        # would never match a marker written from the same string
        # (which stored it as ``"v1 "`` but parses it back as ``"v1"``)
        # and force a full destructive re-migration on every deploy.
        version_match = marker_version == (version or "").strip()

    if days_match and version_match and marker_state == "completed":
        return {
            "state": "completed",
            "days": days,
            "version": marker_version,
            "value": value,
            "should_short_circuit": True,
            "should_skip_purge": False,
        }
    if days_match and version_match and marker_state == "in_progress":
        return {
            "state": "in_progress",
            "days": days,
            "version": marker_version,
            "value": value,
            "should_short_circuit": False,
            "should_skip_purge": True,
        }
    return {
        "state": "unrecognised",
        "days": None,
        "version": marker_version,
        "value": value,
        "should_short_circuit": False,
        "should_skip_purge": False,
    }


def _write_marker(
    state: str,
    days: int,
    version: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the SSM migration marker to the given state.

    Two-phase design (see class-level comment):
      * ``state='in_progress'`` — written after purge, before backfill.
        Retries resuming after Lambda timeout read this and skip purge.
      * ``state='completed'`` — written by the state machine's terminal
        WriteCompletedMarker task, only if ``check_hours_failed`` reported
        ``all_hours_clean=True``.

    ``version`` is the ``MigrationVersion`` CFN property (e.g. ``"v1"``).
    When present, it's included in the payload so a future
    ``_check_marker_state`` invocation with a different version routes
    to full flow instead of short-circuiting.

    Returns the value written so the state machine can log / return it.
    """
    ssm_client = boto3.client("ssm")
    now_iso = datetime.now(timezone.utc).isoformat()
    # ``is not None`` intentionally — an empty string means "the caller
    # is using versioning but the version identifier itself is blank",
    # which _check_marker_state's ``if version is None`` branch treats
    # differently from a missing segment. Using ``if version`` (falsy)
    # here caused every deploy with ``MigrationVersion=""`` to write a
    # marker without a ``version=`` segment; the next _check_marker_state
    # would parse ``marker_version = None`` and compare it against the
    # caller's ``version = ""``, evaluate them unequal, and route to
    # full InitialPurge every time — destructive on every deploy.
    version_segment = f"version={version};" if version is not None else ""
    if state == "in_progress":
        value = f"days={days};{version_segment}state=in_progress;started_at={now_iso}"
        description = (
            "Data-mart rollup migration marker. state=in_progress means the "
            "state machine has completed the S3 purge but not confirmed all "
            "chunks; a restart of the state machine must SKIP the purge to "
            "preserve prior chunk writes."
        )
    elif state == "completed":
        value = f"days={days};{version_segment}state=completed;completed_at={now_iso}"
        description = (
            "Data-mart rollup migration marker — state=completed. Delete "
            "this parameter (or bump ForceFresh on the CustomResource) to "
            "force a fresh migration."
        )
    else:
        raise ValueError(
            f"_write_marker: state must be 'in_progress' or 'completed'; got {state!r}"
        )
    ssm_client.put_parameter(
        Name=_MIGRATION_MARKER_NAME,
        Value=value,
        Type="String",
        Overwrite=True,
        Description=description,
    )
    logger.info("Wrote migration marker: %s = %s", _MIGRATION_MARKER_NAME, value)
    return {"marker": value, "state": state, "days": days}


def _check_lake_state() -> Dict[str, Any]:
    """Return ``{"is_empty": bool}`` describing whether the raw
    ``metering/`` prefix has any objects at all.

    Used by the state machine to short-circuit a full migration on a
    fresh install (no metering data yet) — the full-flow branch
    otherwise runs ~720+ empty Athena queries in ~20 min against a
    zero-row lake, and every one produces no useful state.
    ``ListObjectsV2 MaxKeys=1`` is the cheapest possible check; it
    returns as soon as the first key is found (empty stacks respond
    with an empty ``Contents``).
    """
    if not REPORTING_BUCKET:
        # No bucket configured — treat as non-empty so the caller
        # takes the full-flow path and fails visibly at the first
        # Athena query rather than silently short-circuiting.
        return {"is_empty": False, "reason": "REPORTING_BUCKET not configured"}
    try:
        resp = s3_client.list_objects_v2(
            Bucket=REPORTING_BUCKET, Prefix="metering/", MaxKeys=1
        )
        contents = resp.get("Contents") or []
        return {"is_empty": not contents}
    except Exception as exc:  # noqa: BLE001
        # Fail closed: on any list error, proceed with the full flow
        # rather than accidentally short-circuit past data the operator
        # was expecting to see rolled up.
        logger.warning(
            "check_lake_state: ListObjectsV2 failed (%s); reporting as "
            "not-empty so the migration proceeds",
            exc,
        )
        return {"is_empty": False, "reason": f"list_objects_v2 failed: {exc}"}


def _purge_rollup_prefixes_task(anchor: datetime, days: int) -> Dict[str, Any]:
    """Delete parquet under the four rollup prefixes, scoped to the
    date= partitions inside ``[anchor - days, anchor)``.

    Task-mode wrapper around ``_purge_rollup_window`` so the state
    machine can invoke a single Lambda action for the whole purge
    (rather than four separate SDK integrations). Returns per-prefix
    delete counts for the state machine's logs.

    Window semantics match ``_plan_migration_chunks``: ``anchor`` is
    truncated to top-of-hour for the upper bound, and the lower bound
    is that minus ``days`` truncated further to top-of-day so every
    day in the migration range gets 24 hours of coverage before the
    daily backfill aggregates.

    Why not delete every object under the four prefixes: the four
    rollup tables' data is derived from raw ``metering`` +
    ``document_sections_*`` and can be regenerated in principle, BUT
    the migration only repopulates ``days`` at a time (bounded to 90
    by ``_plan_migration_chunks``). A stack running on the default
    365-day retention has up to 365 days of rollup parquet; an
    unbounded purge followed by a 30-day repopulate silently destroys
    up to 335 days of aggregates that nothing in the state machine
    then rebuilds. Bounding the purge to the migrated window keeps the
    older aggregates intact — they remain queryable from the widened
    tables even though they were written at the old grain (the older
    partitions simply predate the ``document_class`` column and behave
    as NULL when the column is projected).
    """
    if days < 1 or days > 90:
        raise ValueError(f"purge_rollup_prefixes: days={days} out of range (1..90)")
    end_dt = anchor.replace(minute=0, second=0, microsecond=0)
    start_dt = (end_dt - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prefixes = [
        "metering_hourly/",
        "metering_daily/",
        "metering_docs_hourly/",
        "metering_docs_daily/",
    ]
    summary: Dict[str, int] = {}
    for prefix in prefixes:
        deleted = _purge_rollup_window(REPORTING_BUCKET, prefix, start_dt, end_dt)
        summary[prefix] = deleted
        logger.info(
            "purge_rollup_prefixes: deleted %d object(s) under s3://%s/%s "
            "[date=%s .. %s)",
            deleted,
            REPORTING_BUCKET,
            prefix,
            start_dt.strftime("%Y-%m-%d"),
            end_dt.strftime("%Y-%m-%d"),
        )
    return {
        "purged": summary,
        "total": sum(summary.values()),
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
    }


def _purge_rollup_window(
    bucket: str, prefix: str, start_dt: datetime, end_dt: datetime
) -> int:
    """Delete every object under ``s3://<bucket>/<prefix>date=YYYY-MM-DD/``
    for each date in ``[start_dt, end_dt)``, where the daily bound is
    the ``date=`` value on the parquet-partition key.

    Rollup tables are date-partitioned under prefixes like
    ``metering_hourly/date=2026-09-22/hour=13/data.parquet``. Listing
    with ``Delimiter="/"`` under the table prefix returns the set of
    ``date=`` partition sub-prefixes; each partition matching the
    window is deleted with the existing ``_purge_s3_prefix`` helper
    (which itself surfaces DeleteObjects Errors — see its comment).

    Partitions that don't parse as ``date=YYYY-MM-DD/`` are ignored —
    the crawler occasionally creates auxiliary metadata under the
    table prefix (Glue symlinks, etc.) that we must not delete.
    """
    start_date = start_dt.strftime("%Y-%m-%d")
    # ``end_dt`` is the top-of-hour of the anchor time. The purge date
    # bound must be INCLUSIVE of the anchor date — deleting only
    # ``[start_date, end_date_inclusive)`` (exclusive upper bound) as
    # a prior version did left ``date=<anchor-day>/`` partitions in
    # place at the pre-widening grain, and the migration chunks that
    # follow write hours 00..anchor-hour of the anchor date. The
    # ``_partition_already_written`` probe sees pre-existing old-grain
    # rows on those hours and short-circuits the chunk, so up to 23
    # hours of the anchor day silently stay NULL for ``document_class``
    # inside a window the SSM marker reports as ``completed``. Deleting
    # the whole anchor-date prefix is safe: the migration rewrites
    # hours 00..anchor-hour at the widened grain, and the :35 reconciler
    # rebuilds any anchor-date hours after anchor-hour on its next fire.
    end_date_inclusive = end_dt.strftime("%Y-%m-%d")
    total = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    date_pattern = re.compile(rf"^{re.escape(prefix)}date=(\d{{4}}-\d{{2}}-\d{{2}})/$")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []) or []:
            date_prefix = cp.get("Prefix", "")
            m = date_pattern.match(date_prefix)
            if not m:
                continue
            partition_date = m.group(1)
            # Closed [start_date, end_date_inclusive] — includes the
            # anchor date. Older partitions outside this window stay
            # preserved (the customer-history-preserving guard the
            # earlier bounded-purge fix introduced).
            if start_date <= partition_date <= end_date_inclusive:
                total += _purge_s3_prefix(bucket, date_prefix)
    return total


def _plan_migration_chunks(
    anchor: datetime, days: int, chunk_hours: int
) -> Dict[str, Any]:
    """Return the list of ``(start, end)`` ISO 8601 time ranges the state
    machine's Map state iterates. Each chunk covers ``chunk_hours`` of
    the retention window, sized so one Lambda invocation of
    ``mode: backfill`` fits comfortably in 900 s (SFN Lambda-task budget).

    Anchor is the CFN CustomResource fire time (via EventBridge event
    ``time`` field, or now() fallback). ``end`` is anchor truncated to
    top-of-hour (an exclusive upper bound — the current in-flight hour
    is not part of the migration). ``start`` is ``end - days`` truncated
    to top-of-DAY.

    Why top-of-day on the low end and top-of-hour on the high end:
    ``_run_backfill_daily_range`` (which runs AFTER MigrateChunks
    finishes) aggregates ``metering_daily`` from ``metering_hourly`` on
    a whole-day basis. If MigrateChunks' start were mid-day — as it
    would be for any off-midnight CustomResource fire — the earliest
    day's ``metering_hourly`` rows would cover only a tail slice (e.g.
    hours 14-23 for a 14:35 UTC fire), and the earliest ``metering_daily``
    row would then be short 14 hours of data. Extending ``start`` down
    to top-of-day gives every day in the range full 24-hour coverage
    in ``metering_hourly`` before the daily aggregation runs. Overshoot
    is at most 24 hours of extra work; on chunk_hours=1 that's 24
    additional chunks that HeadObject-skip fast if already rolled up.
    """
    if days < 1 or days > 90:
        raise ValueError(f"plan_migration_chunks: days={days} out of range (1..90)")
    if chunk_hours < 1 or chunk_hours > 168:  # 1 hour to 1 week
        raise ValueError(
            f"plan_migration_chunks: chunk_hours={chunk_hours} out of range (1..168)"
        )

    # Cap the total chunk count. Step Functions has a hard 256 KB
    # limit on the accumulated state document (``$.chunk_results`` +
    # ``$.plan.chunks`` + everything else), and even with the narrow
    # ResultSelector that projects only counters + range from each
    # BackfillChunk (~155 B per chunk_result) and the ~66 B per
    # plan.chunks entry, Days=60 (1440 chunks) exceeds ~300 KB and
    # Days=90 (2160 chunks) reaches ~470 KB — the Map's Catch then
    # fires on States.DataLimitExceeded and operators lose the
    # aggregated failing_chunks output. Cap chunk count at 720 (the
    # shipped Days=30 default at chunk_hours=1) by auto-scaling
    # chunk_hours upward when the caller's request would blow that
    # ceiling. For Days=60 the effective chunk_hours becomes 2 (720
    # chunks); for Days=90 it becomes 3 (720 chunks). Retry blast
    # radius grows linearly with chunk_hours; still small enough that
    # a single-chunk retry fits comfortably in the 900 s Lambda
    # budget on real-volume workloads.
    _MAX_CHUNKS = 720
    total_hours = days * 24
    requested_chunk_count = math.ceil(total_hours / chunk_hours)
    if requested_chunk_count > _MAX_CHUNKS:
        original_chunk_hours = chunk_hours
        chunk_hours = math.ceil(total_hours / _MAX_CHUNKS)
        logger.warning(
            "plan_migration_chunks: requested chunk_hours=%d would produce "
            "%d chunks over %d days, exceeding the %d-chunk state-quota "
            "cap. Auto-scaling chunk_hours to %d (%d chunks). Retry blast "
            "radius grows linearly; state stays under Step Functions' "
            "256 KB limit.",
            original_chunk_hours,
            requested_chunk_count,
            days,
            _MAX_CHUNKS,
            chunk_hours,
            math.ceil(total_hours / chunk_hours),
        )

    end = anchor.replace(minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    chunks: List[Dict[str, str]] = []
    cursor = start
    delta = timedelta(hours=chunk_hours)
    while cursor < end:
        chunk_end = min(cursor + delta, end)
        chunks.append({"start": cursor.isoformat(), "end": chunk_end.isoformat()})
        cursor = chunk_end
    logger.info(
        "plan_migration_chunks: %d chunk(s) of %d h across %d d, %s → %s",
        len(chunks),
        chunk_hours,
        days,
        start.isoformat(),
        end.isoformat(),
    )
    return {"chunks": chunks, "count": len(chunks)}


def _check_hours_failed(
    chunk_results: List[Dict[str, Any]],
    daily_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aggregate the state machine's Map-state chunk results AND
    (optionally) the daily-backfill result to decide whether the
    migration is done cleanly.

    Each chunk result is the return value of one ``mode: backfill``
    invocation, containing ``hours_failed``, ``hours_succeeded``,
    ``hours_partial``, ``hours_attempted``. The daily result has the
    same shape (per-day accounting via ``_run_backfill_daily_range``).

    Returns ``{"all_hours_clean": bool, "total_failed": int, ...}`` for
    the state machine's Choice state to route on. Emits a warning log
    listing failed chunks so an operator investigating a marker stuck
    at state=in_progress has a starting point.
    """
    total_attempted = 0
    total_succeeded = 0
    total_partial = 0
    total_failed = 0
    failing_chunks: List[Dict[str, Any]] = []
    # Include the daily result in the aggregation if provided. Treated
    # as one additional "chunk" for accounting; per-day granularity is
    # inside the payload already.
    # Track chunk-hour and daily-unit contributions separately so an
    # operator can see whether a failure came from an hour chunk or
    # from a day of the daily backfill. Historically these were summed
    # into ``total_*`` fields whose name implied "hours" but actually
    # counted (chunk_hours + backfill_days) — an operator reading
    # ``total_attempted=750`` on a 30 d migration would see the answer
    # 720 + 30 without knowing why. Fields ``total_*`` remain for
    # backward compatibility with the state machine's Choice state; new
    # per-unit-type ``chunk_hours_*`` and ``daily_units_*`` fields make
    # the composition explicit.
    chunk_hours_attempted = chunk_hours_succeeded = 0
    chunk_hours_partial = chunk_hours_failed = 0
    daily_units_attempted = daily_units_succeeded = 0
    daily_units_partial = daily_units_failed = 0

    def _extract(chunk: Dict[str, Any]) -> Dict[str, Any]:
        payload = chunk.get("backfill") if isinstance(chunk, dict) else None
        if payload is None:
            payload = chunk if isinstance(chunk, dict) else {}
        return {
            "attempted": int(payload.get("hours_attempted", 0) or 0),
            "succeeded": int(payload.get("hours_succeeded", 0) or 0),
            "partial": int(payload.get("hours_partial", 0) or 0),
            "failed": int(payload.get("hours_failed", 0) or 0),
            "start": payload.get("start"),
            "end": payload.get("end"),
            "failures": payload.get("failures", []),
        }

    for chunk in chunk_results:
        c = _extract(chunk)
        chunk_hours_attempted += c["attempted"]
        chunk_hours_succeeded += c["succeeded"]
        chunk_hours_partial += c["partial"]
        chunk_hours_failed += c["failed"]
        total_attempted += c["attempted"]
        total_succeeded += c["succeeded"]
        total_partial += c["partial"]
        total_failed += c["failed"]
        if c["failed"] > 0 or c["partial"] > 0:
            failing_chunks.append(
                {
                    "start": c["start"],
                    "end": c["end"],
                    "hours_failed": c["failed"],
                    "hours_partial": c["partial"],
                    "failures": c["failures"],
                }
            )

    # Semantic note: a "partial" hour means one or more (but not all)
    # rollup arms failed. In routine hourly ops that's tolerated
    # because the reconciler fills the gap, but in the migration
    # context we MUST NOT declare success while any arm is missing
    # data — the migration is the reconciler for these tables and
    # a "partial" chunk maps to a customer table with rows silently
    # missing (2026-09-22 incident: 38 chunks reported partial and
    # zero rows landed in metering_hourly). Treat partial as needing
    # replay so the state machine surfaces the failure instead of
    # writing a WriteCompletedMarker over an empty table — see the
    # ``all_hours_clean`` guard in the return dict below.
    if daily_result is not None:
        d = _extract(daily_result)
        daily_units_attempted += d["attempted"]
        daily_units_succeeded += d["succeeded"]
        daily_units_partial += d["partial"]
        daily_units_failed += d["failed"]
        total_attempted += d["attempted"]
        total_succeeded += d["succeeded"]
        total_partial += d["partial"]
        total_failed += d["failed"]
        if d["failed"] > 0 or d["partial"] > 0:
            failing_chunks.append(
                {
                    "start": d["start"],
                    "end": d["end"],
                    "hours_failed": d["failed"],
                    "hours_partial": d["partial"],
                    "failures": d["failures"],
                }
            )
    if failing_chunks:
        # Compact WARNING summary (safely under CloudWatch's 256 KB
        # per-event cap even on a 720-chunk migration where every chunk
        # partial-fails). Full detail follows at INFO, one event per
        # chunk, so a postmortem always has the per-chunk failures
        # regardless of how many chunks failed.
        logger.warning(
            "check_hours_failed: %d chunk(s) had failing/partial hours "
            "(first %d ranges: %s); full detail below at INFO",
            len(failing_chunks),
            min(len(failing_chunks), 5),
            [(fc.get("start"), fc.get("end")) for fc in failing_chunks[:5]],
        )
        for i, fc in enumerate(failing_chunks):
            logger.info(
                "failing_chunk %d/%d: %s → %s hours_failed=%s hours_partial=%s failures=%s",
                i + 1,
                len(failing_chunks),
                fc.get("start"),
                fc.get("end"),
                fc.get("hours_failed"),
                fc.get("hours_partial"),
                fc.get("failures"),
            )
    return {
        # Floor on total_attempted > 0: a zero-work aggregation (empty
        # chunk_results AND daily_result=None, or PlanChunks having
        # produced 0 chunks) would otherwise satisfy the two ``==0``
        # conditions and route the state machine to WriteCompletedMarker
        # with nothing actually populated. That is never the intended
        # signal — if the state machine reached the aggregator with no
        # work observed, treat it as unclean so the operator sees the
        # anomaly instead of a false-success marker.
        "all_hours_clean": (
            total_attempted > 0 and total_failed == 0 and total_partial == 0
        ),
        # ``total_*`` are unit-agnostic sums (chunk hours + daily
        # units) preserved for the state machine's Choice state and
        # for backward compatibility with any operator consuming this
        # shape. Prefer the per-unit-type fields below when reasoning
        # about what actually failed.
        "total_attempted": total_attempted,
        "total_succeeded": total_succeeded,
        "total_partial": total_partial,
        "total_failed": total_failed,
        "chunk_hours_attempted": chunk_hours_attempted,
        "chunk_hours_succeeded": chunk_hours_succeeded,
        "chunk_hours_partial": chunk_hours_partial,
        "chunk_hours_failed": chunk_hours_failed,
        "daily_units_attempted": daily_units_attempted,
        "daily_units_succeeded": daily_units_succeeded,
        "daily_units_partial": daily_units_partial,
        "daily_units_failed": daily_units_failed,
        "failing_chunks": failing_chunks,
    }


def _run_backfill_daily_range(anchor: datetime, days: int) -> Dict[str, Any]:
    """Iterate each day in ``[anchor - days, anchor)`` and invoke
    ``_run_daily`` so the daily rollup tables (``metering_daily`` and
    ``metering_docs_daily``) get populated for the whole retention
    window.

    Called by the state machine's ``BackfillDailyRange`` Task AFTER
    ``MigrateChunks`` has populated the hourly rollups. ``_run_daily``
    reads from ``metering_hourly`` and ``metering_docs_hourly`` to
    aggregate into ``metering_daily`` and ``metering_docs_daily`` — so
    the hourly tables MUST be complete when this fires (guaranteed by
    the state machine's Map → Task ordering).

    Idempotent: ``_run_daily``'s ``_partition_already_written`` guard
    on each daily table means re-invocation skips already-completed
    days.

    Returns a per-day success/failure summary the state machine's
    ``CheckMigrationSuccess`` aggregator consumes alongside the chunk
    results — same shape (``hours_attempted/succeeded/partial/failed``,
    treating each day as one "hour" for aggregation purposes). Each
    counter increments once per DAY iterated, not per hour; the field
    names are ``hours_*`` because that is what ``_check_hours_failed``'s
    ``_extract`` shape expects (the aggregator sums this alongside the
    chunk_hours_* contributions into ``daily_units_*`` and ``total_*``
    fields, where the ``daily_units_*`` breakdown is the truthful
    per-day view — see the state machine's CheckMigrationSuccess
    ResultSelector). Renaming the return dict keys here would break
    ``_extract`` without a coordinated aggregator change.

    Budget behaviour: this function iterates days sequentially inside
    one Lambda invocation. Per-day work is typically 5-10 s (small
    Athena INSERTs), so 30 days finishes in 150-300 s — well within
    the 900 s Lambda budget. At the upper bound of the accepted range
    (days=90) with worst-case Athena warmups (~20 s per day = 1800 s
    total) the invocation will hit Lambda's 900 s timeout mid-run;
    the state machine's BackfillDailyRange Retry (MaxAttempts=5,
    ErrorEquals includes States.Timeout) then re-fires. Because each
    day's rollup is idempotent (HeadObject-skip in ``_run_daily``),
    every retry HeadObject-skips the days already completed and picks
    up from where the previous attempt left off. 5 × 900 s = 4500 s of
    forward-progress budget covers the 90-day worst case; typical
    ``Days: 30`` deployments complete on the first attempt.
    """
    if days < 1 or days > 90:
        raise ValueError(f"backfill_daily_range: days={days} out of range (1..90)")

    end = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)

    results: Dict[str, Any] = {
        "mode": "backfill_daily_range",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours_attempted": 0,
        "hours_succeeded": 0,
        "hours_partial": 0,
        "hours_failed": 0,
        "failures": [],
    }

    cursor = start
    one_day = timedelta(days=1)
    while cursor < end:
        # ``_run_daily`` computes ``_previous_day(anchor)`` — so to
        # process day X, we pass an anchor of X+1 (start of next day
        # UTC). That's what ``cursor + one_day`` gives us.
        daily_anchor = cursor + one_day
        results["hours_attempted"] += 1
        try:
            _run_daily(daily_anchor)
            # ``_run_daily`` raises on ANY sub-INSERT failure (see the
            # ``if errors: raise`` guard at the end of _run_daily), so
            # a successful return means BOTH sub-writes succeeded (or
            # were already written = skipped-idempotent). No per-table
            # inspection needed — any per-table failure surfaces via
            # the outer ``except`` clause below.
            results["hours_succeeded"] += 1
        except Exception as e:  # noqa: BLE001
            # Match ``_run_backfill``'s per-hour try/except so one bad
            # day doesn't abort the range. State machine's
            # ``check_hours_failed`` aggregator will surface any failures.
            results["hours_failed"] += 1
            logger.warning(
                "backfill_daily_range day=%s FAILED: %s",
                cursor.strftime("%Y-%m-%d"),
                e,
            )
            results["failures"].append(
                {
                    "date": cursor.strftime("%Y-%m-%d"),
                    "error": f"{type(e).__name__}: {e}",
                }
            )
        cursor += one_day

    logger.info(
        "Daily backfill %s → %s: attempted=%d succeeded=%d partial=%d failed=%d",
        results["start"],
        results["end"],
        results["hours_attempted"],
        results["hours_succeeded"],
        results["hours_partial"],
        results["hours_failed"],
    )
    return results


def _run_reconcile(anchor: datetime) -> Dict[str, Any]:
    """Re-run the four per-document hourly rollups across the trailing
    24 h to fill in gaps left by missed schedules.

    Idempotent — already-written partitions are no-ops via the
    ``HeadObject``-skip guard in each rollup fn. Cheap when nothing is
    missing (24 partition-existence checks per table × 4 tables per hour
    = ~90 HeadObject probes total across the whole day, sub-second on
    S3). Non-trivial only when a gap is present, in which case it does
    exactly the work the scheduled rollup would have done.

    Range end is the top-of-hour of ``anchor`` itself (an exclusive
    upper bound). At :35 that resolves to :00 of the current hour, so
    the last hour actually processed is the interval ``[prev-hour-top,
    current-hour-top)`` — i.e. the previous fully-sealed hour. The
    reconciler never chases the CURRENT hour — which is by definition
    still in flight when this runs at :35 of the hour.
    """
    # ``end`` is the exclusive upper bound of the half-open range passed
    # to ``_run_backfill``: the last hour written is the one immediately
    # before ``end``. Truncating ``anchor`` to top-of-hour gives the
    # boundary at the start of the current in-flight hour, so the last
    # hour processed is the previous fully-sealed one.
    end = anchor.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=24)
    logger.info(
        "Reconcile: scanning [%s → %s) for missed hourly rollups",
        start.isoformat(),
        end.isoformat(),
    )
    return _run_backfill(start.isoformat(), end.isoformat())


def _purge_s3_prefix(bucket: str, prefix: str) -> int:
    """Delete every object under ``s3://<bucket>/<prefix>``. Returns the
    total number of objects deleted.

    Used by ``_purge_rollup_prefixes_task`` (the state machine's
    ``InitialPurge`` Task) to empty the rollup S3 prefixes before
    repopulation. Paginates + batches — S3 ``delete_objects`` caps at
    1000 keys per call.
    """
    if not bucket or not prefix:
        raise ValueError(
            f"_purge_s3_prefix requires bucket and prefix; got {bucket!r}, {prefix!r}"
        )
    total = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        contents = page.get("Contents", [])
        if not contents:
            continue
        # Delete in batches of 1000 (S3 hard cap on DeleteObjects).
        for start in range(0, len(contents), 1000):
            batch = contents[start : start + 1000]
            resp = s3_client.delete_objects(
                Bucket=bucket,
                Delete={
                    "Objects": [{"Key": obj["Key"]} for obj in batch],
                    "Quiet": True,
                },
            )
            # ``Quiet=True`` suppresses the successful-Deleted list, NOT
            # ``Errors``. A silent partial failure here (S3 5xx, IAM,
            # transient throttling on a subset of keys) would leave
            # pre-widening parquet in place and cause the widening rewrite
            # to be skipped by ``_partition_already_written``. Surface
            # the failure so SFN's InitialPurge Retry (MaxAttempts=2)
            # fires — the second call HeadObject-skips the already-deleted
            # keys and retries only the survivors.
            errors = resp.get("Errors") or []
            if errors:
                sample = errors[:5]
                raise RuntimeError(
                    f"S3 DeleteObjects reported {len(errors)} error(s) under "
                    f"s3://{bucket}/{prefix} — first {len(sample)}: {sample!r}"
                )
            total += len(batch)
    return total


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


def _empty_partition_sentinel_key(
    table: str, date: str, hour: Optional[str] = None
) -> str:
    """S3 key used to record "this partition has been rolled up and
    was empty" so a subsequent reconciler pass over the same hour
    doesn't re-run an already-known-empty INSERT.

    Placed under the partition prefix itself so the marker is
    partition-scoped: ``metering_hourly/date=YYYY-MM-DD/hour=HH/_empty``
    or, for daily rollups, ``metering_daily/date=YYYY-MM-DD/_empty``.
    Lifecycle rule on the reporting bucket (``DeleteAfterNDays`` at the
    customer-configured ``DataRetentionInDays``) applies to the sentinel
    just like the parquet, so retention semantics are preserved.
    """
    if hour is not None:
        return f"{table}/date={date}/hour={hour}/_empty"
    return f"{table}/date={date}/_empty"


# Empty-sentinel TTL — how long a partition stays "known empty" before
# the reconciler is allowed to re-check it. Set to 24 h so a late-arriving
# batch of raw metering rows for a previously-empty hour gets caught on
# the next day's :35 fire; without a TTL the sentinel would be permanent
# and any late writes would silently miss the rollup. Balances two
# failure modes: (a) an unnecessarily-frequent re-check on a truly-empty
# hour wastes an Athena query per day per hour, (b) an infrequent
# re-check on a hour that later got data misses those rows on the
# rollup. 24 h matches the reconciler's own trailing-24-hour scan
# window: an hour that receives late data more than 24 h after original
# emptiness was recorded was already out of reconciler range under the
# previous design.
_EMPTY_SENTINEL_TTL_SECONDS = 24 * 60 * 60


def _partition_marked_empty(table: str, date: str, hour: Optional[str] = None) -> bool:
    """Returns True if a previous rollup wrote an ``_empty`` sentinel
    for this partition AND the sentinel is still fresh (< 24 h old).
    Reconciler use case: an hour that produced 0 rows on its scheduled
    write would otherwise be re-attempted on every :35 fire — the
    SELECT-based ``_partition_already_written`` probe returns False on
    any partition that never had data written, so nothing tells the
    reconciler "we already tried and there was nothing here". The
    sentinel closes that loop, and the 24 h TTL prevents a stale
    sentinel from hiding late-arriving data.
    """
    if not REPORTING_BUCKET:
        return False
    key = _empty_partition_sentinel_key(table, date, hour)
    try:
        resp = s3_client.head_object(Bucket=REPORTING_BUCKET, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        # Any other error — throttle, permission blip — treat as
        # not-marked so the caller proceeds normally. A false-negative
        # here just means we run an extra empty INSERT once; a
        # false-positive (marking as done when it wasn't) could hide
        # real data, so err toward re-checking.
        logger.warning(
            "HeadObject for empty-sentinel s3://%s/%s failed with %s; "
            "treating as not-marked",
            REPORTING_BUCKET,
            key,
            code,
        )
        return False
    last_modified = resp.get("LastModified")
    if not isinstance(last_modified, datetime):
        # Should not happen — HeadObject always returns a datetime for
        # LastModified — but treat non-datetime (missing key, mocked
        # test double, etc.) as expired so the reconciler re-checks
        # rather than blocking on an unusable timestamp.
        return False
    age = (datetime.now(timezone.utc) - last_modified).total_seconds()
    if age > _EMPTY_SENTINEL_TTL_SECONDS:
        logger.info(
            "Empty-sentinel for %s/date=%s%s is %.0f s old (> %.0f s TTL); "
            "treating as expired so the rollup re-checks for late writes",
            table,
            date,
            f"/hour={hour}" if hour is not None else "",
            age,
            _EMPTY_SENTINEL_TTL_SECONDS,
        )
        return False
    return True


def _mark_partition_empty(table: str, date: str, hour: Optional[str] = None) -> None:
    """Write the ``_empty`` sentinel after an INSERT produced 0 rows.
    Best-effort — a failure to write the marker means the next
    reconciler run repeats the empty INSERT (annoying, not incorrect).
    """
    if not REPORTING_BUCKET:
        return
    key = _empty_partition_sentinel_key(table, date, hour)
    try:
        s3_client.put_object(Bucket=REPORTING_BUCKET, Key=key, Body=b"")
        logger.info(
            "Marked empty partition %s/date=%s%s — future reconciler passes "
            "will short-circuit instead of re-running the 0-row INSERT",
            table,
            date,
            f"/hour={hour}" if hour is not None else "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to write empty-sentinel for %s date=%s hour=%s: %s",
            table,
            date,
            hour,
            exc,
        )


def _partition_produced_rows(table: str, date: str, hour: Optional[str] = None) -> bool:
    """Post-INSERT check: did the query land any parquet at the target
    partition prefix? Uses ListObjectsV2 rather than a fresh Athena
    SELECT — sub-millisecond vs a full query submit. Excludes the
    ``_empty`` sentinel itself so a re-run against an already-marked
    empty partition doesn't mistake the sentinel for data.
    """
    if not REPORTING_BUCKET:
        return True  # can't check; assume data landed to be safe
    prefix = (
        f"{table}/date={date}/hour={hour}/"
        if hour is not None
        else f"{table}/date={date}/"
    )
    try:
        resp = s3_client.list_objects_v2(
            Bucket=REPORTING_BUCKET, Prefix=prefix, MaxKeys=10
        )
        for obj in resp.get("Contents") or []:
            key = obj.get("Key") or ""
            if key.endswith("/_empty"):
                continue
            # Any non-sentinel key under the partition prefix counts as
            # data — Athena writes parquet with UUID names, no path to
            # match exactly.
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        # On list failure, assume rows landed so we don't false-mark
        # a real partition as empty. A false negative here (assuming
        # rows when there were none) just means the next reconciler
        # re-runs the empty INSERT once.
        logger.warning(
            "ListObjectsV2 on %s failed (%s); assuming rows landed",
            prefix,
            exc,
        )
        return True


def _partition_already_written(
    table: str, date: str, hour: Optional[str] = None
) -> bool:
    """Cheap idempotency check — does the target partition already have
    at least one row, OR has a previous rollup pass marked it as empty?

    Checks the empty-sentinel first (S3 HeadObject, sub-millisecond)
    before falling through to the Athena LIMIT-1 SELECT. The sentinel
    branch is what stops the reconciler from re-running an
    already-known-empty INSERT on every :35 fire — the SELECT alone
    returns False for any partition that never had data, and the arm
    would then re-attempt indefinitely.

    Narrow fail-open policy: ONLY treats "table does not exist" as
    not-yet-written (the first-invocation-after-deploy case). Any other
    error — throttle, permission blip, malformed response — RE-RAISES.
    Fail-open on transient errors lets an INSERT run against an
    already-populated partition and permanently double-counts cost;
    re-raising lets the caller's DLQ + async retry recover.
    """
    if _partition_marked_empty(table, date, hour):
        return True
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
    # Guard: Athena rejects StartQueryExecution with
    # ``InvalidRequestException: Idempotent parameters do not match`` when
    # a ClientRequestToken from a prior (<24h) submission is reused with a
    # different QueryString. That happens on migration re-runs where the
    # SELECT text changes between deploys (e.g. 2.0 → 2.1 added the
    # document_class CTE). The cached-failure restart branch below only
    # fires AFTER a successful submit, so it can't help here — we have to
    # catch the client-side reject BEFORE the query is submitted and
    # resubmit with a salted token. Same salt semantics as round-20's
    # fresh-salt (UTC-second, wide enough for concurrent retries of THIS
    # invocation, distinct across sequential retries).
    try:
        response = athena_client.start_query_execution(**kwargs)
    except ClientError as e:
        # Match on BOTH the structured error code AND the free-form
        # message text. Athena raises the specific idempotency-mismatch
        # case as either ``IdempotentParameterMismatchException`` or
        # (in some botocore versions) the broader
        # ``InvalidRequestException`` code — but ``InvalidRequestException``
        # on its own covers many unrelated Athena failure modes we must
        # not treat as an idempotency mismatch (bad SQL, missing
        # workgroup, malformed OutputLocation, etc.). The additional
        # ``"Idempotent" in error_msg`` guard narrows the broad code
        # class down to the specific case the fresh-salt branch is
        # designed for. The AND is deliberate; a prior comment saying
        # "either signal" was wrong — narrowing on either alone lets
        # unrelated ``InvalidRequestException``s spuriously salt and
        # retry.
        error = e.response.get("Error", {}) or {}
        error_code = error.get("Code", "")
        error_msg = error.get("Message", "") or str(e)
        is_idempotency_mismatch = (
            error_code
            in {"IdempotentParameterMismatchException", "InvalidRequestException"}
            and "Idempotent" in error_msg
        )
        if idempotency_key and is_idempotency_mismatch:
            fresh_salt = str(int(time.time()))
            salted_key = f"{idempotency_key[:116]}-r{fresh_salt}"[:128]
            logger.warning(
                f"Athena rejected reuse of ClientRequestToken "
                f"({idempotency_key[:40]}...) with different QueryString "
                f"(likely a code deploy that changed the SELECT text). "
                f"Resubmitting with fresh salt."
            )
            kwargs["ClientRequestToken"] = salted_key
            response = athena_client.start_query_execution(**kwargs)
        else:
            raise
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
        # ``ClientError`` is module-scoped (import at top of file); the
        # BotoCoreError parent class is not, so it needs the local import.
        from botocore.exceptions import (  # noqa: PLC0415
            BotoCoreError as _AthenaBotoCoreError,
        )

        for probe_attempt in range(3):
            try:
                initial = athena_client.get_query_execution(QueryExecutionId=query_id)
                initial_state = initial["QueryExecution"]["Status"]["State"]
                break
            except (ClientError, _AthenaBotoCoreError) as e:
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
            # This is the ONE window ``ClientRequestToken`` can't close.
            # Historically ``DataMartRollupFunction`` set
            # ``ReservedConcurrentExecutions: 1`` in template.yaml to make
            # this window unreachable; that value was later raised to 12 so the migration
            # state machine's Map (MaxConcurrency=8) can run in parallel.
            # The residual race is closed by the ``_partition_already_written``
            # HeadObject-skip firing BEFORE the Athena INSERT in each rollup
            # arm — see the ReservedConcurrentExecutions block comment in
            # template.yaml for the full argument. Do not drop the reserved
            # concurrency below the state machine's MaxConcurrency=8; also
            # do not remove the HeadObject-skip, which is now what makes
            # the concurrent-restart case idempotent.
            #
            # The salt can't be derived from the event anchor time instead:
            # the anchor is identical across async retries by design (that's
            # what makes retries target the right partition), so an
            # anchor-derived salt would be stable across retries and
            # reinstate exactly the cached-failure lock this branch exists to
            # break. The two requirements — differ across sequential retries,
            # match across concurrent duplicates — have no single-token
            # solution, hence the HeadObject-skip as the outer guard.
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

    Bounded by ``_MAX_RESULT_PAGES`` (~10M rows) — defense in depth. A
    real Athena query with >10 k pages would be a misconfiguration
    (rollups are aggregations with tiny outputs); ALSO catches the case
    where a test's mocked ``athena_client.get_query_results`` returns a
    ``MagicMock`` whose ``.get("NextToken")`` is truthy on every iteration,
    which would otherwise busy-loop and burn CPU + memory (past incident
    on the test host). Raises ``RuntimeError`` on overshoot so the
    misuse is surfaced instead of hanging.
    """
    _MAX_RESULT_PAGES = 10_000
    query_id = _run_athena(sql, emit_self_cost=emit_self_cost)
    all_rows: List[List[str]] = []
    next_token: Optional[str] = None
    first_page = True
    for _page in range(_MAX_RESULT_PAGES):
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
            return all_rows
    raise RuntimeError(
        f"Athena get_query_results paginator exceeded {_MAX_RESULT_PAGES} pages for "
        f"query {query_id}. This is either a legitimately huge result set (in which "
        f"case rethink the query — rollups should aggregate to a small output) or a "
        f"mocked athena_client whose NextToken is truthy on every iteration (in "
        f"which case the caller should stub _run_athena_query_with_results itself)."
    )


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
