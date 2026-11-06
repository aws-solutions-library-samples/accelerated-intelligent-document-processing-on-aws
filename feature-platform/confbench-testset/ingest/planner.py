# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ConfBench ingest planner and finalizer — the ends of the state machine.

`plan_handler` runs once at the start of a job: it downloads the dataset's
parquet metadata (a single 76 KB file), writes a compact row index to S3 for the
variant workers to share, and emits the Map state's input list.

`finalize_handler` runs once at the end: it registers the test-set record Test
Studio lists, consolidates per-variant failure reports, and closes out the job
row.

Splitting these out of the worker keeps the parquet download to exactly one
occurrence per job. The original PR #583 deployer re-read a rows.json blob
containing the full ground truth on every one of its ~14 continuations; here the
index carries only what the workers need to locate and label a file.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from botocore.config import Config

# HuggingFace cache must live in Lambda's only writable directory.
os.environ.setdefault("HF_HOME", "/tmp/huggingface")  # nosec B108 - Lambda sandbox
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", "/tmp/huggingface/hub")  # nosec B108

import pyarrow.parquet as pq  # noqa: E402 - must follow the HF_HOME setup above
from huggingface_hub import hf_hub_download  # noqa: E402
from variants import HF_PARQUET_PATH, HF_REPO_ID, totals  # noqa: E402

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TESTSET_BUCKET = os.environ["TESTSET_BUCKET"]
JOB_TABLE = os.environ["JOB_TABLE"]
HOST_TRACKING_TABLE = os.environ["HOST_TRACKING_TABLE"]
# Configuration profile the ui-deployer created from this extension's bundled
# preset (named after the feature id), recorded on each test-set row so Test
# Studio preselects it. Empty on an older host that has no configVersion
# support — harmless, the UI just falls back to the active profile.
CONFIG_VERSION_NAME = os.environ.get("CONFIG_VERSION_NAME", "")

_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3 = boto3.client(
    "s3",
    endpoint_url=_s3_endpoint_url,
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual" if _s3_endpoint_url else "path"},
    ),
)
_ddb = boto3.resource("dynamodb")

_CACHE_DIR = "/tmp/huggingface/hub"  # nosec B108 - Lambda sandbox


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _state_prefix(job_id: str) -> str:
    return f"_confbench_jobs/{job_id}"


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------
def plan_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Download parquet metadata, write the shared row index, emit Map input.

    Event: {jobId, testSetId, variants: [...]}
    Returns: {jobId, testSetId, shards: [{jobId, testSetId, variant, offset}], ...}
    """
    job_id = event["jobId"]
    test_set_id = event["testSetId"]
    selected: List[str] = list(event["variants"])

    os.makedirs(_CACHE_DIR, exist_ok=True)
    logger.info("Downloading ConfBench parquet metadata from %s", HF_REPO_ID)
    parquet_path = hf_hub_download(  # nosec B615 - pinned public dataset, revision below
        repo_id=HF_REPO_ID,
        filename=HF_PARQUET_PATH,
        repo_type="dataset",
        revision="main",
        cache_dir=_CACHE_DIR,
    )

    table = pq.read_table(parquet_path)
    columns = table.to_pydict()
    wanted = set(selected)

    # Compact index: only rows for the selected variants, only the fields a
    # worker needs. json_response IS the heavy column, so we keep it here (the
    # workers need it to write baselines) but exclude every row they won't touch.
    rows: List[Dict[str, Any]] = []
    for i, document_id in enumerate(columns["id"]):
        variant = columns["noise_variant"][i]
        if variant not in wanted:
            continue
        rows.append(
            {
                "id": document_id,
                "noise_variant": variant,
                "page_count": columns["page_count"][i],
                "json_response": columns["json_response"][i],
            }
        )

    key = f"{_state_prefix(job_id)}/rows.json"
    _s3.put_object(
        Bucket=TESTSET_BUCKET,
        Key=key,
        Body=json.dumps(rows).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(
        "Wrote %d row descriptors to s3://%s/%s", len(rows), TESTSET_BUCKET, key
    )

    expected_files, expected_bytes = totals(selected)
    # Reconcile the static catalog against what the parquet actually contains.
    # A mismatch means upstream re-published; the job proceeds (the parquet is
    # authoritative) but the discrepancy is recorded for the operator.
    if len(rows) != expected_files:
        logger.warning(
            "Row count %d != catalog expectation %d for variants %s — upstream "
            "dataset may have changed; proceeding with the parquet's contents",
            len(rows),
            expected_files,
            ",".join(sorted(selected)),
        )

    _ddb.Table(JOB_TABLE).update_item(
        Key={"jobId": job_id},
        UpdateExpression=(
            "SET jobStatus = :s, plannedFiles = :pf, expectedBytes = :eb, "
            "startedAt = if_not_exists(startedAt, :t), updatedAt = :t"
        ),
        ExpressionAttributeValues={
            ":s": "RUNNING",
            ":pf": len(rows),
            ":eb": expected_bytes,
            ":t": _now(),
        },
    )

    # Mark the host test-set record IN_PROGRESS so Test Studio shows the set
    # arriving rather than nothing at all.
    _register_test_set(test_set_id, "IN_PROGRESS", file_count=None)

    return {
        "jobId": job_id,
        "testSetId": test_set_id,
        "plannedFiles": len(rows),
        "shards": [
            {
                "jobId": job_id,
                "testSetId": test_set_id,
                "variant": variant,
                "offset": 0,
            }
            for variant in sorted(selected)
        ],
    }


# ---------------------------------------------------------------------------
# Host test-set registration
# ---------------------------------------------------------------------------
def _register_test_set(
    test_set_id: str,
    status: str,
    file_count: int | None,
    description: str | None = None,
) -> None:
    """Create/refresh the host's test-set record without clobbering metadata.

    Test Studio lists ItemType='testset' rows. if_not_exists on the descriptive
    fields means a re-ingest (e.g. adding variants to an existing set) updates
    status and counts while preserving anything an admin edited.
    """
    now = _now()
    names = {
        "#status": "status",
        "#itemType": "ItemType",
        "#id": "id",
        "#name": "name",
        "#createdAt": "createdAt",
        "#iet": "InitialEventTime",
        "#description": "description",
        "#source": "source",
    }
    values: Dict[str, Any] = {
        ":status": status,
        ":itemType": "testset",
        ":id": test_set_id,
        ":name": _display_name(test_set_id),
        ":createdAt": now,
        ":description": description
        or (
            "ConfBench — FCC invoices with Augraphy noise degradation, for "
            "confidence calibration and OCR robustness evaluation. Deployed by "
            "the Test Set - ConfBench extension."
        ),
        ":source": f"huggingface:{HF_REPO_ID}",
    }
    sets = [
        "#status = :status",
        "#itemType = :itemType",
        "#id = :id",
        "#name = if_not_exists(#name, :name)",
        "#createdAt = if_not_exists(#createdAt, :createdAt)",
        "#iet = if_not_exists(#iet, :createdAt)",
        "#description = if_not_exists(#description, :description)",
        "#source = :source",
    ]
    if file_count is not None:
        names["#fileCount"] = "fileCount"
        values[":fileCount"] = file_count
        sets.append("#fileCount = :fileCount")
    # Declare the configuration profile Test Studio should preselect for this
    # test set. Without it the UI falls back to matching a profile whose name
    # equals the test set id — which never matches here, because the Feature
    # Platform names an extension's preset profile after the feature id, not
    # after any of its test sets.
    #
    # if_not_exists: an admin who repoints a ConfBench test set at their own
    # tuned configuration keeps that choice across re-ingests.
    if CONFIG_VERSION_NAME:
        names["#configVersion"] = "configVersion"
        values[":configVersion"] = CONFIG_VERSION_NAME
        sets.append("#configVersion = if_not_exists(#configVersion, :configVersion)")
    try:
        _ddb.Table(HOST_TRACKING_TABLE).update_item(
            Key={"PK": f"testset#{test_set_id}", "SK": "metadata"},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        logger.info("Registered test set %s as %s", test_set_id, status)
    except Exception as exc:  # noqa: BLE001 - never fail a job on bookkeeping
        logger.warning("Could not register test set %s: %s", test_set_id, exc)


def _display_name(test_set_id: str) -> str:
    """Friendly name for the Test Studio list."""
    suffixes = {
        "confbench": "ConfBench (full)",
        "confbench-clean": "ConfBench (clean baseline)",
        "confbench-light": "ConfBench (light noise)",
        "confbench-representative": "ConfBench (representative)",
        "confbench-custom": "ConfBench (custom selection)",
    }
    return suffixes.get(test_set_id, test_set_id)


# ---------------------------------------------------------------------------
# Finalizer
# ---------------------------------------------------------------------------
def finalize_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Close out a job: consolidate failures, count what landed, mark status.

    Event: {jobId, testSetId, results: [...]} — `results` is the Map output.
    """
    job_id = event["jobId"]
    test_set_id = event["testSetId"]
    results = event.get("results") or []

    uploaded = sum(int(r.get("uploaded") or 0) for r in results if isinstance(r, dict))
    skipped = sum(int(r.get("skipped") or 0) for r in results if isinstance(r, dict))
    failed = sum(int(r.get("failed") or 0) for r in results if isinstance(r, dict))

    # Count what is actually in the bucket rather than trusting the counters —
    # the authoritative number for the Test Studio file count. Paginated: a full
    # ingest is ~1,346 objects, well past list_objects_v2's 1,000-key page.
    actual = 0
    token: str | None = None
    while True:
        kwargs: Dict[str, Any] = {
            "Bucket": TESTSET_BUCKET,
            "Prefix": f"{test_set_id}/input/",
        }
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kwargs)
        actual += resp.get("KeyCount", 0)
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            break

    # Consolidate per-variant failure objects into one report.
    all_failures: List[Dict[str, str]] = []
    token = None
    while True:
        kwargs = {
            "Bucket": TESTSET_BUCKET,
            "Prefix": f"{_state_prefix(job_id)}/failures/",
        }
        if token:
            kwargs["ContinuationToken"] = token
        resp = _s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            obj_key = obj.get("Key")
            if not obj_key:
                continue
            try:
                body = _s3.get_object(Bucket=TESTSET_BUCKET, Key=obj_key)
                all_failures.extend(json.loads(body["Body"].read()))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not read failure report %s: %s", obj_key, exc)
        token = resp.get("NextContinuationToken")
        if not resp.get("IsTruncated"):
            break

    report_key = f"{_state_prefix(job_id)}/report.json"
    _s3.put_object(
        Bucket=TESTSET_BUCKET,
        Key=report_key,
        Body=json.dumps(
            {
                "jobId": job_id,
                "testSetId": test_set_id,
                "completedAt": _now(),
                "uploaded": uploaded,
                "skipped": skipped,
                "failed": failed,
                "objectsInBucket": actual,
                "failures": all_failures,
            },
            indent=2,
        ).encode("utf-8"),
        ContentType="application/json",
    )

    # A job that moved nothing at all is a failure; partial success is COMPLETED
    # with a recorded failure count (matching the accelerator's other test-set
    # deployers, which degrade gracefully rather than block).
    status = "COMPLETED" if actual > 0 else "FAILED"
    _register_test_set(test_set_id, status, file_count=actual)

    _ddb.Table(JOB_TABLE).update_item(
        Key={"jobId": job_id},
        UpdateExpression=(
            "SET jobStatus = :s, completedAt = :t, updatedAt = :t, "
            "filesInBucket = :a, reportKey = :rk"
        ),
        ExpressionAttributeValues={
            ":s": status,
            ":t": _now(),
            ":a": actual,
            ":rk": report_key,
        },
    )

    # Drop the row index — it is the largest artifact of the job and useless
    # once the transfer is done.
    try:
        _s3.delete_object(
            Bucket=TESTSET_BUCKET, Key=f"{_state_prefix(job_id)}/rows.json"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not delete row index for job %s: %s", job_id, exc)

    logger.info(
        "job=%s finalized: status=%s uploaded=%d skipped=%d failed=%d inBucket=%d",
        job_id,
        status,
        uploaded,
        skipped,
        failed,
        actual,
    )
    return {
        "jobId": job_id,
        "testSetId": test_set_id,
        "status": status,
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
        "filesInBucket": actual,
        "reportKey": report_key,
    }


def fail_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Mark a job FAILED when the state machine's catch-all fires."""
    job_id = event.get("jobId", "unknown")
    test_set_id = event.get("testSetId", "")
    error = json.dumps(event.get("error") or {})[:500]
    try:
        _ddb.Table(JOB_TABLE).update_item(
            Key={"jobId": job_id},
            UpdateExpression=(
                "SET jobStatus = :s, completedAt = :t, updatedAt = :t, lastError = :e"
            ),
            ExpressionAttributeValues={":s": "FAILED", ":t": _now(), ":e": error},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not mark job %s failed: %s", job_id, exc)
    if test_set_id:
        _register_test_set(test_set_id, "FAILED", file_count=None)
    logger.error("job=%s failed: %s", job_id, error)
    return {"jobId": job_id, "status": "FAILED", "error": error}
