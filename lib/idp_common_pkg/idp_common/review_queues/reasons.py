# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why a document is in review: typed reasons built from its confidence alerts.

A section's ``confidence_threshold_alerts`` are computed during processing
(assessment or extraction in Pipeline mode, BDA processing in BDA mode): a field
raises one when its confidence is below its own threshold
(``x-aws-idp-confidence-threshold`` in the class schema), or below
``hitl.confidence_threshold`` when it has none. Each alert becomes a
``low_confidence`` reason, so routing agrees exactly with the alerts users see.
BDA mode adds a ``blueprint_confidence`` reason when the matched blueprint's
confidence is under ``hitl.confidence_threshold``. No result is read from S3.

This module must not import ``idp_common.assessment`` (or anything heavy): the
Pipeline ProcessResults Lambda installs only the ``review_queues`` extra.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal, Mapping, Optional

from .models import ReviewReason

logger = logging.getLogger(__name__)

#: Reasons kept on an item and in the status object; ``ReasonCount`` has the total.
MAX_REASONS = 20
#: ``hitl.confidence_threshold`` when the configuration carries none.
DEFAULT_HITL_THRESHOLD = 0.8

_INDEX = re.compile(r"\[(\d+)\]")


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _class_schema(config: Any, doc_class: Optional[str]) -> Optional[Mapping[str, Any]]:
    if not doc_class or config is None:
        return None
    from idp_common.config.schema_utils import find_class_schema

    return find_class_schema(doc_class, getattr(config, "classes", None))


def _hitl_threshold(config: Any) -> float:
    """``config.hitl.confidence_threshold``, or the default when it is unset."""
    hitl = getattr(config, "hitl", None) if config is not None else None
    value = _num(getattr(hitl, "confidence_threshold", None))
    return DEFAULT_HITL_THRESHOLD if value is None else value


def build_reasons(
    document: Any,
    config: Any = None,
    *,
    mode: Literal["pipeline", "bda"],
) -> list[ReviewReason]:
    """Every reason for the document's sections, worst first (callers truncate).

    ``config`` supplies the schema titles used as field labels and, in BDA mode,
    ``hitl.confidence_threshold`` for the blueprint confidence.
    """
    reasons: list[ReviewReason] = []
    for section in list(getattr(document, "sections", None) or []):
        section_id = str(section.section_id)
        schema = _class_schema(config, getattr(section, "classification", None))
        for alert in getattr(section, "confidence_threshold_alerts", None) or []:
            if not isinstance(alert, Mapping):
                continue
            path = alert.get("attribute_name") or alert.get("attributeName")
            score = _num(alert.get("confidence"))
            threshold = _num(
                alert.get("confidence_threshold", alert.get("confidenceThreshold"))
            )
            if score is None or threshold is None or score >= threshold:
                continue
            reasons.append(
                ReviewReason(
                    kind="low_confidence",
                    section_id=section_id,
                    field=path or None,
                    label=field_label(schema, path) if path else None,
                    score=score,
                    threshold=threshold,
                )
            )
        if mode == "bda":
            confidence = _num(getattr(section, "confidence", None))
            threshold = _hitl_threshold(config)
            if confidence is not None and confidence < threshold:
                reasons.append(
                    ReviewReason(
                        kind="blueprint_confidence",
                        section_id=section_id,
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
