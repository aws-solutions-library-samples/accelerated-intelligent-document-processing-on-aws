# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading a finished run back from the stack into a run record.

The run record (``fit-run-record.json``) holds everything ``report.py`` needs and
nothing it has to fetch, so ``idp-cli fit-check --from-record`` rebuilds a report
without AWS access. Its shape, per document::

    {"file", "format", "pages", "object_key", "status", "error", "errors",
     "duration_seconds", "metering", "issues", "notes", "text_layer_scripts",
     "page_text": {page_id: text},
     "sections": [{"section_id", "class", "page_ids", "issues",
                   "inference_result", "explainability_info"}],
     "evaluation": overall_metrics | None}

and for the profile, ``configuration.settings`` (see :func:`profile_settings`).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

RECORD_VERSION = 1


def canonical_sha256(value: Any) -> str:
    """SHA-256 of ``value`` serialised with sorted keys, for before/after checks."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _class_id(cls: Dict[str, Any]) -> Optional[str]:
    return cls.get("x-aws-idp-document-type") or cls.get("$id") or cls.get("name")


def _granularity(classification: Dict[str, Any]) -> Optional[str]:
    strategy = classification.get("strategy") or "combined"
    if strategy == "combined":
        return (classification.get("combined") or {}).get("granularity")
    splitting = (classification.get("split_then_classify") or {}).get("splitting") or {}
    return (splitting.get("llm") or {}).get("granularity")


def profile_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """The settings of a processing profile that the report and detectors read."""
    ocr = config.get("ocr") or {}
    features = [
        f.get("name") if isinstance(f, dict) else f for f in ocr.get("features") or []
    ]
    classes = {}
    for cls in config.get("classes") or []:
        cid = _class_id(cls)
        if cid:
            classes[cid] = list((cls.get("properties") or {}).keys())
    return {
        "use_bda": bool(config.get("use_bda")),
        "ocr_backend": ocr.get("backend"),
        "ocr_features": [f for f in features if f],
        "extraction_mode": (config.get("extraction") or {}).get("mode"),
        "classification_granularity": _granularity(config.get("classification") or {}),
        # Each confidence leaf the assessment step writes carries its own
        # threshold, so the report reads that; there is no profile-wide default.
        "confidence_threshold": None,
        "classes": classes,
    }


def _read_json(s3, bucket: str, key: str) -> Optional[Dict[str, Any]]:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


def _issue_dict(issue: Any) -> Dict[str, Any]:
    return issue.to_dict() if hasattr(issue, "to_dict") else dict(issue)


def collect_document(
    *,
    sample: Dict[str, Any],
    object_key: str,
    status: Dict[str, Any],
    tracking: Any,
    s3: Any,
    output_bucket: str,
    with_evaluation: bool,
) -> Dict[str, Any]:
    """One document's record. ``status`` is its ``batch.get_status`` entry."""
    record: Dict[str, Any] = {
        "file": sample["file"],
        "format": sample.get("format"),
        "pages": status.get("num_pages") or sample.get("pages"),
        "object_key": object_key,
        "status": status.get("status"),
        "error": status.get("error"),
        "errors": [],
        "duration_seconds": status.get("duration_seconds"),
        "metering": {},
        "issues": [],
        "notes": list(status.get("notes") or []),
        "text_layer_scripts": sample.get("text_layer_scripts") or {},
        "page_text": {},
        "sections": [],
        "evaluation": None,
    }
    document = tracking.get_document(object_key) if tracking else None
    if document is None:
        record["notes"].append("No tracking record was found for this document")
        return record
    record["pages"] = document.num_pages or record["pages"]
    record["errors"] = list(document.errors or [])
    record["metering"] = dict(document.metering or {})
    record["issues"] = [_issue_dict(i) for i in document.processing_issues or []]
    page_ids = set()
    for section in document.sections or []:
        page_ids.update(section.page_ids or [])
        result = (
            _read_json(
                s3,
                output_bucket,
                f"{object_key}/sections/{section.section_id}/result.json",
            )
            or {}
        )
        explainability = result.get("explainability_info") or {}
        if isinstance(explainability, list):
            explainability = explainability[0] if explainability else {}
        record["sections"].append(
            {
                "section_id": str(section.section_id),
                "class": section.classification,
                "page_ids": [str(p) for p in section.page_ids or []],
                "issues": [_issue_dict(i) for i in section.processing_issues or []],
                "inference_result": result.get("inference_result") or {},
                "explainability_info": explainability,
            }
        )
    for page_id in sorted(page_ids, key=lambda p: int(p) if str(p).isdigit() else 0):
        parsed = _read_json(
            s3, output_bucket, f"{object_key}/pages/{page_id}/result.json"
        )
        if parsed and isinstance(parsed.get("text"), str):
            record["page_text"][str(page_id)] = parsed["text"]
    if with_evaluation:
        from idp_common.evaluation.contract import evaluation_results_key

        result = _read_json(s3, output_bucket, evaluation_results_key(object_key))
        if result:
            record["evaluation"] = result.get("overall_metrics") or None
    return record


def new_record(
    *,
    stack_name: str,
    region: Optional[str],
    solution_version: Optional[str],
    samples: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "record_version": RECORD_VERSION,
        "solution_version": solution_version,
        "stack": {"name": stack_name, "region": region},
        "configuration": {},
        "samples": samples,
        "documents": [],
        "ground_truth": False,
        "guard": {},
        "cleanup": None,
    }
