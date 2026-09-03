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

# Round-9 review fix uses S3's ``Delimiter="/hour="`` to filter
# hour-partitioned keys server-side, so no client-side hour pattern is
# needed in this module. The standalone script
# ``scripts/migrate_metering_hour_partition.py`` still uses one.
DATE_PART_PATTERN = re.compile(r"^metering/date=(\d{4}-\d{2}-\d{2})/([^/]+\.parquet)$")

# Cap boto3 retries and per-call timeouts so a hung S3 API doesn't
# stretch a single worker past the outer deadline. Round-8 review fix.
_boto_config = boto3.session.Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=15,
)
s3_client = boto3.client("s3", config=_boto_config)


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

    # Manual shutdown so we can return without waiting on running futures
    # after the copy-budget deadline. ``with ThreadPoolExecutor`` would
    # block on shutdown(wait=True) at context exit — a hung boto3 retry
    # could extend the Lambda run toward its 900s timeout, past the
    # deadline. Round-8 review fix.
    # Bounded drain window on deadline exceeded — round-11 review fix.
    # Each _migrate_one is atomic per file (copy_object → delete_object
    # sequentially), so an in-flight future that already did the copy
    # but not the delete would leave both source and target present. On
    # CFN rollback (Glue projection reverts to date=X/*), Athena would
    # recursively read BOTH — a transient double-count until manual
    # cleanup. To keep the window tight, we drain in-flight futures for
    # up to DRAIN_BUDGET_SEC before returning FAILED.
    DRAIN_BUDGET_SEC = 20  # noqa: N806 (local-scope const)
    executor = ThreadPoolExecutor(max_workers=COPY_CONCURRENCY)
    deadline_exceeded = False  # round-15 review fix — see finally block below
    try:
        futures = {executor.submit(_migrate_one, bucket, key): key for key in old_keys}
        for future in as_completed(futures):
            if time.time() > deadline:
                deadline_exceeded = True
                # Wait briefly for any in-flight futures to complete their
                # delete-after-copy so we don't leave source-and-target
                # dupes.
                drain_deadline = time.time() + DRAIN_BUDGET_SEC
                in_flight = [f for f in futures if not f.done()]
                logger.warning(
                    f"Copy budget exceeded. Draining {len(in_flight)} "
                    f"in-flight future(s) for up to {DRAIN_BUDGET_SEC}s "
                    f"to avoid mid-copy dupes."
                )
                for f in in_flight:
                    remaining = drain_deadline - time.time()
                    if remaining <= 0:
                        break
                    try:
                        f.result(timeout=remaining)
                    except Exception:
                        # Best-effort drain; count is reported below.
                        pass
                still_in_flight = sum(1 for f in futures if not f.done())
                return _send(
                    event,
                    context,
                    "FAILED",
                    reason=(
                        f"Migration budget of {COPY_BUDGET_SEC}s exceeded after "
                        f"moving {moved}/{total} files "
                        f"({still_in_flight} in-flight during drain — a small "
                        f"number of source-and-target dupes may remain if the "
                        f"stack rolls back). Run "
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
                    # File NOT copied — left at its original old-layout
                    # location. Operator retry will re-list it via the
                    # lister's ``Delimiter="/hour="`` server-side filter.
                    hour_fallbacks += 1
                else:
                    moved += 1
            except Exception as e:
                logger.warning(f"Failed to migrate {key}: {e}")
                errors += 1
    finally:
        # Round-15 review fix: on the DEADLINE path we still want
        # cancel_futures + wait=False so we can return FAILED promptly;
        # the drain window above already gave in-flight futures 20s to
        # finish their copy+delete pair. But on every OTHER exit path
        # (natural completion, errors > 0, hour_fallbacks > 0), the
        # ``as_completed`` loop has fully consumed the futures — so
        # nothing is in flight and we can safely ``wait=True`` to be
        # crisp about lifecycle. This also removes the last remaining
        # window where a mid-``copy_object``/``delete_object`` future
        # could be truncated on the graceful path if a caller ever
        # ``return``ed early from inside the for-loop.
        if deadline_exceeded:
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)

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
    # A file whose hour we couldn't infer stays at its original old-layout
    # location — we deliberately do NOT copy it to hour=00 because a
    # mis-placed file is unrescuable (the lister's /hour= exclusion won't
    # yield it on a re-run). Round-6+round-7 review fixes.
    if hour_fallbacks:
        return _send(
            event,
            context,
            "FAILED",
            reason=(
                f"Migrated {moved}/{total} files; {hour_fallbacks} files "
                f"could not have their hour inferred and were LEFT IN PLACE "
                f"(not copied). This usually means KMS/IAM misconfiguration, "
                f"schema drift, or corrupted parquet. Check per-file WARN "
                f"logs, fix the root cause, then re-run the migration — the "
                f"leftover files will be re-listed and retried."
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
    """Yield metering/*.parquet keys that lack ``/hour=`` in their path.

    Uses S3's ``Delimiter=/hour=`` so hour-partitioned keys collapse into
    ``CommonPrefixes`` server-side and don't appear in ``Contents`` at
    all. On a mostly-migrated bucket this cuts the list cost from
    O(all-keys) to O(un-migrated-keys + partition-prefixes). Round-9
    review fix.

    S3 behavior: with ``Prefix=metering/`` and ``Delimiter=/hour=``, a
    key like ``metering/date=X/hour=00/foo.parquet`` matches the
    delimiter after the prefix and is grouped into a common prefix; a
    key like ``metering/date=X/foo.parquet`` has no ``/hour=`` after
    the prefix and lands in Contents — exactly the old-layout set we
    want to yield.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=bucket, Prefix="metering/", Delimiter="/hour="
    ):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            # Server-side filter already excluded hour-partitioned keys.
            # Client-side .parquet suffix check remains as a defensive
            # guard against any non-parquet debris under metering/
            # (e.g. Athena query result manifests if someone points a
            # workgroup output at this bucket).
            if key.endswith(".parquet"):
                yield key


def _migrate_one(bucket: str, key: str) -> str:
    """Copy ``key`` to its hour-partitioned target, then delete the
    original. Returns ``"moved"``, ``"stray"`` (non-metering key,
    skipped), or ``"fallback"`` (hour couldn't be inferred, so the file is
    LEFT IN PLACE, uncopied — the caller escalates to a failed migration).

    ``"fallback"`` deliberately does NOT park the file at ``hour=00``: the
    lister excludes anything already under ``/hour=NN/``, so a mis-placed file
    could never be re-attempted. Leaving it put keeps a retry possible once the
    operator fixes the underlying cause. (See the "Files parked at hour=00 by a
    prior run" note below for the historical behavior this replaced.)

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
      up the same key via the lister's ``Delimiter="/hour="`` filter.
    - **Copy-succeeded-delete-failed** — the target key AND original
      both exist for this one file. Same double-count risk as the
      "leave originals" design, but scoped to one file rather than the
      whole dataset. A re-run picks up the delete idempotently
      (``delete_object`` on a missing key is a no-op).
    - **Files parked at hour=00 by a prior run** — the lister excludes
      any key already under ``/hour=NN/``, so those files are NOT
      re-attempted here. If a hypothetical v1.0-like bad run parked
      files at hour=00, they stay there permanently — no rescue path
      exists today. Since no such shipped v1.0 exists, this is a
      documented Phase-2 hardening gap. A future rescue script would
      need to explicitly list the ``hour=00`` subprefix and re-infer
      hour from each file's own timestamp column.
    """
    # Sanity-check the key shape BEFORE reading the parquet body — a
    # stray non-metering file (legacy Athena query output, etc.) has
    # nothing to migrate and shouldn't cost an S3 GetObject to
    # discover. Cheap regex match on the key string; no S3 call.
    # Round-6 + round-12 review fixes.
    if not DATE_PART_PATTERN.match(key):
        logger.info(
            f"Skipping stray non-metering parquet key: {key} "
            f"(no date=YYYY-MM-DD/ prefix)"
        )
        return "stray"
    hour, inferred = _infer_hour(bucket, key)
    if not inferred:
        # DO NOT copy — leave the file at its original location. A copy
        # to hour=00 would be a physical, irrecoverable data-loss event
        # (the lister excludes /hour=NN/ keys on retry so the mis-placed
        # file could not be rescued). The outer loop escalates to
        # FAILED; operator fixes the underlying issue and re-runs.
        # Round-6 review fix.
        return "fallback"
    target = _new_key(key, hour)
    # ``target`` is guaranteed non-None here (same DATE_PART_PATTERN
    # as the shape check above), so we don't guard for None.
    if target == key:
        return "moved"  # already migrated (defensive)
    # Defensive collision check — round-8 review fix. If a prior run
    # copied source→target and its delete failed, then the same source
    # still lives at the old-layout location; a re-run would copy over
    # the existing target. Metering filenames include a UUID so a
    # collision on DISTINCT sources is astronomically unlikely, but the
    # HeadObject is cheap and turns a silent overwrite into an
    # observable skip.
    try:
        target_head = s3_client.head_object(Bucket=bucket, Key=target)
        # Round-13 review fix: verify target integrity BEFORE deleting
        # source. A prior partial run could have written a truncated
        # or corrupt target; blindly trusting HEAD-succeeds and
        # deleting the source would make that corruption permanent.
        # Compare ContentLength against the source; if they mismatch,
        # DO NOT delete — copy over the existing target (S3 CopyObject
        # replaces) and only then delete the source.
        try:
            source_head = s3_client.head_object(Bucket=bucket, Key=key)
        except s3_client.exceptions.ClientError as e:
            src_code = e.response.get("Error", {}).get("Code")
            if src_code in ("404", "NoSuchKey", "NotFound"):
                # Round-15 review fix: target exists AND source is gone —
                # this file is fully migrated. A concurrent migrator (or
                # a prior successful run whose future result was lost)
                # already did the copy+delete, so we have nothing to do
                # and MUST NOT fall through to copy_object below (which
                # would fail with NoSuchKey on the source and flip this
                # file to FAILED, wedging the custom resource).
                logger.info(
                    f"Target exists and source is already deleted "
                    f"({key} → {target}): another migrator got here "
                    f"first. Nothing to do."
                )
                return "moved"
            raise
        target_size = target_head.get("ContentLength")
        source_size = source_head.get("ContentLength")
        if (
            target_size is not None
            and source_size is not None
            and target_size == source_size
        ):
            logger.info(
                f"Target already exists with matching size ({target_size} bytes): "
                f"{target}. Cleaning up leftover source."
            )
            s3_client.delete_object(Bucket=bucket, Key=key)
            return "moved"
        logger.warning(
            f"Target exists but size mismatch (source={source_size}, "
            f"target={target_size}): {target}. Overwriting target with "
            f"a fresh copy to prevent data loss on truncated prior run."
        )
        # Fall through to the copy + delete path below.
    except s3_client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise
    # Round-16 review fix: copy_object below can NoSuchKey if a concurrent
    # migrator (or the operator's manual retry script) deleted the source
    # between our HEADs and the copy. Guard the call so we don't flip a
    # fully-migrated file to FAILED. If target already exists (from a
    # prior partial run) and source is now gone, the migration is done.
    try:
        s3_client.copy_object(
            Bucket=bucket,
            Key=target,
            CopySource={"Bucket": bucket, "Key": key},
        )
    except s3_client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            # Confirm target still exists — if so, this file was migrated
            # by another actor and we can safely return "moved".
            try:
                s3_client.head_object(Bucket=bucket, Key=target)
                logger.info(
                    f"copy_object saw source gone but target exists: "
                    f"{target}. Another migrator finished this file first."
                )
                return "moved"
            except s3_client.exceptions.ClientError:
                pass
        raise
    s3_client.delete_object(Bucket=bucket, Key=key)
    return "moved"


def _open_parquet_range_read(bucket: str, key: str):
    """Return a ``pyarrow.parquet.ParquetFile`` backed by S3 range reads.

    Prefers ``pyarrow.fs.S3FileSystem`` (reads footer, then only the
    needed row-groups over HTTP range requests). Falls back to loading
    the whole body via boto3 into a BytesIO if pyarrow.fs isn't
    importable — bundled pyarrow wheels include it, so the fallback is
    a paranoia belt-and-braces, not an expected path.
    """
    if _pq is None:  # pragma: no cover
        raise RuntimeError("pyarrow not importable")
    try:
        import pyarrow.fs as _pafs
    except ImportError:
        _pafs = None  # type: ignore[assignment]
    if _pafs is not None:
        # Region is required for S3FileSystem to pick the right endpoint
        # inside a Lambda; default region falls back to AWS_REGION env
        # which Lambda sets.
        fs = _pafs.S3FileSystem(region=os.environ.get("AWS_REGION"))
        return _pq.ParquetFile(fs.open_input_file(f"{bucket}/{key}"))
    # Fallback: load full body (memory-heavy but functional).
    obj = s3_client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    return _pq.ParquetFile(io.BytesIO(body))


def _infer_hour(bucket: str, key: str) -> tuple[str, bool]:
    """Read the first row's timestamp column and return
    ``(hour_HH, inferred)``. ``inferred=False`` means we couldn't read
    a real timestamp — caller MUST NOT copy this file; the outer loop
    escalates to a migration-failure so the operator sees it rather
    than silently losing hour precision.

    Uses ``pq.ParquetFile`` to inspect the schema first, then reads only
    the timestamp column(s) that actually exist. Pre-Phase-1 metering
    parquets have only ``timestamp`` (queue-time); Phase-1+ have both
    ``timestamp`` (completion-time) and ``initial_event_time``.
    Requesting a missing column raises ArrowInvalid, which would fail
    every legacy file — the round-6 blocker fix uses runtime schema
    detection instead. Also keeps the memory-savings intent: pyarrow
    reads only the requested column at Parquet-column granularity.
    """
    if _pq is None:  # pragma: no cover — handler guards this at entry
        raise RuntimeError("pyarrow import guard should have caught this")
    try:
        # Round-9 review fix: read via pyarrow.fs.S3FileSystem instead of
        # ``obj["Body"].read() → io.BytesIO``. The BytesIO path loaded the
        # WHOLE parquet body into memory before we could stream row groups;
        # at 50-worker concurrency × multi-MB legacy files this was
        # 50× the necessary footprint. pyarrow.fs issues range reads
        # (footer first, then only the first row-group's column chunks),
        # so memory stays O(footer + one row-group × wanted-columns).
        # Falls back to the BytesIO path if pyarrow.fs isn't available
        # (unusual — bundled with pyarrow) so an install without S3FS
        # still works.
        pf = _open_parquet_range_read(bucket, key)
        available_names = set(pf.schema_arrow.names)
        wanted = [
            c for c in ("timestamp", "initial_event_time") if c in available_names
        ]
        if not wanted:
            logger.warning(
                f"infer_hour: {key} has no timestamp/initial_event_time "
                f"column (schema: {sorted(available_names)}) — cannot "
                f"infer hour; leaving file in place for operator to inspect"
            )
            return ("00", False)
        # Read only the FIRST batch of ONLY the wanted columns — this
        # is O(one row group × wanted-columns) not O(whole file). Round-8
        # review fix: pf.read(columns=wanted) loaded all rows of the
        # wanted columns even though only row 0 is consumed; on
        # multi-MB legacy files at 50-worker concurrency that was a
        # 50× larger memory footprint than needed.
        # Round-19 review fix (#551): the round-18 "scan through nulls"
        # loop only saw row 0 because ``iter_batches(batch_size=1)``
        # produced 1-row batches. Real fix: iterate MULTIPLE batches
        # until we find a non-null value in any wanted column, then
        # stop. Keeps the memory-savings intent (streaming batches,
        # not full-file read) while actually scanning past null rows.
        # Cap iterations at a modest ROW_SCAN_CAP so a degenerate
        # all-nulls parquet doesn't stream forever.
        # Round-20 review fix (#566): iterate ROW-first, then columns
        # within each row. Round-19's column-first loop incremented
        # ``rows_scanned`` per (row × column) — so if column
        # ``timestamp`` had 128 nulls we hit the cap and broke out
        # BEFORE ever checking ``initial_event_time`` on those rows,
        # effectively halving the cap when both columns are wanted.
        # Row-first with the counter incremented ONCE per row makes
        # ROW_SCAN_CAP mean what the name says.
        ROW_SCAN_CAP = 128  # noqa: N806
        rows_scanned = 0
        found_hour: Optional[str] = None
        try:
            batches = pf.iter_batches(batch_size=16, columns=wanted)
            for batch in batches:
                # Determine max rows in this batch across the wanted columns.
                cols = {c: batch.column(c) for c in wanted}
                batch_len = max((len(cols[c]) for c in wanted), default=0)
                for row_idx in range(batch_len):
                    rows_scanned += 1
                    # Try each wanted column at this row in preferred order.
                    for candidate in wanted:
                        col = cols[candidate]
                        if row_idx >= len(col):
                            continue
                        ts = col[row_idx].as_py()
                        if ts is None:
                            continue
                        # Round-18 review fix (#578): tz-aware non-UTC →
                        # convert to UTC before strftime.
                        if ts.tzinfo is None:
                            logger.warning(
                                f"infer_hour: {key} column {candidate} row "
                                f"{row_idx} returned a naive datetime "
                                f"({ts.isoformat()}). Assuming UTC."
                            )
                            ts_utc = ts
                        else:
                            from datetime import timezone as _tz

                            ts_utc = ts.astimezone(_tz.utc)
                        found_hour = ts_utc.strftime("%H")
                        break
                    if found_hour is not None:
                        break
                    if rows_scanned >= ROW_SCAN_CAP:
                        break
                if found_hour is not None or rows_scanned >= ROW_SCAN_CAP:
                    break
        except StopIteration:
            pass
        if found_hour is not None:
            return (found_hour, True)
        if rows_scanned == 0:
            logger.warning(
                f"infer_hour: {key} has zero rows in {wanted} — cannot "
                f"infer hour; leaving file in place for operator to inspect"
            )
            return ("00", False)
    except Exception as e:
        # Log with enough detail for the operator to correlate — a
        # KMS/IAM issue looks identical to a corrupted-parquet issue in
        # the log summary otherwise.
        logger.warning(
            f"infer_hour failed for {key} ({type(e).__name__}: {e}) — "
            f"leaving file in place for operator to inspect"
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
    # Round-11 review fix: retry the PUT twice on transient failure
    # before giving up. A silent single-shot failure left CFN waiting
    # for the ServiceTimeout ceiling; retries recover from a flaky
    # connection while still bounded to a few seconds.
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=15.0, read=30.0))
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            resp = http.request(
                "PUT",
                response_url,
                body=json.dumps(response_body).encode("utf-8"),
                headers={"Content-Type": ""},
            )
            # Round-13 review fix: urllib3 doesn't raise on HTTP error
            # codes — a 4xx/5xx from the CFN presigned S3 endpoint used
            # to look like success. Treat only 2xx as success; anything
            # else is a real failure that should trigger the retry loop
            # or surface via the final error log.
            if 200 <= resp.status < 300:
                logger.info(f"CFN response sent: {status} — {reason}")
                return {"status": status, "reason": reason}
            raise RuntimeError(
                f"CFN response PUT returned HTTP {resp.status}: {resp.data[:512]!r}"
            )
        except Exception as e:
            last_error = e
            logger.warning(f"CFN response PUT failed (attempt {attempt + 1}/3): {e}")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))  # 2s, 4s
    # All three attempts failed. Log with maximum detail so the operator
    # has enough to diagnose from CloudWatch when CFN eventually hits
    # ServiceTimeout. We don't re-raise — the Lambda invocation is done
    # regardless, and raising here would just show up as a Lambda error
    # to CFN, which STILL waits for the ServiceTimeout ceiling.
    logger.error(
        f"CFN response PUT failed on all retries — CFN will wait for "
        f"the ServiceTimeout ceiling before rolling back. Last error: "
        f"{last_error!r}. Status was: {status}, reason: {reason}"
    )
    return {"status": status, "reason": reason}
