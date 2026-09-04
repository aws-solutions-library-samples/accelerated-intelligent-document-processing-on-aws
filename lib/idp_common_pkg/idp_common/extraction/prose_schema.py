# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How much of the class schema to restate as prose in the task prompt (#710).

``{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`` is substituted with the whole cleaned class
schema, serialized as JSON. When the same schema is ALSO on the wire as a tool
input schema — always on the Advanced path, and on the Simple path when
``extraction.forced_tool.enabled`` — that prose is a second copy of information
the model already has, and #710 measured it at ~1,485 of ~5,692 schema tokens per
request on the lending ``Payslip`` class.

Three renderings, so the trade can be measured rather than assumed:

``full``
    Today's behaviour: the cleaned JSON Schema, indented. Every description, type,
    format and constraint the class declares.

``minimal``
    The descriptions a tool schema **loses**, and nothing else. On the Advanced
    path the toolSpec is derived from a generated Pydantic model, and that round
    trip drops the ROOT class description on every class tested and any
    object/group-level description (verified on
    ``config_library/unified/lending-package-sample``: ``Payslip`` keeps 34 of 37
    descriptions, losing the root plus ``Address`` and ``EmployeeName``; four of the
    preset's six classes declare only a root description, so they lose *all* of it).
    Per-FIELD descriptions survive the round trip, so restating them is the
    redundant part and is what this drops.

``names``
    The top-level attribute names only. For the Simple path, where the toolSpec is
    built from the class schema directly and is **lossless** — verified:
    ``sanitize_tool_schema`` strips only non-constraining document metadata
    (``$id``/``$schema``/``$anchor``/``$comment``/``id``) and ``x-aws-idp-*``, so
    all 37 ``Payslip`` descriptions including the root survive — there is nothing
    for prose to add.

Why ``names`` is not the empty string: the placeholder is user-editable template
text sitting inside ``<attributes> … </attributes>``, and the shipped prompts say
"If the attributes section below contains a list of attribute names and
descriptions, then output only those attributes". Substituting nothing leaves a
dangling empty element and makes that sentence false. A bare name list keeps the
prompt readable and costs tens of tokens rather than thousands.

Every renderer is **deterministic** for a given schema. The substituted text sits
before ``<<CACHEPOINT>>``, so a rendering that varied run to run would invalidate
the cached prompt prefix on every request.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from idp_common.config.models import (
    DEFAULT_PROSE_SCHEMA_MODE,
    PROSE_SCHEMA_MODES,
    normalize_prose_schema_mode,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROSE_SCHEMA_MODE",
    "PROSE_SCHEMA_MODES",
    "describe_saving",
    "normalize_prose_schema_mode",
    "render",
]


def _type_hint(prop: Dict[str, Any]) -> str:
    """A short type label for a property, or "" when the schema does not say.

    Deliberately terse: the tool schema carries the authoritative types, so this is
    an orientation aid in the prompt, not a specification. Callers pass a dict.
    """
    if "$ref" in prop and "type" not in prop:
        return "object"
    ptype = prop.get("type")
    if isinstance(ptype, list):
        ptype = "|".join(str(t) for t in ptype if t != "null") or None
    if not ptype:
        return ""
    if ptype == "array":
        items = prop.get("items")
        inner = _type_hint(items) if isinstance(items, dict) else ""
        return f"array of {inner}" if inner else "array"
    fmt = prop.get("format")
    return f"{ptype} ({fmt})" if fmt else str(ptype)


def _attribute_lines(schema: Dict[str, Any]) -> List[str]:
    """``- name (type)`` for each top-level property, in declaration order."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    lines = []
    for name, prop in properties.items():
        hint = _type_hint(prop if isinstance(prop, dict) else {})
        lines.append(f"- {name} ({hint})" if hint else f"- {name}")
    return lines


def _resolve_ref(schema: Dict[str, Any], ref: str) -> Optional[Dict[str, Any]]:
    """Resolve a local ``#/$defs/Name`` (or ``#/definitions/Name``) pointer.

    Only local pointers are followed; anything else returns None rather than
    reaching for the network or guessing.
    """
    if not ref.startswith("#/"):
        return None
    node: Any = schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, dict) else None


def _group_descriptions(schema: Dict[str, Any]) -> List[Tuple[str, str]]:
    """``(label, description)`` for every OBJECT-typed subschema that has one.

    These are the descriptions the Advanced path loses: a generated Pydantic model
    keeps ``Field(description=...)`` for a field — including one whose type is a
    nested model — but has nowhere to put a description belonging to the nested
    MODEL itself, so a ``$defs`` entry's own description is dropped from
    ``model_json_schema()`` and never reaches the toolSpec.

    Two sources, in this order, because they are not the same sentence:

    1. Object-typed **properties**, labelled by the property path the model sees
       (``account_holder_address``) rather than by a ``$defs`` key, because the
       property name is what appears in the answer. A ``$ref`` sibling description
       wins over the target's, matching JSON Schema 2020-12 composition.
    2. ``$defs`` / ``definitions`` entries whose description was NOT already emitted
       above, labelled by the properties that reference them.

    Source 2 is what makes this complete rather than plausible. On the shipped
    lending ``Payslip``, ``EmployeeAddress`` and ``CompanyAddress`` both ``$ref`` one
    ``Address`` definition and both carry their own sibling description, so
    sibling-wins hides the definition's — and the definition's is precisely the one
    the round trip loses. Splicing it into the property lines instead would be
    wrong, not merely verbose: that definition's text says "business address", and
    restating it under ``EmployeeAddress`` (residential) would put a false statement
    in the prompt.
    """
    found: List[Tuple[str, str]] = []
    seen_pairs: set[Tuple[str, str]] = set()
    emitted_texts: set[str] = set()
    #: def name -> property paths that reference it, for labelling source 2.
    referenced_by: Dict[str, List[str]] = {}

    def note(label: str, text: str) -> None:
        pair = (label, text)
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)
        emitted_texts.add(text)
        found.append(pair)

    def walk(node: Any, path: str, depth: int) -> None:
        # Bounded so a schema with a self-referential $def cannot spin here.
        if not isinstance(node, dict) or depth > 6:
            return
        target = node
        ref = node.get("$ref")
        if isinstance(ref, str):
            resolved = _resolve_ref(schema, ref)
            if resolved is None:
                return
            if path:
                referenced_by.setdefault(ref.rsplit("/", 1)[-1], []).append(path)
            target = {**resolved, **{k: v for k, v in node.items() if k != "$ref"}}
        properties = target.get("properties")
        is_object = target.get("type") == "object" or isinstance(properties, dict)
        description = target.get("description")
        if path and is_object and isinstance(description, str) and description.strip():
            note(path, description.strip())
        if isinstance(properties, dict):
            for name, prop in properties.items():
                walk(prop, f"{path}.{name}" if path else name, depth + 1)
        items = target.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]" if path else "[]", depth + 1)

    walk(schema, "", 0)

    for container in ("$defs", "definitions"):
        defs = schema.get(container)
        if not isinstance(defs, dict):
            continue
        for name, definition in defs.items():
            if not isinstance(definition, dict):
                continue
            text = definition.get("description")
            if not (isinstance(text, str) and text.strip()):
                continue
            text = text.strip()
            if text in emitted_texts:
                continue
            users = referenced_by.get(name)
            label = f"{', '.join(users)} (shared group)" if users else name
            note(label, text)

    return found


def _class_name(schema: Dict[str, Any]) -> str:
    """The class's own name, for the one-line header. Falls back to "document"."""
    for key in ("title", "$id", "x-aws-idp-document-type"):
        value = schema.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "document"


def _render_names(schema: Dict[str, Any]) -> str:
    lines = _attribute_lines(schema)
    if not lines:
        return ""
    return "\n".join(
        [
            f"Attribute names ({len(lines)}). Types and descriptions are in the "
            "tool schema you must answer with:",
            *lines,
        ]
    )


def _render_minimal(schema: Dict[str, Any]) -> str:
    parts: List[str] = []
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(f"{_class_name(schema)}: {description.strip()}")
    groups = _group_descriptions(schema)
    if groups:
        parts.append(
            "\n".join(
                ["Nested groups:", *(f"- {path}: {text}" for path, text in groups)]
            )
        )
    names = _render_names(schema)
    if names:
        parts.append(names)
    return "\n\n".join(parts)


def render(schema: Dict[str, Any], mode: str) -> str:
    """Render the prose schema block for ``{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}``.

    Args:
        schema: the CLEANED class schema (``x-aws-idp-*`` already stripped) that is
            being sent on the wire — including the multi-instance detection probe
            when one was added, so the prose and the toolSpec describe the same
            shape.
        mode: one of :data:`PROSE_SCHEMA_MODES`.

    Returns:
        The text to substitute. Never ``None``; may be ``""`` only for a schema
        that declares no properties at all, which the service treats as an empty
        class and never sends to a model.
    """
    mode = normalize_prose_schema_mode(mode)
    if mode == "full":
        return json.dumps(schema, indent=2)
    if mode == "minimal":
        return _render_minimal(schema)
    return _render_names(schema)


def describe_saving(full: str, rendered: str) -> str:
    """A log-line fragment quantifying what a non-``full`` rendering reclaimed.

    Characters, at ~4 per token — the same crude conversion #710 uses — because an
    exact tokenizer is model-specific and this is an operator log line, not a
    measurement. The benchmark suite is where the number gets measured.
    """
    saved = max(len(full) - len(rendered), 0)
    return f"{saved} chars (~{saved // 4} tokens) of prompt"
