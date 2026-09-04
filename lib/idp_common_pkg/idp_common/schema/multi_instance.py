# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesize mode: turn a single-record class into a List-of-Class (#715).

A class flagged ``x-aws-idp-multi-instance: true`` has its **effective** schema
replaced by a wrapper::

    {"type": "object", "title": "<Class> Instances",
     "properties": {"instances": {"type": "array", "minItems": 1,
                                  "items": {…the original class schema…}}},
     "required": ["instances"]}

so a user does not have to hand-author that wrapper and degrade a single-record
class's schema. Opt-in per class, never auto-detected — auto-detecting would move
#565's detection problem one stage later and make every single-document section
pay for a wrapper level. Detection is #753 and stays a warning there; it never
flips this flag on anyone's behalf.

**A schema TRANSFORM, not an output envelope.** This is the decision that makes
the feature affordable. Because the wrapper is declared *in* the class schema,
``inference_result`` remains a valid instance of its own declared schema, and the
prompt, the generated Pydantic model, the JSON-Schema validator,
``_filter_extracted_to_schema``, assessment's ``attr_type == "list"`` branch,
``resolve_array_item_thresholds`` and Stickler's Hungarian row matching all work
**unchanged** — none of them special-case anything, they all just read "the class
schema". Treating the wrapper as an out-of-band key instead breaks every one of
them (``_filter_extracted_to_schema`` deletes it and emits ``{}``; assessment
collapses the whole section to one ``{"confidence": 0.5}`` leaf;
``_schema_field_mismatch_reason`` blacklists the section so the escalation ladder
skips it permanently; evaluation builds a Stickler model with zero declared
fields and silently scores 0.0).

Assessment, evaluation, reporting and the analytics agent read the class schema
**from config**, not from the extraction output, so they must derive the same
wrapped schema independently. That is why this is one shared, pure, idempotent
helper called everywhere a class schema is loaded — no boto3, no Strands, no
service imports.

Two things found empirically and easy to get wrong:

1. **The wrapper also designates the synthesized property as the instance axis**
   (``x-aws-idp-instance-array: instances``). The wrapper alone extracts every
   record but leaves ``Section.instance_count`` at 1, so the UI badge, the
   ``extraction_multi_instance_detected`` warning and the
   ``MultiInstanceSections`` / ``MultiInstanceRecordsRecovered`` metrics all
   still report a single instance — everything #694 shipped goes dark on exactly
   the schemas this creates.
2. **The inner ``items`` must NOT keep the class title.**
   ``_find_model_in_module`` picks the generated Pydantic model by title/label
   priority, so a wrapper whose items carry the class title selects the INNER
   model and silently validates one instance instead of the list. The wrapper
   gets ``"<Class> Instances"`` and the items get ``"<Class> Record"``.
"""

from __future__ import annotations

import logging
from typing import Any

from idp_common.config.schema_constants import (
    DEFS_FIELD,
    ID_FIELD,
    REF_FIELD,
    SCHEMA_FIELD,
    SCHEMA_ITEMS,
    SCHEMA_PROPERTIES,
    SCHEMA_TYPE,
    TYPE_ARRAY,
    TYPE_OBJECT,
    X_AWS_IDP_DOCUMENT_TYPE,
    X_AWS_IDP_EVALUATION_MATCH_THRESHOLD,
    X_AWS_IDP_INSTANCE_ARRAY,
    X_AWS_IDP_MULTI_INSTANCE,
)

logger = logging.getLogger(__name__)

#: The synthesized top-level property. Also the value of
#: ``x-aws-idp-instance-array`` on the wrapper, so the instance count, the UI
#: badge and the CloudWatch metrics light up without any extra wiring.
INSTANCES_KEY = "instances"

#: JSON-Schema keywords that describe the SHAPE OF ONE RECORD. They move down
#: into ``items``; anything else on the class is class-level metadata other
#: stages read (identity, classification hints, per-class model/prompt
#: overrides, exclusion flags, few-shot examples) and stays on the wrapper.
#:
#: ``$defs`` is deliberately NOT here: ``{"$ref": "#/$defs/Foo"}`` resolves from
#: the document ROOT, so hoisting definitions to the wrapper is what keeps every
#: ``$ref`` inside the moved properties resolvable.
_RECORD_SHAPE_KEYS = frozenset(
    {
        SCHEMA_TYPE,
        SCHEMA_PROPERTIES,
        "required",
        "additionalProperties",
        "unevaluatedProperties",
        "minProperties",
        "maxProperties",
        "patternProperties",
        "propertyNames",
        "dependentRequired",
        "dependentSchemas",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "title",
        "description",
        # A class-level $ref/$dynamicRef describes the RECORD (2020-12 allows it
        # alongside `properties`), so it must move down with them — left on the
        # wrapper it would claim the wrapper is that type.
        REF_FIELD,
        "$dynamicRef",
        # `examples`/`default`/`readOnly`/`writeOnly`/`deprecated`/`const`/`enum`
        # annotate the RECORD's value. An `examples` list of flat records left on
        # the wrapper would document the wrapper as being a record.
        "examples",
        "default",
        "const",
        "enum",
        "readOnly",
        "writeOnly",
        "deprecated",
    }
)


def is_multi_instance(class_schema: Any) -> bool:
    """True when the class opts into Synthesize mode.

    Tolerant of the string forms a YAML/DynamoDB round-trip can produce
    (``"true"``), because a flag that silently reads as false after a config
    save is the exact class of bug this feature exists to remove.
    """
    if not isinstance(class_schema, dict):
        return False
    raw = class_schema.get(X_AWS_IDP_MULTI_INSTANCE)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1")
    return bool(raw)


def is_wrapped(class_schema: Any) -> bool:
    """True when ``class_schema`` is ALREADY the synthesized wrapper.

    Recognized by the wrapper's own two marks — the flag plus an ``instances``
    array-of-object property — so ``wrap_class_schema`` is idempotent and a
    schema that has been through the transform once is never wrapped twice.
    """
    if not is_multi_instance(class_schema):
        return False
    prop = (class_schema.get(SCHEMA_PROPERTIES) or {}).get(INSTANCES_KEY)
    if not isinstance(prop, dict) or prop.get(SCHEMA_TYPE) != TYPE_ARRAY:
        return False
    items = prop.get(SCHEMA_ITEMS)
    return isinstance(items, dict)


def class_label_of(class_schema: dict[str, Any]) -> str:
    """The class's own name, for building titles. Never raises."""
    for key in (ID_FIELD, X_AWS_IDP_DOCUMENT_TYPE, "title"):
        value = class_schema.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Document"


def wrapper_title(label: str) -> str:
    return f"{label} Instances"


def record_title(label: str) -> str:
    """Title for the inner ``items`` schema.

    MUST differ from the wrapper's title and from the class label, or
    ``_find_model_in_module`` selects the inner model and validates one instance
    where a list was requested (plan D9).
    """
    return f"{label} Record"


def wrap_class_schema(class_schema: Any) -> Any:
    """Return the effective schema for a class, wrapping it when flagged.

    Pure and **idempotent**: an unflagged class is returned unchanged (same
    object), and an already-wrapped schema is returned unchanged. Never mutates
    its input.
    """
    if not isinstance(class_schema, dict) or not is_multi_instance(class_schema):
        return class_schema
    if is_wrapped(class_schema):
        return class_schema

    properties = class_schema.get(SCHEMA_PROPERTIES)
    if not isinstance(properties, dict) or not properties:
        # Nothing to wrap. A class with no attributes already short-circuits
        # extraction (`_handle_empty_schema`), and synthesizing a list of empty
        # objects would only add a nesting level to an empty result.
        logger.warning(
            "Class '%s' sets %s but declares no properties; leaving the schema "
            "unwrapped",
            class_label_of(class_schema),
            X_AWS_IDP_MULTI_INSTANCE,
        )
        return class_schema

    label = class_label_of(class_schema)

    # The record: every shape-describing keyword, with a DISTINCT title (D9).
    record: dict[str, Any] = {
        key: value for key, value in class_schema.items() if key in _RECORD_SHAPE_KEYS
    }
    record[SCHEMA_TYPE] = TYPE_OBJECT
    record["title"] = record_title(label)
    if not record.get("description"):
        record["description"] = f"One complete {label} document."

    # The wrapper: everything else (class identity, classification hints,
    # per-class model/prompt overrides, exclusion flags, few-shot examples,
    # and $defs so every "#/$defs/..." inside `record` still resolves).
    wrapper: dict[str, Any] = {
        key: value
        for key, value in class_schema.items()
        if key not in _RECORD_SHAPE_KEYS
    }
    wrapper[SCHEMA_TYPE] = TYPE_OBJECT
    wrapper["title"] = wrapper_title(label)
    wrapper["description"] = (
        f"One or more {label} documents found in these pages. Return one entry "
        f"in '{INSTANCES_KEY}' per complete {label} document — several separate "
        f"documents of this type can appear in one page range, including when "
        f"one starts part-way down a page."
    )

    instances_property: dict[str, Any] = {
        SCHEMA_TYPE: TYPE_ARRAY,
        "description": (
            f"Every {label} document in these pages, in the order they appear. "
            f"One entry per document — not one entry per page."
        ),
        "minItems": 1,
        SCHEMA_ITEMS: record,
    }

    # Row-level match threshold for evaluation: Stickler REQUIRES Hungarian
    # matching for List[Object], so declaring the threshold here is what makes
    # record alignment order-insensitive. Taken from the class-level threshold,
    # which asked the same question ("is this the same document?") one level up.
    class_match_threshold = class_schema.get(X_AWS_IDP_EVALUATION_MATCH_THRESHOLD)
    if class_match_threshold is not None:
        instances_property[X_AWS_IDP_EVALUATION_MATCH_THRESHOLD] = class_match_threshold

    wrapper[SCHEMA_PROPERTIES] = {INSTANCES_KEY: instances_property}
    wrapper["required"] = [INSTANCES_KEY]

    # Point the existing instance-axis machinery (#694) at the synthesized
    # property. Without this the wrapper extracts every record but reports
    # instance_count = 1, so the UI badge, the multi-instance warning and the
    # CloudWatch metrics all go dark on exactly the schemas this creates.
    wrapper[X_AWS_IDP_INSTANCE_ARRAY] = INSTANCES_KEY
    wrapper[X_AWS_IDP_MULTI_INSTANCE] = True

    # Preserve JSON-Schema preamble ordering conventions where present.
    for key in (SCHEMA_FIELD, ID_FIELD, DEFS_FIELD):
        if key in class_schema:
            wrapper[key] = class_schema[key]

    return wrapper


def unwrap_instances(inference_result: Any) -> list[dict[str, Any]] | None:
    """Return the instance list from a wrapped result, or None.

    ``None`` means "this is not a wrapped result" — every downstream consumer
    can therefore call this unconditionally and fall back to its existing
    single-record path. An empty list is returned as ``[]`` (a wrapped result
    that legitimately found nothing), NOT as ``None``, so "unwrapped" and
    "wrapped but empty" stay distinguishable.
    """
    if not isinstance(inference_result, dict):
        return None
    value = inference_result.get(INSTANCES_KEY)
    if not isinstance(value, list):
        return None
    records = [item for item in value if isinstance(item, dict)]
    if len(records) != len(value):
        # A dropped record is exactly the loss this whole feature exists to
        # surface, so it must not happen quietly.
        logger.warning(
            "%d of %d elements of '%s' were not objects and were dropped; those "
            "records are NOT in the result",
            len(value) - len(records),
            len(value),
            INSTANCES_KEY,
        )
    return records


def wrap_instances(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a wrapped result/baseline from a list of records.

    The inverse of :func:`unwrap_instances`, used by the evaluation-baseline
    migration and by synthetic test-set generation.
    """
    return {INSTANCES_KEY: list(records)}


def effective_class_schema(class_schema: Any) -> Any:
    """Alias for :func:`wrap_class_schema`, read at call sites as intent.

    Reads better where a stage is loading "the class schema" and must get the
    *effective* one: ``schema = effective_class_schema(config_class)``.
    """
    return wrap_class_schema(class_schema)
