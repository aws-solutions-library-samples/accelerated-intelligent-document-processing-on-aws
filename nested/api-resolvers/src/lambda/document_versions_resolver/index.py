# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Document versions resolver.

Serves the document version-history API fields:

- ``listDocumentVersions(objectKey)`` — run records for a document, newest first
- ``getDocumentVersion(objectKey, runId)`` — one run record incl. pinned files
- ``compareDocumentVersions(objectKey, runIdA, runIdB)`` — section-level diff of
  two runs' extraction results (read via manifest-pinned S3 VersionIds)
- ``deleteDocumentVersion(objectKey, runId)`` — Admin-only; removes the run
  record, its manifest, and its pinned S3 object versions

A "version" is an immutable record of one successful processing run: the
workflow tracker snapshots the S3 VersionId of every output object into a
per-run manifest (``<key>/runs/<run_id>/manifest.json``) and writes a run item
(``PK=doc#<key>, SK=run#<run_id>``) to the tracking table. Because the output
bucket is versioned, ``GetObject(key, VersionId)`` returns that run's exact
bytes even after later runs overwrite the objects.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from idp_common.docs_service import create_document_service
from idp_common.document_versions import (
    delete_run_artifacts,
    load_run_manifest,
    manifest_version_map,
)
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3_client = boto3.client("s3")
document_service = create_document_service()

output_bucket = os.environ.get("OUTPUT_BUCKET")


def _caller_groups(event) -> List[str]:
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return list(groups)


def _as_float(value: Any) -> Optional[float]:
    """Coerce a stored DynamoDB number to a GraphQL Float, preserving null.

    DynamoDB returns numbers as ``Decimal``, which this resolver's
    ``json.dumps(..., default=str)`` would emit as a quoted string — a Float
    field would then arrive at the UI as ``"0.85"``. ``None`` stays ``None``
    (not scored) rather than becoming 0.0.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _shape_processing_issues(section: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Shape a run section's stored issues to the GraphQL ProcessingIssue type.

    Only the displayed fields are returned; the stored ``details`` blob (a
    JSON string that can be large) is intentionally dropped — it is not part of
    the GraphQL type and the version panel never renders it.
    """
    return [
        {
            "stage": issue.get("stage"),
            "severity": issue.get("severity"),
            "code": issue.get("code"),
            "message": issue.get("message"),
            "rootCause": issue.get("rootCause"),
        }
        for issue in section.get("ProcessingIssues") or []
    ]


def _shape_version(item: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a run item into the GraphQL DocumentVersion type."""
    sections = [
        {
            "Id": s.get("Id"),
            "PageIds": s.get("PageIds", []),
            "Class": s.get("Class"),
            "OutputJSONUri": s.get("OutputJSONUri"),
            # Confidence in the section's CLASS. Coerced to float because
            # DynamoDB hands back a Decimal, which this resolver's
            # json.dumps(default=str) would emit as a quoted string for a
            # GraphQL Float field. None when the run was not scored.
            "Confidence": _as_float(s.get("Confidence")),
            # Per-section quality data snapshotted by create_document_run. The
            # UI's "Low Confidence Fields" count and section Status read these,
            # so dropping them here makes every historical section look clean.
            # Runs recorded before these were snapshotted have no such keys —
            # emit empty lists rather than null so the UI's Array.isArray()
            # checks behave the same as on the live document.
            "ConfidenceThresholdAlerts": s.get("ConfidenceThresholdAlerts") or [],
            "ProcessingIssues": _shape_processing_issues(s),
            # Multi-instance count, snapshotted by create_document_run. This
            # allow-list is explicit, so a key omitted here is silently dropped
            # from every historical view — the same way the confidence alerts
            # were once lost (see CHANGELOG). Runs recorded before this existed
            # have no such key; 0 means "undetermined", matching the live doc.
            "InstanceCount": int(s.get("InstanceCount") or 0),
            # Exclusion flags, snapshotted by create_document_run. Same allow-list
            # hazard: omitted here, a historical excluded section loses its
            # "Skipped" badge and reads as an unexplained empty section.
            "Excluded": bool(s.get("Excluded", False)),
            "ExclusionReason": s.get("ExclusionReason"),
        }
        for s in item.get("Sections", []) or []
    ]
    pages = [
        {
            "Id": p.get("Id"),
            "Class": p.get("Class"),
            "ImageUri": p.get("ImageUri"),
            "TextUri": p.get("TextUri"),
            "OcrPageDataUri": p.get("OcrPageDataUri"),
            # Classification signals snapshotted by create_document_run. Like the
            # section allow-list above, an omitted key here is silently dropped
            # from every historical view.
            "ClassConfidence": _as_float(p.get("ClassConfidence")),
            "ClassReason": p.get("ClassReason") or None,
            "ClassCandidates": [
                {
                    "Class": c.get("Class"),
                    "Probability": _as_float(c.get("Probability")),
                }
                for c in p.get("ClassCandidates") or []
            ],
            "Boundary": p.get("Boundary") or None,
        }
        for p in item.get("Pages", []) or []
    ]
    return {
        "RunId": item.get("RunId") or item.get("SK", "").replace("run#", ""),
        "ObjectKey": item.get("ObjectKey"),
        "CompletionTime": item.get("CompletionTime"),
        "QueuedTime": item.get("QueuedTime"),
        "WorkflowStartTime": item.get("WorkflowStartTime"),
        "WorkflowExecutionArn": item.get("WorkflowExecutionArn"),
        "ConfigVersion": item.get("ConfigVersion"),
        "PageCount": item.get("PageCount"),
        "FileCount": item.get("FileCount"),
        "ManifestUri": item.get("ManifestUri"),
        "SummaryReportUri": item.get("SummaryReportUri"),
        "EvaluationReportUri": item.get("EvaluationReportUri"),
        "Metering": item.get("Metering"),
        "Sections": sections,
        "Pages": pages,
    }


def list_document_versions(object_key: str) -> List[Dict[str, Any]]:
    runs = document_service.list_document_runs(object_key)
    return [_shape_version(r) for r in runs]


def get_document_version(object_key: str, run_id: str) -> Optional[Dict[str, Any]]:
    run = document_service.get_document_run(object_key, run_id)
    if not run:
        return None
    version = _shape_version(run)
    # Include the manifest's pinned files so clients can fetch exact bytes.
    manifest = load_run_manifest(s3_client, output_bucket, object_key, run_id)
    if manifest:
        version["Files"] = [
            {
                "Key": f.get("key"),
                "VersionId": f.get("version_id"),
                "Size": f.get("size"),
            }
            for f in manifest.get("files", [])
        ]
    return version


def _load_section_results(
    object_key: str, run: Dict[str, Any], run_id: str
) -> Dict[str, Dict[str, Any]]:
    """
    Load each section's extraction result for a run, pinned to the run's
    manifest VersionIds. Keyed by section id.
    """
    manifest = load_run_manifest(s3_client, output_bucket, object_key, run_id)
    version_map = manifest_version_map(manifest) if manifest else {}
    results: Dict[str, Dict[str, Any]] = {}
    for section in run.get("Sections", []) or []:
        uri = section.get("OutputJSONUri") or ""
        if not uri.startswith("s3://"):
            continue
        bucket, _, key = uri[5:].partition("/")
        get_kwargs: Dict[str, Any] = {"Bucket": bucket, "Key": key}
        version_id = version_map.get(key)
        if version_id and version_id != "null":
            get_kwargs["VersionId"] = version_id
        try:
            response = s3_client.get_object(**get_kwargs)
            data = json.loads(response["Body"].read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"Could not load section result {uri} (run {run_id}): {e}")
            continue
        results[str(section.get("Id"))] = {
            "class": section.get("Class"),
            "result": data.get("inference_result", data),
        }
    return results


def _diff_values(path: str, a: Any, b: Any, changes: List[Dict[str, Any]]) -> None:
    """Recursively diff two JSON values, appending leaf-level changes."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            child = f"{path}.{k}" if path else str(k)
            if k not in a:
                changes.append({"path": child, "type": "added", "b": b[k]})
            elif k not in b:
                changes.append({"path": child, "type": "removed", "a": a[k]})
            else:
                _diff_values(child, a[k], b[k], changes)
    elif isinstance(a, list) and isinstance(b, list):
        # Intentional shallow list handling: emit a single length_changed marker
        # and diff only the common prefix (zip stops at the shorter list). This
        # keeps the version-comparison summary concise; elements beyond the
        # shorter length are not itemized by design.
        if len(a) != len(b):
            changes.append(
                {"path": path, "type": "length_changed", "a": len(a), "b": len(b)}
            )
        for i, (av, bv) in enumerate(zip(a, b)):
            _diff_values(f"{path}[{i}]", av, bv, changes)
    elif a != b:
        changes.append({"path": path, "type": "changed", "a": a, "b": b})


def compare_document_versions(
    object_key: str, run_id_a: str, run_id_b: str
) -> Dict[str, Any]:
    """
    Compare the extraction results of two runs section-by-section.

    Sections are matched by classification (falling back to section id) since
    section ids/boundaries can differ between runs.
    """
    run_a = document_service.get_document_run(object_key, run_id_a)
    run_b = document_service.get_document_run(object_key, run_id_b)
    if not run_a or not run_b:
        missing = run_id_a if not run_a else run_id_b
        raise ValueError(f"Version not found: {missing}")

    results_a = _load_section_results(object_key, run_a, run_id_a)
    results_b = _load_section_results(object_key, run_b, run_id_b)

    def by_match_key(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        keyed = {}
        for section_id, entry in results.items():
            match_key = entry.get("class") or section_id
            # First occurrence wins; duplicate classes fall back to class#id
            if match_key in keyed:
                match_key = f"{match_key}#{section_id}"
            keyed[match_key] = entry
        return keyed

    keyed_a = by_match_key(results_a)
    keyed_b = by_match_key(results_b)

    sections = []
    for match_key in sorted(set(keyed_a) | set(keyed_b)):
        entry_a = keyed_a.get(match_key)
        entry_b = keyed_b.get(match_key)
        if entry_a and not entry_b:
            sections.append(
                {"section": match_key, "status": "only_in_a", "changes": []}
            )
        elif entry_b and not entry_a:
            sections.append(
                {"section": match_key, "status": "only_in_b", "changes": []}
            )
        else:
            changes: List[Dict[str, Any]] = []
            _diff_values("", entry_a["result"], entry_b["result"], changes)
            sections.append(
                {
                    "section": match_key,
                    "status": "changed" if changes else "identical",
                    "changes": changes,
                }
            )

    return {
        "objectKey": object_key,
        "runIdA": run_id_a,
        "runIdB": run_id_b,
        "configVersionA": run_a.get("ConfigVersion"),
        "configVersionB": run_b.get("ConfigVersion"),
        "completionTimeA": run_a.get("CompletionTime"),
        "completionTimeB": run_b.get("CompletionTime"),
        "identical": all(s["status"] == "identical" for s in sections),
        "sections": sections,
    }


def delete_document_version(object_key: str, run_id: str) -> bool:
    run = document_service.get_document_run(object_key, run_id)
    if not run:
        raise ValueError(f"Version not found: {run_id}")

    # Guard against deleting the CURRENT (newest) version. Its manifest pins the
    # IsLatest S3 object versions — i.e. the live document's current outputs —
    # and delete_run_artifacts hard-deletes them (no delete marker), which would
    # destroy the document the detail page reads via plain GetObject. The UI
    # already disables this, but the mutation must enforce it server-side since a
    # direct API/SDK call bypasses the UI. run_id is timestamp-prefixed, so the
    # lexicographically-greatest run_id is the newest.
    runs = document_service.list_document_runs(object_key)
    if runs:
        newest_run_id = max(
            r.get("RunId") or r.get("SK", "").replace("run#", "") for r in runs
        )
        if run_id == newest_run_id:
            raise ValueError(
                "Cannot delete the current (most recent) version. Reprocess the "
                "document to create a newer version first, or delete the whole "
                "document instead."
            )

    # Reclaim storage first (pinned object versions + manifest), then the record.
    delete_run_artifacts(s3_client, output_bucket, object_key, run_id)
    document_service.delete_document_run(object_key, run_id)
    return True


def handler(event, context):
    logger.info(
        "Document versions resolver invoked: %s",
        json.dumps(sanitize_event_for_logging(event)),
    )
    if not output_bucket:
        raise Exception("OUTPUT_BUCKET environment variable is not set")

    field = event.get("info", {}).get("fieldName", "")
    args = event.get("arguments", {}) or {}
    object_key = args.get("objectKey")
    if not object_key:
        raise ValueError("objectKey is required")

    if field == "listDocumentVersions":
        return list_document_versions(object_key)
    if field == "getDocumentVersion":
        return get_document_version(object_key, args["runId"])
    if field == "compareDocumentVersions":
        result = compare_document_versions(object_key, args["runIdA"], args["runIdB"])
        # AWSJSON field: return as a JSON string
        return json.dumps(result, default=str)
    if field == "deleteDocumentVersion":
        # Destructive: permanently removes pinned S3 object versions. Admin only.
        if "Admin" not in _caller_groups(event):
            raise PermissionError(
                "Unauthorized: deleteDocumentVersion requires Admin group"
            )
        return delete_document_version(object_key, args["runId"])

    raise ValueError(f"Unknown field: {field}")
