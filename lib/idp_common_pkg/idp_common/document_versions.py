# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Document version (processing run) utilities.

Every successful workflow execution is recorded as an immutable "run" of the
document. Output artifacts are written to deterministic keys under
``{input_key}/`` in the output bucket, so a re-run overwrites them in place.
All output buckets have S3 versioning enabled, which means the bytes of every
prior run are already retained as noncurrent object versions — but S3 alone
cannot say which object versions belong to which run (a run may produce a
different set of sections/pages than the last one).

This module closes that gap: at workflow completion it snapshots the current
VersionId of every output object into a per-run **manifest** stored at
``{input_key}/runs/{run_id}/manifest.json``. Fetching
``GetObject(key, VersionId)`` with a manifest entry returns the exact bytes of
that run even after the object is overwritten or deleted by a later run.

The ``runs/`` prefix is reserved: it is excluded from run snapshots and must be
preserved by reprocessing (which deletes the rest of the output prefix to
defeat stale-OCR recovery).
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reserved sub-prefix of a document's output prefix holding per-run manifests.
# Must never collide with pipeline artifacts (pages/, sections/, summary/,
# evaluation/ ...) and must survive reprocess output deletion.
RUNS_PREFIX_SEGMENT = "runs"


def runs_prefix(input_key: str) -> str:
    """Return the reserved runs prefix for a document, e.g. ``<key>/runs/``."""
    return f"{input_key}/{RUNS_PREFIX_SEGMENT}/"


def delete_current_output_objects(s3_client, output_bucket: str, input_key: str) -> int:
    """
    Delete the current output objects for a document, preserving run manifests.

    Used to defeat the OCR function's retry-safe recovery
    (``discover_existing_ocr_pages``) when a document key is being processed
    again — either via the explicit "Reprocess" resolver, or when an S3 upload
    reuses an existing filename. Without this cleanup the OCR service reads
    stale ``pages/*`` artefacts from the previous document, skips OCR, and
    downstream classification/extraction consume text from the OLD document.

    Preserves ``{input_key}/runs/`` (per-run manifests plus the noncurrent
    object versions they pin) so document version history survives. On a
    versioned bucket, ``delete_objects`` without VersionId writes delete
    markers, so the underlying bytes remain retrievable via the manifests.

    Returns:
        Number of objects deleted (0 if the prefix was empty).
    """
    prefix = f"{input_key}/"
    preserved_prefix = runs_prefix(input_key)
    deleted = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
        objects = [
            obj
            for obj in page.get("Contents", [])
            if not obj["Key"].startswith(preserved_prefix)
        ]
        if not objects:
            continue
        for i in range(0, len(objects), 1000):
            batch = [{"Key": obj["Key"]} for obj in objects[i : i + 1000]]
            s3_client.delete_objects(
                Bucket=output_bucket, Delete={"Objects": batch, "Quiet": True}
            )
            deleted += len(batch)
    return deleted


def build_run_id(completion_time: str, workflow_execution_arn: Optional[str]) -> str:
    """
    Build a stable, human-sortable run id from the completion timestamp and the
    Step Functions execution name.

    Example: ``20250707T141530Z-a1b2c3d4-...`` — the timestamp prefix keeps ids
    sortable; the execution name makes them unique and traceable back to the
    execution.
    """
    # Normalize to a compact UTC timestamp: 2025-07-07T14:15:30.123+00:00 (or
    # with any offset / trailing Z) -> 20250707T141530Z. Parsing first, rather
    # than string-munging, so a non-UTC offset is correctly converted to UTC
    # instead of producing a corrupt, non-sortable id (which would break the
    # "run_id sorts chronologically" invariant list_document_runs relies on).
    try:
        normalized = completion_time.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        ts = dt.strftime("%Y%m%dT%H%M%SZ")
    except (ValueError, TypeError):
        # Best-effort fallback for unexpected formats: strip subseconds and
        # separators (assumes the input is already UTC-ish).
        ts = (
            completion_time.split(".")[0]
            .split("+")[0]
            .replace("-", "")
            .replace(":", "")
        )
        if not ts.endswith("Z"):
            ts += "Z"
    execution_id = None
    if workflow_execution_arn:
        execution_id = workflow_execution_arn.split(":")[-1]
    return f"{ts}-{execution_id}" if execution_id else ts


def list_current_output_versions(
    s3_client, output_bucket: str, input_key: str
) -> List[Dict[str, Any]]:
    """
    List the *current* S3 version of every output object for a document.

    Uses ListObjectVersions and keeps only ``IsLatest`` non-delete-marker
    entries, excluding the reserved ``runs/`` prefix (prior run manifests are
    not artifacts of this run).

    Returns:
        List of ``{"key", "version_id", "size", "last_modified", "etag"}``.
    """
    prefix = f"{input_key}/"
    excluded_prefix = runs_prefix(input_key)
    files: List[Dict[str, Any]] = []

    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
        for version in page.get("Versions", []):
            if not version.get("IsLatest"):
                continue
            key = version["Key"]
            if key.startswith(excluded_prefix):
                continue
            last_modified = version.get("LastModified")
            files.append(
                {
                    "key": key,
                    # Unversioned buckets report the literal string "null";
                    # preserve it as-is (GetObject accepts VersionId="null").
                    "version_id": version.get("VersionId", "null"),
                    "size": version.get("Size", 0),
                    "last_modified": (
                        last_modified.isoformat() if last_modified else None
                    ),
                    "etag": version.get("ETag"),
                }
            )
    return files


def snapshot_output_versions(
    s3_client,
    output_bucket: str,
    input_key: str,
    run_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Snapshot the current output object versions for a document into a per-run
    manifest stored at ``s3://<output_bucket>/<input_key>/runs/<run_id>/manifest.json``.

    Args:
        s3_client: boto3 S3 client
        output_bucket: Output bucket name
        input_key: Document input key (output prefix)
        run_id: Run identifier (see :func:`build_run_id`)
        metadata: Optional run metadata merged into the manifest (e.g.
            completion_time, config_version, workflow_execution_arn)

    Returns:
        Tuple of (manifest dict, manifest S3 URI)
    """
    files = list_current_output_versions(s3_client, output_bucket, input_key)

    manifest: Dict[str, Any] = {
        "manifest_version": "1.0",
        "run_id": run_id,
        "object_key": input_key,
        "output_bucket": output_bucket,
        "file_count": len(files),
        "files": files,
    }
    if metadata:
        manifest.update(metadata)

    manifest_key = f"{runs_prefix(input_key)}{run_id}/manifest.json"
    s3_client.put_object(
        Bucket=output_bucket,
        Key=manifest_key,
        Body=json.dumps(manifest, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    manifest_uri = f"s3://{output_bucket}/{manifest_key}"
    logger.info(
        f"Wrote run manifest for {input_key} run {run_id}: "
        f"{len(files)} files pinned at {manifest_uri}"
    )
    return manifest, manifest_uri


def load_run_manifest(
    s3_client, output_bucket: str, input_key: str, run_id: str
) -> Optional[Dict[str, Any]]:
    """Load a run manifest; returns None if it does not exist."""
    manifest_key = f"{runs_prefix(input_key)}{run_id}/manifest.json"
    try:
        response = s3_client.get_object(Bucket=output_bucket, Key=manifest_key)
        return json.loads(response["Body"].read().decode("utf-8"))
    except s3_client.exceptions.NoSuchKey:
        return None


def manifest_version_map(manifest: Dict[str, Any]) -> Dict[str, str]:
    """Return a ``{key: version_id}`` map from a manifest."""
    return {f["key"]: f["version_id"] for f in manifest.get("files", [])}


def list_run_ids(s3_client, output_bucket: str, input_key: str) -> List[str]:
    """
    List the run ids for a document by enumerating its run manifests under the
    reserved runs/ prefix (``<key>/runs/<run_id>/manifest.json``).
    """
    prefix = runs_prefix(input_key)
    run_ids: List[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/manifest.json"):
                # <prefix><run_id>/manifest.json -> run_id
                run_ids.append(key[len(prefix) : -len("/manifest.json")])
    return run_ids


def referenced_versions_excluding(
    s3_client, output_bucket: str, input_key: str, exclude_run_id: str
) -> set:
    """
    Build the set of ``(key, version_id)`` pairs still pinned by any run manifest
    for this document OTHER than ``exclude_run_id``.

    Used to reference-count before deleting a run's pinned object versions: a
    version shared with another run (e.g. an artifact a later run did not
    rewrite, so both manifests reference the same VersionId) must not be
    deleted, or it would corrupt the run that still references it.
    """
    referenced: set = set()
    for other_run_id in list_run_ids(s3_client, output_bucket, input_key):
        if other_run_id == exclude_run_id:
            continue
        other_manifest = load_run_manifest(
            s3_client, output_bucket, input_key, other_run_id
        )
        if not other_manifest:
            continue
        for f in other_manifest.get("files", []):
            if f.get("version_id"):
                referenced.add((f["key"], f["version_id"]))
    return referenced


def delete_run_artifacts(
    s3_client, output_bucket: str, input_key: str, run_id: str
) -> int:
    """
    Delete a run's pinned S3 object versions and its manifest.

    Deleting a specific VersionId permanently removes those bytes (no delete
    marker), so this is the storage-reclaiming counterpart of a run-record
    delete. Current (latest) object versions belonging to the newest run are
    also pinned by that run's manifest, so this must only be called for run
    records the caller has decided to remove.

    Reference-counts against all OTHER run manifests for the document and skips
    any ``(key, version_id)`` still referenced elsewhere, so deleting one run
    never destroys bytes another retained run still points at.

    Returns:
        Number of object versions deleted.
    """
    manifest = load_run_manifest(s3_client, output_bucket, input_key, run_id)
    deleted = 0
    if manifest:
        # Versions still pinned by any other run of this document must survive.
        still_referenced = referenced_versions_excluding(
            s3_client, output_bucket, input_key, run_id
        )
        objects = []
        skipped = 0
        for f in manifest.get("files", []):
            version_id = f.get("version_id")
            if not version_id:
                continue
            if (f["key"], version_id) in still_referenced:
                skipped += 1
                continue
            objects.append({"Key": f["key"], "VersionId": version_id})
        if skipped:
            logger.info(
                f"Skipping {skipped} object version(s) for run {run_id} still "
                "referenced by other retained versions"
            )
        for i in range(0, len(objects), 1000):
            batch = objects[i : i + 1000]
            s3_client.delete_objects(
                Bucket=output_bucket, Delete={"Objects": batch, "Quiet": True}
            )
            deleted += len(batch)

    # Remove the manifest itself (and anything else under the run's prefix).
    run_prefix = f"{runs_prefix(input_key)}{run_id}/"
    paginator = s3_client.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=output_bucket, Prefix=run_prefix):
        entries = page.get("Versions", []) + page.get("DeleteMarkers", [])
        objects = [
            {"Key": e["Key"], "VersionId": e["VersionId"]}
            for e in entries
            if e.get("VersionId")
        ]
        for i in range(0, len(objects), 1000):
            batch = objects[i : i + 1000]
            s3_client.delete_objects(
                Bucket=output_bucket, Delete={"Objects": batch, "Quiet": True}
            )
    logger.info(f"Deleted {deleted} pinned object versions for run {run_id}")
    return deleted
