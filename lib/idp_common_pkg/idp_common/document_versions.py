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
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Reserved sub-prefix of a document's output prefix holding per-run manifests.
# Must never collide with pipeline artifacts (pages/, sections/, summary/,
# evaluation/ ...) and must survive reprocess output deletion.
RUNS_PREFIX_SEGMENT = "runs"


def runs_prefix(input_key: str) -> str:
    """Return the reserved runs prefix for a document, e.g. ``<key>/runs/``."""
    return f"{input_key}/{RUNS_PREFIX_SEGMENT}/"


def delete_current_output_objects(
    s3_client,
    output_bucket: str,
    input_key: str,
    subprefixes: Optional[Sequence[str]] = None,
) -> int:
    """
    Delete the current output objects for a document, preserving run manifests.

    Used to defeat the OCR function's retry-safe recovery
    (``discover_existing_ocr_pages``) when a document key is being processed
    again — either via the explicit "Reprocess" resolver, or when an S3 upload
    reuses an existing filename. Without this cleanup the OCR service reads
    stale ``pages/*`` artefacts from the previous document, skips OCR, and
    downstream classification/extraction consume text from the OLD document.

    ``subprefixes``:
      - ``None`` (default) — purge ``<key>/*``, preserving only
        ``<key>/runs/``. Used by the Reprocess resolver, where "start
        over" is the intent. ⚠️ For nested-key deployments (uploads
        exist at ``foo`` AND ``foo/bar.pdf``) this deletes the ENTIRE
        nested document — ``foo/bar.pdf/pages/*``,
        ``foo/bar.pdf/sections/*``, ``foo/bar.pdf/summary/*``,
        ``foo/bar.pdf/evaluation/*`` AND ``foo/bar.pdf/runs/*`` — not
        just its version history. The preserved-prefix filter only
        protects THIS document's ``<key>/runs/``, so anything under a
        nested document's prefix is unreachable and gets swept. Reprocess
        accepts that risk because the caller is an authenticated admin
        action ("start over" is deliberate); other callers should scope
        via ``subprefixes`` to avoid destroying nested-doc data.
      - ``("pages/",)`` (or similar list) — purge ONLY those subprefixes
        under ``<key>/``. Used by queue_sender for re-upload cleanup so a
        re-upload of ``foo`` cannot destroy nested-document data at
        ``foo/bar.pdf/*`` (issue #719 follow-up). Reduces blast radius to
        exactly the subprefixes that reintroduce the bug being fixed.
        Values are normalized: leading and trailing ``/`` are stripped
        and one trailing ``/`` re-appended, so both ``pages`` and
        ``/pages/`` scan ``<key>/pages/`` — never ``<key>/pages`` (which
        would match sibling prefixes like ``pages_backup/*``).

    Preserves the CURRENT document's ``{input_key}/runs/`` (per-run
    manifests plus the noncurrent object versions they pin) so its
    version history survives. On a versioned bucket, ``delete_objects``
    without VersionId writes delete markers, so the underlying bytes
    remain retrievable via the manifests.

    Returns:
        Number of objects successfully deleted (0 if the prefix was
        empty).

    Raises:
        ValueError: on bad ``subprefixes`` input — a bare ``str``
        instead of a sequence, or a sequence containing a non-str
        entry (``None``, int, ...), or a sequence with an entry that
        normalizes to empty after stripping slashes (``""``, ``"/"``).
        Each raises with a message naming the fix so a caller
        typo doesn't silently reduce to a no-op scan.
        RuntimeError: if any batch's ``delete_objects`` returns per-key
        ``Errors`` (visible in the response even with ``Quiet=True``).
        Silently continuing on partial failures would re-open #719 —
        OCR's retry-safe recovery only needs ONE complete 4-file page
        (rawText/result/textConfidence/image) to survive to resurrect
        stale text. Callers should catch and decide the escalation
        (queue_sender logs at ERROR so a metric filter can alarm on it).
    """
    # Strip any trailing slashes so an accidental ``input_key="foo/"``
    # (folder pseudo-object shape) doesn't produce ``foo//pages/`` — S3
    # treats that as a literal prefix that matches nothing, silently
    # reducing the purge to a no-op and leaving stale artefacts.
    input_key = input_key.rstrip("/")
    preserved_prefix = runs_prefix(input_key)
    if subprefixes is not None:
        # ``str`` is a Sequence[str] too but iterates over characters —
        # reject it BEFORE materialization (list(str) would produce a
        # char list that passes downstream checks and produce per-char
        # scans). A caller who typo'd ``subprefixes="pages/"`` instead
        # of ``subprefixes=("pages/",)`` gets a clear error.
        if isinstance(subprefixes, str):
            raise ValueError(
                f"subprefixes must be a sequence of strings (e.g. ``('pages/',)``), "
                f"not a bare string ``{subprefixes!r}`` — a bare str would iterate "
                f"over characters and produce per-character prefix scans."
            )
        # Materialize iterators (generators aren't Sequences but callers
        # sometimes pass one anyway) so the two passes below — the
        # isinstance validation loop and the ``strip`` comprehension —
        # see the same values. Without this a generator exhausts on the
        # first pass and the second reduces to an empty scan.
        subprefixes = list(subprefixes)
        # Explicit list — normalize both leading and trailing slashes so
        # ``pages`` / ``/pages`` / ``/pages/`` all produce ``<key>/pages/``
        # (a bare ``<key>/pages`` would match ``<key>/pages_backup/*``).
        # Empty tuple ``()`` is an intentional no-op (see below).
        # Reject non-str entries up front — the docstring advertises
        # ValueError as the failure mode; without this check a ``None`` /
        # int / other non-str entry raises AttributeError from
        # ``.strip('/')`` instead, breaking the documented contract.
        for i, sp in enumerate(subprefixes):
            if not isinstance(sp, str):
                raise ValueError(
                    f"subprefixes[{i}]={sp!r} is not a str (got "
                    f"{type(sp).__name__}). Each entry must be a string "
                    f"like ``'pages/'``."
                )
        # An entry like ``""`` or ``"/"`` normalizes to empty, which would
        # silently reduce to a no-op — indistinguishable from a fresh-key
        # upload. Callers passing a non-empty list clearly intend to
        # scope-purge; raise so a bad entry is loud rather than a silent
        # miss that lets stale ``pages/*`` survive.
        normalized = [sp.strip("/") for sp in subprefixes]
        if subprefixes and not all(normalized):
            raise ValueError(
                f"subprefixes contains an empty entry (after stripping slashes): "
                f"{list(subprefixes)!r}. Pass an empty tuple ``()`` for no-op "
                f"or ``None`` for broad purge — an empty string / bare ``/`` "
                f"would silently reduce to a no-op and let stale artefacts "
                f"survive."
            )
        prefixes_to_scan = [f"{input_key}/{n}/" for n in normalized]
    else:
        # None → broad purge under ``<key>/`` (reprocess semantics).
        prefixes_to_scan = [f"{input_key}/"]
    deleted = 0
    total_errors: List[Dict[str, Any]] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in prefixes_to_scan:
        for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix):
            # Defense-in-depth: even when scanning a subprefix that
            # structurally cannot include runs/ (e.g. ``pages/``), keep
            # the filter — it protects future callers that pass ``("",)``
            # or ``("runs/",)`` and would otherwise nuke the manifests.
            objects = [
                obj
                for obj in page.get("Contents", [])
                if not obj["Key"].startswith(preserved_prefix)
            ]
            if not objects:
                continue
            for i in range(0, len(objects), 1000):
                batch = [{"Key": obj["Key"]} for obj in objects[i : i + 1000]]
                response = s3_client.delete_objects(
                    Bucket=output_bucket, Delete={"Objects": batch, "Quiet": True}
                )
                errors = response.get("Errors", [])
                deleted += len(batch) - len(errors)
                total_errors.extend(errors)
    if total_errors:
        # Even one surviving 4-file page (rawText+result+textConfidence
        # +image) lets discover_existing_ocr_pages re-open #719.
        # Raise so the caller's error path (queue_sender: logger.error;
        # reprocess resolver: caller's try/except) fires visibly rather
        # than logging-and-continuing with a partial success count.
        # ``or '?'`` guards against a malformed error dict missing Key
        # or Code — a real occurrence when S3 returns a partial error
        # entry, and rendering ``None(None)`` in triage output is worse
        # than a labeled unknown.
        sample = ", ".join(
            f"{e.get('Key') or '?'}({e.get('Code') or '?'})" for e in total_errors[:5]
        )
        # Report the exact prefix(es) scanned so triage knows what was
        # actually purged — a narrow subprefixes=("pages/",) scan and a
        # broad reprocess purge report different failure surface even
        # for the same ``input_key``.
        scope = (
            prefixes_to_scan[0]
            if len(prefixes_to_scan) == 1
            else f"{len(prefixes_to_scan)} prefixes: {prefixes_to_scan!r}"
        )
        raise RuntimeError(
            f"delete_objects reported {len(total_errors)} per-key "
            f"failure(s) while purging s3://{output_bucket}/{scope} "
            f"({deleted} succeeded, {len(total_errors)} failed; sample: "
            f"{sample}). Stale artefacts may remain and OCR's retry-safe "
            f"recovery could re-open #719 on the next run."
        )
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
