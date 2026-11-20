# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Building ``fit-report.json`` (schema_version 1) and ``fit-report.md`` from a run
record. Pure: no AWS calls, so a recorded run rebuilds the same report offline.

The record's shape is written by ``collect.py``; ``fit-report.schema.json`` beside
this module is the contract the result is validated against.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from idp_sdk._core.fit_check.detectors import run_detectors

SCHEMA_VERSION = 1
SOLUTION_ID = "genai-idp-accelerator"
SCHEMA_PATH = Path(__file__).with_name("fit-report.schema.json")

CONFIDENCE_IS_NOT_ACCURACY = (
    "Confidence is not accuracy: these scores are the model's own estimate of each "
    "value and were not checked against ground truth. Give --ground-truth to measure "
    "accuracy."
)

_SUCCEEDED = {"COMPLETED", "AWAITING_REVIEW"}


def contract_version(pep440: Optional[str]) -> Optional[str]:
    """The solution-profile form of a PEP 440 version (``0.6.16.dev1`` ->
    ``0.6.16-dev1``), as ``scripts/check_solution_profile.py`` writes it."""
    if not pep440:
        return None
    import re

    match = re.match(r"^(\d+\.\d+\.\d+)(?:[.-]?([0-9A-Za-z.]+))?$", pep440.strip())
    if not match:
        return pep440.strip()
    base, suffix = match.groups()
    return base if not suffix else f"{base}-{suffix}"


# ------------------------------------------------------------------- fields


def _leaves(node: Any) -> List[Dict[str, Any]]:
    """Every ``{"confidence": ...}`` leaf under an explainability node."""
    if isinstance(node, dict):
        if "confidence" in node and not isinstance(node.get("confidence"), dict):
            return [node]
        out: List[Dict[str, Any]] = []
        for value in node.values():
            out.extend(_leaves(value))
        return out
    if isinstance(node, list):
        out = []
        for value in node:
            out.extend(_leaves(value))
        return out
    return []


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, list, dict)) and len(value) == 0:
        return False
    return True


def build_fields(
    schema_fields: List[str],
    inference_result: Dict[str, Any],
    explainability: Dict[str, Any],
    default_threshold: Optional[float],
) -> List[Dict[str, Any]]:
    """One entry per top-level field of the class.

    ``confidence`` is the lowest confidence among the field's leaves (a list or an
    object has one per value), ``below_threshold`` whether any leaf is under its
    threshold; both are ``null`` when the field carries no confidence.
    """
    names = list(schema_fields) or list(inference_result)
    for extra in inference_result:
        if extra not in names:
            names.append(extra)
    fields = []
    for name in names:
        leaves = _leaves(explainability.get(name))
        scored: List[Tuple[float, Optional[float]]] = []
        for leaf in leaves:
            confidence = _as_float(leaf.get("confidence"))
            if confidence is None:
                continue
            threshold = _as_float(leaf.get("confidence_threshold"))
            scored.append(
                (confidence, threshold if threshold is not None else default_threshold)
            )
        confidence = min((c for c, _ in scored), default=None)
        below: Optional[bool] = None
        if scored and any(t is not None for _, t in scored):
            below = any(t is not None and c < t for c, t in scored)
        fields.append(
            {
                "name": name,
                "present": _present(inference_result.get(name)),
                "confidence": confidence,
                "below_threshold": below,
            }
        )
    return fields


# ---------------------------------------------------------------- documents


def _main_section(sections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not sections:
        return None
    return max(sections, key=lambda s: len(s.get("page_ids") or []))


def _issue_warnings(doc: Dict[str, Any]) -> List[str]:
    out = []
    issues = list(doc.get("issues") or [])
    for section in doc.get("sections") or []:
        issues.extend(section.get("issues") or [])
    for issue in issues:
        if (issue.get("severity") or "").lower() in ("warning", "error"):
            out.append(f"{issue.get('stage') or 'processing'}: {issue.get('message')}")
    return out


def build_document(
    doc: Dict[str, Any], config: Dict[str, Any], cost: Optional[float]
) -> Dict[str, Any]:
    sections = doc.get("sections") or []
    main = _main_section(sections)
    warnings: List[str] = list(doc.get("notes") or [])
    if len(sections) > 1:
        warnings.append(
            "Processed as a packet of "
            f"{len(sections)} sections: "
            + ", ".join(f"{s.get('section_id')}={s.get('class')}" for s in sections)
            + f"; class and fields are those of section {main.get('section_id') if main else '?'}, "
            "which covers the most pages"
        )
    warnings += _issue_warnings(doc)
    findings = run_detectors(doc, config)
    warnings += [f.warning for f in findings]
    refs = sorted({f.ref for f in findings if f.ref})

    status = "succeeded" if doc.get("status") in _SUCCEEDED else "failed"
    fields: List[Dict[str, Any]] = []
    doc_class = None
    if main is not None:
        doc_class = main.get("class")
        schema_fields = (config.get("classes") or {}).get(doc_class) or []
        explainability = main.get("explainability_info") or {}
        fields = build_fields(
            schema_fields,
            main.get("inference_result") or {},
            explainability,
            _as_float(config.get("confidence_threshold")),
        )
    error = doc.get("error")
    if status == "failed" and not error:
        error = f"processing status {doc.get('status') or 'unknown'}"
    return {
        "file": doc["file"],
        "format": doc.get("format") or "",
        "pages": doc.get("pages"),
        "status": status,
        "class": doc_class,
        "fields": fields,
        "triggered_limitations": refs,
        "warnings": list(dict.fromkeys(warnings)),
        "latency_seconds": _as_float(doc.get("duration_seconds")),
        "estimated_cost_usd": cost,
        "error": error if status == "failed" else None,
    }


def build_refused(sample: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": sample["file"],
        "format": sample.get("format") or "",
        "pages": sample.get("pages"),
        "status": "refused",
        "class": None,
        "fields": [],
        "triggered_limitations": [sample["refused_ref"]]
        if sample.get("refused_ref")
        else [],
        "warnings": [],
        "latency_seconds": None,
        "estimated_cost_usd": None,
        "error": f"Refused before upload: {sample['refused']}",
    }


# ------------------------------------------------------------------ summary


def _percentile(values: List[float], q: float) -> Optional[float]:
    """Linear interpolation between closest ranks (NumPy's default method)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    value = ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return round(value, 3)


def build_summary(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    succeeded = [d for d in documents if d["status"] == "succeeded"]
    refused = [d for d in documents if d["status"] == "refused"]
    classes: Dict[str, Dict[str, Any]] = {}
    for doc in succeeded:
        name = doc["class"] or "unclassified"
        entry = classes.setdefault(name, {"documents": 0, "scores": [], "below": set()})
        entry["documents"] += 1
        for field in doc["fields"]:
            if field["confidence"] is not None:
                entry["scores"].append(field["confidence"])
            if field["below_threshold"]:
                entry["below"].add(field["name"])
    refs: Dict[str, int] = {}
    for doc in documents:
        for ref in doc["triggered_limitations"]:
            refs[ref] = refs.get(ref, 0) + 1
    latencies = [
        d["latency_seconds"] for d in succeeded if d["latency_seconds"] is not None
    ]
    priced = [d for d in documents if d["estimated_cost_usd"] is not None]
    priced_pages = sum(d["pages"] or 0 for d in priced)
    per_page = (
        round(sum(d["estimated_cost_usd"] for d in priced) / priced_pages, 6)
        if priced and priced_pages
        else None
    )
    return {
        "documents": len(documents),
        "succeeded": len(succeeded),
        "failed": len(documents) - len(succeeded),
        "refused": len(refused),
        "classes": [
            {
                "class": name,
                "documents": entry["documents"],
                "mean_confidence": (
                    round(sum(entry["scores"]) / len(entry["scores"]), 4)
                    if entry["scores"]
                    else None
                ),
                "fields_below_threshold": sorted(entry["below"]),
            }
            for name, entry in sorted(classes.items())
        ],
        "triggered_limitations": [
            {"ref": ref, "documents": n} for ref, n in sorted(refs.items())
        ],
        "latency_seconds": {
            "p50": _percentile(latencies, 0.5),
            "p90": _percentile(latencies, 0.9),
        },
        "estimated_cost_usd_per_page": per_page,
    }


def build_evaluation(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Mean of the evaluation step's per-document overall metrics, or ``None``
    when the run had no ground truth."""
    if not record.get("ground_truth"):
        return None
    metrics = ("precision", "recall", "f1_score", "accuracy")
    evaluated = []
    missing = []
    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for doc in record.get("documents") or []:
        result = doc.get("evaluation")
        if not isinstance(result, dict):
            missing.append(doc["file"])
            continue
        evaluated.append(result)
        cls = (_main_section(doc.get("sections") or []) or {}).get(
            "class"
        ) or "unclassified"
        by_class.setdefault(cls, []).append(result)

    def mean(rows: List[Dict[str, Any]], key: str) -> Optional[float]:
        values = [v for v in (_as_float(r.get(key)) for r in rows) if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    return {
        "method": "mean of the evaluation step's per-document overall metrics",
        "documents_evaluated": len(evaluated),
        "documents_without_result": sorted(missing),
        **{key: mean(evaluated, key) for key in metrics},
        "by_class": [
            {
                "class": cls,
                "documents": len(rows),
                **{k: mean(rows, k) for k in metrics},
            }
            for cls, rows in sorted(by_class.items())
        ],
    }


# ------------------------------------------------------------------- report


def _costs(record: Dict[str, Any]) -> Dict[str, Optional[float]]:
    from idp_common.config.models import IDPConfig
    from idp_common.reporting.pricing import build_pricing_map, estimate_metering_cost

    entries = (record.get("configuration") or {}).get("pricing") or []
    pricing = IDPConfig.model_validate({"pricing": entries}).pricing if entries else []
    pricing_map = build_pricing_map(pricing)
    out: Dict[str, Optional[float]] = {}
    for doc in record.get("documents") or []:
        metering = doc.get("metering") or {}
        cost = estimate_metering_cost(metering, pricing_map).estimated_cost_usd
        out[doc["file"]] = round(cost, 6) if cost is not None else None
    return out


def build_report(record: Dict[str, Any]) -> Dict[str, Any]:
    configuration = record.get("configuration") or {}
    settings = configuration.get("settings") or {}
    costs = _costs(record)
    documents = [
        build_document(doc, settings, costs.get(doc["file"]))
        for doc in record.get("documents") or []
    ]
    documents += [
        build_refused(s) for s in record.get("samples") or [] if s.get("refused")
    ]
    documents.sort(key=lambda d: d["file"])
    version = record.get("solution_version")
    return {
        "schema_version": SCHEMA_VERSION,
        "solution": {
            "id": SOLUTION_ID,
            "version": version,
            "profile_version": contract_version(version),
        },
        "stack": {
            "name": (record.get("stack") or {}).get("name"),
            "region": (record.get("stack") or {}).get("region"),
            "mode": "bda" if settings.get("use_bda") else "pipeline",
        },
        "configuration": {
            "profile": configuration.get("profile"),
            "generated_by": configuration.get("generated_by"),
            "classes": sorted((settings.get("classes") or {}).keys()),
        },
        "documents": documents,
        "evaluation": build_evaluation(record),
        "summary": build_summary(documents),
    }


def load_schema() -> Dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_report(report: Dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` if ``report`` breaks the contract."""
    import jsonschema

    jsonschema.validate(report, load_schema())


# ----------------------------------------------------------------- markdown


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: Dict[str, Any], record: Dict[str, Any]) -> str:
    s = report["summary"]
    cfg = report["configuration"]
    lines = [
        "# Fit check report",
        "",
        f"Stack `{report['stack']['name']}` in {report['stack']['region']}, solution "
        f"version {report['solution']['version']}, {report['stack']['mode']} mode.",
        f"Configuration profile `{cfg['profile']}` ({cfg['generated_by']}), classes: "
        + (", ".join(f"`{c}`" for c in cfg["classes"]) or "none")
        + ".",
        "",
        "This report states measurements. It does not judge whether the accelerator "
        "fits; that is for the reader.",
        "",
        "## Summary",
        "",
        "| Documents | Succeeded | Failed | Of which refused before upload |",
        "|---|---|---|---|",
        f"| {s['documents']} | {s['succeeded']} | {s['failed']} | {s['refused']} |",
        "",
        f"Latency p50 {_fmt(s['latency_seconds']['p50'], 1)} s, p90 "
        f"{_fmt(s['latency_seconds']['p90'], 1)} s. Estimated cost per page "
        f"{_fmt(s['estimated_cost_usd_per_page'], 4)} USD.",
        "",
        "Costs are estimates from the processing profile's pricing table applied to "
        "each document's metering. They leave out generating the configuration "
        "(Discovery and Quick Start run from this workstation and are not metered by "
        "the stack) and any attempt that Step Functions retried.",
        "",
    ]
    if s["classes"]:
        lines += [
            "| Class | Documents | Mean confidence | Fields below threshold |",
            "|---|---|---|---|",
        ]
        for c in s["classes"]:
            lines.append(
                f"| {c['class']} | {c['documents']} | {_fmt(c['mean_confidence'])} | "
                f"{', '.join(c['fields_below_threshold']) or 'none'} |"
            )
        lines.append("")
    if s["triggered_limitations"]:
        lines += ["Solution-profile limitations observed:", ""]
        lines += [
            f"- `{t['ref']}`: {t['documents']} document(s)"
            for t in s["triggered_limitations"]
        ]
        lines.append("")
    lines += ["## Accuracy", ""]
    evaluation = report["evaluation"]
    if evaluation is None:
        lines += [CONFIDENCE_IS_NOT_ACCURACY, ""]
    else:
        lines += [
            f"Evaluated {evaluation['documents_evaluated']} document(s) against ground "
            f"truth ({evaluation['method']}): precision {_fmt(evaluation['precision'])}, "
            f"recall {_fmt(evaluation['recall'])}, F1 {_fmt(evaluation['f1_score'])}, "
            f"accuracy {_fmt(evaluation['accuracy'])}.",
            "",
        ]
    lines += ["## Documents", ""]
    for d in report["documents"]:
        lines.append(
            f"### {d['file']}: {d['status']}"
            + (f", class `{d['class']}`" if d["class"] else "")
        )
        lines.append("")
        lines.append(
            f"Format {d['format'] or 'none'}, pages {_fmt(d['pages'])}, latency "
            f"{_fmt(d['latency_seconds'], 1)} s, estimated cost "
            f"{_fmt(d['estimated_cost_usd'], 4)} USD."
        )
        if d["error"]:
            lines.append(f"Error: {d['error']}")
        if d["fields"]:
            lines += [
                "",
                "| Field | Present | Confidence | Below threshold |",
                "|---|---|---|---|",
            ]
            for f in d["fields"]:
                lines.append(
                    f"| {f['name']} | {'yes' if f['present'] else 'no'} | "
                    f"{_fmt(f['confidence'])} | {_fmt(f['below_threshold'])} |"
                )
        for ref in d["triggered_limitations"]:
            lines.append(f"- Limitation `{ref}`")
        for w in d["warnings"]:
            lines.append(f"- {w}")
        lines.append("")
    guard = record.get("guard") or {}
    if guard:
        lines += ["## Active configuration", ""]
        before, after = guard.get("before") or {}, guard.get("after") or {}
        lines += [
            "| | Active profile | Active profile SHA-256 | Config#default SHA-256 |",
            "|---|---|---|---|",
            f"| Before | {before.get('active_profile')} | {before.get('active_sha256')} | {before.get('default_sha256')} |",
            f"| After | {after.get('active_profile')} | {after.get('active_sha256')} | {after.get('default_sha256')} |",
            "",
            "Unchanged."
            if guard.get("unchanged")
            else "**CHANGED** — see the run output.",
            "",
        ]
    cleanup = record.get("cleanup")
    if cleanup:
        lines += ["## Cleanup", "", cleanup.get("summary", ""), ""]
    return "\n".join(lines).rstrip() + "\n"
