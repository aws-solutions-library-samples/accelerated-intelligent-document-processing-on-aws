# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why a document is in review: typed reasons built from the stored alerts.

Routing consumes ``Section.confidence_threshold_alerts`` (written by assessment
or BDA ProcessResults with per-field thresholds already resolved) and never
recomputes thresholds. BDA mode adds an explicit ``blueprint_confidence`` reason
when the matched blueprint's confidence is under ``hitl.confidence_threshold``,
which is the second half of today's BDA trigger.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Mapping, Optional

from .models import ReviewReason

#: Reasons kept on an item and in the status object; ``ReasonCount`` has the total.
MAX_REASONS = 20

_INDEX = re.compile(r"\[(\d+)\]")


def _alert_fields(
    alert: Mapping[str, Any],
) -> tuple[str, Optional[float], Optional[float]]:
    name = alert.get("attribute_name") or alert.get("attributeName") or ""
    score = alert.get("confidence")
    threshold = alert.get("confidence_threshold", alert.get("confidenceThreshold"))
    return str(name), _num(score), _num(threshold)


def _num(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _class_schema(config: Any, doc_class: Optional[str]) -> Optional[Mapping[str, Any]]:
    if not doc_class or config is None:
        return None
    from idp_common.assessment.threshold_resolver import find_class_schema

    return find_class_schema(doc_class, getattr(config, "classes", None))


def build_reasons(
    document: Any, config: Any, *, mode: Literal["pipeline", "bda"]
) -> list[ReviewReason]:
    """Every reason for the document's sections, worst first (callers truncate)."""
    threshold = float(
        getattr(getattr(config, "hitl", None), "confidence_threshold", 0.8)
    )
    reasons: list[ReviewReason] = []
    for section in getattr(document, "sections", None) or []:
        schema = _class_schema(config, getattr(section, "classification", None))
        for alert in getattr(section, "confidence_threshold_alerts", None) or []:
            name, score, alert_threshold = _alert_fields(alert)
            if score is None or alert_threshold is None:
                continue
            reasons.append(
                ReviewReason(
                    kind="low_confidence",
                    section_id=str(section.section_id),
                    field=name or None,
                    label=field_label(schema, name) if name else None,
                    score=score,
                    threshold=alert_threshold,
                )
            )
        if mode == "bda":
            confidence = _num(getattr(section, "confidence", None))
            if confidence is not None and confidence < threshold:
                reasons.append(
                    ReviewReason(
                        kind="blueprint_confidence",
                        section_id=str(section.section_id),
                        field=None,
                        label=None,
                        score=confidence,
                        threshold=threshold,
                    )
                )
    reasons.sort(
        key=lambda r: (
            -(r.threshold - r.score),
            _section_order(r.section_id),
            r.field or "",
        )
    )
    return reasons


def _section_order(section_id: str) -> tuple[int, Any]:
    return (0, int(section_id)) if section_id.isdigit() else (1, section_id)


def _humanise(segment: str) -> str:
    if "_" in segment or segment.islower():
        words = [w for w in re.split(r"[_\s]+", segment) if w]
        return " ".join(words).capitalize() if words else segment
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", segment)
    return spaced


def field_label(
    class_schema: Optional[Mapping[str, Any]], attribute_name: str
) -> Optional[str]:
    """The schema ``title`` for an attribute path, else a humanised last segment.

    Paths look like ``Address.City`` or ``LineItems[2].Amount``.
    """
    if not attribute_name:
        return None
    segments = [s for s in _INDEX.sub("", attribute_name).split(".") if s]
    node: Any = class_schema
    for segment in segments:
        if not isinstance(node, Mapping):
            node = None
            break
        if node.get("type") == "array" or "items" in node and "properties" not in node:
            node = node.get("items")
            if not isinstance(node, Mapping):
                node = None
                break
        properties = node.get("properties") if isinstance(node, Mapping) else None
        node = properties.get(segment) if isinstance(properties, Mapping) else None
    if (
        isinstance(node, Mapping)
        and isinstance(node.get("title"), str)
        and node["title"].strip()
    ):
        return node["title"].strip()
    return _humanise(segments[-1]) if segments else attribute_name
