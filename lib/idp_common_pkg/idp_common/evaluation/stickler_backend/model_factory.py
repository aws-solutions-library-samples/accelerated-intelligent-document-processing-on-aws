# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stickler ``StructuredModel`` construction from an IDP config schema.

Owns the code path from a translated IDP schema (via ``mapper.py``) to a
Pydantic ``StructuredModel`` subclass ready for ``compare_with``. Includes
the two workarounds around Stickler / Pydantic behavior that Stickler doesn't
ship out of the box:

- ``clean_null_descriptions`` — Stickler's JSON Schema validator rejects
  ``description: null``; empty strings behave identically for evaluation.
- ``make_model_fields_nullable`` — widens every field annotation to
  ``Optional[...]`` so the confidence path's ``from_json`` -> ``model_dump``
  round-trip accepts None (upstream stickler/#149; PR pending).

Both are tagged UPSTREAM in the docstrings; expiry is a follow-up upstream
merge, at which point these can be deleted.
"""

import inspect
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Optional, Set, Type, Union

from idp_common.evaluation.stickler_backend.mapper import SticklerConfigMapper

if TYPE_CHECKING:
    from stickler import StructuredModel

logger = logging.getLogger(__name__)


def clean_null_descriptions(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively replace ``description: null`` with empty strings.

    Stickler's JSON Schema validator raises on ``null`` descriptions; empty
    strings behave identically for evaluation. Handles ``properties``,
    ``items``, and ``$defs`` recursively. Mutates in place; also returns the
    schema for convenience.
    """
    if "properties" in schema:
        for prop_def in schema["properties"].values():
            if isinstance(prop_def, dict):
                if prop_def.get("description") is None:
                    prop_def["description"] = ""
                if "properties" in prop_def:
                    clean_null_descriptions(prop_def)
                if "items" in prop_def and isinstance(prop_def["items"], dict):
                    clean_null_descriptions(prop_def["items"])

    if "$defs" in schema:
        for def_schema in schema["$defs"].values():
            if isinstance(def_schema, dict):
                clean_null_descriptions(def_schema)

    return schema


def make_model_fields_nullable(
    model_class: Type["StructuredModel"],
    _seen: Optional[Set[Type[Any]]] = None,
) -> None:
    """Recursively widen every field annotation to ``Optional[...]``.

    Stickler's ``JsonSchemaFieldConverter`` creates optional fields (default
    None) but keeps the original non-nullable annotation (e.g. ``str``). Plain
    ``ModelClass(**data)`` tolerates that — Pydantic only applies the default
    and never validates it — but the confidence path
    (``from_json`` → ``model_dump`` round-trip in ``ConfigurationHelper``)
    materializes None for missing fields and re-validates, raising
    ``"Input should be a valid string [input_value=None]"``. Widening every
    annotation to ``Optional[...]`` makes both paths accept None.

    Descends into nested ``StructuredModel`` and ``List[StructuredModel]``.
    UPSTREAM: https://github.com/awslabs/stickler/issues/149 — delete this
    shim when the fix lands.
    """
    from stickler.structured_object_evaluator.models.structured_model import (
        StructuredModel,
    )

    if _seen is None:
        _seen = set()
    if model_class in _seen:
        return
    _seen.add(model_class)

    changed = False
    for field_name, field_info in model_class.model_fields.items():
        if field_name == "extra_fields":
            continue

        annotation = field_info.annotation
        inner = annotation
        origin = getattr(annotation, "__origin__", None)
        if origin is Union:
            inner = next(
                (a for a in getattr(annotation, "__args__", ()) if a is not type(None)),
                annotation,
            )

        inner_origin = getattr(inner, "__origin__", None)
        if inspect.isclass(inner) and issubclass(inner, StructuredModel):
            make_model_fields_nullable(inner, _seen)
        elif inner_origin is list:
            list_args = getattr(inner, "__args__", ())
            if (
                list_args
                and inspect.isclass(list_args[0])
                and issubclass(list_args[0], StructuredModel)
            ):
                make_model_fields_nullable(list_args[0], _seen)

        if origin is not Union:
            field_info.annotation = Optional[annotation]
            changed = True

    if changed:
        model_class.model_rebuild(force=True)


def get_stickler_model(
    document_class: str,
    stickler_models: Dict[str, Dict[str, Any]],
    model_cache: Dict[str, Type["StructuredModel"]],
    auto_generated_models: Set[str],
    infer_schema_fn,
    expected_data: Optional[Dict[str, Any]] = None,
) -> Type["StructuredModel"]:
    """Get-or-create a Stickler ``StructuredModel`` subclass for a document class.

    Uses Stickler's ``JsonSchemaFieldConverter`` to convert the translated IDP
    schema into Pydantic field definitions, then ``pydantic.create_model`` to
    build the subclass. Applies the configured ``match_threshold`` (R2 top-level
    fix — ``create_model`` bypasses Stickler's own ``ModelFactory`` so the
    ClassVar default would otherwise linger) and widens every field to
    ``Optional`` (upstream #149 shim).

    Args:
        document_class: Class name from the IDP config (case-insensitive).
        stickler_models: Pre-built Stickler configs keyed by lowercased class
            name (populated by ``SticklerConfigMapper.build_all_stickler_configs``).
        model_cache: Per-service cache of already-built model classes.
        auto_generated_models: Set of class names whose schema was auto-inferred
            from expected_data (annotated in report output).
        infer_schema_fn: Callback to infer a JSON Schema from expected data
            (``EvaluationService._infer_schema_from_data``). Kept as an arg so
            this module doesn't depend on the service for schema inference.
        expected_data: Optional dict — if provided AND no config exists for
            ``document_class``, its shape is used to auto-generate a schema.

    Returns:
        Stickler ``StructuredModel`` subclass ready for ``compare_with``.

    Raises:
        ValueError: No config found and no ``expected_data`` to infer from.
    """
    from pydantic import create_model
    from stickler import StructuredModel
    from stickler.structured_object_evaluator.models.json_schema_field_converter import (
        JsonSchemaFieldConverter,
    )

    cache_key = document_class.lower()
    if cache_key in model_cache:
        logger.debug(f"Using cached Stickler model for class: {document_class}")
        return model_cache[cache_key]

    stickler_config = stickler_models.get(cache_key)
    if not stickler_config:
        if expected_data:
            logger.info(
                f"No configuration found for '{document_class}'. "
                f"Auto-generating schema from expected data structure."
            )
            inferred_schema = infer_schema_fn(expected_data, document_class)
            stickler_config = SticklerConfigMapper.build_stickler_model_config(
                inferred_schema
            )
            stickler_models[cache_key] = stickler_config
            auto_generated_models.add(cache_key)
        else:
            raise ValueError(
                f"No schema configuration found for document class: {document_class}. "
                f"Cannot auto-generate schema without expected data."
            )

    schema = stickler_config["schema"]
    model_name = stickler_config["model_name"]

    logger.info(
        f"Creating Stickler model for class: {document_class}\n"
        f"  Schema summary:\n"
        f"    - Properties: {list(schema.get('properties', {}).keys())}\n"
        f"    - Required fields: {schema.get('required', [])}\n"
        f"    - Schema ID: {schema.get('$id', 'N/A')}\n"
        f"    - Model name: {model_name}"
    )
    if expected_data:
        logger.info(
            f"  Expected data keys for {document_class}: {list(expected_data.keys())}"
        )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Full JSON Schema for {document_class}: {json.dumps(schema, default=str)}"
        )

    try:
        logger.debug(f"Converting schema properties for {document_class}")
        schema = clean_null_descriptions(schema)

        converter = JsonSchemaFieldConverter(schema)
        field_definitions = converter.convert_properties_to_fields(
            schema.get("properties", {}), schema.get("required", [])
        )
        logger.info(
            f"Successfully converted schema for {document_class} with "
            f"{len(field_definitions)} fields"
        )
    except Exception as e:
        error_message = str(e)
        if (
            "jsonschema.exceptions.SchemaError" in str(type(e))
            or "Invalid JSON Schema" in error_message
        ):
            field_match = re.search(
                r"On schema\['properties'\]\['([^']+)'\]", error_message
            )
            field_name = field_match.group(1) if field_match else "unknown"
            constraint_match = re.search(
                r"\['([^']+)'\]\s*:\s*'([^']+)'", error_message
            )
            constraint = constraint_match.group(1) if constraint_match else "unknown"
            bad_value = constraint_match.group(2) if constraint_match else "unknown"
            helpful_message = (
                f"Invalid JSON Schema for document class '{document_class}'.\n\n"
                f"Problem detected:\n"
                f"  Field: {field_name}\n"
                f"  Constraint: {constraint}\n"
                f"  Current value: '{bad_value}' (type: {type(bad_value).__name__})\n\n"
                f"Common fixes:\n"
                f"  1. If '{constraint}' should be a number, remove quotes in your config:\n"
                f"     {constraint}: '{bad_value}' → {constraint}: {bad_value}\n"
                f"  2. Check your config YAML for field '{field_name}' in class '{document_class}'\n"
                f"  3. Ensure all numeric constraints (maxItems, minItems, minimum, maximum, etc.) are numbers, not strings\n\n"
                f"Original error: {error_message}"
            )
            logger.error(helpful_message)
            logger.error(
                f"Full schema that caused the error:\n{json.dumps(schema, indent=2, default=str)}"
            )
            raise ValueError(helpful_message) from e
        logger.error(
            f"Unexpected error creating Stickler model for {document_class}: {error_message}"
        )
        logger.error(
            f"Schema being processed:\n{json.dumps(schema, indent=2, default=str)}"
        )
        raise

    # Type checker can't understand dynamic field unpacking - this is expected
    model_class = create_model(  # type: ignore  # pyright: reportArgumentType=false
        model_name, **field_definitions, __base__=StructuredModel
    )

    # R2: apply configured document-level match_threshold. `create_model`
    # doesn't route through Stickler's ModelFactory so the ClassVar default
    # (0.7 in 0.5.0) would otherwise linger regardless of config.
    configured_match_threshold = stickler_config.get("match_threshold")
    if configured_match_threshold is not None:
        model_class.match_threshold = configured_match_threshold  # type: ignore[attr-defined]

    make_model_fields_nullable(model_class)

    # Capture Stickler 1.0's per-field comparator/threshold/weight AND its
    # decision trace for the Test Studio Comparator Changes panel and the
    # per-attribute Source column. ``spec.explain()`` returns a
    # ``{dotted_path: {comparator, threshold, weight, source, why}}`` dict —
    # ``source`` is ``"explicit"`` when the operator's translated
    # ``x-aws-stickler-comparator`` reached Stickler, and either ``"type"``
    # or ``"name-token"`` when the ``x-aws-stickler-infer-unspecified: true``
    # flag on the schema root drove native inference. Stashed on the class
    # so ``stickler_backend.results.transform_stickler_result`` can populate
    # ``AttributeEvaluationResult.inference_source`` / ``.inference_why``
    # without an extra call. Defensive ``try/except`` because provenance is
    # a UI concern — a failure here must not fault the evaluation pipeline.
    try:
        import stickler as _stickler

        spec = _stickler.eval_for(model_class)
        model_class.__idp_explain__ = spec.explain()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 — provenance capture must never fault the pipeline
        logger.warning(
            f"stickler.eval_for provenance capture failed for {model_name}: {e}. "
            f"Inference source/trace will be absent from results.json for this class."
        )
        model_class.__idp_explain__ = {}  # type: ignore[attr-defined]

    model_cache[cache_key] = model_class
    logger.debug(f"Cached Stickler model: {model_class.__name__}")

    return model_class
