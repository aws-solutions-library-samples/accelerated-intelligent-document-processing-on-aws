# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why a document is in review: typed reasons built against the queue threshold.

Routing reads each section's extraction result (``extraction_result_uri``) once
and compares every field confidence in its ``explainability_info`` with the
Default queue's ``confidence_threshold``. Per-field schema thresholds and the
profile's ``hitl.confidence_threshold`` are not used here: they still drive the
assessment alerts and the field highlights. Both processing modes store field
confidences the same way (``explainability_info`` in the section's
``result.json``); BDA mode adds a ``blueprint_confidence`` reason when the
matched blueprint's confidence is under the same threshold. A section whose
result can't be read gives a reason too, so a read failure never skips review.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal, Mapping, Optional

from .models import ReviewReason

logger = logging.getLogger(__name__)

#: Reasons kept on an item and in the status object; ``ReasonCount`` has the total.
MAX_REASONS = 20
#: The label of the reason recorded when a section's confidences can't be read.
UNAVAILABLE_LABEL = "Confidence unavailable"
#: Section results read at once.
_READ_WORKERS = 8

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


def _read_confidences(section: Any, s3: Any) -> Optional[list]:
    """``(confidence, threshold, path)`` per field, or ``None`` when unreadable."""
    from .testset_summaries import walk_confidence_named

    uri = getattr(section, "extraction_result_uri", None)
    if not uri:
        logger.warning(
            "event=review_confidence_unavailable section=%s reason=no_result",
            getattr(section, "section_id", None),
        )
        return None
    try:
        result = s3.get_json_content(uri)
    except Exception:  # noqa: BLE001 — fail toward review, never skip it
        logger.warning(
            "event=review_confidence_unavailable section=%s reason=read_failed",
            getattr(section, "section_id", None),
            exc_info=True,
        )
        return None
    if not isinstance(result, Mapping):
        return None
    return walk_confidence_named(result.get("explainability_info") or [])


def build_reasons(
    document: Any,
    config: Any = None,
    *,
    threshold: float,
    mode: Literal["pipeline", "bda"],
    s3: Any = None,
) -> list[ReviewReason]:
    """Every reason for the document's sections, worst first (callers truncate).

    ``threshold`` is the Default queue's; ``config`` only supplies the schema
    titles used as field labels.
    """
    if s3 is None:
        import idp_common.s3 as s3_module

        s3 = s3_module
    sections = list(getattr(document, "sections", None) or [])
    with ThreadPoolExecutor(
        max_workers=max(1, min(_READ_WORKERS, len(sections)))
    ) as pool:
        found = list(pool.map(lambda sec: _read_confidences(sec, s3), sections))
    reasons: list[ReviewReason] = []
    for section, confidences in zip(sections, found):
        section_id = str(section.section_id)
        if confidences is None:
            reasons.append(
                ReviewReason(
                    kind="low_confidence",
                    section_id=section_id,
                    field=None,
                    label=UNAVAILABLE_LABEL,
                    score=0.0,
                    threshold=threshold,
                )
            )
        else:
            schema = _class_schema(config, getattr(section, "classification", None))
            for score, _field_threshold, path in confidences:
                if score >= threshold:
                    continue
                reasons.append(
                    ReviewReason(
                        kind="low_confidence",
                        section_id=section_id,
                        field=path,
                        label=field_label(schema, path) if path else None,
                        score=score,
                        threshold=threshold,
                    )
                )
        if mode == "bda":
            confidence = _num(getattr(section, "confidence", None))
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
