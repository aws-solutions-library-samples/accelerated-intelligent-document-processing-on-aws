# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
1S-TopK candidate resolution: single-step extraction + confidence.

When the extraction prompt instructs the LLM to return its top-K guesses with
probabilities per field (``G1/P1``, ``G2/P2`` … ``GK/PK``), this module resolves
that structure into the standard ``inference_result`` (the top guess ``G1``) plus
raw per-field confidence leaves (``P1``). The confidence leaves are handed to the
shared :func:`idp_common.assessment.batching.enrich_assessment_with_thresholds`,
which attaches ``confidence_threshold`` and builds alerts — exactly like every
other confidence path — so the downstream Assessment Lambda auto-skips (it skips
whenever ``explainability_info`` is already present) and the UI/HITL/evaluation
consumers work unchanged.

Requesting K candidates (rather than a single value + a single confidence number)
yields better-calibrated, less-overconfident scores: forcing the model to
enumerate and rank alternatives makes it distribute probability mass instead of
defaulting to ~1.0. See Tian et al., "Just Ask for Calibration" (EMNLP 2023).

Used by the non-agentic (simple mode) integrated confidence path; the resolver is
also reused by the agentic ``integrated_confidence_strategy: topk`` path.

The number of candidates (K) is flexible — ``G1``/``P1`` alone is valid. Only
``G1``/``P1`` are consumed here; ``G2..GK``/``P2..PK`` are preserved verbatim in
the returned candidates metadata for auditability.

When ``geometry.mode`` is ``llm`` or ``llm_grounded`` the extraction prompt also
appends the bounding-box block, so the model may emit ``bbox``/``page`` alongside
the candidates. Those keys are carried through onto the confidence leaf (exactly
as the agentic ``provide_field_assessment`` tool preserves them), so the shared
grounding path can consume them — under pure ``llm`` geometry (no OCR grounding)
this is the only source of a box.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Optional geometry keys the model emits alongside candidates when the bbox block
# is appended to the prompt (geometry.mode in {llm, llm_grounded}). Carried onto
# the confidence leaf so the shared grounding path can consume them.
_GEOMETRY_KEYS = ("bbox", "page")


def _geometry_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Extract any ``bbox``/``page`` the model emitted alongside a candidate."""
    return {k: candidate[k] for k in _GEOMETRY_KEYS if k in candidate}


def is_topk_response(extracted_fields: dict[str, Any]) -> bool:
    """Detect whether an LLM response is in TopK candidate format (``G1``/``P1``).

    True if any top-level field is a ``{G1, P1, ...}`` candidate object, or any
    array item's sub-attribute is one. Cheap structural check used to route the
    response to :func:`resolve_candidates`.
    """
    for value in extracted_fields.values():
        if isinstance(value, dict) and "G1" in value and "P1" in value:
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for sub_val in item.values():
                        if (
                            isinstance(sub_val, dict)
                            and "G1" in sub_val
                            and "P1" in sub_val
                        ):
                            return True
    return False


def resolve_candidates(
    raw_result: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Resolve top-K candidate guesses into values + raw confidence + candidates.

    Takes the top guess (``G1``) as the extracted value and its probability
    (``P1``) as the confidence. Handles candidates nested inside array items
    (e.g. ``LineItems``). Supports two LLM output shapes for arrays:

      1. Wrapped:  ``{"LineItems": {"G1": [...], "P1": 1.0, ...}}``
      2. Direct:   ``{"LineItems": [{sub_attr: {G1, P1, ...}, ...}, ...]}``

    Confidence leaves are emitted WITHOUT ``confidence_threshold`` — thresholds
    and alerts are attached by the shared
    :func:`idp_common.assessment.batching.enrich_assessment_with_thresholds` so
    there is a single source of truth for thresholds across all confidence modes.
    Any ``bbox``/``page`` the model emitted alongside a candidate (LLM-box
    geometry modes) is carried through onto the leaf for the grounding path.

    Args:
        raw_result: the parsed LLM response (field -> candidate object / list).
        schema: the class JSON schema (used only for numeric coercion of values).

    Returns:
        ``(inference_result, assessment_data, candidates_metadata)`` where
        ``inference_result`` holds the resolved values, ``assessment_data`` holds
        raw ``{"confidence": P1}`` leaves mirroring the values, and
        ``candidates_metadata`` preserves the full candidate objects for audit.
    """
    inference_result: dict[str, Any] = {}
    assessment_data: dict[str, Any] = {}
    candidates_metadata: dict[str, Any] = {}
    props = schema.get("properties", {})
    defs = schema.get("$defs", {})

    for attr_name, value in raw_result.items():
        attr_schema = _deref(props.get(attr_name, {}), defs)

        # Resolve item schema for arrays (deref $ref against $defs).
        item_schema: dict[str, Any] = {}
        if attr_schema.get("type") == "array":
            item_schema = _deref(attr_schema.get("items", {}), defs)

        # Case 1: Wrapped candidate — {"G1": <value>, "P1": <prob>, ...}
        if isinstance(value, dict) and "G1" in value:
            inner = value.get("G1")

            if attr_schema.get("type") == "array" and isinstance(inner, list):
                resolved_items, item_assessments = _resolve_list_items(
                    inner, item_schema
                )
                inference_result[attr_name] = resolved_items
                assessment_data[attr_name] = item_assessments
            else:
                inference_result[attr_name] = _coerce_value(inner, attr_schema)
                assessment_data[attr_name] = {
                    "confidence": _safe_prob(value.get("P1", 0.0)),
                    **_geometry_from_candidate(value),
                }
            candidates_metadata[attr_name] = value

        # Case 2: Direct array — [{sub_attr: {G1, P1, ...}, ...}, ...]
        elif isinstance(value, list) and attr_schema.get("type") == "array":
            resolved_items, item_assessments = _resolve_list_items(
                value, item_schema, defs
            )
            inference_result[attr_name] = resolved_items
            assessment_data[attr_name] = item_assessments
            candidates_metadata[attr_name] = value

        # Case 3: GROUP (object) field whose SUB-ATTRIBUTES are candidates —
        # {"Address": {"City": {G1, P1, ...}, "State": {G1, P1, ...}}}.
        #
        # This case used to fall through to the pass-through below, which stored
        # the raw candidate dicts AS THE EXTRACTED VALUE: a group's
        # `City` came out as `{"G1": "Anytown", "P1": 0.95, ...}` instead of
        # `"Anytown"`, and the group produced NO confidence leaves at all, so its
        # fields were also invisible to threshold alerts and HITL. Observed live
        # on every group field of every document processed in integrated mode.
        elif isinstance(value, dict) and _looks_like_candidate_group(value):
            resolved_group, group_assess = _resolve_group(value, attr_schema, defs)
            inference_result[attr_name] = resolved_group
            assessment_data[attr_name] = group_assess
            candidates_metadata[attr_name] = value

        # Case 4: Non-candidate value (the model ignored TopK for this field) —
        # pass the value through with no confidence leaf.
        else:
            inference_result[attr_name] = value

    return inference_result, assessment_data, candidates_metadata


def _deref(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Resolve a one-level ``$ref`` against ``$defs``; return the schema as-is otherwise."""
    if isinstance(schema, dict) and "$ref" in schema:
        def_name = schema["$ref"].split("/")[-1]
        return defs.get(def_name, schema)
    return schema if isinstance(schema, dict) else {}


def _looks_like_candidate_group(value: dict[str, Any], _depth: int = 0) -> bool:
    """True if this object contains a ``{G1, P1, ...}`` candidate at any depth.

    Structural rather than schema-driven on purpose: a group can arrive without a
    usable schema entry (auto-generated schemas, ``additionalProperties``), and
    the consequence of guessing wrong is symmetric — either way the value falls
    through to pass-through, which is the previous behaviour.

    Recurses so a group nested inside a group is still recognised; depth-bounded
    purely as a guard against a pathological payload.
    """
    if _depth > 8:
        return False
    for sub in value.values():
        if isinstance(sub, dict):
            if "G1" in sub and "P1" in sub:
                return True
            if _looks_like_candidate_group(sub, _depth + 1):
                return True
        elif isinstance(sub, list):
            for item in sub:
                if isinstance(item, dict) and _looks_like_candidate_group(
                    item, _depth + 1
                ):
                    return True
    return False


def _resolve_group(
    group: dict[str, Any], attr_schema: dict[str, Any], defs: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve a group (object) field whose sub-attributes are candidates.

    Recurses, so a group nested inside a group also resolves, and a list nested
    inside a group is handled by the list resolver.
    """
    group_props = _deref(attr_schema, defs).get("properties", {})
    resolved: dict[str, Any] = {}
    assess: dict[str, Any] = {}
    for key, val in group.items():
        sub_schema = _deref(group_props.get(key, {}), defs)
        if isinstance(val, dict) and "G1" in val:
            inner = val.get("G1")
            if sub_schema.get("type") == "array" and isinstance(inner, list):
                items, item_assess = _resolve_list_items(
                    inner, _deref(sub_schema.get("items", {}), defs), defs
                )
                resolved[key] = items
                assess[key] = item_assess
            else:
                resolved[key] = _coerce_value(inner, sub_schema)
                assess[key] = {
                    "confidence": _safe_prob(val.get("P1", 0.0)),
                    **_geometry_from_candidate(val),
                }
        elif isinstance(val, list) and sub_schema.get("type") == "array":
            items, item_assess = _resolve_list_items(
                val, _deref(sub_schema.get("items", {}), defs), defs
            )
            resolved[key] = items
            assess[key] = item_assess
        elif isinstance(val, dict) and _looks_like_candidate_group(val):
            sub_resolved, sub_assess = _resolve_group(val, sub_schema, defs)
            resolved[key] = sub_resolved
            assess[key] = sub_assess
        else:
            resolved[key] = val
    return resolved, assess


def _resolve_list_items(
    items_list: list, item_schema: dict[str, Any], defs: dict[str, Any] | None = None
) -> tuple[list[dict], list[dict]]:
    """Resolve candidate dicts within array items into values + confidence leaves.

    Returns ``(resolved_items, item_assessments)`` index-aligned with the input,
    each item assessment being a per-sub-attribute ``{"confidence": P1}`` map.
    """
    defs = defs or {}
    resolved_items: list[dict] = []
    item_assessments: list[dict] = []
    item_props = _deref(item_schema, defs).get("properties", {})

    for item in items_list:
        if not isinstance(item, dict):
            resolved_items.append(item)
            item_assessments.append({})
            continue
        resolved_item: dict[str, Any] = {}
        item_assess: dict[str, Any] = {}
        for key, val in item.items():
            sub_schema = _deref(item_props.get(key, {}), defs)
            if isinstance(val, dict) and "G1" in val:
                resolved_item[key] = _coerce_value(val.get("G1"), sub_schema)
                item_assess[key] = {
                    "confidence": _safe_prob(val.get("P1", 0.0)),
                    **_geometry_from_candidate(val),
                }
            elif isinstance(val, dict) and _looks_like_candidate_group(val):
                # A group nested inside a list row.
                sub_resolved, sub_assess = _resolve_group(val, sub_schema, defs)
                resolved_item[key] = sub_resolved
                item_assess[key] = sub_assess
            else:
                resolved_item[key] = val
        resolved_items.append(resolved_item)
        item_assessments.append(item_assess)
    return resolved_items, item_assessments


def _coerce_value(v: Any, schema: dict[str, Any]) -> Any:
    """Coerce a ``number``-typed candidate value to float; leave others as-is."""
    if v is not None and schema.get("type") == "number":
        try:
            return float(v)
        except (ValueError, TypeError):
            return v
    return v


def _safe_prob(v: Any) -> float:
    """Convert a probability to a float clamped to [0, 1] (0.0 on failure)."""
    try:
        f = float(v) if v is not None else 0.0
        return max(0.0, min(1.0, f))
    except (ValueError, TypeError):
        return 0.0
