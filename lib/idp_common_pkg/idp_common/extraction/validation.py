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

_REQUIRED_PROP_RE = re.compile(r"""(['"])(.+?)\1 is a required property""")


@dataclass
class FieldError:
    """A single schema violation, located by a human-readable path."""

    path: str
    message: str
    validator: str
    # The top-level property this error belongs to (None for unattributable root
    # errors). Lets callers reason PER FIELD rather than by total count — see
    # ``escalation_outcome`` for why the distinction matters.
    field: str | None = None
    # For a ``required`` error: whether the missing property is declared as a
    # SCALAR in the schema (None when unknown). Drives the abstention note in
    # ``agent_feedback``: "leave it null" is right for an unreadable cell and
    # wrong for a whole list or group.
    leaf: bool | None = None

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

        # A missing/null REQUIRED scalar is the one violation an agent may be
        # unable to fix honestly. Saying only "fix each one" invites it to invent a
        # plausible value — a fabricated 0.0 for an unreadable number is
        # schema-valid, silent and indistinguishable from a real zero (#782). So
        # for SCALAR leaves, ask for the value only if it is readable and make
        # abstention a sanctioned outcome. Deliberately NOT for a missing list or
        # group: "leave it null" there would sanction nulling a whole table, which
        # is the #666 failure — and ``build_empty_list_feedback`` says the opposite.
        abstainable = [
            err for err in self.errors if err.validator == "required" and err.leaf
        ]
        shown = list(self.errors[:_MAX_FEEDBACK_ERRORS])
        if abstainable and not any(e is err for err in shown for e in abstainable):
            # The note must never refer to an error the agent cannot see. APPEND
            # rather than displace: the 25th shown error may be the only instance
            # of its kind.
            shown.append(abstainable[0])
        lines = [
            "The extraction violates the following schema constraints. "
            "Fix each one using the available tools and keep all other data:",
        ]
        lines.extend(f"  - {err}" for err in shown)
        if len(self.errors) > len(shown):
            lines.append(
                f"  ... and {len(self.errors) - len(shown)} more violation(s)."
            )
        if abstainable:
            lines.append(
                "  NOTE on a missing required VALUE (a single cell or field, not a "
                "list): supply it ONLY if you can actually read it in the document. "
                "If it is genuinely absent, unreadable or illegible, leave that one "
                "cell null — do NOT guess, and do NOT substitute a placeholder such "
                "as 0, false or an empty string. A null cell is recorded and reported "
                "as missing, which is correct; an invented value is indistinguishable "
                "from a real one and is worse than no answer. This never applies to "
                "a list or group: never null a whole list or drop a row — emit every "
                "row and null only the unreadable cell."
            )
        return "\n".join(lines)

    def errors_by_field(self) -> dict[str | None, int]:
        """Error count per top-level field (``None`` bucket for root errors)."""
        counts: dict[str | None, int] = {}
        for err in self.errors:
            counts[err.field] = counts.get(err.field, 0) + 1
        return counts

    def to_metadata(self) -> dict[str, Any]:
        """Compact, JSON-serializable summary for the extraction metadata block."""
        return {
            "valid": self.valid,
            "error_count": len(self.errors),
            "failed_fields": sorted(self.failed_top_level_fields),
            "errors_by_field": {
                (k if k is not None else "(root)"): v
                for k, v in sorted(
                    self.errors_by_field().items(), key=lambda kv: str(kv[0])
                )
            },
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


def coerce_numeric_schema_keywords(schema: Any) -> Any:
    """Return ``schema`` with every stringified numeric constraint made numeric.

    ``ConfigurationRecord._stringify_values`` turns every numeric scalar into a
    string on the way into the Configuration table ("avoids Decimal conversion
    issues") and it recurses into a class's ``json_schema``; nothing coerces them
    back on read, because ``classes`` is typed ``List[Dict[str, Any]]`` so
    validation never descends into it. A class authored in the Web UI therefore
    arrives with ``minItems: "100"``.

    Readers that compare such a value against a number raise ``TypeError``, and
    the ones that guard themselves do it one site at a time — five separate
    implementations of this rule had accumulated (this module, the Stickler
    mapper, and three ad-hoc guards in ``extraction.service``) before a sixth
    reader crashed on the constraint it was supposed to enforce (#797). Calling
    this once where the schema enters the service gives every reader numbers.

    Returns the input object unchanged when there was nothing to coerce, so the
    common case allocates nothing and callers can keep sharing the config's own
    dict. Never mutates its input. A value that cannot be read as a number is
    left exactly as it is and logged at WARNING: the constraint is then ignored
    by the guards downstream, and "constraint silently ignored" is the failure
    mode that let #797 hide, so it must not be silent here too.
    """
    coerced, changed = _coerce_numerics(schema, "")
    return coerced if changed else schema


def _coerce_numerics(node: Any, path: str) -> tuple[Any, bool]:
    """Recursive worker for :func:`coerce_numeric_schema_keywords`.

    Returns ``(value, changed)`` so an untouched subtree can be handed back by
    identity rather than rebuilt.
    """
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        changed = False
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            new_value, sub_changed = _coerce_numerics(value, here)
            if isinstance(key, str) and key in _NUMERIC_SCHEMA_KEYWORDS:
                candidate = _coerce_numeric_keyword(key, new_value)
                if candidate is not new_value:
                    new_value, sub_changed = candidate, True
                elif isinstance(new_value, str):
                    logger.warning(
                        "Schema constraint %s at '%s' is not a number (%r); it "
                        "will be ignored rather than enforced",
                        key,
                        here,
                        new_value,
                    )
            out[key] = new_value
            changed = changed or sub_changed
        return (out, True) if changed else (node, False)
    if isinstance(node, list):
        items = []
        changed = False
        for index, item in enumerate(node):
            new_item, sub_changed = _coerce_numerics(item, f"{path}[{index}]")
            items.append(new_item)
            changed = changed or sub_changed
        return (items, True) if changed else (node, False)
    return node, False


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


_SCALAR_JSON_TYPES = frozenset({"string", "number", "integer", "boolean"})


def _required_error_is_scalar(
    error: jsonschema.ValidationError, root: dict[str, Any] | None = None
) -> bool | None:
    """For a ``required`` error, is the missing property declared as a scalar?

    ``error.schema`` is the object subschema whose ``required`` list failed, so the
    missing property's declaration is ``error.schema["properties"][name]``. Returns
    None for non-``required`` errors or when the declaration cannot be resolved
    (e.g. a ``$ref``), so callers treat unknown as "do not assume a leaf".
    """
    if error.validator != "required":
        return None
    match = _REQUIRED_PROP_RE.search(error.message)
    if not match or not isinstance(error.schema, dict):
        return None
    # group(2) is the name; group(1) is the quote character (jsonschema uses
    # repr(), which switches to double quotes for a name with an apostrophe).
    prop = (error.schema.get("properties") or {}).get(match.group(2))
    if isinstance(prop, dict) and "const" in prop:
        # A const leaf is not abstainable: the transport model does not widen it,
        # so telling the agent "leave it null" would be rejected. Agrees with
        # ``nullable_leaves_for_transport``.
        return False
    if isinstance(prop, dict) and "$ref" in prop and isinstance(root, dict):
        # One level of local $ref via the root $defs, so a scalar declared as
        # `{"$ref": "#/$defs/Amount"}` still counts as a leaf.
        target = str(prop["$ref"]).split("/")[-1]
        prop = (root.get("$defs") or {}).get(target, prop)
    if not isinstance(prop, dict):
        return None
    declared = prop.get("type")
    types = [declared] if isinstance(declared, str) else declared
    if not isinstance(types, list) or not types:
        return None
    return all(x in _SCALAR_JSON_TYPES for x in types if x != "null")


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
            return match.group(2)
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
        top = _top_level_field(err)
        errors.append(
            FieldError(
                path=_format_path(err.absolute_path),
                message=err.message,
                validator=str(err.validator),
                field=top,
                leaf=_required_error_is_scalar(err, cleaned),
            )
        )
        if top is not None:
            failed_fields.add(top)

    return ValidationReport(
        valid=not errors,
        errors=errors,
        failed_top_level_fields=failed_fields,
    )


def shard_validation_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The class schema with every ``required`` list and every ``minItems`` /
    ``maxItems`` bound removed.

    For validating ONE SHARD of a sharded agentic section. A shard sees only its
    pages and is told "if a field does not appear in your pages, leave it null —
    another shard will provide it", so a document-level required scalar is
    legitimately null on most shards and a declared list legitimately empty on a
    cover page. Validating a shard against the full schema turned that correct
    behaviour into up to three extra agent turns per shard and told a cover-page
    shard to produce rows its pages do not contain (the #666 fabrication pressure).
    Row-count bounds describe the whole section, not one shard, in BOTH directions
    — a shard whose pages hold more rows than the section's ``maxItems`` must not
    be told to drop rows before the merge counts them. What remains — type,
    format, enum, pattern, value bounds, ``uniqueItems`` — is what a shard CAN fix.
    Presence and row counts are enforced once, on the merged section, against the
    real schema.

    Only the KEYWORD forms are dropped: ``required`` as a list of names, and the
    bounds as integers. A property literally named ``minItems`` (its value is a
    schema, not an int) survives.
    """
    return _strip_shard_keywords(schema)


def _strip_shard_keywords(node: Any) -> Any:
    if isinstance(node, list):
        return [_strip_shard_keywords(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {
        k: _strip_shard_keywords(v)
        for k, v in node.items()
        if not (k == "required" and isinstance(v, list))
        and not (k in ("minItems", "maxItems") and isinstance(v, int))
    }


def required_null_paths(
    data: Any, schema: dict[str, Any], *, limit: int = 200
) -> tuple[list[str], int]:
    """Paths of REQUIRED properties that are null or absent, plus the total count.

    Independent of ``extraction.validation.enabled`` on purpose: with a nullable
    transport model an abstention no longer trips the Pydantic guard, so this is
    the record that survives when validation is switched off (v0.6-migrated
    stacks carry ``enabled: false``). Recorded as ``metadata.abstained_fields``.

    Counts SCALAR leaves only. A null list or group is not an abstention — it is
    the whole-list loss this codebase treats as a defect (#666) — and must not be
    filed under the benign label. Walks objects and array items; ``$ref`` is
    resolved one level via ``$defs``; ``anyOf``/``oneOf`` branches,
    ``additionalProperties`` and ``prefixItems`` are not walked (a known
    under-count, never an over-count).
    """
    defs = (schema or {}).get("$defs") or {}

    def _deref(node: Any) -> Any:
        if isinstance(node, dict) and "$ref" in node:
            return defs.get(str(node["$ref"]).split("/")[-1], {})
        return node

    found: list[str] = []
    total = 0

    def _declares_scalar(prop: Any) -> bool:
        if not isinstance(prop, dict):
            return False
        if "properties" in prop or "items" in prop or "const" in prop:
            return False
        declared = prop.get("type")
        types = [declared] if isinstance(declared, str) else declared
        if not isinstance(types, list) or not types:
            return False
        return all(x in _SCALAR_JSON_TYPES for x in types if x != "null")

    def _walk(value: Any, node: Any, path: str) -> None:
        nonlocal total
        node = _deref(node)
        if not isinstance(node, dict):
            return
        if isinstance(value, dict):
            props = node.get("properties") or {}
            for name in node.get("required") or []:
                if value.get(name) is None and _declares_scalar(
                    _deref(props.get(name))
                ):
                    total += 1
                    if len(found) < limit:
                        found.append(f"{path}.{name}" if path else name)
            for name, sub in props.items():
                if name in value:
                    _walk(value[name], sub, f"{path}.{name}" if path else name)
        elif isinstance(value, list):
            item_schema = node.get("items")
            for i, item in enumerate(value):
                _walk(item, item_schema, f"{path}[{i}]")

    _walk(data, schema, "")
    return found, total


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


def _populated_leaves(value: Any) -> int:
    """Count leaves that carry a value. null, empty/blank strings, empty
    containers and all-null rows contribute nothing."""
    if value is None:
        return 0
    if isinstance(value, dict):
        return sum(_populated_leaves(v) for v in value.values())
    if isinstance(value, list):
        return sum(_populated_leaves(v) for v in value)
    if isinstance(value, str) and not value.strip():
        return 0
    return 1


def _populated_rows(value: Any) -> int | None:
    """Rows of a list that carry at least one value; None if not a list."""
    if not isinstance(value, list):
        return None
    return sum(1 for row in value if _populated_leaves(row) > 0)


# Constraints whose ONLY fix is removing something. A row-count decrease on a
# field that violated one of these is a correction, not a truncation.
_REMOVAL_FIXES = frozenset({"maxItems", "uniqueItems"})


def _field_errors(report: ValidationReport | None, field: str) -> list[FieldError]:
    if report is None:
        return []
    return [e for e in report.errors if e.field == field]


def escalation_data_loss(
    original: dict[str, Any],
    escalated: dict[str, Any],
    before: ValidationReport | None = None,
) -> list[str]:
    """Top-level fields where the escalated result carries LESS data than the
    original without that reduction being the fix. Empty when nothing was lost.

    Measured on populated leaves (values that are not null/blank), not on raw
    row counts: a row of all-null placeholders has nothing to lose, and a list
    that keeps its row count but empties every row has lost everything.

    A reduction is allowed only when the original value was PRESENT BUT WRONG,
    because then removing it can be the correction — retracting a hallucinated
    enum value to null, dropping duplicate rows for ``uniqueItems``, trimming to
    ``maxItems``, removing a forbidden extra key. A ``required`` error means data
    was MISSING, and removing more data never fixes that, so a field whose only
    errors were ``required`` may not shrink at all. That is what keeps the two
    #791 hazards rejected: 100 abstained cells (100 ``required`` errors) may not
    become a nulled list, and a 100-row list may not come back as 50 clean rows.

    Only top-level fields are compared, but leaf counting recurses, so a nested
    list destroyed inside a group registers as loss on the group.
    """
    lost: list[str] = []
    for name, before_val in (original or {}).items():
        after_val = (escalated or {}).get(name)
        leaves_before, leaves_after = (
            _populated_leaves(before_val),
            _populated_leaves(after_val),
        )
        if leaves_before == 0:
            continue
        errs = _field_errors(before, name)
        non_required = [e for e in errs if e.validator != "required"]
        rows_before, rows_after = (
            _populated_rows(before_val),
            _populated_rows(after_val),
        )
        if rows_before is not None and rows_before > 0:
            rows_after = rows_after if rows_after is not None else 0
            if rows_after < rows_before and not any(
                e.validator in _REMOVAL_FIXES for e in errs
            ):
                lost.append(
                    f"{name}: had {rows_before} populated rows, escalation returned "
                    f"{rows_after}" + ("" if rows_after else " (null/absent)")
                )
                continue
        if leaves_after < leaves_before and not non_required:
            what = "null" if after_val is None else f"{leaves_after} populated value(s)"
            lost.append(
                f"{name}: had {leaves_before} populated value(s), escalation returned {what}"
            )
    return lost


def _root_error_kinds(report: ValidationReport) -> set[tuple[str, str]]:
    return {(e.validator, e.message) for e in report.errors if e.field is None}


def select_escalated_fields(
    original: dict[str, Any],
    escalated: dict[str, Any],
    before: ValidationReport,
    after: ValidationReport,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge an escalation result FIELD BY FIELD, accepting each top-level field
    on its own merits. Returns ``(merged, decisions)`` where ``decisions`` maps
    every field the escalation changed to ``"accepted: ..."`` or ``"rejected: ..."``.

    Per field, in order: rejected if it lost data (``escalation_data_loss``);
    rejected if it has more errors than before; accepted if it has fewer errors
    or is now clean; otherwise rejected as no visible change. Accepting per field
    means one field the stronger model got wrong cannot sink four it got right.

    Root-level errors (``field is None``) are not attributable, so any root error
    KIND that is new in the escalated result rejects the whole result — a count
    comparison would let a new ``additionalProperties`` violation hide behind an
    unparseable ``required`` one that went away.
    """
    original = original or {}
    escalated = escalated or {}
    decisions: dict[str, str] = {}
    merged = dict(original)

    new_root = _root_error_kinds(after) - _root_error_kinds(before)
    if new_root:
        kinds = ", ".join(sorted({v for v, _ in new_root}))
        for name in escalated:
            if escalated.get(name) != original.get(name):
                decisions[name] = f"rejected: escalation introduced root-level {kinds}"
        return merged, decisions

    lost = {
        entry.split(":", 1)[0]: entry
        for entry in escalation_data_loss(original, escalated, before)
    }
    before_counts, after_counts = before.errors_by_field(), after.errors_by_field()
    for name in escalated:
        if escalated.get(name) == original.get(name):
            continue
        if name in lost:
            decisions[name] = "rejected: " + lost[name]
            continue
        b, a = before_counts.get(name, 0), after_counts.get(name, 0)
        if a > b:
            decisions[name] = f"rejected: errors rose {b} -> {a}"
        elif a < b:
            decisions[name] = f"accepted: errors fell {b} -> {a}"
            merged[name] = escalated[name]
        elif a == 0 and b == 0:
            # Valid before and after; the escalation changed a clean field. That is
            # neither a fix nor a loss — keep the original, the known-good value.
            decisions[name] = "rejected: field was already valid"
        else:
            decisions[name] = f"rejected: still {a} error(s), nothing visibly fixed"
    return merged, decisions


def escalation_outcome(
    original: dict[str, Any],
    escalated: dict[str, Any],
    before: ValidationReport,
    after: ValidationReport,
) -> tuple[bool, str]:
    """Whole-result verdict over :func:`select_escalated_fields`.

    ``keep`` is True when at least one field was accepted. The reason lists every
    per-field decision, so a rejected escalation is explainable. Replaces
    ``esc.valid or len(esc.errors) < len(full.errors)``, which compared TOTAL
    error counts: per-row errors scale with the row count while a whole-field
    error is always one, so totals favoured the result with LESS data — a nulled
    100-row list "improved" from 100 errors to 1 and was kept (#791).
    """
    merged, decisions = select_escalated_fields(original, escalated, before, after)
    accepted = sorted(k for k, v in decisions.items() if v.startswith("accepted"))
    rejected = sorted(k for k, v in decisions.items() if v.startswith("rejected"))
    if not decisions:
        return False, "escalation changed nothing"
    parts = [f"{k} {decisions[k]}" for k in accepted + rejected]
    return bool(accepted), "; ".join(parts)


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
