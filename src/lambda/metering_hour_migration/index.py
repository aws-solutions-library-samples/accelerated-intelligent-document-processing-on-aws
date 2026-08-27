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
    executor = ThreadPoolExecutor(max_workers=COPY_CONCURRENCY)
    try:
        futures = {executor.submit(_migrate_one, bucket, key): key for key in old_keys}
        for future in as_completed(futures):
            if time.time() > deadline:
                # Return the FAILED response and let the finally-block
                # shut the executor down without waiting.
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
                    # File NOT copied — left at its original old-layout
                    # location. Operator retry will re-list it via the
                    # lister's ``not HOUR_KEY_PATTERN`` filter.
                    hour_fallbacks += 1
                else:
                    moved += 1
            except Exception as e:
                logger.warning(f"Failed to migrate {key}: {e}")
                errors += 1
    finally:
        # Cancel pending, don't wait on running. Round-8 review fix.
        executor.shutdown(wait=False, cancel_futures=True)

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
    if not inferred:
        # DO NOT copy — leave the file at its original location. A copy
        # to hour=00 would be a physical, irrecoverable data-loss event
        # (the lister excludes /hour=NN/ keys on retry so the mis-placed
        # file could not be rescued). The outer loop escalates to
        # FAILED; operator fixes the underlying issue and re-runs.
        # Round-6 review fix.
        return "fallback"
    target = _new_key(key, hour)
    if target is None:
        # Round-9 review fix: was ``assert target is not None`` which is
        # a no-op under PYTHONOPTIMIZE. Raising RuntimeError keeps the
        # invariant enforced regardless of the interpreter flags.
        raise RuntimeError(
            f"shape re-check failed after placeholder-hour probe passed: {key!r}"
        )
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
        s3_client.head_object(Bucket=bucket, Key=target)
        # Target already exists. Trust it (idempotent) and just delete
        # the leftover source — matches the "delete idempotently on
        # re-run" comment in the copy-then-delete design note.
        logger.info(
            f"Target already exists (prior copy succeeded, delete may "
            f"have failed): {target}. Cleaning up source and moving on."
        )
        s3_client.delete_object(Bucket=bucket, Key=key)
        return "moved"
    except s3_client.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3_client.copy_object(
        Bucket=bucket,
        Key=target,
        CopySource={"Bucket": bucket, "Key": key},
    )
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
        try:
            batch = next(pf.iter_batches(batch_size=1, columns=wanted))
        except StopIteration:
            # No rows at all — treat as un-inferrable.
            logger.warning(
                f"infer_hour: {key} has zero rows in {wanted} — cannot "
                f"infer hour; leaving file in place for operator to inspect"
            )
            return ("00", False)
        for candidate in wanted:  # honours the wanted-order preference
            col = batch.column(candidate)
            if len(col) > 0:
                ts = col[0].as_py()
                if ts is not None:
                    return (ts.strftime("%H"), True)
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
