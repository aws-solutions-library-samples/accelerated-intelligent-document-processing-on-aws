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
confidence is under ``hitl.confidence_threshold``, or when the blueprint matched
but its confidence is unscored (``Section.blueprint_unscored``), at every
threshold, as legacy HITL marks it; that reason carries a score of 0.0. With
``hitl.route_missing_required_fields`` on (the default), each of a section's
``missing_required_fields`` (a field its class schema requires that extraction
left absent, #1527) becomes a ``missing_required`` reason with a score of 0.0 and
the field's threshold, or ``hitl.confidence_threshold`` when it has none. A
section carrying an error-severity ``extraction_failed`` processing issue (its
extraction produced no successful result, for example because a content filter
withheld the model's reply, #2038) becomes one ``extraction_failed`` reason whose
label is the issue's message, scored 0.0 against ``hitl.confidence_threshold``.
Missing required fields drive the review queues only; a failed extraction also
triggers legacy HITL (``routing.legacy_section_triggers`` reads
:func:`failed_extraction_issue`), so both modes hold the document. No result is
read from S3.

This module must not import ``idp_common.assessment`` (or anything heavy): the
Pipeline ProcessResults Lambda installs only the ``review_queues`` extra.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any, Collection, Literal, Mapping, Optional

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


def route_missing_required_fields(config: Any) -> bool:
    """``config.hitl.route_missing_required_fields``; on when unset."""
    hitl = getattr(config, "hitl", None) if config is not None else None
    value = getattr(hitl, "route_missing_required_fields", None)
    return True if value is None else bool(value)


#: ``ProcessingIssue.code`` of a section whose extraction did not succeed. The
#: same value as ``idp_common.extraction.failure.EXTRACTION_FAILED_CODE``, spelled
#: here because importing ``idp_common.extraction`` loads the extraction service,
#: which the Pipeline ProcessResults Lambda does not install; a test pins the two.
EXTRACTION_FAILED_ISSUE_CODE = "extraction_failed"


def _issue_field(issue: Any, name: str) -> Any:
    if isinstance(issue, Mapping):
        return issue.get(name)
    return getattr(issue, name, None)


def failed_extraction_issue(section: Any) -> Optional[Any]:
    """The section's error-severity ``extraction_failed`` issue, or ``None``.

    Such a section has no successful extraction result, so it has no fields and no
    confidence scores to raise an alert with; this is what holds it for review.
    """
    for issue in getattr(section, "processing_issues", None) or []:
        if (
            _issue_field(issue, "code") == EXTRACTION_FAILED_ISSUE_CODE
            and _issue_field(issue, "severity") == "error"
        ):
            return issue
    return None


def hitl_threshold(config: Any) -> float:
    """``config.hitl.confidence_threshold``, or the default when it is unset."""
    hitl = getattr(config, "hitl", None) if config is not None else None
    value = _num(getattr(hitl, "confidence_threshold", None))
    return DEFAULT_HITL_THRESHOLD if value is None else value


def build_reasons(
    document: Any,
    config: Any = None,
    *,
    mode: Literal["pipeline", "bda"],
    confirmed_sections: Collection[str] = (),
) -> list[ReviewReason]:
    """Every reason for the document's sections, worst first (callers truncate).

    ``config`` supplies the schema titles used as field labels and, in BDA mode,
    ``hitl.confidence_threshold`` for the blueprint confidence.

    A section whose id is in ``confirmed_sections`` gives no reason: a reviewer
    has confirmed its values, so its alerts and missing fields no longer ask for
    a review.
    """
    reasons: list[ReviewReason] = []
    route_missing = route_missing_required_fields(config)
    default_threshold = hitl_threshold(config)
    confirmed = {str(section_id) for section_id in confirmed_sections}
    for section in list(getattr(document, "sections", None) or []):
        section_id = str(section.section_id)
        if section_id in confirmed:
            continue
        schema = _class_schema(config, getattr(section, "classification", None))
        failed = failed_extraction_issue(section)
        if failed is not None:
            message = _issue_field(failed, "message")
            reasons.append(
                ReviewReason(
                    kind="extraction_failed",
                    section_id=section_id,
                    field=None,
                    label=message if isinstance(message, str) and message else None,
                    score=0.0,
                    threshold=default_threshold,
                )
            )
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
        if route_missing:
            for missing in getattr(section, "missing_required_fields", None) or []:
                if not isinstance(missing, Mapping):
                    continue
                path = missing.get("attribute_name") or missing.get("attributeName")
                if not path:
                    continue
                threshold = _num(
                    missing.get(
                        "confidence_threshold", missing.get("confidenceThreshold")
                    )
                )
                reasons.append(
                    ReviewReason(
                        kind="missing_required",
                        section_id=section_id,
                        field=path,
                        label=field_label(schema, path),
                        score=0.0,
                        threshold=default_threshold if threshold is None else threshold,
                    )
                )
        if mode == "bda":
            confidence = _num(getattr(section, "confidence", None))
            threshold = hitl_threshold(config)
            unscored = confidence is None and bool(
                getattr(section, "blueprint_unscored", False)
            )
            # A matched blueprint with no usable confidence is held for review
            # at every threshold, as legacy HITL marks it (GitHub #1581).
            if unscored or (confidence is not None and confidence < threshold):
                reasons.append(
                    ReviewReason(
                        kind="blueprint_confidence",
                        section_id=section_id,
                        field=None,
                        label=None,
                        score=0.0 if confidence is None else confidence,
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


def count_confidence_reasons(
    document: Any,
    config: Any = None,
    *,
    mode: Literal["pipeline", "bda"],
    confirmed_sections: Collection[str] = (),
) -> int:
    """How many confidence reasons the document has: the alert count routing reads.

    The ``low_confidence`` and ``blueprint_confidence`` reasons ``build_reasons``
    gives, with one difference. A section's alert list can be a capped subset (the
    tracking row stores what fits, and the true figure beside it), so a section
    counts the larger of its low-confidence reasons and its
    ``confidence_alert_total``. Sections in ``confirmed_sections`` count nothing.
    """
    reasons = build_reasons(
        document, config, mode=mode, confirmed_sections=confirmed_sections
    )
    low_confidence = Counter(
        r.section_id for r in reasons if r.kind == "low_confidence"
    )
    total = sum(1 for r in reasons if r.kind == "blueprint_confidence")
    confirmed = {str(section_id) for section_id in confirmed_sections}
    for section in list(getattr(document, "sections", None) or []):
        section_id = str(section.section_id)
        if section_id in confirmed:
            continue
        stored = _num(getattr(section, "confidence_alert_total", None)) or 0
        total += max(low_confidence.get(section_id, 0), int(stored))
    return total


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
