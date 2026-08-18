# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CloudFormation custom-resource handler that migrates the metering S3
layout on stack upgrade — old ``metering/date=X/*.parquet`` files are
relocated into ``metering/date=X/hour=HH/`` subdirs so the new Glue
projection can see them.

Design
------
- **On Create:** the reporting bucket has no metering data yet — return
  SUCCESS immediately.
- **On Update:** list all keys under ``metering/`` that lack ``/hour=``;
  if the count is zero (already migrated, or nothing to migrate), return
  SUCCESS. Otherwise migrate in-Lambda with a ThreadPoolExecutor for
  parallel ``CopyObject`` calls.
- **On Delete:** no-op — the S3 files are managed by their bucket's own
  retention policy.

Why inline (vs. Step Functions):
- Common case is small (dev stacks: 0 files; test stacks: 10K-100K).
- With ~50 concurrent ``CopyObject`` calls, ~500K files fit in a 900s
  Lambda invocation.
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
DATE_PART_PATTERN = re.compile(
    r"^metering/date=(\d{4}-\d{2}-\d{2})/([^/]+\.parquet)$"
)

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

    with ThreadPoolExecutor(max_workers=COPY_CONCURRENCY) as executor:
        futures = {
            executor.submit(_migrate_one, bucket, key): key for key in old_keys
        }
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
                future.result()
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
    return _send(
        event,
        context,
        "SUCCESS",
        data={"Migrated": moved},
        reason=f"Migrated {moved} metering parquet files into hour-partitioned layout",
    )


def _iter_old_layout_keys(bucket: str) -> Iterator[str]:
    """Yield metering/*.parquet keys that lack ``/hour=`` in their path."""
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="metering/"):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith(".parquet") and not HOUR_KEY_PATTERN.search(key):
                yield key


def _migrate_one(bucket: str, key: str) -> None:
    """Copy ``key`` to its hour-partitioned target and delete the original.

    Interruption between copy and delete is safe — a re-run treats already-
    migrated files as no-ops (they carry ``/hour=`` in the key and the
    lister skips them).
    """
    hour = _infer_hour(bucket, key)
    target = _new_key(key, hour)
    if target is None:
        raise ValueError(f"key does not match expected shape: {key}")
    if target == key:
        return  # already migrated (defensive)
    s3_client.copy_object(
        Bucket=bucket,
        Key=target,
        CopySource={"Bucket": bucket, "Key": key},
    )
    s3_client.delete_object(Bucket=bucket, Key=key)


def _infer_hour(bucket: str, key: str) -> str:
    """Read the first row's timestamp column and return its UTC hour as HH.

    Falls back to ``initial_event_time`` if ``timestamp`` isn't present,
    and then to ``"00"`` if neither is readable — better to park an
    uncertain file in ``hour=00`` (still visible to the new projection)
    than fail the whole migration on one bad file.
    """
    try:
        import pyarrow.parquet as pq  # local import — pyarrow is heavy

        obj = s3_client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        table = pq.read_table(io.BytesIO(body))
        for candidate in ("timestamp", "initial_event_time"):
            if candidate in table.column_names and table.num_rows > 0:
                ts = table.column(candidate)[0].as_py()
                if ts is not None:
                    return ts.strftime("%H")
    except Exception as e:
        logger.warning(f"infer_hour failed for {key} ({e}) — parking in hour=00")
    return "00"


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

    http = urllib3.PoolManager()
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
