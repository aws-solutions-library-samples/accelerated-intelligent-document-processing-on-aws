# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Stickler Configuration Mapper.

This module provides mapping between IDP evaluation configuration
(using x-aws-idp-evaluation-* extensions) and Stickler configuration format.

The mapper acts as an abstraction layer, allowing the IDP system to use
neutral evaluation configuration that can be translated to Stickler's format.
"""

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Set

from idp_common.config.schema_constants import (
    EVALUATION_METHOD_DATE,
    EVALUATION_METHOD_EXACT,
    EVALUATION_METHOD_FUZZY,
    EVALUATION_METHOD_HUNGARIAN,
    EVALUATION_METHOD_LEVENSHTEIN,
    EVALUATION_METHOD_LLM,
    EVALUATION_METHOD_NUMERIC_EXACT,
    EVALUATION_METHOD_SEMANTIC,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EVALUATION_LLM_IN_LIST,
    X_AWS_IDP_EVALUATION_MATCH_THRESHOLD,
    X_AWS_IDP_EVALUATION_METHOD,
    X_AWS_IDP_EVALUATION_METHOD_CONFIG,
    X_AWS_IDP_EVALUATION_MODEL_NAME,
    X_AWS_IDP_EVALUATION_THRESHOLD,
    X_AWS_IDP_EVALUATION_WEIGHT,
)

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any) -> bool:
    """Coerce a schema value to a boolean, treating YAML-quoted ``"false"``
    / ``"no"`` / ``"off"`` (case-insensitive) as False.

    Raw Python ``bool()`` returns True for any non-empty string, so a
    YAML config with ``x-aws-idp-evaluation-llm-in-list: "false"``
    would evaluate truthy under ``bool()`` and bypass truthiness
    guards. This helper honors the strings the way YAML would if
    the field were unquoted.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "false", "no", "off", "0", "n"}
    return bool(value)


class SticklerConfigMapper:
    """
    Maps IDP evaluation configuration to Stickler configuration.

    This mapper provides a backend-agnostic abstraction layer that translates
    neutral IDP evaluation extensions into Stickler-specific format.

    Note: LLM comparator configuration is handled via global config in
    llm_comparator.py, not through schema extensions.
    """

    # Mapping from IDP evaluation methods to Stickler comparator class names
    METHOD_TO_COMPARATOR = {
        EVALUATION_METHOD_EXACT: "ExactComparator",
        EVALUATION_METHOD_NUMERIC_EXACT: "NumericComparator",
        EVALUATION_METHOD_FUZZY: "FuzzyComparator",
        EVALUATION_METHOD_LEVENSHTEIN: "LevenshteinComparator",
        EVALUATION_METHOD_SEMANTIC: "SemanticComparator",
        EVALUATION_METHOD_DATE: "DateComparator",  # Stickler v0.5.0+
        EVALUATION_METHOD_LLM: "IDPLLMComparator",  # IDP's Bedrock-backed comparator
        EVALUATION_METHOD_HUNGARIAN: None,  # Built-in for arrays
    }

    # R11: the legacy ``map_field_config`` / ``_map_type`` / ``_resolve_ref`` /
    # ``JSON_SCHEMA_TO_STICKLER_TYPE`` fields-config path that used to live
    # here (~200 lines) was deleted. Production only calls
    # ``build_stickler_model_config`` → ``_translate_extensions_in_schema`` →
    # Stickler's ``JsonSchemaFieldConverter``. NUMERIC_EXACT tolerance
    # handling — the one behavior the legacy path did *better* — was lifted
    # into the production path in Phase 1 (see ``_translate_extensions_in_schema``).

    @classmethod
    def _is_unevaluable_object(cls, schema: Dict[str, Any]) -> bool:
        """
        Check if an object schema cannot be evaluated by Stickler.

        Stickler requires object types to have a 'properties' key with at least
        one field. Objects that are free-form (using additionalProperties without
        defined properties) or have empty properties cannot be evaluated.

        Args:
            schema: JSON Schema dict to check

        Returns:
            True if the object schema cannot be evaluated by Stickler
        """
        if not isinstance(schema, dict):
            return False
        if schema.get(SCHEMA_TYPE) != TYPE_OBJECT:
            return False

        prop_properties = schema.get(SCHEMA_PROPERTIES)

        # Case 1: No 'properties' key at all (free-form object, e.g. additionalProperties: true)
        if prop_properties is None:
            return True

        # Case 2: Empty properties dict
        if isinstance(prop_properties, dict) and len(prop_properties) == 0:
            return True

        return False

    @classmethod
    def _is_unevaluable_array(cls, schema: Dict[str, Any]) -> bool:
        """
        Check if an array schema cannot be evaluated by Stickler.

        Stickler's JsonSchemaFieldConverter needs an 'items' schema with a usable
        'type' to build a typed list. genson emits a bare ``{"type": "array"}``
        (no 'items') for empty lists in the baseline data, and an array whose
        items are an unevaluable object cannot be evaluated either. Both crash the
        converter with "Unsupported JSON Schema type: None".

        Args:
            schema: JSON Schema dict to check

        Returns:
            True if the array schema cannot be evaluated by Stickler
        """
        if not isinstance(schema, dict):
            return False
        if schema.get(SCHEMA_TYPE) != TYPE_ARRAY:
            return False

        items_schema = schema.get(SCHEMA_ITEMS)

        # Case 1: No 'items' schema at all (e.g. genson output for an empty list)
        if not isinstance(items_schema, dict):
            return True

        # Case 2: Items resolve to an unevaluable object (free-form / no properties)
        if cls._is_unevaluable_object(items_schema):
            return True

        # Case 3: Items have no usable 'type' and no $ref to resolve
        if items_schema.get(SCHEMA_TYPE) is None and "$ref" not in items_schema:
            return True

        return False

    @classmethod
    def _remove_empty_object_properties(
        cls, schema: Dict[str, Any], field_path: str = ""
    ) -> List[str]:
        """
        Recursively remove properties that are objects without evaluable properties.

        Stickler's ModelFactory requires at least one field to create a Pydantic model.
        This method removes:
        - Object properties with empty properties (e.g., {"type": "object", "properties": {}})
        - Object properties with NO properties key (free-form objects, e.g.,
          {"type": "object", "additionalProperties": true})
        - Array properties whose items are unevaluable objects (e.g.,
          {"type": "array", "items": {"type": "object", "additionalProperties": true}})

        These cause validation errors in Stickler and provide no evaluation value.

        Args:
            schema: Schema to process (modified in-place)
            field_path: Current path for logging

        Returns:
            List of removed property paths for logging
        """
        removed: List[str] = []

        if not isinstance(schema, dict):
            return removed

        if SCHEMA_PROPERTIES in schema:
            props_to_remove = []

            for prop_name, prop_schema in schema[SCHEMA_PROPERTIES].items():
                prop_path = f"{field_path}.{prop_name}" if field_path else prop_name

                if not isinstance(prop_schema, dict):
                    continue

                prop_type = prop_schema.get(SCHEMA_TYPE)
                prop_properties = prop_schema.get(SCHEMA_PROPERTIES)

                if prop_type == TYPE_OBJECT:
                    if cls._is_unevaluable_object(prop_schema):
                        # Object with no properties or empty properties - remove it
                        props_to_remove.append(prop_name)
                        removed.append(prop_path)
                        has_additional = "additionalProperties" in prop_schema
                        logger.info(
                            f"Removing unevaluable object property '{prop_path}' - "
                            f"Stickler requires at least one field in nested objects"
                            f"{' (has additionalProperties but no defined properties)' if has_additional else ''}"
                        )
                    elif prop_properties is not None:
                        # Recurse into non-empty objects FIRST, then re-check: removing
                        # unevaluable children (e.g. empty arrays) can leave the parent
                        # with no properties, which Stickler also cannot evaluate.
                        removed.extend(
                            cls._remove_empty_object_properties(prop_schema, prop_path)
                        )
                        if cls._is_unevaluable_object(prop_schema):
                            props_to_remove.append(prop_name)
                            removed.append(prop_path)
                            logger.info(
                                f"Removing object property '{prop_path}' - "
                                f"became empty after removing unevaluable children"
                            )

                # Check arrays - remove if items are missing or unevaluable
                elif prop_type == TYPE_ARRAY:
                    if cls._is_unevaluable_array(prop_schema):
                        # Array with no usable 'items' schema (e.g. empty list in
                        # baseline) or free-form object items - can't be evaluated
                        props_to_remove.append(prop_name)
                        removed.append(prop_path)
                        logger.info(
                            f"Removing array property '{prop_path}' - "
                            f"array has no evaluable 'items' schema "
                            f"(missing items or free-form objects), "
                            f"which Stickler cannot evaluate"
                        )
                    else:
                        # Recurse into object items FIRST, then re-check: the items
                        # object can become empty after its children are removed.
                        items_schema = prop_schema[SCHEMA_ITEMS]
                        removed.extend(
                            cls._remove_empty_object_properties(
                                items_schema, f"{prop_path}[]"
                            )
                        )
                        if cls._is_unevaluable_array(prop_schema):
                            props_to_remove.append(prop_name)
                            removed.append(prop_path)
                            logger.info(
                                f"Removing array property '{prop_path}' - "
                                f"items became unevaluable after removing children"
                            )

            # Remove unevaluable properties
            for prop_name in props_to_remove:
                del schema[SCHEMA_PROPERTIES][prop_name]

        # Check array items at this level too
        if SCHEMA_ITEMS in schema:
            removed.extend(
                cls._remove_empty_object_properties(
                    schema[SCHEMA_ITEMS], f"{field_path}[]"
                )
            )

        # Process $defs as well
        if "$defs" in schema:
            for def_name, def_schema in list(schema["$defs"].items()):
                # Check if the definition itself is an unevaluable object
                if isinstance(def_schema, dict) and cls._is_unevaluable_object(
                    def_schema
                ):
                    has_additional = "additionalProperties" in def_schema
                    logger.info(
                        f"Removing unevaluable $defs entry '{def_name}' - "
                        f"Stickler requires at least one field in nested objects"
                        f"{' (has additionalProperties but no defined properties)' if has_additional else ''}"
                    )
                    del schema["$defs"][def_name]
                    removed.append(f"$defs.{def_name}")
                else:
                    removed.extend(
                        cls._remove_empty_object_properties(
                            def_schema, f"$defs.{def_name}"
                        )
                    )

        return removed

    @classmethod
    def _inline_refs(
        cls,
        schema: Dict[str, Any],
        defs: Dict[str, Any],
        visited: Optional[Set[str]] = None,
        field_path: str = "",
    ) -> Dict[str, Any]:
        """
        Recursively inline all $ref references in the schema.

        This replaces all #/$defs/XXX references with the actual definition content,
        allowing Stickler's JsonSchemaFieldConverter to process nested schemas
        without losing $defs context.

        Handles circular references by tracking visited definitions.

        UPSTREAM: candidate for `awslabs/stickler` — Stickler's
        `JsonSchemaFieldConverter._handle_array_type` doesn't propagate the root
        `$defs` into nested item schemas the way `_handle_nested_object` does,
        so nested `$ref` resolution silently drops context. When upstream
        supports root-`$defs` propagation in array items, delete this method.
        No open issue yet — file one if this branch survives beyond one Stickler
        upgrade.

        Args:
            schema: Schema or sub-schema to process
            defs: The $defs dictionary from the root schema
            visited: Set of definition names already being processed (for circular ref detection)
            field_path: Current path for logging

        Returns:
            Schema with all $ref references inlined
        """
        if visited is None:
            visited = set()

        if not isinstance(schema, dict):
            return schema

        # Handle $ref - inline the referenced definition
        if "$ref" in schema:
            ref_path = schema["$ref"]
            if ref_path.startswith("#/$defs/"):
                def_name = ref_path[8:]  # Remove "#/$defs/"
                if def_name in defs:
                    if def_name in visited:
                        # Circular reference detected - keep $ref to avoid infinite loop
                        logger.warning(
                            f"Circular $ref detected at '{field_path}': {ref_path}. Keeping reference."
                        )
                        return schema

                    # Mark as being processed
                    visited.add(def_name)

                    # Deep copy and recursively inline the definition
                    inlined = copy.deepcopy(defs[def_name])
                    inlined = cls._inline_refs(
                        inlined, defs, visited.copy(), f"{field_path}({def_name})"
                    )

                    # Merge any other properties from the original schema (like description)
                    # onto the inlined definition (original props take precedence)
                    for key, value in schema.items():
                        if key != "$ref" and key not in inlined:
                            inlined[key] = value

                    logger.debug(f"Inlined $ref '{ref_path}' at '{field_path}'")
                    return inlined
                else:
                    logger.warning(
                        f"Cannot inline $ref '{ref_path}' at '{field_path}': "
                        f"definition not found in $defs. Available: {list(defs.keys())}"
                    )

        # Recursively process properties
        if SCHEMA_PROPERTIES in schema:
            schema[SCHEMA_PROPERTIES] = {
                k: cls._inline_refs(v, defs, visited.copy(), f"{field_path}.{k}")
                for k, v in schema[SCHEMA_PROPERTIES].items()
            }

        # Recursively process array items
        if SCHEMA_ITEMS in schema:
            schema[SCHEMA_ITEMS] = cls._inline_refs(
                schema[SCHEMA_ITEMS], defs, visited.copy(), f"{field_path}[]"
            )

        # Process allOf, anyOf, oneOf
        for keyword in ["allOf", "anyOf", "oneOf"]:
            if keyword in schema:
                schema[keyword] = [
                    cls._inline_refs(
                        item, defs, visited.copy(), f"{field_path}.{keyword}[{i}]"
                    )
                    for i, item in enumerate(schema[keyword])
                ]

        # Process additionalProperties if it's a schema
        if "additionalProperties" in schema and isinstance(
            schema["additionalProperties"], dict
        ):
            schema["additionalProperties"] = cls._inline_refs(
                schema["additionalProperties"],
                defs,
                visited.copy(),
                f"{field_path}.additionalProperties",
            )

        return schema

    @classmethod
    def _validate_method_for_field(
        cls, schema: Dict[str, Any], method: str, field_path: str = ""
    ) -> None:
        """
        Validate that evaluation method is appropriate for the field type.

        Raises:
            ValueError: If method is incompatible with field type
        """
        field_type = schema.get(SCHEMA_TYPE)
        is_array = field_type == TYPE_ARRAY

        # Check if this is a structured array (array of objects)
        is_structured_array = False
        if is_array and SCHEMA_ITEMS in schema:
            items = schema[SCHEMA_ITEMS]
            if isinstance(items, dict):
                is_structured_array = (
                    items.get(SCHEMA_TYPE) == TYPE_OBJECT or "$ref" in items
                )

        # Validate HUNGARIAN - only for structured arrays
        if method == EVALUATION_METHOD_HUNGARIAN:
            if not is_structured_array:
                raise ValueError(
                    f"Field '{field_path}': HUNGARIAN method requires List[Object] (structured array). "
                    f"Field has type '{field_type}' with items type '{schema.get(SCHEMA_ITEMS, {}).get(SCHEMA_TYPE, 'N/A')}'"
                )

        # Validate other methods - should NOT be used on structured arrays
        elif method in [
            EVALUATION_METHOD_EXACT,
            EVALUATION_METHOD_FUZZY,
            EVALUATION_METHOD_LEVENSHTEIN,
            EVALUATION_METHOD_SEMANTIC,
            EVALUATION_METHOD_NUMERIC_EXACT,
            EVALUATION_METHOD_DATE,
        ]:
            if is_structured_array:
                raise ValueError(
                    f"Field '{field_path}': {method} cannot be used on List[Object]. "
                    f"Use HUNGARIAN for structured array matching."
                )

    @classmethod
    def _coerce_to_float(cls, value: Any, field_name: str = "") -> float:
        """
        Coerce value to float, handling strings and ints.

        Args:
            value: Value to coerce
            field_name: Field name for error messages

        Returns:
            Float value

        Raises:
            ValueError: If value cannot be converted to float
        """
        if isinstance(value, float):
            return value
        if isinstance(value, (str, int)):
            try:
                return float(value)
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Field '{field_name}': Cannot convert '{value}' to float: {e}"
                )
        raise ValueError(
            f"Field '{field_name}': Expected numeric value, got {type(value).__name__}"
        )

    @classmethod
    def _coerce_json_schema_types(
        cls, schema: Dict[str, Any], field_path: str = ""
    ) -> None:
        """
        Coerce string values to proper JSON Schema types.

        This fixes common issues where numeric constraints are provided as strings
        instead of numbers (e.g., maxItems: '7' should be maxItems: 7).

        Args:
            schema: Schema to coerce (modified in-place)
            field_path: Current path for error messages
        """
        if not isinstance(schema, dict):
            return

        # Numeric constraints that must be integers
        INTEGER_CONSTRAINTS = [
            "maxItems",
            "minItems",
            "maxLength",
            "minLength",
            "maxProperties",
            "minProperties",
            "multipleOf",
        ]

        # Numeric constraints that must be numbers (int or float)
        NUMBER_CONSTRAINTS = [
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
        ]

        for key, value in list(schema.items()):
            # Coerce integer constraints
            if key in INTEGER_CONSTRAINTS and isinstance(value, str):
                try:
                    schema[key] = int(value)
                    logger.info(
                        f"Field '{field_path}': Coerced {key} from string '{value}' to integer {schema[key]}"
                    )
                except ValueError:
                    logger.error(
                        f"Field '{field_path}': Cannot coerce {key}='{value}' to integer. "
                        f"This will cause validation errors."
                    )

            # Coerce number constraints
            elif key in NUMBER_CONSTRAINTS and isinstance(value, str):
                try:
                    schema[key] = float(value)
                    logger.info(
                        f"Field '{field_path}': Coerced {key} from string '{value}' to float {schema[key]}"
                    )
                except ValueError:
                    logger.error(
                        f"Field '{field_path}': Cannot coerce {key}='{value}' to number. "
                        f"This will cause validation errors."
                    )

        # Recursively process nested schemas
        if SCHEMA_PROPERTIES in schema:
            for prop_name, prop_schema in schema[SCHEMA_PROPERTIES].items():
                prop_path = f"{field_path}.{prop_name}" if field_path else prop_name
                cls._coerce_json_schema_types(prop_schema, prop_path)

        if SCHEMA_ITEMS in schema:
            items_path = f"{field_path}[]" if field_path else "items"
            cls._coerce_json_schema_types(schema[SCHEMA_ITEMS], items_path)

        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                cls._coerce_json_schema_types(def_schema, f"$defs.{def_name}")

    @classmethod
    def _translate_extensions_in_schema(
        cls,
        schema: Dict[str, Any],
        field_path: str = "",
        llm_config: Optional[Dict[str, Any]] = None,
        in_list_items: bool = False,
        document_class: str = "",
    ) -> Dict[str, Any]:
        """
        Recursively translate IDP evaluation extensions to Stickler extensions.

        This modifies the schema in-place to replace:
        - x-aws-idp-evaluation-method → x-aws-stickler-comparator (except for structured arrays)
        - x-aws-idp-evaluation-threshold → x-aws-stickler-threshold (for non-arrays)
        - x-aws-idp-evaluation-match-threshold → x-aws-stickler-match-threshold (for array items)
        - x-aws-idp-evaluation-weight → x-aws-stickler-weight

        Also adds empty "required" arrays to objects that don't have one,
        making all fields optional by default.

        Args:
            schema: Schema (or sub-schema) to translate
            field_path: Path for error messages (e.g., "Transaction.items.amount")
            llm_config: Optional dict from ``evaluation.llm_method`` in the IDP
                config. When provided, each LLM-method field's schema receives
                the config as ``x-aws-stickler-comparator-config`` so Stickler's
                ``JsonSchemaFieldConverter`` forwards it verbatim to
                ``create_comparator``. Replaces the previous module-level
                singleton in ``llm_comparator.py`` — two ``EvaluationService``
                instances with different configs in one process no longer share
                state.
            in_list_items: True while recursing INSIDE a structured array's
                ``items`` subtree. Set by the recursion, not by callers. Used to
                reject LLM comparators there, where Hungarian matching makes them
                O(N^2) Bedrock calls (see the downgrade below). ``_inline_refs``
                runs before this pass, so ``items`` subtrees are fully
                materialized and this flag sees every field a list row contains.
            document_class: Class name, threaded down so LLM-method fields can be
                given their ``{DOCUMENT_CLASS}`` context.

        Returns:
            Translated schema (same object, modified in-place)
        """
        if not isinstance(schema, dict):
            return schema

        # Coerce types FIRST, before any other processing
        cls._coerce_json_schema_types(schema, field_path)

        # Force all fields optional for evaluation by clearing any 'required' array.
        # For evaluation a field that is present in the baseline but correctly
        # extracted as null (or vice versa) is a scored MISS, not a hard schema
        # failure. If an explicit config marks a field required (e.g. RealKIE
        # Invoice: required: [Agency, Advertiser, LineItems]) and a document
        # genuinely has no value for it, Stickler/Pydantic would otherwise raise
        # "Field required [type=missing]" after None-stripping and fail-score the
        # WHOLE document. Overwriting (not just adding-when-missing) makes explicit
        # configs behave like the genson auto-schema path (_strip_required).
        #
        # UPSTREAM (long-term): a "lenient" or "eval" mode on
        # `StructuredModel.from_json_schema` that treats every field as optional
        # would let us delete this line AND make our `make_model_fields_nullable`
        # shim redundant. See `awslabs/stickler#149` for the related nullable-
        # widening issue. Until then, forcing `required: []` here is the
        # authoritative IDP evaluation semantic.
        if schema.get(SCHEMA_TYPE) == TYPE_OBJECT and SCHEMA_PROPERTIES in schema:
            schema["required"] = []

        # Check if this is an array with structured items
        is_structured_array = False
        if schema.get(SCHEMA_TYPE) == TYPE_ARRAY and SCHEMA_ITEMS in schema:
            items = schema[SCHEMA_ITEMS]
            # Check if items is an object or has $ref (both indicate structured list)
            if isinstance(items, dict):
                is_structured_array = (
                    items.get(SCHEMA_TYPE) == TYPE_OBJECT or "$ref" in items
                )

        # Validate evaluation method if present
        if X_AWS_IDP_EVALUATION_METHOD in schema:
            method = schema[X_AWS_IDP_EVALUATION_METHOD]
            try:
                cls._validate_method_for_field(schema, method, field_path)
            except ValueError as e:
                logger.error(str(e))
                # Remove invalid method to prevent downstream errors, but
                # STILL recurse into children — an outer method validation
                # failure shouldn't strip IDP-evaluation extensions from
                # nested items, which have their own methods to translate
                # (finding from code review — an earlier ``return schema``
                # here silently skipped nested extension translation on
                # any parent with an invalid method).
                del schema[X_AWS_IDP_EVALUATION_METHOD]
                return cls._recurse_children(
                    schema, field_path, llm_config, in_list_items, document_class
                )

        # An evaluation method on a STRUCTURED ARRAY is silently discarded below
        # (structured lists score through their item fields' comparators, and
        # Hungarian matching supplies the row-level comparison). Say so, rather
        # than dropping the author's intent without a word — a config carrying
        # `x-aws-idp-evaluation-method: LLM` on a list looks like it configured
        # something and did not.
        #
        # HUNGARIAN is the default/correct list-matching algorithm — no
        # warning when that's what the author asked for (finding from
        # #625 review). Also strip the key after warning so it doesn't
        # linger as dead metadata that downstream code could re-interpret
        # (finding from #625 review).
        if is_structured_array and X_AWS_IDP_EVALUATION_METHOD in schema:
            list_method = schema[X_AWS_IDP_EVALUATION_METHOD]
            if list_method != EVALUATION_METHOD_HUNGARIAN:
                logger.warning(
                    f"Field '{field_path}': evaluation method "
                    f"'{list_method}' on a structured array is "
                    f"ignored — structured lists are scored through their item fields' "
                    f"comparators (row matching is Hungarian). Set the method on the "
                    f"item fields instead."
                )
            # Strip the key regardless — it's inert for structured arrays,
            # keeping it lingering as dead metadata invites a downstream
            # consumer to reinterpret it later.
            del schema[X_AWS_IDP_EVALUATION_METHOD]

        # Translate evaluation method to comparator
        # BUT skip for structured arrays - they use item field comparators
        numeric_tolerance: Optional[float] = None
        llm_in_list_downgraded = False
        if X_AWS_IDP_EVALUATION_METHOD in schema and not is_structured_array:
            method = schema[X_AWS_IDP_EVALUATION_METHOD]

            # An LLM comparator on a field INSIDE a structured list is quadratic
            # and unusable. Hungarian matching builds a full N_gt x N_pred
            # similarity matrix, invoking each item field's comparator for EVERY
            # cell (stickler/algorithms/hungarian.py builds the matrix in a nested
            # loop), then scores the matched pairs — measured at N^2 + 2N calls.
            # One Bedrock round trip per cell means a 54-row invoice needs ~3,000
            # sequential calls (~45 min), so the 900 s evaluation Lambda can never
            # finish it at any retry count; observed wedging a whole stack.
            # Downgrade to Stickler's type-appropriate deterministic default
            # (string -> Levenshtein, number -> Numeric, boolean -> Exact), which
            # is what a matching cost function should be anyway, unless the author
            # explicitly opts in for a small list.
            # ``_coerce_bool`` handles YAML-quoted ``"false"`` / ``"no"``
            # / ``"off"`` (all truthy under raw Python ``bool()``, so
            # ``not schema.get(...)`` would let a config with
            # ``x-aws-idp-evaluation-llm-in-list: "false"`` bypass this
            # guard, triggering ~N² Bedrock calls and a Lambda timeout).
            if (
                method == EVALUATION_METHOD_LLM
                and in_list_items
                and not _coerce_bool(
                    schema.get(X_AWS_IDP_EVALUATION_LLM_IN_LIST, False)
                )
            ):
                logger.warning(
                    f"Field '{field_path}': LLM evaluation method inside a "
                    f"structured list is not supported and has been downgraded to "
                    f"the deterministic default for type "
                    f"'{schema.get(SCHEMA_TYPE, 'string')}'. Hungarian row matching "
                    f"invokes this comparator once per (ground-truth row x "
                    f"predicted row) cell, so an LLM call here costs O(N^2) Bedrock "
                    f"requests and times out the evaluation Lambda on lists of a "
                    f"few dozen rows. Set "
                    f"'{X_AWS_IDP_EVALUATION_LLM_IN_LIST}: true' on this field to "
                    f"override (only safe for very small lists)."
                )
                # Drop the method so no x-aws-stickler-comparator is emitted and
                # Stickler's JsonSchemaFieldConverter applies its own type default.
                # Deliberately fall THROUGH rather than returning early: this
                # field's threshold / weight / clip-under-threshold / aggregate
                # extensions are translated further down and must still be
                # honored — only the comparator choice is being overridden.
                del schema[X_AWS_IDP_EVALUATION_METHOD]
                llm_in_list_downgraded = True

            comparator = (
                None if llm_in_list_downgraded else cls.METHOD_TO_COMPARATOR.get(method)
            )
            if comparator:
                schema["x-aws-stickler-comparator"] = comparator

                # Pass through optional comparator config (e.g. DateComparator's
                # dayfirst / tolerance / range_mode). Stickler's
                # JsonSchemaFieldConverter forwards x-aws-stickler-comparator-config
                # verbatim to create_comparator().
                method_config = schema.get(X_AWS_IDP_EVALUATION_METHOD_CONFIG)
                if isinstance(method_config, dict) and method_config:
                    schema["x-aws-stickler-comparator-config"] = dict(method_config)

                # LLM method: emit the caller-supplied llm_config as the
                # per-field comparator-config. Stickler will pass this to
                # ``IDPLLMComparator(**config)`` so a per-service config change
                # takes effect for its own EvaluationService only, instead of
                # the previous module-level singleton.
                if (
                    method == EVALUATION_METHOD_LLM
                    and isinstance(llm_config, dict)
                    and llm_config
                    and "x-aws-stickler-comparator-config" not in schema
                ):
                    schema["x-aws-stickler-comparator-config"] = dict(llm_config)

                # LLM method: also hand the comparator its FIELD CONTEXT. The
                # shipped prompt interpolates {DOCUMENT_CLASS}, {ATTRIBUTE_NAME}
                # and {ATTRIBUTE_DESCRIPTION}, but Stickler's comparator protocol
                # is compare(value1, value2) — no field context — so the adapter
                # used to pass empty strings and every judge call went out as
                # `class: . For the attribute named "" described as "":`. The
                # description is what makes a *semantic* match decision possible,
                # so this is the difference between a judge that knows it is
                # comparing an agency name and one guessing at two bare strings.
                # Per-field comparator instances are already how config reaches
                # the comparator, so the context rides the same channel.
                if method == EVALUATION_METHOD_LLM:
                    ctx = schema.setdefault("x-aws-stickler-comparator-config", {})
                    if isinstance(ctx, dict):
                        ctx.setdefault("document_class", document_class or "")
                        # Strip bracket suffixes (``Items[]`` → ``Items``) so
                        # the LLM judge's prompt shows the schema attribute
                        # name, not the list-index punctuation that a
                        # top-level list retains from Stickler's field-path
                        # syntax (finding from code review — a top-level
                        # list attribute previously sent ``ATTRIBUTE_NAME
                        # = "Items[]"`` to the judge).
                        #
                        # Fall back to ``document_class`` (then ``"root"``)
                        # when field_path is empty — a top-level scalar
                        # attribute scored via LLM would otherwise send
                        # ``ATTRIBUTE_NAME = ""`` and lose the context-
                        # threading fix this block enforces (finding
                        # from #625 review).
                        raw_name = field_path.split(".")[-1] or field_path
                        # Fallback chain: bracket-stripped → document_class
                        # → "root". Each candidate is ``.strip()``-checked
                        # so a whitespace-only value doesn't leak through
                        # as truthy — the LLM judge would otherwise see
                        # ``ATTRIBUTE_NAME = "   "`` (finding from #625
                        # self-review — same trap as the bracketed-raw
                        # case this block already avoided).
                        stripped = re.sub(r"\[[^\]]*\]", "", raw_name).strip()
                        doc_class_str = (document_class or "").strip()
                        clean_name = stripped or doc_class_str or "root"
                        ctx.setdefault("attribute_name", clean_name)
                        ctx.setdefault(
                            "attribute_description", schema.get("description") or ""
                        )

                # NUMERIC_EXACT: user-configured evaluation-threshold means
                # ±tolerance, NOT the 0..1 score threshold the generic branch
                # below would emit. NumericComparator.compare is binary 1.0/0.0,
                # so a score-threshold gate is a no-op — the intended tolerance
                # never reaches the comparator. Route it to comparator-config
                # (constructor alias for absolute_tolerance) here so the generic
                # branch skips this field.
                if method == EVALUATION_METHOD_NUMERIC_EXACT and (
                    X_AWS_IDP_EVALUATION_THRESHOLD in schema
                ):
                    numeric_tolerance = cls._coerce_to_float(
                        schema[X_AWS_IDP_EVALUATION_THRESHOLD],
                        f"{field_path}.tolerance",
                    )
                    existing = schema.get("x-aws-stickler-comparator-config", {})
                    if not isinstance(existing, dict):
                        existing = {}
                    existing.setdefault("tolerance", numeric_tolerance)
                    schema["x-aws-stickler-comparator-config"] = existing

        # Handle thresholds based on field type
        # For structured arrays (Hungarian): use match_threshold at field level
        if is_structured_array:
            # Validate: structured arrays should use match-threshold, not threshold
            if X_AWS_IDP_EVALUATION_THRESHOLD in schema:
                raise ValueError(
                    f"Field '{field_path}': Cannot use 'evaluation-threshold' on List[Object]. "
                    f"Use 'evaluation-match-threshold' for HUNGARIAN matching instead."
                )

            # Set match_threshold at field level (array itself)
            if X_AWS_IDP_EVALUATION_MATCH_THRESHOLD in schema:
                match_threshold = cls._coerce_to_float(
                    schema[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD],
                    f"{field_path}.match_threshold",
                )
                # Set on the field itself (used by IDP's display + re-derived
                # match logic and Stickler >0.5.0 will honor this position).
                schema["x-aws-stickler-match-threshold"] = match_threshold
                # ALSO set on the items schema — this is where Stickler's
                # `from_json_schema` reads x-aws-stickler-match-threshold when
                # building the list-element class (structured_model.py:594 in
                # 0.5.0). Without this, the element class silently keeps the
                # ClassVar default (0.7) regardless of config.
                items_schema = schema.get(SCHEMA_ITEMS)
                if isinstance(items_schema, dict):
                    items_schema.setdefault(
                        "x-aws-stickler-match-threshold", match_threshold
                    )
                logger.debug(
                    f"Field '{field_path}': Set match_threshold={match_threshold} at field level and on items schema for Hungarian matching"
                )

        # For non-array fields: use threshold — unless NUMERIC_EXACT already
        # consumed evaluation-threshold as a comparator-config tolerance above.
        elif X_AWS_IDP_EVALUATION_THRESHOLD in schema and numeric_tolerance is None:
            threshold = cls._coerce_to_float(
                schema[X_AWS_IDP_EVALUATION_THRESHOLD], f"{field_path}.threshold"
            )
            schema["x-aws-stickler-threshold"] = threshold

        # Translate weight (valid for non-array fields)
        # Note: Stickler doesn't support weight on array fields themselves
        if not is_structured_array and X_AWS_IDP_EVALUATION_WEIGHT in schema:
            weight = cls._coerce_to_float(
                schema[X_AWS_IDP_EVALUATION_WEIGHT], f"{field_path}.weight"
            )
            schema["x-aws-stickler-weight"] = weight

        # R16: pass-throughs for Stickler's per-field flags that IDP previously
        # hid from users. ``clip_under_threshold`` and ``aggregate`` are read
        # by ``JsonSchemaFieldConverter`` (json_schema_field_converter.py:248-262
        # in 0.5.0) via the ``x-aws-stickler-*`` keys — we accept the neutral
        # ``x-aws-idp-evaluation-*`` names and forward verbatim.
        if "x-aws-idp-evaluation-clip-under-threshold" in schema:
            schema["x-aws-stickler-clip-under-threshold"] = bool(
                schema["x-aws-idp-evaluation-clip-under-threshold"]
            )
        if "x-aws-idp-evaluation-aggregate" in schema:
            schema["x-aws-stickler-aggregate"] = schema[
                "x-aws-idp-evaluation-aggregate"
            ]

        return cls._recurse_children(
            schema, field_path, llm_config, in_list_items, document_class
        )

    @classmethod
    def _recurse_children(
        cls,
        schema: Dict[str, Any],
        field_path: str,
        llm_config: Optional[Dict[str, Any]],
        in_list_items: bool,
        document_class: str,
    ) -> Dict[str, Any]:
        """Recurse into a schema's children, tracking path and list-items depth.

        Factored out of ``_translate_extensions_in_schema`` so the LLM-in-list
        downgrade can return early without skipping the children of the field it
        just rewrote.
        """
        if SCHEMA_PROPERTIES in schema:
            for prop_name, prop_schema in schema[SCHEMA_PROPERTIES].items():
                prop_path = f"{field_path}.{prop_name}" if field_path else prop_name
                cls._translate_extensions_in_schema(
                    prop_schema,
                    prop_path,
                    llm_config=llm_config,
                    in_list_items=in_list_items,
                    document_class=document_class,
                )

        if SCHEMA_ITEMS in schema:
            items_path = f"{field_path}[]" if field_path else "items"
            # Everything below an array's items is a list row: once inside, stay
            # inside (a nested list inside a row is still, transitively, matched
            # per row).
            cls._translate_extensions_in_schema(
                schema[SCHEMA_ITEMS],
                items_path,
                llm_config=llm_config,
                in_list_items=True,
                document_class=document_class,
            )

        # $defs / definitions are reached independently of where they are $ref'd
        # from, so list-membership cannot be known here. _inline_refs has already
        # materialized every referenced subtree into its use site (that inlined
        # copy is what Stickler's converter reads), so the copy under items is the
        # one the guard needs to catch — and it does. These bare definitions are
        # translated for completeness with in_list_items left as inherited.
        if "$defs" in schema:
            for def_name, def_schema in schema["$defs"].items():
                cls._translate_extensions_in_schema(
                    def_schema,
                    f"$defs.{def_name}",
                    llm_config=llm_config,
                    in_list_items=in_list_items,
                    document_class=document_class,
                )

        if "definitions" in schema:
            for def_name, def_schema in schema["definitions"].items():
                cls._translate_extensions_in_schema(
                    def_schema,
                    f"definitions.{def_name}",
                    llm_config=llm_config,
                    in_list_items=in_list_items,
                    document_class=document_class,
                )

        return schema

    @classmethod
    def build_stickler_model_config(
        cls,
        document_class_schema: Dict[str, Any],
        llm_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build complete Stickler model configuration from IDP document class schema.

        Translates IDP evaluation extensions to Stickler extensions throughout
        the schema. Stickler's JsonSchemaFieldConverter will handle $ref resolution,
        required vs optional fields, and nested structures.

        Args:
            document_class_schema: JSON Schema for document class with IDP extensions
            llm_config: Optional dict from ``evaluation.llm_method`` — surfaced
                as ``x-aws-stickler-comparator-config`` on LLM-method fields
                (see ``_translate_extensions_in_schema``).

        Returns:
            Configuration dict with translated schema for JsonSchemaFieldConverter
        """
        import copy

        # Make a deep copy to avoid modifying the original
        schema = copy.deepcopy(document_class_schema)

        # Extract model name
        model_name = (
            schema.get(X_AWS_IDP_EVALUATION_MODEL_NAME)
            or schema.get(X_AWS_IDP_DOCUMENT_TYPE)
            or "Document"
        )

        # Sanitize model name to be valid Python identifier
        model_name = cls._sanitize_model_name(model_name)

        # Extract match threshold (document-level)
        match_threshold = schema.get(X_AWS_IDP_EVALUATION_MATCH_THRESHOLD, 0.8)

        logger.info(
            f"Building Stickler config for model '{model_name}' with match_threshold={match_threshold}"
        )

        # Inline all $ref references BEFORE translating extensions
        # This is necessary because Stickler's JsonSchemaFieldConverter creates new
        # converter instances for nested schemas without propagating root $defs,
        # causing nested $ref resolution to fail
        defs = schema.get("$defs", {}) or schema.get("definitions", {})
        if defs:
            num_defs = len(defs)
            schema = cls._inline_refs(schema, defs, field_path=model_name)
            logger.info(f"Inlined {num_defs} $defs references for model '{model_name}'")

        # Remove empty object properties AFTER inlining refs but BEFORE translating extensions
        # This prevents Stickler's ModelFactory from failing on nested objects with no fields
        # (e.g., "AccidentInformation": {"type": "object", "properties": {}})
        removed_properties = cls._remove_empty_object_properties(
            schema, field_path=model_name
        )
        if removed_properties:
            logger.info(
                f"Removed {len(removed_properties)} empty object properties for model '{model_name}': "
                f"{removed_properties}"
            )

        # Translate IDP extensions to Stickler extensions throughout the schema
        # document_class is threaded down so LLM-method fields can be handed their
        # {DOCUMENT_CLASS} context. Prefer the authored document type over the
        # sanitized Python model name — the judge reads it as prose.
        cls._translate_extensions_in_schema(
            schema,
            llm_config=llm_config,
            document_class=str(
                document_class_schema.get(X_AWS_IDP_DOCUMENT_TYPE)
                or document_class_schema.get("$id")
                or model_name
            ),
        )

        # Return the full schema - JsonSchemaFieldConverter will handle everything else
        stickler_config = {
            "model_name": model_name,
            "match_threshold": match_threshold,
            "schema": schema,  # Full JSON Schema with translated extensions
        }

        logger.info(
            f"Built Stickler config for model '{model_name}' using native JSON Schema with JsonSchemaFieldConverter"
        )

        return stickler_config

    @classmethod
    def build_all_stickler_configs(
        cls, idp_config: Dict[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        """
        Build Stickler configurations for all document classes in IDP config.

        Args:
            idp_config: Complete IDP configuration with 'classes' array

        Returns:
            Dictionary mapping document class names to Stickler configs
        """
        stickler_configs = {}
        classes = idp_config.get("classes", [])

        # Extract per-service LLM comparator config from evaluation.llm_method
        # and thread it into schema translation so every LLM-method field lands
        # with an x-aws-stickler-comparator-config that Stickler's converter
        # forwards to IDPLLMComparator. Replaces the module-level singleton
        # previously living in llm_comparator.py.
        evaluation_config = idp_config.get("evaluation") or {}
        llm_config: Optional[Dict[str, Any]] = None
        if isinstance(evaluation_config, dict):
            candidate = evaluation_config.get("llm_method")
            if isinstance(candidate, dict) and candidate:
                llm_config = candidate

        logger.info(f"Building Stickler configs for {len(classes)} document classes")

        # Evaluation reads class schemas FROM CONFIG, not from the extraction
        # output, so it must derive the multi-instance wrapper independently
        # (GitHub #715, plan D2). Without this, a class flagged
        # x-aws-idp-multi-instance produces a Stickler model built from the FLAT
        # schema: the prediction's only key is `instances`, zero declared fields
        # match, and the section SILENTLY scores 0.0. A no-op for unflagged
        # classes.
        from idp_common.schema.multi_instance import wrap_class_schema

        for raw_class_schema in classes:
            try:
                class_schema = wrap_class_schema(raw_class_schema)
                doc_type = class_schema.get(X_AWS_IDP_DOCUMENT_TYPE)
                if not doc_type:
                    logger.warning("Document class missing x-aws-idp-document-type")
                    continue

                # Build Stickler config for this document class
                stickler_config = cls.build_stickler_model_config(
                    class_schema, llm_config=llm_config
                )

                # Use lowercase document type as key
                stickler_configs[doc_type.lower()] = stickler_config

                logger.info(f"Built Stickler config for document type: {doc_type}")

            except Exception as e:
                logger.error(
                    f"Error building Stickler config for class: {str(e)}", exc_info=True
                )
                # Continue with other classes
                continue

        logger.info(
            f"Successfully built {len(stickler_configs)} Stickler configurations"
        )
        return stickler_configs

    @staticmethod
    def _sanitize_model_name(name: str) -> str:
        """
        Sanitize model name to be a valid Python identifier.

        Stickler requires model names to be valid Python identifiers,
        which means they cannot contain hyphens or start with numbers.

        Args:
            name: Original model name (may contain hyphens)

        Returns:
            Sanitized name that is a valid Python identifier
        """
        # Replace hyphens with underscores
        sanitized = name.replace("-", "_")

        # Replace spaces with underscores
        sanitized = sanitized.replace(" ", "_")

        # Remove any other non-alphanumeric characters except underscores
        sanitized = "".join(c if c.isalnum() or c == "_" else "_" for c in sanitized)

        # Ensure it doesn't start with a number
        if sanitized and sanitized[0].isdigit():
            sanitized = f"_{sanitized}"

        logger.debug(f"Sanitized model name: '{name}' -> '{sanitized}'")
        return sanitized
