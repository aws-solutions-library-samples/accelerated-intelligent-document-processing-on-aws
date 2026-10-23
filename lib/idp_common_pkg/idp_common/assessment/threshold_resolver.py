# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolve per-sub-field confidence thresholds for array items using $ref/$defs.

When a JSON Schema array property uses ``$ref`` to reference item definitions in
``$defs``, the individual sub-field thresholds (``x-aws-idp-confidence-threshold``)
live inside the referenced definition — not on the array property itself. This
module provides a single utility to resolve the reference and build a
``{field_name: threshold}`` lookup dict that the assessment enrichment paths use
to apply per-sub-field thresholds to list item assessments.

Import-light (no boto3/S3/Bedrock) so it can be used from both the standalone
assessment service and the BDA processresults function.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_array_item_thresholds(
    prop_schema: dict[str, Any],
    class_schema: dict[str, Any],
    default_threshold: float,
) -> dict[str, float]:
    """Resolve per-sub-field confidence thresholds for an array property's items.

    Given the schema of an array property (``prop_schema``) and the full class
    schema (which contains ``$defs``), this function:

    1. Retrieves the ``items`` schema (inline or via ``$ref`` → ``$defs``).
    2. Iterates over the item's ``properties`` to extract each sub-field's
       ``x-aws-idp-confidence-threshold``.
    3. Returns a dict ``{sub_field_name: threshold}`` — fields without an
       explicit threshold are mapped to ``default_threshold``.

    If the items schema cannot be resolved (missing ``$ref`` target, no
    ``properties``, etc.), returns an empty dict — the caller should fall back
    to applying ``default_threshold`` uniformly.

    Args:
        prop_schema: The JSON Schema dict of the array property (contains
            ``type: "array"`` and ``items``).
        class_schema: The full document-class JSON Schema (contains ``$defs``).
        default_threshold: Fallback threshold for sub-fields without an explicit
            ``x-aws-idp-confidence-threshold``.

    Returns:
        Dict mapping sub-field names to their confidence thresholds. Empty if
        the item schema cannot be resolved or has no properties.
    """
    from idp_common.config.schema_constants import (
        DEFS_FIELD,
        REF_FIELD,
        SCHEMA_ITEMS,
        SCHEMA_PROPERTIES,
        X_AWS_IDP_CONFIDENCE_THRESHOLD,
    )

    items_schema = (prop_schema or {}).get(SCHEMA_ITEMS, {})
    if not items_schema:
        return {}

    # Resolve $ref → $defs if present
    if REF_FIELD in items_schema:
        ref_path = items_schema[REF_FIELD]  # e.g. "#/$defs/W2CopyItem"
        def_name = ref_path.split("/")[-1]
        defs = (class_schema or {}).get(DEFS_FIELD, {})
        resolved = defs.get(def_name)
        if resolved:
            logger.debug(
                "Resolved $ref '%s' → '%s' for array item threshold lookup",
                ref_path,
                def_name,
            )
            items_schema = resolved
        else:
            logger.warning(
                "Could not resolve $ref '%s': definition '%s' not found in $defs. "
                "Using default threshold for all sub-fields.",
                ref_path,
                def_name,
            )
            return {}

    # Extract per-sub-field thresholds from the resolved item properties
    item_properties = items_schema.get(SCHEMA_PROPERTIES, {})
    if not item_properties:
        return {}

    thresholds: dict[str, float] = {}
    for field_name, field_schema in item_properties.items():
        raw_threshold = field_schema.get(X_AWS_IDP_CONFIDENCE_THRESHOLD)
        if raw_threshold is not None:
            try:
                thresholds[field_name] = float(raw_threshold)
            except (ValueError, TypeError):
                thresholds[field_name] = default_threshold
        else:
            thresholds[field_name] = default_threshold

    return thresholds


def get_threshold_for_field(
    field_name: str,
    item_thresholds: dict[str, float],
    default_threshold: float,
) -> float:
    """Look up the threshold for a specific sub-field, with fallback.

    Args:
        field_name: The sub-field name within a list item.
        item_thresholds: Dict from :func:`resolve_array_item_thresholds`.
        default_threshold: Fallback if the field isn't in the map.

    Returns:
        The resolved threshold for the field.
    """
    return item_thresholds.get(field_name, default_threshold)


def _deref(schema: dict[str, Any], class_schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve a ``$ref`` against the class schema's ``$defs`` (one hop)."""
    from idp_common.config.schema_constants import DEFS_FIELD, REF_FIELD

    if not isinstance(schema, dict) or REF_FIELD not in schema:
        return schema if isinstance(schema, dict) else {}
    def_name = schema[REF_FIELD].split("/")[-1]
    resolved = (class_schema or {}).get(DEFS_FIELD, {}).get(def_name)
    if not resolved:
        logger.warning("Could not resolve $ref '%s' against $defs", schema[REF_FIELD])
        return {}
    return resolved


def resolve_threshold_for_path(
    key_path: list[Any],
    class_schema: dict[str, Any],
    default_threshold: float,
) -> float:
    """Resolve a field's confidence threshold by walking its path in the schema.

    Handles arbitrary nesting: object groups, arrays (including ``$ref`` →
    ``$defs`` item schemas), and nested arrays. Array index markers in the path
    are skipped when descending (an index selects an element of ``items``, not a
    named property).

    Index markers are recognized as either integers or strings of the form
    ``"_0"``, ``"_12"`` — the convention used by the BDA explainability
    traversal.

    Resolution order matches the array-item helper: the field's own
    ``x-aws-idp-confidence-threshold`` wins; otherwise the nearest ancestor
    container that declares one (e.g. the array attribute itself); otherwise
    ``default_threshold``.

    Args:
        key_path: Path parts from the root of the class schema, e.g.
            ``["w2_copies", "_0", "w2_box_a_employee_ssn"]``.
        class_schema: The full document-class JSON Schema (contains ``$defs``).
        default_threshold: Returned when the path can't be resolved or neither the
            field nor any ancestor declares a threshold.

    Returns:
        The resolved threshold for the field at ``key_path``.
    """
    from idp_common.config.schema_constants import (
        SCHEMA_ITEMS,
        SCHEMA_PROPERTIES,
        X_AWS_IDP_CONFIDENCE_THRESHOLD,
    )

    def _is_index(part: Any) -> bool:
        if isinstance(part, int):
            return True
        return isinstance(part, str) and part.startswith("_") and part[1:].isdigit()

    def _own(node: dict[str, Any]) -> float | None:
        raw = (node or {}).get(X_AWS_IDP_CONFIDENCE_THRESHOLD)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    current = class_schema or {}
    # Nearest ancestor threshold seen so far; falls back to the caller's default.
    inherited = default_threshold

    for part in key_path or []:
        if _is_index(part):
            # Descend into the array's item schema
            current = _deref(current.get(SCHEMA_ITEMS, {}), class_schema)
            if not current:
                return inherited
            continue

        # Named property: an array/group container may need dereferencing first
        current = _deref(current, class_schema)
        props = current.get(SCHEMA_PROPERTIES, {})
        if part not in props:
            # Some paths point at an array container whose items hold the
            # property (BDA omits the index for single-element lists).
            items = _deref(current.get(SCHEMA_ITEMS, {}), class_schema)
            props = items.get(SCHEMA_PROPERTIES, {})
            if part not in props:
                return inherited
        current = props[part]
        # A container that declares its own threshold becomes the new fallback
        # for everything beneath it (matches resolve_array_item_thresholds).
        container_threshold = _own(current)
        if container_threshold is not None:
            inherited = container_threshold

    current = _deref(current, class_schema)
    own = _own(current)
    return own if own is not None else inherited
