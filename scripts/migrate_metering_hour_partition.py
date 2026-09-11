#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One-shot migration for the metering table's new ``hour`` partition key.

Context
-------
Phase 1 of the reporting SQL layer added an ``hour`` partition key to the
Glue ``metering`` table (see ``docs/reporting-sql-layer.md`` §2.3). New
writes go to ``metering/date=YYYY-MM-DD/hour=HH/…``. Old writes lived at
``metering/date=YYYY-MM-DD/…`` (no hour subdirectory) and are invisible
to the new projection template, which walks
``date=${date}/hour=${hour}/`` paths only.

This script moves each existing ``date=X/*.parquet`` file into
``date=X/hour=HH/`` under the same date partition, where ``HH`` is
read from the file's own ``timestamp`` (or ``initial_event_time`` as
fallback) column. Historical dashboards that read via the new Glue
table become visible again after this runs.

Idempotency: files whose key already contains ``/hour=`` are skipped.

Usage
-----
    # Dry run first — reports what WOULD move
    python scripts/migrate_metering_hour_partition.py \\
        --bucket <reporting-bucket> \\
        --dry-run

    # For real (parallel S3 CopyObject; downloads each parquet to read the
    # hour from its `timestamp` column)
    python scripts/migrate_metering_hour_partition.py \\
        --bucket <reporting-bucket>

Safety
------
- **Copy-then-delete** per key. Athena reads a partition location
  recursively, so leaving originals in place would cause a rolled-back
  Glue table (reverted to ``date=X/``) to double-scan the ``hour=HH/``
  copies. Deleting the source after a verified copy keeps exactly one
  physical location per row regardless of which Glue projection is
  committed. A re-run picks up any missed deletes idempotently.
- Skips writes to Athena — the projection picks up the new partitions
  automatically once objects land under the ``hour=`` subpath.
- Parallelized with a ThreadPoolExecutor; ``--concurrency`` (default 50)
  matches the in-Lambda migration handler.
- Pass ``--profile`` to select an AWS CLI profile.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator, Optional

import boto3

# pyarrow imported at module scope so a missing dep fails the OPERATOR
# process cleanly at startup instead of being caught by
# ThreadPoolExecutor's worker→future.result() plumbing as SystemExit.
# Round-8 review fix.
try:
    import pyarrow.parquet as _pq
except ImportError:
    print(
        "ERROR: pyarrow required. `pip install pyarrow`.",
        file=sys.stderr,
    )
    sys.exit(2)

HOUR_KEY_PATTERN = re.compile(r"/hour=\d{2}/")
DATE_PART_PATTERN = re.compile(r"metering/date=(\d{4}-\d{2}-\d{2})/([^/]+\.parquet)$")


def iter_metering_parquet_keys(s3, bucket: str) -> Iterator[str]:
    """Yield every metering/*.parquet key under the bucket."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="metering/"):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith(".parquet"):
                yield key


def infer_hour_from_parquet(s3, bucket: str, key: str) -> Optional[str]:
    """Read the first row's timestamp column and return its UTC hour as HH.

    Uses ``ParquetFile.schema_arrow.names`` to detect which of
    ``timestamp``/``initial_event_time`` actually exists in this file
    (pre-Phase-1 parquets have only ``timestamp``, Phase-1+ have both),
    then reads only the wanted column via ``columns=[...]``. Round-7
    review fix: previously read the WHOLE parquet body with
    ``columns=None``, holding up to 50 × full-parquet in memory at
    50-worker concurrency; column projection makes memory O(timestamp
    column) not O(whole file).

    Returns ``None`` if the file has no readable timestamp — caller
    MUST NOT copy it to hour=00 (physical move would be irrecoverable).
    """
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    pf = _pq.ParquetFile(io.BytesIO(body))
    available_names = set(pf.schema_arrow.names)
    wanted = [c for c in ("timestamp", "initial_event_time") if c in available_names]
    if not wanted:
        return None
    # Round-19 review fix (#551): iterate MULTIPLE batches to find
    # the first non-null row across wanted columns. Previous
    # ``batch_size=1`` produced 1-row batches so the row-scan loop
    # from round-18 only ever saw row 0.
    from datetime import timezone as _tz

    ROW_SCAN_CAP = 128  # noqa: N806
    rows_scanned = 0
    # Round-28 review fix: iterate ROW-first, then column-per-row (mirrors
    # the Lambda's ``_infer_hour`` at metering_hour_migration/index.py
    # after round-20's #1948 fix). The previous column-first loop
    # exhausted rows_scanned scanning column-A's nulls before ever looking
    # at column-B — so on null-heavy files with one populated column, the
    # rescue script would give up at ROW_SCAN_CAP/2 effective rows and
    # return None for a file the Lambda would have succeeded on. Since the
    # whole point of this script is to rescue rows the Lambda flagged as
    # `hour_fallbacks`, that gap defeated the rescue.
    try:
        for batch in pf.iter_batches(batch_size=16, columns=wanted):
            cols = {c: batch.column(c) for c in wanted}
            batch_len = max((len(cols[c]) for c in wanted), default=0)
            for row_idx in range(batch_len):
                rows_scanned += 1
                for candidate in wanted:
                    col = cols[candidate]
                    if row_idx >= len(col):
                        continue
                    ts = col[row_idx].as_py()
                    if ts is None:
                        continue
                    ts_utc = ts if ts.tzinfo is None else ts.astimezone(_tz.utc)
                    return ts_utc.strftime("%H")
                if rows_scanned >= ROW_SCAN_CAP:
                    break
            if rows_scanned >= ROW_SCAN_CAP:
                break
    except StopIteration:
        pass
    return None


def new_key(old_key: str, hour: str) -> Optional[str]:
    """Rewrite ``metering/date=X/foo.parquet`` → ``metering/date=X/hour=HH/foo.parquet``."""
    match = DATE_PART_PATTERN.search(old_key)
    if not match:
        return None
    date_part, filename = match.groups()
    return f"metering/date={date_part}/hour={hour}/{filename}"


def _process_one(s3, bucket: str, key: str, dry_run: bool) -> str:
    """Migrate a single key (called from the parallel pool).

    Returns one of: ``"moved"`` (or ``"dry-moved"``), ``"skipped"``,
    ``"error"``, ``"unreadable"``. Errors and unreadable-hour cases log
    to stderr; they don't abort other workers, but the top-level caller
    treats a non-zero unreadable count as failure. Round-7 review fix:
    previously silently parked unreadable files at ``default_hour``
    ("00" by default), losing hour precision with no operator signal.
    Now the file is LEFT IN PLACE and reported so the operator can fix
    the underlying issue and re-run.
    """
    if HOUR_KEY_PATTERN.search(key):
        return "skipped"
    try:
        hour = infer_hour_from_parquet(s3, bucket, key)
    except Exception as e:
        print(f"  WARN read failed for {key}: {e}", file=sys.stderr)
        hour = None
    if hour is None:
        print(
            f"  UNREADABLE {key} — cannot infer hour; leaving in place. "
            f"Fix the underlying issue (KMS/IAM/corruption) and re-run.",
            file=sys.stderr,
        )
        return "unreadable"

    target = new_key(key, hour)
    if target is None:
        print(f"  SKIP {key} — path shape did not match")
        return "skipped"

    if dry_run:
        print(f"  DRY-RUN {key} -> {target}")
        return "dry-moved"

    try:
        # Copy-then-delete: after a verified copy, remove the original so
        # a rolled-back projection (that would read `date=X/` recursively)
        # doesn't double-scan both the source and the new hour subdir.
        s3.copy_object(
            Bucket=bucket,
            Key=target,
            CopySource={"Bucket": bucket, "Key": key},
        )
        s3.delete_object(Bucket=bucket, Key=key)
        print(f"  MOVED {key} -> {target}")
        return "moved"
    except Exception as e:
        print(f"  ERROR {key}: {e}", file=sys.stderr)
        return "error"


def migrate(
    bucket: str,
    dry_run: bool,
    concurrency: int = 50,
) -> tuple[int, int, int, int]:
    """Move every un-hour-partitioned metering parquet under ``bucket`` to
    its ``date=X/hour=HH/`` target — in parallel (copy-then-delete).

    Returns (moved, skipped, errors, unreadable). Files whose hour
    cannot be inferred are LEFT IN PLACE and reported as unreadable —
    operator must fix the underlying issue and re-run. See
    ``_process_one`` for rationale.
    """
    s3 = boto3.client("s3")
    keys = list(iter_metering_parquet_keys(s3, bucket))
    moved = skipped = errors = unreadable = 0
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(_process_one, s3, bucket, k, dry_run) for k in keys]
        for f in as_completed(futures):
            outcome = f.result()
            if outcome in ("moved", "dry-moved"):
                moved += 1
            elif outcome == "skipped":
                skipped += 1
            elif outcome == "unreadable":
                unreadable += 1
            else:
                errors += 1
    return moved, skipped, errors, unreadable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True, help="Reporting bucket name")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report moves without executing them",
    )
    parser.add_argument(
        "--profile",
        help="AWS CLI profile name",
    )
    parser.add_argument(
        "--default-hour",
        default="00",
        help=(
            "IGNORED. Accepted so an older caller's command line keeps working. "
            "The script no longer parks unreadable files at a default hour — "
            "they are left in place and the migration exits non-zero, because a "
            "mis-placed file cannot be re-attempted (the lister excludes keys "
            "already under /hour=NN/)."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=50,
        help="Number of parallel CopyObject workers (default: 50, matching the in-Lambda migration handler)",
    )
    args = parser.parse_args()

    if args.profile:
        boto3.setup_default_session(profile_name=args.profile)

    moved, skipped, errors, unreadable = migrate(
        bucket=args.bucket,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
    )
    print(
        f"\nSummary: moved={moved} skipped={skipped} errors={errors} "
        f"unreadable={unreadable}"
    )
    if unreadable:
        print(
            f"\nERROR: {unreadable} files could not have their hour inferred "
            f"and were LEFT IN PLACE. Fix the underlying issue and re-run.",
            file=sys.stderr,
        )
    return 1 if errors or unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
