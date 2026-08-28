# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Full JSON-Schema constraint validation for extraction output.

The dynamic Pydantic model produced by
``idp_common.schema.pydantic_generator.create_pydantic_model_from_json_schema``
runs with ``field_constraints=True``, so ``enum``, ``pattern``, numeric bounds
and ``minItems``/``maxItems`` are already enforced when the agent calls
``extraction_tool``. This module adds the pieces that path does NOT cover:

1. **``format`` keywords** (``date``, ``date-time``, ``email``, ``uuid``, ...)
   which datamodel-code-generator does not translate into enforced Pydantic
   constraints.
2. **All-errors feedback** — Pydantic surfaces validation errors, but this
   module collects *every* violation with a readable field path so the agent
   (or an escalation pass) can fix them in one round instead of one-at-a-time.
3. A stable **escalation gate** — a single place that answers "is this object
   schema-valid?" for the merged result of a concurrent/batched extraction and
   decides which top-level fields a stronger model should re-extract.

This module is intentionally free of PIL / Strands / boto3 imports so it can be
unit-tested in isolation and imported cheaply.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker

logger = logging.getLogger(__name__)

# Prefix for IDP's custom JSON-Schema extension keys. These are stripped before
# validation because they are not part of the standard vocabulary the validator
# understands. Kept in sync with config.schema_constants but inlined here so
# this module depends only on ``jsonschema`` (a core dependency) and never pulls
# in the heavier schema/pydantic-generator stack.
_IDP_EXTENSION_PREFIX = "x-aws-idp-"

# Cap the number of errors echoed back to the agent so a pathological result
# (e.g. every row of a 1000-row table failing a format check) cannot blow the
# context window. The full count is always reported in the summary line.
_MAX_FEEDBACK_ERRORS = 25

_REQUIRED_PROP_RE = re.compile(r"'([^']+)' is a required property")


@dataclass
class FieldError:
    """A single schema violation, located by a human-readable path."""

    path: str
    message: str
    validator: str

    def __str__(self) -> str:
        loc = self.path or "(root)"
        return f"{loc}: {self.message}"


@dataclass
class ValidationReport:
    """Outcome of validating an extracted object against its class schema."""

    valid: bool
    errors: list[FieldError] = field(default_factory=list)
    # Top-level property names that have at least one violation. Used to scope
    # an escalation re-extraction to only the fields that need it.
    failed_top_level_fields: set[str] = field(default_factory=set)

    def agent_feedback(self) -> str:
        """Concise, actionable message listing the violations for an LLM to fix."""
        if self.valid:
            return "All extracted fields satisfy the schema constraints."

        shown = self.errors[:_MAX_FEEDBACK_ERRORS]
        lines = [
            "The extraction violates the following schema constraints. "
            "Fix each one using the available tools and keep all other data:",
        ]
        lines.extend(f"  - {err}" for err in shown)
        if len(self.errors) > _MAX_FEEDBACK_ERRORS:
            lines.append(
                f"  ... and {len(self.errors) - _MAX_FEEDBACK_ERRORS} more "
                "violation(s) of the same kind."
            )
        return "\n".join(lines)

    def to_metadata(self) -> dict[str, Any]:
        """Compact, JSON-serializable summary for the extraction metadata block."""
        return {
            "valid": self.valid,
            "error_count": len(self.errors),
            "failed_fields": sorted(self.failed_top_level_fields),
            "errors": [
                {"path": e.path, "validator": e.validator, "message": e.message}
                for e in self.errors[:_MAX_FEEDBACK_ERRORS]
            ],
        }


# Standard JSON Schema constraint keywords that are numeric but which the
# Configuration table stores as strings. jsonschema's validators compare them
# directly against instance values (e.g. ``len(instance) < minItems``), which
# raises ``TypeError`` if the constraint is a string — so coerce them back.
_NUMERIC_SCHEMA_KEYWORDS = frozenset(
    {
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minProperties",
        "maxProperties",
        "multipleOf",
    }
)


def _coerce_numeric_keyword(key: str, value: Any) -> Any:
    """Coerce a stringified numeric JSON Schema constraint back to a number."""
    if key in _NUMERIC_SCHEMA_KEYWORDS and isinstance(value, str):
        try:
            return int(value) if value.lstrip("-").isdigit() else float(value)
        except ValueError:
            return value
    return value


def _strip_idp_extensions(schema: Any) -> Any:
    """Recursively drop ``x-aws-idp-*`` keys so only standard JSON Schema remains.

    Mirrors ``schema.pydantic_generator.clean_schema_for_generation`` but is
    inlined to keep this module's dependency surface limited to ``jsonschema``.
    Also coerces stringified numeric constraint keywords (the Configuration
    table stores them as strings) so jsonschema validators don't raise.
    """
    if isinstance(schema, dict):
        return {
            k: _coerce_numeric_keyword(k, _strip_idp_extensions(v))
            for k, v in schema.items()
            if not k.startswith(_IDP_EXTENSION_PREFIX)
        }
    if isinstance(schema, list):
        return [_strip_idp_extensions(item) for item in schema]
    return schema


def _drop_null_properties(value: Any) -> Any:
    """Recursively remove ``null``-valued object properties.

    In this system a ``null`` field means "not present in the document" (the
    extraction prompt instructs the model to "return null if a field is not
    found"), and the Pydantic model generated from the class schema makes every
    non-required property ``Optional[...] = None``. Such nulls therefore round-trip
    through the agent's tools but are **not** valid against the raw JSON Schema,
    which declares ``type: string`` (not ``["string", "null"]``).

    Dropping nulls before validation makes the two views agree:
    - an optional property left null is treated as absent → passes, and
    - a *required* property left null becomes a ``required`` violation → a real,
      actionable error (rather than a confusing "None is not of type 'string'").

    Enum/pattern/format/numeric/minItems checks on present values are unaffected.
    List elements are preserved (only their nested null properties are dropped).
    """
    if isinstance(value, dict):
        return {k: _drop_null_properties(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_null_properties(item) for item in value]
    return value


def _format_path(absolute_path: Any) -> str:
    """Render a jsonschema error path (deque of keys/indices) as ``a.b[3].c``."""
    parts: list[str] = []
    for token in absolute_path:
        if isinstance(token, int):
            parts.append(f"[{token}]")
        else:
            parts.append(f".{token}" if parts else str(token))
    return "".join(parts)


def _top_level_field(error: jsonschema.ValidationError) -> str | None:
    """Identify the top-level property an error belongs to, if any.

    For most errors the first element of ``absolute_path`` is the offending
    top-level property. Root-level ``required`` errors carry no path, so the
    missing property name is parsed from the message instead.
    """
    if error.absolute_path:
        first = error.absolute_path[0]
        return first if isinstance(first, str) else None
    if error.validator == "required":
        match = _REQUIRED_PROP_RE.search(error.message)
        if match:
            return match.group(1)
    return None


def validate_extraction(
    data: dict[str, Any],
    schema: dict[str, Any],
    *,
    check_formats: bool = True,
) -> ValidationReport:
    """Validate an extracted object against its (cleaned) class JSON Schema.

    Args:
        data: The extracted object (``model_dump(mode="json")`` output).
        schema: The class JSON Schema, including ``x-aws-idp-*`` extensions
            (they are stripped here before validation).
        check_formats: When True, enforce JSON-Schema ``format`` keywords
            (``date``, ``email``, ``uuid``, ...) via a ``FormatChecker``. Note
            that ``format: date`` follows JSON Schema (ISO-8601 ``YYYY-MM-DD``);
            disable this if a config uses ``format: date`` for non-ISO values
            (e.g. ``MM/DD/YYYY``). Format checks whose optional backing library
            is absent are skipped silently by ``jsonschema``.

    Returns:
        A ``ValidationReport``. On an unusable schema the report is marked
        valid (fail-open) so validation can never harden extraction into a
        hard failure on a malformed config.
    """
    cleaned = _strip_idp_extensions(schema)

    # A null property means "absent" here (see _drop_null_properties). Validate
    # the null-free view so optional-but-null fields pass while required-but-null
    # fields surface as actionable 'required' errors.
    data = _drop_null_properties(data)

    try:
        format_checker = FormatChecker() if check_formats else None
        validator = Draft202012Validator(cleaned, format_checker=format_checker)
    except jsonschema.SchemaError as exc:
        logger.warning(
            "Skipping schema-constraint validation: invalid class schema",
            extra={"error": str(exc)},
        )
        return ValidationReport(valid=True)

    errors: list[FieldError] = []
    failed_fields: set[str] = set()
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        errors.append(
            FieldError(
                path=_format_path(err.absolute_path),
                message=err.message,
                validator=str(err.validator),
            )
        )
        top = _top_level_field(err)
        if top is not None:
            failed_fields.add(top)

    return ValidationReport(
        valid=not errors,
        errors=errors,
        failed_top_level_fields=failed_fields,
    )


def find_empty_declared_lists(
    data: dict[str, Any], schema: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Split the schema's top-level array fields into ``(empty, populated)``.

    "Empty" means absent, ``null``, or a zero-length list — the three ways a
    declared list can come back carrying no rows. They are grouped together
    deliberately: from the consumer's point of view they are indistinguishable,
    and each is a table's worth of data that is simply gone.

    No JSON Schema constraint is violated by any of them unless the config
    happens to set ``minItems``, which is why this is a separate function rather
    than part of :func:`validate_extraction` — the caller supplies the evidence
    (an OCR pre-flight that found a substantial table) that turns "empty" into
    "wrong".
    """
    empty: list[str] = []
    populated: list[str] = []
    defs = schema.get("$defs") or {}
    for name, field_schema in (schema.get("properties") or {}).items():
        if not isinstance(field_schema, dict):
            continue
        # A top-level array could be declared through a $ref. No shipped config
        # does that today (arrays are inline with `items: {$ref: ...}`), but
        # missing one would silently disable this check rather than fail loudly,
        # and a one-level deref is what topk_resolver already does.
        if "$ref" in field_schema:
            target = defs.get(str(field_schema["$ref"]).split("/")[-1])
            if isinstance(target, dict):
                field_schema = target
        if field_schema.get("type") != "array":
            continue
        value = data.get(name)
        if value is None or (isinstance(value, list) and not value):
            empty.append(name)
        else:
            populated.append(name)
    return empty, populated


def build_empty_list_feedback(
    fields: list[str], tables_detected: int, estimated_row_count: int
) -> str:
    """Agent feedback for declared lists that returned no rows despite evidence.

    Written to counter the specific reasoning that produced this failure live: an
    agent declined the deterministic table parser because one column ("Amount")
    was OCR-corrupted, then returned the whole 100-row list as ``null`` rather
    than extracting it directly — treating "I cannot map this cleanly" as
    "therefore no rows". One unreadable column is not a reason to discard the
    other columns, the other rows, or the field.
    """
    names = ", ".join(f"'{f}'" for f in fields)
    plural = "s" if len(fields) > 1 else ""
    return (
        f"The list field{plural} {names} came back with NO rows, but the OCR text "
        f"for this section contains {tables_detected} table region(s) with roughly "
        f"{estimated_row_count} rows. Those rows are in the document and must be "
        "extracted.\n"
        "Extract them now, using whichever route works:\n"
        "  - the deterministic parser (parse_table + map_table_to_schema + "
        "finalize_table_extraction), or\n"
        "  - extraction_tool / apply_json_patches directly, in batches.\n"
        "Rules that are not negotiable:\n"
        "  - NEVER return null or an empty list for a field whose rows are "
        "visible in the document.\n"
        "  - If ONE column is unreadable or cannot be mapped, still emit every "
        "row, with that single cell set to null or to the literal text you can "
        "see. Do not drop the row, and do not drop the whole list.\n"
        "  - Keep every field you have already extracted correctly."
    )


def build_subset_schema(
    schema: dict[str, Any], fields: set[str] | list[str]
) -> dict[str, Any]:
    """Build a reduced copy of ``schema`` containing only ``fields``.

    Used to scope an escalation re-extraction to just the top-level properties
    that failed validation: a smaller schema means a smaller model, a smaller
    prompt, and smaller output than re-extracting the whole section — faster and
    cheaper, and the already-valid fields are preserved untouched.

    The subset keeps:
    - only the named top-level ``properties`` (with their full constraints),
    - ``required`` intersected with ``fields`` (so we don't demand fields we
      aren't re-extracting), and
    - the entire ``$defs`` block (referenced types are shared and cheap to keep;
      pruning the ``$ref`` graph precisely is not worth the complexity/risk).

    Returns the original schema unchanged when ``fields`` is empty or none of
    them exist as properties (caller should fall back to whole-section
    escalation in that case).
    """
    fields = set(fields)
    properties = schema.get("properties") or {}
    kept = {name: properties[name] for name in fields if name in properties}
    if not kept:
        return schema

    subset: dict[str, Any] = {
        k: v for k, v in schema.items() if k not in ("properties", "required")
    }
    subset["properties"] = kept
    original_required = schema.get("required") or []
    intersected = [r for r in original_required if r in kept]
    if intersected:
        subset["required"] = intersected
    return subset
