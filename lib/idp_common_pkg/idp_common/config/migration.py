from typing import Any, Dict, List, Optional, Union
from .schema_constants import (
    # JSON Schema standard fields
    SCHEMA_FIELD,
    ID_FIELD,
    DEFS_FIELD,
    REF_FIELD,
    SCHEMA_TYPE,
    SCHEMA_PROPERTIES,
    SCHEMA_ITEMS,
    SCHEMA_REQUIRED,
    SCHEMA_DESCRIPTION,
    TYPE_OBJECT,
    TYPE_ARRAY,
    TYPE_STRING,
    # AWS IDP extensions
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EXAMPLES,
    X_AWS_IDP_LIST_ITEM_DESCRIPTION,
    X_AWS_IDP_ORIGINAL_NAME,
    X_AWS_IDP_EVALUATION_METHOD,
    X_AWS_IDP_CONFIDENCE_THRESHOLD,
    X_AWS_IDP_PROMPT_OVERRIDE,
    X_AWS_IDP_CLASS_PROMPT,
    X_AWS_IDP_ATTRIBUTES_PROMPT,
    X_AWS_IDP_IMAGE_PATH,
    X_AWS_IDP_DOCUMENT_NAME_REGEX,
    X_AWS_IDP_EXCLUDE_FROM_PROCESSING,
    X_AWS_IDP_EXCLUSION_REASON,
    X_AWS_IDP_PAGE_CONTENT_REGEX,
    VALID_EVALUATION_METHODS,
    MAX_PROMPT_OVERRIDE_LENGTH,
    # Attribute types (for legacy migration only)
    ATTRIBUTE_TYPE_SIMPLE,
    ATTRIBUTE_TYPE_GROUP,
    ATTRIBUTE_TYPE_LIST,
    # Legacy field names
    LEGACY_ATTRIBUTES,
    LEGACY_NAME,
    LEGACY_DESCRIPTION,
    LEGACY_ATTRIBUTE_TYPE,
    LEGACY_GROUP_ATTRIBUTES,
    LEGACY_LIST_ITEM_TEMPLATE,
    LEGACY_ITEM_ATTRIBUTES,
    LEGACY_ITEM_DESCRIPTION,
    LEGACY_EVALUATION_METHOD,
    LEGACY_CONFIDENCE_THRESHOLD,
    LEGACY_PROMPT_OVERRIDE,
    LEGACY_EXAMPLES,
    LEGACY_CLASS_PROMPT,
    LEGACY_ATTRIBUTES_PROMPT,
    LEGACY_IMAGE_PATH,
    LEGACY_DOCUMENT_NAME_REGEX,
    LEGACY_DOCUMENT_PAGE_CONTENT_REGEX,
)


def is_legacy_format(data: Union[Dict, List, Any]) -> bool:
    """
    Detect if data is in legacy format (vs JSON Schema).

    Legacy format has:
    - "attributes" key with list value
    - No "$schema", "$id", or "properties" keys

    JSON Schema format has:
    - "$schema", "$id", or "properties" keys
    - "attributes" as dict (nested schema) or absent

    Args:
        data: Configuration data (dict, list, or other)

    Returns:
        True if legacy format, False if JSON Schema or unknown
    """
    if data is None:
        return False

    # Handle list of classes
    if isinstance(data, list):
        if len(data) == 0:
            return False
        # Check first element
        return is_legacy_format(data[0])

    # Handle single class/schema dict
    if isinstance(data, dict):
        # Definitive JSON Schema markers
        if any(key in data for key in [SCHEMA_FIELD, ID_FIELD, SCHEMA_PROPERTIES]):
            return False

        # Special marker for our schema format
        if X_AWS_IDP_DOCUMENT_TYPE in data:
            return False

        # Legacy marker: attributes is a list
        if LEGACY_ATTRIBUTES in data:
            attributes = data[LEGACY_ATTRIBUTES]
            return isinstance(attributes, list)

        # No attributes at all - assume modern format
        return False

    # Unknown type
    return False


def is_json_schema_format(data: Union[Dict, List, Any]) -> bool:
    """
    Detect if data is in JSON Schema format.

    Inverse of is_legacy_format for clarity.

    Args:
        data: Configuration data

    Returns:
        True if JSON Schema format, False if legacy or unknown
    """
    if data is None:
        return False
    return not is_legacy_format(data)


def _migrate_examples(examples: Any) -> Any:
    """
    Normalize a class's few-shot examples to the canonical ``x-aws-idp-*`` keys.

    The examples *container* has always been migrated to ``x-aws-idp-examples``,
    but each entry's fields kept their legacy camelCase names
    (``classPrompt`` / ``attributesPrompt`` / ``imagePath``) even though the
    canonical names are what the docs describe. Both spellings are accepted when
    examples are consumed (see ``idp_common.utils.few_shot_example_builder``);
    this converts them on migration so stored configs converge on one form.

    A pre-existing canonical key always wins, and any other keys (``name``,
    ``id``, ...) are preserved untouched. Non-list / non-dict input is returned
    as-is so malformed config never breaks migration.

    Args:
        examples: The value of a class's examples list.

    Returns:
        The examples list with canonical field keys.
    """
    if not isinstance(examples, list):
        return examples

    aliases = (
        (X_AWS_IDP_CLASS_PROMPT, LEGACY_CLASS_PROMPT),
        (X_AWS_IDP_ATTRIBUTES_PROMPT, LEGACY_ATTRIBUTES_PROMPT),
        (X_AWS_IDP_IMAGE_PATH, LEGACY_IMAGE_PATH),
    )

    migrated = []
    for example in examples:
        if not isinstance(example, dict):
            migrated.append(example)
            continue
        entry = dict(example)
        for canonical, legacy in aliases:
            if legacy in entry:
                value = entry.pop(legacy)
                entry.setdefault(canonical, value)
        migrated.append(entry)
    return migrated


def migrate_legacy_to_schema(
    legacy_classes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Migrate legacy classes to JSON Schema format.

    Each legacy class is treated as a document type in the new format.
    Always returns an array of JSON Schema objects for consistency.

    Args:
        legacy_classes: List of legacy class configurations
        validate: Whether to validate the result against JSON Schema (default: True)

    Returns:
        List of migrated JSON Schema objects (always array, even for single schema)

    Raises:
        ValueError: If validation is enabled and result is invalid
    """
    migrated_classes = []

    for class_config in legacy_classes:
        migrated_class = {
            LEGACY_NAME: class_config.get(LEGACY_NAME, ""),
            LEGACY_DESCRIPTION: class_config.get(LEGACY_DESCRIPTION, ""),
            X_AWS_IDP_DOCUMENT_TYPE: True,  # Mark as document type
            LEGACY_ATTRIBUTES: {
                SCHEMA_TYPE: TYPE_OBJECT,
                SCHEMA_PROPERTIES: {},
                SCHEMA_REQUIRED: [],
            },
        }

        # Migrate examples if present
        if LEGACY_EXAMPLES in class_config:
            migrated_class[X_AWS_IDP_EXAMPLES] = _migrate_examples(
                class_config[LEGACY_EXAMPLES]
            )

        # Migrate regex patterns if present
        if LEGACY_DOCUMENT_NAME_REGEX in class_config:
            migrated_class[X_AWS_IDP_DOCUMENT_NAME_REGEX] = class_config[
                LEGACY_DOCUMENT_NAME_REGEX
            ]

        if LEGACY_DOCUMENT_PAGE_CONTENT_REGEX in class_config:
            migrated_class[X_AWS_IDP_PAGE_CONTENT_REGEX] = class_config[
                LEGACY_DOCUMENT_PAGE_CONTENT_REGEX
            ]

        # Migrate excluded-class flags (support legacy snake_case and new
        # x-aws-idp-* keys both as input)
        if X_AWS_IDP_EXCLUDE_FROM_PROCESSING in class_config:
            migrated_class[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] = class_config[
                X_AWS_IDP_EXCLUDE_FROM_PROCESSING
            ]
        elif "exclude_from_processing" in class_config:
            migrated_class[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] = class_config[
                "exclude_from_processing"
            ]

        if X_AWS_IDP_EXCLUSION_REASON in class_config:
            migrated_class[X_AWS_IDP_EXCLUSION_REASON] = class_config[
                X_AWS_IDP_EXCLUSION_REASON
            ]
        elif "exclusion_reason" in class_config:
            migrated_class[X_AWS_IDP_EXCLUSION_REASON] = class_config[
                "exclusion_reason"
            ]

        legacy_attributes = class_config.get(LEGACY_ATTRIBUTES, [])

        for attr in legacy_attributes:
            attr_name = attr.get(LEGACY_NAME, "")
            attr_type = attr.get(LEGACY_ATTRIBUTE_TYPE, ATTRIBUTE_TYPE_SIMPLE)

            if attr_type == ATTRIBUTE_TYPE_SIMPLE:
                schema_attr = _migrate_simple_attribute(attr)
            elif attr_type == ATTRIBUTE_TYPE_GROUP:
                schema_attr = _migrate_group_attribute(attr)
            elif attr_type == ATTRIBUTE_TYPE_LIST:
                schema_attr = _migrate_list_attribute(attr)
            else:
                schema_attr = _migrate_simple_attribute(attr)

            migrated_class[LEGACY_ATTRIBUTES][SCHEMA_PROPERTIES][attr_name] = (
                schema_attr
            )

        migrated_classes.append(migrated_class)

    # Convert class array to JSON Schema format
    result = _convert_classes_to_json_schema(migrated_classes)

    return result


def _validate_and_set_aws_extensions(
    schema_attr: Dict[str, Any], source_attr: Dict[str, Any]
) -> None:
    """
    Set AWS IDP extension fields without validation.

    Migration should preserve data as-is from legacy format.

    Args:
        schema_attr: Target schema attribute to update
        source_attr: Source attribute with potential AWS extensions
    """
    if LEGACY_EVALUATION_METHOD in source_attr:
        schema_attr[X_AWS_IDP_EVALUATION_METHOD] = source_attr[LEGACY_EVALUATION_METHOD]

    if LEGACY_CONFIDENCE_THRESHOLD in source_attr:
        threshold = source_attr[LEGACY_CONFIDENCE_THRESHOLD]
        # Convert string to float if needed
        if isinstance(threshold, str):
            try:
                threshold = float(threshold)
            except (ValueError, TypeError):
                threshold = source_attr[LEGACY_CONFIDENCE_THRESHOLD]
        schema_attr[X_AWS_IDP_CONFIDENCE_THRESHOLD] = threshold

    if LEGACY_PROMPT_OVERRIDE in source_attr:
        schema_attr[X_AWS_IDP_PROMPT_OVERRIDE] = source_attr[LEGACY_PROMPT_OVERRIDE]


def _migrate_simple_attribute(attr: Dict[str, Any]) -> Dict[str, Any]:
    # Simple attributes are just string properties in JSON Schema
    schema_attr = {
        SCHEMA_TYPE: TYPE_STRING,
        SCHEMA_DESCRIPTION: attr.get(LEGACY_DESCRIPTION, ""),
    }

    _validate_and_set_aws_extensions(schema_attr, attr)

    return schema_attr


def _migrate_group_attribute(attr: Dict[str, Any]) -> Dict[str, Any]:
    # Group attributes are object properties with nested properties
    schema_attr = {
        SCHEMA_TYPE: TYPE_OBJECT,
        SCHEMA_DESCRIPTION: attr.get(LEGACY_DESCRIPTION, ""),
        SCHEMA_PROPERTIES: {},
    }

    group_attrs = attr.get(LEGACY_GROUP_ATTRIBUTES, [])
    for group_attr in group_attrs:
        attr_name = group_attr.get(LEGACY_NAME, "")
        schema_attr[SCHEMA_PROPERTIES][attr_name] = _migrate_simple_attribute(
            group_attr
        )

    _validate_and_set_aws_extensions(schema_attr, attr)

    return schema_attr


def _migrate_list_attribute(attr: Dict[str, Any]) -> Dict[str, Any]:
    # List attributes are array properties in JSON Schema
    schema_attr = {
        SCHEMA_TYPE: TYPE_ARRAY,
        SCHEMA_DESCRIPTION: attr.get(LEGACY_DESCRIPTION, ""),
    }

    list_item_template = attr.get(LEGACY_LIST_ITEM_TEMPLATE, {})
    item_attrs = list_item_template.get(LEGACY_ITEM_ATTRIBUTES, [])

    if LEGACY_ITEM_DESCRIPTION in list_item_template:
        schema_attr[X_AWS_IDP_LIST_ITEM_DESCRIPTION] = list_item_template[
            LEGACY_ITEM_DESCRIPTION
        ]

    if len(item_attrs) == 1:
        # Single item attribute - use it directly as items schema
        item_schema = _migrate_simple_attribute(item_attrs[0])
        if LEGACY_NAME in item_attrs[0]:
            item_schema[X_AWS_IDP_ORIGINAL_NAME] = item_attrs[0][LEGACY_NAME]
        schema_attr[SCHEMA_ITEMS] = item_schema
    else:
        # Multiple item attributes - create an object schema
        schema_attr[SCHEMA_ITEMS] = {SCHEMA_TYPE: TYPE_OBJECT, SCHEMA_PROPERTIES: {}}
        for item_attr in item_attrs:
            attr_name = item_attr.get(LEGACY_NAME, "")
            schema_attr[SCHEMA_ITEMS][SCHEMA_PROPERTIES][attr_name] = (
                _migrate_simple_attribute(item_attr)
            )

    # Use the standard validation function for AWS extensions
    # This handles evaluation_method, confidence_threshold, and prompt_override
    _validate_and_set_aws_extensions(schema_attr, attr)

    return schema_attr


def _add_aws_extensions(legacy_attr: Dict[str, Any], schema: Dict[str, Any]) -> None:
    """Add AWS extension fields back to legacy format (for reverse migration if needed)."""
    if X_AWS_IDP_EVALUATION_METHOD in schema:
        legacy_attr[LEGACY_EVALUATION_METHOD] = schema[X_AWS_IDP_EVALUATION_METHOD]

    if X_AWS_IDP_CONFIDENCE_THRESHOLD in schema:
        threshold = schema[X_AWS_IDP_CONFIDENCE_THRESHOLD]
        legacy_attr[LEGACY_CONFIDENCE_THRESHOLD] = (
            str(threshold) if threshold is not None else None
        )

    if X_AWS_IDP_PROMPT_OVERRIDE in schema:
        legacy_attr[LEGACY_PROMPT_OVERRIDE] = schema[X_AWS_IDP_PROMPT_OVERRIDE]


def _sanitize_attribute_schema(attribute: Any) -> Any:
    """Remove internal fields (id, name) from attribute schema recursively."""
    if not attribute or not isinstance(attribute, dict):
        return attribute

    # Create a copy without 'id' and 'name' fields
    sanitized = {k: v for k, v in attribute.items() if k not in ("id", "name")}

    # Recursively sanitize nested structures
    if SCHEMA_ITEMS in sanitized:
        sanitized[SCHEMA_ITEMS] = _sanitize_attribute_schema(sanitized[SCHEMA_ITEMS])

    if SCHEMA_PROPERTIES in sanitized:
        sanitized[SCHEMA_PROPERTIES] = {
            prop_name: _sanitize_attribute_schema(prop_value)
            for prop_name, prop_value in sanitized[SCHEMA_PROPERTIES].items()
        }

    return sanitized


def _find_referenced_classes(
    root_class: Dict[str, Any],
    all_classes: List[Dict[str, Any]],
    visited: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Find all classes referenced by root_class (recursively)."""
    if visited is None:
        visited = set()

    referenced = []

    def process_properties(properties: Dict[str, Any]) -> None:
        for attr in properties.values():
            # Check direct $ref
            if isinstance(attr, dict) and REF_FIELD in attr:
                ref_name = attr[REF_FIELD].replace(f"#{DEFS_FIELD}/", "")
                if ref_name not in visited:
                    ref_class = next(
                        (c for c in all_classes if c[LEGACY_NAME] == ref_name), None
                    )
                    if ref_class and not ref_class.get(X_AWS_IDP_DOCUMENT_TYPE):
                        visited.add(ref_name)
                        referenced.append(ref_class)
                        # Recursively find references in this class
                        referenced.extend(
                            _find_referenced_classes(ref_class, all_classes, visited)
                        )

            # Check array items $ref
            if (
                isinstance(attr, dict)
                and SCHEMA_ITEMS in attr
                and isinstance(attr[SCHEMA_ITEMS], dict)
            ):
                if REF_FIELD in attr[SCHEMA_ITEMS]:
                    ref_name = attr[SCHEMA_ITEMS][REF_FIELD].replace(
                        f"#{DEFS_FIELD}/", ""
                    )
                    if ref_name not in visited:
                        ref_class = next(
                            (c for c in all_classes if c[LEGACY_NAME] == ref_name), None
                        )
                        if ref_class and not ref_class.get(X_AWS_IDP_DOCUMENT_TYPE):
                            visited.add(ref_name)
                            referenced.append(ref_class)
                            referenced.extend(
                                _find_referenced_classes(
                                    ref_class, all_classes, visited
                                )
                            )

            # Check nested object properties
            if (
                isinstance(attr, dict)
                and attr.get(SCHEMA_TYPE) == TYPE_OBJECT
                and SCHEMA_PROPERTIES in attr
            ):
                process_properties(attr[SCHEMA_PROPERTIES])

    attributes = root_class.get(LEGACY_ATTRIBUTES, {})
    properties = attributes.get(SCHEMA_PROPERTIES, {})
    process_properties(properties)

    return referenced


def _convert_classes_to_json_schema(
    classes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert class array to JSON Schema format. Always returns array of schemas."""
    if not classes:
        return []

    # Find all document type classes
    doc_type_classes = [
        cls for cls in classes if cls.get(X_AWS_IDP_DOCUMENT_TYPE) is True
    ]

    # If no document types, treat first class as document type (backward compatibility)
    if not doc_type_classes:
        doc_type_classes = [classes[0]]
        # Mark it as document type
        doc_type_classes[0][X_AWS_IDP_DOCUMENT_TYPE] = True

    # Build schema for each document type
    schemas = []
    for doc_type_class in doc_type_classes:
        # Find classes referenced by this document type
        referenced_classes = _find_referenced_classes(doc_type_class, classes)

        # Build $defs only for referenced classes
        defs = {}
        for cls in referenced_classes:
            sanitized_props = {
                attr_name: _sanitize_attribute_schema(attr_value)
                for attr_name, attr_value in cls.get(LEGACY_ATTRIBUTES, {})
                .get(SCHEMA_PROPERTIES, {})
                .items()
            }

            defs[cls[LEGACY_NAME]] = {
                SCHEMA_TYPE: TYPE_OBJECT,
                SCHEMA_PROPERTIES: sanitized_props,
            }
            if cls.get(LEGACY_DESCRIPTION):
                defs[cls[LEGACY_NAME]][SCHEMA_DESCRIPTION] = cls[LEGACY_DESCRIPTION]

            required = cls.get(LEGACY_ATTRIBUTES, {}).get(SCHEMA_REQUIRED, [])
            if required:
                defs[cls[LEGACY_NAME]][SCHEMA_REQUIRED] = required

        # Build main schema properties
        sanitized_props = {
            attr_name: _sanitize_attribute_schema(attr_value)
            for attr_name, attr_value in doc_type_class.get(LEGACY_ATTRIBUTES, {})
            .get(SCHEMA_PROPERTIES, {})
            .items()
        }

        schema = {
            SCHEMA_FIELD: "https://json-schema.org/draft/2020-12/schema",
            ID_FIELD: doc_type_class[LEGACY_NAME],
            X_AWS_IDP_DOCUMENT_TYPE: doc_type_class[LEGACY_NAME],
            SCHEMA_TYPE: TYPE_OBJECT,
            SCHEMA_PROPERTIES: sanitized_props,
        }

        if doc_type_class.get(LEGACY_DESCRIPTION):
            schema[SCHEMA_DESCRIPTION] = doc_type_class[LEGACY_DESCRIPTION]

        required = doc_type_class.get(LEGACY_ATTRIBUTES, {}).get(SCHEMA_REQUIRED, [])
        if required:
            schema[SCHEMA_REQUIRED] = required

        # Add examples if present (check both legacy and new key). Entry fields are
        # normalized to the canonical x-aws-idp-* names either way.
        if LEGACY_EXAMPLES in doc_type_class and doc_type_class[LEGACY_EXAMPLES]:
            schema[X_AWS_IDP_EXAMPLES] = _migrate_examples(
                doc_type_class[LEGACY_EXAMPLES]
            )
        elif (
            X_AWS_IDP_EXAMPLES in doc_type_class and doc_type_class[X_AWS_IDP_EXAMPLES]
        ):
            schema[X_AWS_IDP_EXAMPLES] = _migrate_examples(
                doc_type_class[X_AWS_IDP_EXAMPLES]
            )

        # Add regex patterns if present
        if X_AWS_IDP_DOCUMENT_NAME_REGEX in doc_type_class:
            schema[X_AWS_IDP_DOCUMENT_NAME_REGEX] = doc_type_class[
                X_AWS_IDP_DOCUMENT_NAME_REGEX
            ]

        if X_AWS_IDP_PAGE_CONTENT_REGEX in doc_type_class:
            schema[X_AWS_IDP_PAGE_CONTENT_REGEX] = doc_type_class[
                X_AWS_IDP_PAGE_CONTENT_REGEX
            ]

        # Propagate excluded-class flags into final schema
        if X_AWS_IDP_EXCLUDE_FROM_PROCESSING in doc_type_class:
            schema[X_AWS_IDP_EXCLUDE_FROM_PROCESSING] = doc_type_class[
                X_AWS_IDP_EXCLUDE_FROM_PROCESSING
            ]

        if X_AWS_IDP_EXCLUSION_REASON in doc_type_class:
            schema[X_AWS_IDP_EXCLUSION_REASON] = doc_type_class[
                X_AWS_IDP_EXCLUSION_REASON
            ]

        if defs:
            schema[DEFS_FIELD] = defs

        schemas.append(schema)

    # Always return array of schemas for consistency
    return schemas
