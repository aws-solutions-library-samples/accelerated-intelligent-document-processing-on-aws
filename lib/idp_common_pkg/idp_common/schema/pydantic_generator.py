# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Pydantic model generation utilities for JSON Schema.

This module provides utilities for dynamically generating Pydantic v2 models
from JSON Schema definitions using datamodel-code-generator.
"""

import importlib.util
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import jsonschema
from datamodel_code_generator import DataModelType, InputFileType, generate
from pydantic import BaseModel, ConfigDict, create_model, model_validator

logger = logging.getLogger(__name__)


class PydanticModelGenerationError(Exception):
    """Exception raised when Pydantic model generation fails."""

    pass


class CircularReferenceError(PydanticModelGenerationError):
    """Exception raised when circular references are detected in schema."""

    pass


def clean_schema_for_generation(
    schema: Dict[str, Any], fields_to_remove: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Clean JSON Schema by removing custom extension fields.

    Args:
        schema: JSON Schema definition
        fields_to_remove: List of field prefixes to remove (default: ["x-aws-idp-"])

    Returns:
        Cleaned JSON Schema without custom extension fields
    """
    if fields_to_remove is None:
        fields_to_remove = ["x-aws-idp-"]

    cleaned = {}

    for key, value in schema.items():
        # Skip fields that match removal patterns
        if any(key.startswith(prefix) for prefix in fields_to_remove):
            continue

        # Recursively clean nested objects
        if isinstance(value, dict):
            cleaned[key] = clean_schema_for_generation(value, fields_to_remove)
        elif isinstance(value, list):
            cleaned[key] = [
                (
                    clean_schema_for_generation(item, fields_to_remove)
                    if isinstance(item, dict)
                    else item
                )
                for item in value
            ]
        else:
            cleaned[key] = value

    return cleaned


def _normalize_class_name(name: str) -> str:
    """
    Normalize a class name to PascalCase.

    Args:
        name: Class name to normalize

    Returns:
        PascalCase version of the name
    """
    return "".join(
        word.capitalize() for word in name.replace("-", " ").replace("_", " ").split()
    )


def has_advanced_constraints(schema: Dict[str, Any]) -> bool:
    """
    Check if schema has constraints that Pydantic doesn't enforce natively.

    Args:
        schema: JSON Schema definition

    Returns:
        True if schema has advanced constraints requiring JSON Schema validation
    """
    advanced_keywords = {
        "contains",
        "minContains",
        "maxContains",
        "contentMediaType",
        "contentEncoding",
        "dependentSchemas",
        "dependentRequired",
        "if",
        "then",
        "else",
    }

    def check_recursive(obj: Any) -> bool:
        if isinstance(obj, dict):
            # Check for advanced keywords at this level
            if any(key in obj for key in advanced_keywords):
                return True
            # Recursively check nested objects
            for value in obj.values():
                if check_recursive(value):
                    return True
        elif isinstance(obj, list):
            for item in obj:
                if check_recursive(item):
                    return True
        return False

    return check_recursive(schema)


def create_json_schema_validator(
    original_schema: Dict[str, Any],
) -> Callable[[BaseModel], BaseModel]:
    """
    Create a Pydantic model validator that enforces JSON Schema constraints.

    Args:
        original_schema: The original JSON Schema definition

    Returns:
        A validator function that can be used with Pydantic's @model_validator
    """

    def validate_against_json_schema(value: BaseModel) -> BaseModel:
        """Validate model data against the original JSON Schema."""
        # Convert Pydantic model to dict for JSON Schema validation
        data = value.model_dump(mode="json")

        try:
            # Validate against JSON Schema
            jsonschema.validate(data, original_schema)
            return value
        except jsonschema.ValidationError as e:
            # Re-raise as ValueError which Pydantic will catch and convert
            raise ValueError(
                f"JSON Schema validation failed: {e.message}. "
                f"Path: {'.'.join(str(p) for p in e.path) if e.path else 'root'}"
            )
        except jsonschema.SchemaError as e:
            # Schema itself is invalid
            logger.error(f"Invalid JSON Schema: {e}")
            raise PydanticModelGenerationError(f"Invalid JSON Schema: {e}")

    return validate_against_json_schema


def _find_model_in_module(
    generated_module: Any,
    schema_dict: Dict[str, Any],
    class_label: str,
) -> Tuple[Optional[Type[BaseModel]], List[Tuple[str, Type[BaseModel]]]]:
    """
    Find the appropriate Pydantic model in a generated module.

    Args:
        generated_module: The dynamically imported module
        schema_dict: The JSON Schema dictionary
        class_label: The class label for fallback matching

    Returns:
        Tuple of (selected_model, all_models_list)
    """
    # Find all BaseModel subclasses in the module
    all_models = [
        (name, obj)
        for name in dir(generated_module)
        if (obj := getattr(generated_module, name))
        and isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj != BaseModel
        and not name.startswith("_")
    ]

    if not all_models:
        return None, []

    # Log all discovered models with their fields
    logger.debug(
        f"Found {len(all_models)} Pydantic models in generated code for class '{class_label}'"
    )
    for model_name, model_obj in all_models:
        field_names = list(model_obj.model_fields.keys())
        logger.debug(
            f"  - Model '{model_name}' with {len(field_names)} fields: {field_names}"
        )

    # Get schema title/id for matching
    schema_title = schema_dict.get("title", schema_dict.get("$id", class_label))
    schema_title_pascal = _normalize_class_name(schema_title)
    class_label_pascal = _normalize_class_name(class_label)

    # Try to find the best matching model
    # Priority: exact schema title > exact class label > "Model" > first available
    matching_names = [
        schema_title_pascal,
        schema_title.replace(" ", ""),
        class_label_pascal,
        class_label.replace(" ", ""),
        "Model",
    ]

    logger.debug(
        f"Searching for model matching one of: {matching_names} (schema_title='{schema_title}', class_label='{class_label}')"
    )

    for name, obj in all_models:
        if name in matching_names:
            logger.debug(f"Selected model '{name}' based on name matching")
            return _ensure_model_covers_schema(obj, all_models, schema_dict), all_models

    # No exact match - use first available
    logger.debug(
        f"No name match found, using first available model: '{all_models[0][0]}'"
    )
    return (
        _ensure_model_covers_schema(all_models[0][1], all_models, schema_dict),
        all_models,
    )


def _declared_field_names(model: Type[BaseModel]) -> set:
    """Field names AND aliases of a generated model.

    Both matter: datamodel-code-generator sanitizes a JSON-Schema property name
    that is not a Python identifier (``"Date of Birth"`` -> ``Date_of_Birth``)
    and records the original as the field's alias.
    """
    names = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name)
        if field.alias:
            names.add(field.alias)
    return names


def _ensure_model_covers_schema(
    selected: Type[BaseModel],
    all_models: List[Tuple[str, Type[BaseModel]]],
    schema_dict: Dict[str, Any],
) -> Type[BaseModel]:
    """Guard against selecting a NESTED model instead of the root one.

    Selection is by title/label priority, which is fine until a schema contains a
    nested object that happens to match the same name. The case that provoked
    this is the multi-instance wrapper (GitHub #715): a schema whose single
    ``instances`` property has ``items`` describing the class would, if those
    items kept the class title, select the INNER model — so the response was
    silently validated as ONE record where a LIST had been requested. Every
    record but the first would be dropped by validation with no error anywhere.

    The check is structural rather than name-based, so it also catches the same
    mis-selection arising any other way: the chosen model must declare every
    top-level property the schema declares. When it does not, a model that DOES
    is preferred (with a warning) and only a total absence of one is fatal —
    raising on a schema that previously worked would be a worse outcome than the
    bug this prevents.
    """
    properties = schema_dict.get("properties")
    if not isinstance(properties, dict) or not properties:
        return selected

    expected = set(properties.keys())
    if expected.issubset(_declared_field_names(selected)):
        return selected

    for name, candidate in all_models:
        if expected.issubset(_declared_field_names(candidate)):
            logger.warning(
                "Generated model '%s' does not declare the schema's top-level "
                "properties %s — it is a nested model, not the root. Using '%s' "
                "instead, which does. (A wrapper schema whose inner items keep "
                "the class title hits this: the inner model validates ONE record "
                "where a LIST was requested.)",
                selected.__name__,
                sorted(expected - _declared_field_names(selected)),
                name,
            )
            return candidate

    raise PydanticModelGenerationError(
        f"No generated Pydantic model declares the schema's top-level properties "
        f"{sorted(expected)}; the closest was '{selected.__name__}' with fields "
        f"{sorted(_declared_field_names(selected))}. Validating against it would "
        f"silently drop data."
    )


def _iter_nested_model_classes(annotation: Any):
    """Yield every Pydantic ``BaseModel`` subclass referenced by a type annotation.

    Walks into containers and unions (``Optional[X]``, ``list[X]``,
    ``dict[str, X]``, ``Union[...]``) so nested object/array-of-object fields are
    reached, not just direct ``BaseModel`` annotations.
    """
    import typing

    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    # typing.get_args handles Optional/Union/list/dict/tuple parameterizations.
    for arg in typing.get_args(annotation):
        yield from _iter_nested_model_classes(arg)


def _apply_alias_config_recursively(
    model: Type[BaseModel],
    config: ConfigDict,
    _seen: Optional[set] = None,
) -> None:
    """Apply ``config`` to ``model`` and every nested generated model in place.

    datamodel-code-generator sanitizes JSON-Schema property names with spaces
    (e.g. ``"Date of Birth"``) into Python identifiers (``Date_of_Birth``) and
    records the original name as the field alias. The alias round-trip
    (``populate_by_name`` / ``serialize_by_alias`` / ``validate_by_*``) is only
    set on the top-level model by the caller, so **nested** sub-models would
    otherwise validate/serialize by field name and silently drop or mis-key
    nested values (see fix for agentic extraction nested-field loss). This walks
    the whole model graph and applies the same config to every nested model,
    then rebuilds each so Pydantic's cached core schema picks up the change.

    ``_seen`` guards against recursive/self-referential schemas.
    """
    if _seen is None:
        _seen = set()
    if model in _seen:
        return
    _seen.add(model)

    # Merge (not replace) so we don't clobber any config the model already has.
    model.model_config.update(config)

    for field in model.model_fields.values():
        for nested in _iter_nested_model_classes(field.annotation):
            _apply_alias_config_recursively(nested, config, _seen)

    # Force a rebuild so the updated config takes effect on the cached schema.
    try:
        model.model_rebuild(force=True)
    except Exception as exc:  # pragma: no cover - defensive; rebuild rarely fails
        logger.warning(
            f"model_rebuild failed for '{model.__name__}' after alias-config "
            f"propagation; nested alias serialization may be incomplete: {exc}"
        )


def create_pydantic_model_from_json_schema(
    schema: Dict[str, Any],
    class_label: str,
    clean_schema: bool = True,
    fields_to_remove: Optional[List[str]] = None,
    enable_json_schema_validation: bool = True,
) -> Type[BaseModel]:
    """
    Dynamically create a Pydantic v2 model from JSON Schema.

    This function uses datamodel-code-generator to create a Pydantic model
    from a JSON Schema definition. The model is generated in a temporary
    file and then dynamically imported.

    When advanced JSON Schema constraints are detected (e.g., contains, minContains,
    if/then/else), a model validator is automatically added to enforce these
    constraints at runtime.

    Args:
        schema: JSON Schema definition (dict or JSON string)
        class_label: Label/name for the class (used for module naming and fallback)
        clean_schema: Whether to clean custom fields before generation (default: True)
        fields_to_remove: List of field prefixes to remove when cleaning (default: ["x-aws-idp-"])
        enable_json_schema_validation: Add JSON Schema validation for advanced constraints (default: True)

    Returns:
        Dynamically created Pydantic BaseModel class

    Raises:
        ValueError: If no model classes are found in generated code
        Exception: Any exception from datamodel-code-generator (e.g., invalid schema)

    Examples:
        >>> schema = {
        ...     "type": "object",
        ...     "title": "Invoice",
        ...     "properties": {
        ...         "invoice_number": {"type": "string"},
        ...         "amount": {"type": "number"}
        ...     }
        ... }
        >>> InvoiceModel = create_pydantic_model_from_json_schema(schema, "Invoice")
        >>> invoice = InvoiceModel(invoice_number="INV-001", amount=100.50)
    """
    # Clean the schema if requested
    if clean_schema:
        processed_schema = clean_schema_for_generation(schema, fields_to_remove)
    else:
        processed_schema = schema

    # Convert to JSON string if needed
    schema_str = (
        json.dumps(processed_schema)
        if isinstance(processed_schema, dict)
        else processed_schema
    )

    # Sanitize class_label for use in module name (remove special chars)
    safe_class_label = "".join(c if c.isalnum() else "_" for c in class_label)
    module_name = f"generated_model_{safe_class_label}"

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / f"model_{safe_class_label}.py"

            # Generate the Pydantic model code
            generate(
                input_=schema_str,
                input_file_type=InputFileType.JsonSchema,
                output_model_type=DataModelType.PydanticV2BaseModel,
                output=tmp_path,
                disable_timestamp=True,
                use_standard_collections=True,
                use_union_operator=True,
                field_constraints=True,
                snake_case_field=False,
                use_title_as_name=True,
            )

            # Import the generated module
            spec = importlib.util.spec_from_file_location(module_name, tmp_path)
            if not spec or not spec.loader:
                raise PydanticModelGenerationError(
                    f"Failed to create module spec for '{class_label}'"
                )

            generated_module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = generated_module
            spec.loader.exec_module(generated_module)

            # Parse schema dict for model selection
            schema_dict = (
                json.loads(schema_str)
                if isinstance(schema_str, str)
                else processed_schema
            )

            # Find all models in the module
            data_model, all_models = _find_model_in_module(
                generated_module, schema_dict, class_label
            )

            if not data_model:
                raise ValueError(
                    f"No Pydantic models found in generated code for class '{class_label}'"
                )

            # Use the model selected by _find_model_in_module (best match based on title/label)
            selected_model = data_model
            original_model_name = selected_model.__name__

            # Rename the model to match the schema title or class_label
            schema_title = schema_dict.get("title", class_label)
            normalized_name = _normalize_class_name(schema_title)
            selected_model.__name__ = normalized_name

            # Check if we need to add JSON Schema validation
            needs_validation = (
                enable_json_schema_validation and has_advanced_constraints(schema)
            )

            if needs_validation:
                # Create a new model class with JSON Schema validation
                validator_func = create_json_schema_validator(schema)

                # Create class with validator using type() and decorator
                class ModelWithValidation(selected_model):  # type: ignore
                    model_config = ConfigDict(
                        populate_by_name=True, serialize_by_alias=True
                    )

                    @model_validator(mode="after")  # type: ignore
                    def validate_json_schema(self):  # type: ignore
                        return validator_func(self)

                # Set the correct name
                ModelWithValidation.__name__ = selected_model.__name__
                ModelWithValidation.__qualname__ = selected_model.__name__

                final_model = ModelWithValidation

                logger.info(
                    f"Added JSON Schema validation to model '{selected_model.__name__}' "
                    f"for class '{class_label}' due to advanced constraints"
                )
            else:
                # Configure model to use aliases for population and serialization
                # This is critical for handling nested objects where datamodel-code-generator
                # adds _1 suffixes to avoid naming conflicts with nested model classes
                final_model = create_model(
                    selected_model.__name__,
                    __base__=selected_model,
                    __config__=ConfigDict(
                        populate_by_name=True, serialize_by_alias=True
                    ),
                )

            # Propagate the alias config to ALL nested models. The caller above
            # only sets it on the top-level model, so without this, nested object
            # properties whose names contain spaces (aliased to identifiers like
            # "Date_of_Birth") validate/serialize by field name and silently drop
            # or mis-key values. This makes nested behavior match the top level.
            _apply_alias_config_recursively(
                final_model,
                ConfigDict(
                    populate_by_name=True,
                    serialize_by_alias=True,
                    validate_by_name=True,
                    validate_by_alias=True,
                ),
            )

            # Log the final model with its fields and aliases
            field_count = len(final_model.model_fields)
            field_info = []
            for field_name, field in selected_model.model_fields.items():
                if field.alias and field.alias != field_name:
                    field_info.append(f"{field_name} (alias: {field.alias})")
                else:
                    field_info.append(field_name)

            logger.info(
                f"Created Pydantic model '{selected_model.__name__}' (renamed from '{original_model_name}') "
                f"from JSON Schema for class '{class_label}' with {field_count} fields: {field_info} "
                f"(selected from {len(all_models)} available models)"
            )

            return final_model

    finally:
        # Clean up the module from sys.modules
        if module_name in sys.modules:
            del sys.modules[module_name]
            logger.debug(f"Cleaned up module '{module_name}' from sys.modules")


def validate_json_schema_for_pydantic(schema: Dict[str, Any]) -> List[str]:
    """
    Validate that a JSON Schema is suitable for Pydantic model generation.

    Args:
        schema: JSON Schema to validate

    Returns:
        List of validation warnings (empty if schema is valid)
    """
    warnings = []

    # Check for required top-level fields
    if "type" not in schema:
        warnings.append("Schema missing 'type' field")

    # Check that type is object for Pydantic models
    if schema.get("type") != "object":
        warnings.append(
            f"Schema type is '{schema.get('type')}' but Pydantic models require type='object'"
        )

    # Check for properties
    if "properties" not in schema:
        warnings.append("Schema has no 'properties' field - model will be empty")

    # Check for potential circular references
    def check_circular(
        obj: Any, path: Optional[List[str]] = None, seen: Optional[set] = None
    ):
        if path is None:
            path = []
        if seen is None:
            seen = set()

        if isinstance(obj, dict):
            # Check for $ref that might cause circular references
            if "$ref" in obj:
                ref = obj["$ref"]
                if ref in seen:
                    warnings.append(
                        f"Potential circular reference detected at path {'.'.join(path)}: {ref}"
                    )
                else:
                    seen.add(ref)

            for key, value in obj.items():
                check_circular(value, path + [key], seen)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                check_circular(item, path + [f"[{i}]"], seen)

    check_circular(schema)

    return warnings
