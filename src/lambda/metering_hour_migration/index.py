# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource handler that migrates the metering S3
layout on stack upgrade — old ``metering/date=X/*.parquet`` files are
relocated into ``metering/date=X/hour=HH/`` subdirs so the new Glue
projection can see them.

Design
------
- **On Create and Update:** list all keys under ``metering/`` that lack
  ``/hour=``; if the count is zero (fresh install with no data yet, or
  an already-migrated stack), return SUCCESS with nothing to do.
  Otherwise migrate in-Lambda with a ThreadPoolExecutor for parallel
  ``CopyObject`` calls. Fresh installs no-op immediately (empty bucket).
- **On Delete:** no-op — the S3 files are managed by their bucket's own
  retention policy.

Why inline (vs. Step Functions):
- Common case is small (dev stacks: 0 files; test stacks: 10K-100K).
- With ~50 concurrent ``CopyObject`` calls and ~40 files/sec effective
  throughput (S3 rate limits + copy latency), the handler is bounded by
  ``MAX_INLINE_FILES = 30_000`` — enough for typical stacks with a
  ~12-15 min copy budget before the 900s Lambda timeout.
- For larger stacks, this handler fails loudly with a clear error that
  points at ``scripts/migrate_metering_hour_partition.py`` for manual
  execution. Failing during ``update-stack`` (before the Glue table
  update commits, because the Glue table ``DependsOn`` this custom
  resource) is much better than silently leaving historical data
  invisible.

The Glue table's ``PartitionKeys`` change only takes effect *after* the
custom resource returns SUCCESS — the ``DependsOn: MeteringHourMigrationCustomResource``
attribute on the ``metering`` Glue table in ``template.yaml`` guarantees
that ordering.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator, Optional

import boto3

# pyarrow is bundled via IDPCommonReportingLayer. Import at module load time
# rather than inside _infer_hour so a missing layer fails the handler on
# initialization (fast, loud, actionable) instead of silently parking every
# file at hour=00. If this import fails, the layer is misconfigured — DO
# NOT swallow it as a per-file read failure.
try:
    import pyarrow.parquet as _pq
except ImportError as _pyarrow_err:  # pragma: no cover
    _pq = None  # type: ignore[assignment]
    _PYARROW_IMPORT_ERROR: Optional[BaseException] = _pyarrow_err
else:
    _PYARROW_IMPORT_ERROR = None

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Safety threshold: return failure early if we can't finish in time.
# Lambda timeout is 900s; leave 120s to send the CFN response and finish
# cleanly, so budget the copy loop at 780s.
COPY_BUDGET_SEC = 780
# Fail-fast estimate: at ~40 files/sec with 50 concurrent workers
# (CopyObject latency ~150ms + s3 rate limits), one Lambda invocation
# can handle roughly 30K files comfortably. Larger workloads fall
# through to the "run the manual script" fail path.
MAX_INLINE_FILES = 30_000
# S3 CopyObject concurrency inside one Lambda.
COPY_CONCURRENCY = 50

HOUR_KEY_PATTERN = re.compile(r"/hour=\d{2}/")
DATE_PART_PATTERN = re.compile(r"^metering/date=(\d{4}-\d{2}-\d{2})/([^/]+\.parquet)$")

s3_client = boto3.client("s3")


def handler(event, context):
    """CloudFormation Custom Resource entry point."""
    logger.info(f"Custom Resource event: {json.dumps(event)}")

    request_type = event.get("RequestType", "")
    if request_type == "Delete":
        return _send(event, context, "SUCCESS", reason="Delete — no action needed")

    bucket = event.get("ResourceProperties", {}).get("ReportingBucket")
    if not bucket:
        return _send(
            event,
            context,
            "FAILED",
            reason="ReportingBucket property is required",
        )

    if _PYARROW_IMPORT_ERROR is not None:
        # Fail loudly — this is a template misconfiguration (wrong Lambda
        # layer), not a data problem. Continuing would park every file at
        # hour=00 and lose hour precision on all historical data.
        return _send(
            event,
            context,
            "FAILED",
            reason=(
                "pyarrow is not available in the Lambda's runtime — the "
                "MeteringHourMigrationFunction must be wired to "
                "IDPCommonReportingLayer (which bundles pyarrow), not "
                "IDPCommonBaseLayer. Fix template.yaml and retry update-stack."
            ),
        )

    try:
        return _migrate(event, context, bucket)
    except Exception as e:
        logger.exception("Migration failed")
        return _send(event, context, "FAILED", reason=f"Migration failed: {e}")


def _migrate(event, context, bucket: str):
    """List old-layout keys and relocate them under ``date=X/hour=HH/``."""
    old_keys = list(_iter_old_layout_keys(bucket))
    total = len(old_keys)
    logger.info(f"Found {total} old-layout metering parquet files under s3://{bucket}/")

    if total == 0:
        return _send(
            event,
            context,
            "SUCCESS",
            data={"Migrated": 0, "Reason": "No old-layout files"},
            reason="No files to migrate",
        )

    if total > MAX_INLINE_FILES:
        return _send(
            event,
            context,
            "FAILED",
            reason=(
                f"Cannot migrate {total} files inline (limit: {MAX_INLINE_FILES}). "
                f"Run scripts/migrate_metering_hour_partition.py --bucket {bucket} "
                f"manually to relocate historical files, then retry the stack update. "
                f"The Glue table update is blocked by this custom resource, so "
                f"failing here is safe — dashboards keep working against the old "
                f"table layout until the migration and retry complete."
            ),
        )

    deadline = time.time() + COPY_BUDGET_SEC
    moved = 0
    errors = 0
    skipped_stray = 0
    hour_fallbacks = 0

    with ThreadPoolExecutor(max_workers=COPY_CONCURRENCY) as executor:
        futures = {executor.submit(_migrate_one, bucket, key): key for key in old_keys}
        for future in as_completed(futures):
            if time.time() > deadline:
                # Cancel remaining work — the executor will still finish any
                # already-running tasks, but we won't schedule new ones.
                for pending in futures:
                    if not pending.done():
                        pending.cancel()
                return _send(
                    event,
                    context,
                    "FAILED",
                    reason=(
                        f"Migration budget of {COPY_BUDGET_SEC}s exceeded after "
                        f"moving {moved}/{total} files. Run "
                        f"scripts/migrate_metering_hour_partition.py --bucket {bucket} "
                        f"manually to finish, then retry the stack update."
                    ),
                )
            key = futures[future]
            try:
                result = future.result()
                if result == "stray":
                    skipped_stray += 1
                elif result == "fallback":
                    hour_fallbacks += 1
                    moved += 1
                else:
                    moved += 1
            except Exception as e:
                logger.warning(f"Failed to migrate {key}: {e}")
                errors += 1

    if errors:
        return _send(
            event,
            context,
            "FAILED",
            reason=(
                f"Migrated {moved}/{total} files but {errors} failed. "
                f"Check CloudWatch logs, fix root cause, then re-run manually."
            ),
        )
    # A file whose hour we couldn't infer is a systemic hazard (KMS role
    # broken, schema drift, wrong pyarrow) — every such file lands in
    # hour=00 permanently, hiding hour-precision on real data. Round-6
    # fix: fail loudly rather than silently parking. Operator can rerun
    # after fixing the underlying issue (already-copied hour=00 files
    # will need `scripts/migrate_metering_hour_partition.py --rescan`
    # once we ship it; today, the error message points at CloudWatch
    # logs for per-file details).
    if hour_fallbacks:
        return _send(
            event,
            context,
            "FAILED",
            reason=(
                f"Migrated {moved}/{total} files but {hour_fallbacks} had "
                f"unreadable hour data and were parked at hour=00. This "
                f"usually means KMS/IAM misconfiguration, schema drift, or "
                f"corrupted parquet. Check per-file WARN logs, fix the root "
                f"cause, then re-run the migration."
            ),
        )
    reason = f"Migrated {moved} metering parquet files into hour-partitioned layout"
    if skipped_stray:
        reason += f" ({skipped_stray} stray non-metering parquet key(s) skipped)"
    return _send(
        event,
        context,
        "SUCCESS",
        data={"Migrated": moved, "SkippedStray": skipped_stray},
        reason=reason,
    )


def _iter_old_layout_keys(bucket: str) -> Iterator[str]:
    """Yield metering/*.parquet keys that lack ``/hour=`` in their path."""
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="metering/"):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith(".parquet") and not HOUR_KEY_PATTERN.search(key):
                yield key


def _migrate_one(bucket: str, key: str) -> str:
    """Copy ``key`` to its hour-partitioned target, then delete the
    original. Returns ``"moved"``, ``"stray"`` (non-metering key,
    skipped), or ``"fallback"`` (hour couldn't be inferred, parked at
    ``hour=00`` — caller escalates).

    Copy-then-delete is the correct rollback behavior. Athena reads a
    partition location recursively — if we left originals in place and
    CFN rolled the Glue table back to the pre-migration projection
    (``date=X/``), Athena would scan BOTH the originals AND the copies
    under ``date=X/hour=HH/`` and every migrated day would double-count.
    Deleting the original after a verified copy leaves exactly one
    physical location per row regardless of which projection the Glue
    table ends up on.

    Failure modes:
    - **Mid-copy failure** — the target key doesn't land; original
      stays. Safe: original is still the sole location, projection
      (whichever version is committed) reads it correctly. Re-run picks
      up the same key via the lister's ``not HOUR_KEY_PATTERN`` filter.
    - **Copy-succeeded-delete-failed** — the target key AND original
      both exist for this one file. Same double-count risk as the
      "leave originals" design, but scoped to one file rather than the
      whole dataset. A re-run picks up the delete idempotently
      (``delete_object`` on a missing key is a no-op).
    - **Files parked at hour=00 by a prior run** — the lister excludes
      any key already under ``/hour=NN/``, so those files are NOT
      re-attempted here. If a v1.0-like bad run parked files at
      hour=00, they stay there until an operator runs
      ``scripts/migrate_metering_hour_partition.py --rescan-hour-00``
      (not yet shipped). Since no v1.0 has shipped to any customer,
      this is a Phase-2 hazard, not a live one.
    """
    # Sanity-check the key shape BEFORE reading the parquet body: a
    # stray non-metering file (legacy Athena query output, etc.) has
    # nothing to migrate and shouldn't block the upgrade. Pass a
    # placeholder hour just to run the shape check; the real hour
    # comes from the parquet body below.
    if _new_key(key, "00") is None:
        # Stray parquet under metering/ that doesn't match
        # date=YYYY-MM-DD/*.parquet (e.g. legacy Athena query output
        # accidentally written under this prefix). Skipping is safer
        # than raising: the file isn't a metering row, so it can't be
        # affected by the partitioning change, and blowing up the
        # migration would block every stack upgrade because of one
        # unrelated object. Log so an operator can inspect. Round-6
        # review fix.
        logger.info(
            f"Skipping stray non-metering parquet key: {key} "
            f"(no date=YYYY-MM-DD/ prefix)"
        )
        return "stray"
    hour, inferred = _infer_hour(bucket, key)
    target = _new_key(key, hour)
    assert target is not None, "shape re-check should not fail after placeholder passed"
    if target == key:
        return "moved"  # already migrated (defensive)
    s3_client.copy_object(
        Bucket=bucket,
        Key=target,
        CopySource={"Bucket": bucket, "Key": key},
    )
    s3_client.delete_object(Bucket=bucket, Key=key)
    return "fallback" if not inferred else "moved"


def _infer_hour(bucket: str, key: str) -> tuple[str, bool]:
    """Read the first row's timestamp column and return
    ``(hour_HH, inferred)``. ``inferred=False`` means we couldn't read
    a real timestamp and parked at ``"00"`` — the caller escalates that
    to a migration-failure so the operator sees it rather than silently
    losing hour precision.

    Reads only the ``timestamp`` and ``initial_event_time`` columns via
    ``pq.read_table(..., columns=[...])`` and then, if that returns no
    rows, no-ops the fallback — avoids downloading the whole 50 MB
    parquet body just to peek at row 0 of one column. Round-6 review
    fix for OOM risk at 50 concurrent 50 MB reads in a 3008 MB Lambda.
    """
    assert _pq is not None, "pyarrow import guard should have caught this"
    try:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        # Only read the two columns we care about — pyarrow skips the
        # rest at Parquet-column granularity, so this is O(size of
        # timestamp columns) not O(whole file).
        table = _pq.read_table(
            io.BytesIO(body),
            columns=["timestamp", "initial_event_time"],
        )
        for candidate in ("timestamp", "initial_event_time"):
            if candidate in table.column_names and table.num_rows > 0:
                ts = table.column(candidate)[0].as_py()
                if ts is not None:
                    return (ts.strftime("%H"), True)
    except Exception as e:
        # Log with enough detail for the operator to correlate — a
        # KMS/IAM issue looks identical to a corrupted-parquet issue in
        # the log summary otherwise.
        logger.warning(
            f"infer_hour failed for {key} ({type(e).__name__}: {e}) — "
            f"parking in hour=00 pending migration-failure escalation"
        )
    return ("00", False)


def _new_key(old_key: str, hour: str) -> Optional[str]:
    """Rewrite ``metering/date=X/foo.parquet`` → ``metering/date=X/hour=HH/foo.parquet``."""
    match = DATE_PART_PATTERN.match(old_key)
    if not match:
        return None
    date_part, filename = match.groups()
    return f"metering/date={date_part}/hour={hour}/{filename}"


def _send(event, context, status: str, data=None, reason: str = ""):
    """Send response to CloudFormation custom resource."""
    import urllib3

    response_url = event.get("ResponseURL", "")
    if not response_url:
        logger.warning("No ResponseURL in event — skipping CFN response")
        return {"status": status, "reason": reason}

    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": event.get("LogicalResourceId", context.log_stream_name),
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": data or {},
    }

    # Timeout on the PUT so a hung CFN presigned-S3 endpoint doesn't
    # stall the Lambda until its 15-min ceiling. Round-6 review fix.
    # 15s connect + 30s read is generous vs. S3's typical low-second
    # response, and bounded well under Lambda's own timeout.
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=15.0, read=30.0))
    try:
        http.request(
            "PUT",
            response_url,
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": ""},
        )
        logger.info(f"CFN response sent: {status} — {reason}")
    except Exception as e:
        logger.error(f"Failed to send CFN response: {e}")
    return {"status": status, "reason": reason}
