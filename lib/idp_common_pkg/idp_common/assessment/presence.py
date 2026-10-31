# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Absent extracted values: kept out of confidence alerts, counted when required.

Two rules live here, and both read the EXTRACTED VALUES, which the alert builders
themselves never see (they are given the assessment and the class schema only):

1. **An absent value raises no confidence alert** (:func:`exclude_absent_value_alerts`).
   A confidence leaf whose own extracted value is absent — see
   :func:`has_extracted_value` — keeps its score and reason in
   ``explainability_info`` but is dropped from the section's
   ``confidence_threshold_alerts``. Everything downstream reads that list: the
   document's ``ConfidenceAlertCount``, the legacy HITL section trigger
   (``review_queues.routing.legacy_section_triggers``) and the ``low_confidence``
   review reasons (``review_queues.reasons``). So the one filter, applied where
   each mode stores its alerts, removes absence from all of them. This makes
   scalars behave the way list fields already did: an empty list keeps no
   confidence leaves (``reconcile_assessment_to_data``) and so never alerted.

2. **An absent REQUIRED field is counted separately**
   (:func:`collect_missing_required_fields`). The class schema's JSON Schema
   ``required`` arrays decide what is required, evaluated per section against that
   section's class schema. The result is a list in the same shape as a confidence
   alert, stored on the section as ``missing_required_fields``; its sum over
   sections is the document's ``MissingRequiredFieldCount``. Nothing routes on it
   yet — it is the input later review-queue logic needs.

The absence predicate is the one #1474 introduced (``_is_populated_value``: null,
blank strings and empty containers are absent), applied DEEPLY so a container
whose every member is absent is itself absent: ``{"State": None, "Zip": ""}`` is
a non-empty dict, but there is nothing in it for a score to be about, and a
required object extracted as that skeleton is missing, not present.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from idp_common import utils
from idp_common.assessment.batching import _is_populated_value

#: Bounds the schema walk. A class schema nests a handful of levels; this only
#: stops a pathological (cyclic after dereferencing) schema from recursing forever.
_MAX_SCHEMA_DEPTH = 32


def has_extracted_value(value: Any) -> bool:
    """True when ``value`` holds something extraction actually found.

    ``_is_populated_value`` applied recursively: null, a blank string and an
    empty container are absent, and so is a container all of whose members are.
    ``0``, ``False`` and any non-blank string are values.
    """
    if not _is_populated_value(value):
        return False
    if isinstance(value, dict):
        return any(has_extracted_value(v) for v in value.values())
    if isinstance(value, list):
        return any(has_extracted_value(v) for v in value)
    return True


def _child_value(value: Any, key: Any) -> Any:
    """The extracted value under ``key`` (a dict key or a list index).

    A populated non-container value under an assessment node that has children
    is a value the confidence model decomposed into sub-keyed fragments (one
    string scored as several parts). There is no per-fragment value to test, so
    the fragment inherits the whole value and counts as present — the
    conservative direction, which keeps today's alerts for that shape.
    """
    if isinstance(key, int):
        if isinstance(value, list):
            return value[key] if 0 <= key < len(value) else None
    elif isinstance(value, dict):
        return value.get(key)
    if isinstance(value, (dict, list)) or not _is_populated_value(value):
        return None
    return value


def iter_assessed_leaf_values(
    assessment: Any, extraction_results: Any
) -> Iterator[tuple[str, dict[str, Any], Any]]:
    """Yield ``(path, leaf, value)`` for every confidence leaf in ``assessment``.

    ``path`` is spelled exactly as the alert builders spell ``attribute_name``
    (``a.b``, ``rows[3].amount``), and ``value`` is the extracted value at that
    path (``None`` where the data has none). A leaf is a dict carrying a
    ``confidence`` key; the walk does not descend into one, matching
    ``enrich_assessment_with_thresholds``.
    """
    if not isinstance(assessment, dict):
        return

    def _walk(node: Any, value: Any, path: str):
        if isinstance(node, dict):
            if "confidence" in node:
                yield path, node, value
                return
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield from _walk(child, _child_value(value, key), child_path)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                yield from _walk(child, _child_value(value, index), f"{path}[{index}]")

    data = extraction_results if isinstance(extraction_results, dict) else {}
    for attr_name, node in assessment.items():
        yield from _walk(node, data.get(attr_name), str(attr_name))


def absent_value_paths(assessment: Any, extraction_results: Any) -> set[str]:
    """Paths of the confidence leaves whose extracted value is absent."""
    return {
        path
        for path, _leaf, value in iter_assessed_leaf_values(
            assessment, extraction_results
        )
        if not has_extracted_value(value)
    }


def _is_under(name: str, paths: set[str]) -> bool:
    """True when ``name`` is one of ``paths`` or lies beneath one of them.

    Beneath means the next character after the prefix is ``.`` or ``[``, so
    ``total`` does not cover ``total_due``. An alert can sit beneath a leaf when
    a builder reports a leaf's own sub-fields too (``AssessmentService
    ._check_confidence_alerts`` does); a value under an absent value is absent.
    """
    if name in paths:
        return True
    for position, char in enumerate(name):
        if char in ".[" and name[:position] in paths:
            return True
    return False


def exclude_absent_value_alerts(
    alerts: list[dict[str, Any]] | None,
    assessment: Any,
    extraction_results: Any,
) -> list[dict[str, Any]]:
    """``alerts`` without those raised on a leaf whose extracted value is absent.

    ``assessment`` is the section's final assessment (what is stored as
    ``explainability_info``) and ``extraction_results`` its ``inference_result``.
    When either is not a dict nothing can be judged and the list is returned
    unchanged, so a caller with no data never loses an alert by accident.
    """
    alerts = list(alerts or [])
    if not isinstance(assessment, dict) or not isinstance(extraction_results, dict):
        return alerts
    absent = absent_value_paths(assessment, extraction_results)
    return [
        alert
        for alert in alerts
        if not (
            isinstance(alert, dict)
            and isinstance(alert.get("attribute_name"), str)
            and _is_under(alert["attribute_name"], absent)
        )
    ]


def _leaf_scores(node: Any) -> tuple[float | None, float | None]:
    """``(confidence, confidence_threshold)`` of an assessment leaf, else Nones."""
    if not isinstance(node, dict) or "confidence" not in node:
        return None, None
    return (
        utils.finite_float(node.get("confidence")),
        utils.finite_float(node.get("confidence_threshold")),
    )


def collect_missing_required_fields(
    extraction_results: Any,
    class_schema: Any,
    assessment: Any = None,
) -> list[dict[str, Any]]:
    """The required fields this section's extraction left absent.

    The rule is JSON Schema ``required``, applied to the extracted values:

    - A property is required when the ``required`` array of the object schema
      that declares it names it. A name in ``required`` with no matching
      ``properties`` entry is still required.
    - A nested object's own ``required`` applies only when that object is
      present. An absent object that is itself required is ONE entry (the
      object's path), never one per child; an absent object that is not
      required contributes nothing.
    - An array's item schema ``required`` applies to every row the list holds
      (``items[2].amount``), including a row that is null or empty. An empty or
      absent list has no rows, so it contributes only its own entry, and only
      when the list itself is required.
    - A schema that declares no ``required`` anywhere yields nothing. That
      includes a multi-instance class: the synthesized wrapper's own
      ``required: ["instances"]`` is the transform's, not the author's, and is
      ignored; each record's ``required`` applies per instance row as above.

    "Absent" is :func:`has_extracted_value` (null, blank, empty, or a container of
    nothing but those). ``$ref`` is resolved against the class schema's ``$defs``.
    ``allOf``/``anyOf``/``oneOf`` and conditional schemas are not evaluated.

    Each entry is shaped like a confidence alert — ``attribute_name``,
    ``confidence``, ``confidence_threshold`` — with the last two read from the
    ``assessment`` leaf at the same path when there is one, else ``None``. That
    is so consumers can treat both lists alike; the score is informational and
    plays no part in whether a field is missing.
    """
    from idp_common.config.schema_constants import (
        SCHEMA_ITEMS,
        SCHEMA_PROPERTIES,
        SCHEMA_REQUIRED,
    )
    from idp_common.config.schema_utils import deref_schema
    from idp_common.schema.multi_instance import INSTANCES_KEY, is_wrapped

    if not isinstance(class_schema, dict) or not isinstance(extraction_results, dict):
        return []
    synthesized_required = {INSTANCES_KEY} if is_wrapped(class_schema) else set()

    missing: list[dict[str, Any]] = []

    def _walk_object(schema: Any, value: Any, assess: Any, path: str, depth: int):
        # ``schema`` arrives dereferenced: every caller (the root, _walk_value,
        # the row loop) resolves ``$ref`` before handing it over.
        if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
            return
        properties = schema.get(SCHEMA_PROPERTIES)
        properties = properties if isinstance(properties, dict) else {}
        required_raw = schema.get(SCHEMA_REQUIRED)
        required = (
            [name for name in required_raw if isinstance(name, str)]
            if isinstance(required_raw, list)
            else []
        )
        if depth == 0 and synthesized_required:
            required = [n for n in required if n not in synthesized_required]
        names = list(properties) + [n for n in required if n not in properties]
        obj = value if isinstance(value, dict) else {}
        assess_obj = assess if isinstance(assess, dict) else {}
        for name in names:
            child_path = f"{path}.{name}" if path else name
            child_value = obj.get(name)
            child_assess = assess_obj.get(name)
            # A list is present when it has rows, whatever they hold: the item
            # schema's ``required`` then applies to each row, so a row of nulls
            # reports its required fields rather than hiding inside an "absent"
            # list. Anything else is present when it holds a value.
            present = (
                bool(child_value)
                if isinstance(child_value, list)
                else has_extracted_value(child_value)
            )
            if not present:
                if name in required:
                    confidence, threshold = _leaf_scores(child_assess)
                    missing.append(
                        {
                            "attribute_name": child_path,
                            "confidence": confidence,
                            "confidence_threshold": threshold,
                        }
                    )
                continue
            _walk_value(
                properties.get(name), child_value, child_assess, child_path, depth + 1
            )

    def _walk_value(schema: Any, value: Any, assess: Any, path: str, depth: int):
        if depth > _MAX_SCHEMA_DEPTH:
            return
        schema = deref_schema(schema, class_schema)
        if isinstance(value, dict):
            _walk_object(schema, value, assess, path, depth)
        elif isinstance(value, list):
            items = deref_schema(schema.get(SCHEMA_ITEMS), class_schema)
            if not items:
                return
            for index, row in enumerate(value):
                row_assess = (
                    assess[index]
                    if isinstance(assess, list) and index < len(assess)
                    else None
                )
                row_path = f"{path}[{index}]"
                if isinstance(row, (dict, list)) or SCHEMA_PROPERTIES in items:
                    # A row that exists but is null or empty is a row with every
                    # field absent, so its required fields are each missing.
                    if isinstance(row, list):
                        _walk_value(items, row, row_assess, row_path, depth + 1)
                    else:
                        _walk_object(items, row, row_assess, row_path, depth + 1)

    _walk_object(class_schema, extraction_results, assessment, "", 0)
    return missing
