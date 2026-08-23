# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Schema bridge between IDP config-class schemas and the SEED document
generator's ``schema.json`` format.

IDP document-class schemas are JSON Schema **draft-2020-12** with
``x-aws-idp-*`` annotations and may use ``$defs`` / ``$ref`` for shared
sub-objects. The generator consumes a flat, inline JSON Schema **draft-07**
``schema.json`` (a ``title``, ``type: object`` and inline ``properties``).

The load-bearing invariant of this whole feature: **every leaf field name must
be preserved exactly** across the bridge. The data the generator produces
becomes the evaluation baseline ``inference_result``; if a generated field name
drifts from the IDP class field name, evaluation scores 0 for that field. The
:func:`leaf_field_paths` helper exists so tests (and a runtime validation gate)
can assert this invariant holds.

Public API:
  * :func:`config_class_to_generator_schema` - IDP class -> generator schema.json
  * :func:`leaf_field_paths` - the set of leaf field paths in a schema (either form)
  * :func:`inline_refs` - resolve ``$defs``/``$ref`` into an inline schema
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional, Set

from idp_common.config import schema_constants as sc

logger = logging.getLogger(__name__)

# Standard JSON Schema keys the generator understands and we carry through.
_CARRIED_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "items",
        "required",
        "enum",
        "format",
        "pattern",
        "minimum",
        "maximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "title",
    }
)

_GENERATOR_SCHEMA_DRAFT = "http://json-schema.org/draft-07/schema#"


def _is_aws_idp_key(key: str) -> bool:
    """True for IDP-only annotations the generator does not consume."""
    return key.startswith("x-aws-idp-")


def inline_refs(
    schema: Dict[str, Any], defs: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Resolve ``$defs``/``$ref`` (``#/$defs/Name``) into a fully inline schema.

    IDP classes reference shared sub-objects via ``$ref``; the generator wants
    everything inline. Returns a deep copy; the input is not mutated. A sibling
    ``description`` next to a ``$ref`` (a common IDP pattern) is preserved and
    wins over the referenced object's own description.
    """
    if defs is None:
        defs = schema.get(sc.DEFS_FIELD, {}) or {}

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if sc.REF_FIELD in node:
                ref = node[sc.REF_FIELD]
                prefix = f"#/{sc.DEFS_FIELD}/"
                if not ref.startswith(prefix):
                    logger.warning("Unsupported $ref %r; leaving inline", ref)
                    resolved = {k: v for k, v in node.items() if k != sc.REF_FIELD}
                    return _resolve(resolved)
                name = ref[len(prefix) :]
                target = defs.get(name)
                if target is None:
                    raise ValueError(f"$ref target not found: {ref}")
                resolved = _resolve(copy.deepcopy(target))
                sibling_desc = node.get("description")
                if sibling_desc:
                    resolved["description"] = sibling_desc
                return resolved
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    out = _resolve(copy.deepcopy(schema))
    out.pop(sc.DEFS_FIELD, None)
    return out


def _strip_to_generator(node: Any) -> Any:
    """Recursively drop IDP-only annotations, keeping standard JSON Schema.

    Field NAMES (the keys under ``properties``) are never touched - only the
    metadata keys describing each field are filtered.
    """
    if isinstance(node, dict):
        result: Dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                result["properties"] = {
                    fname: _strip_to_generator(fval) for fname, fval in value.items()
                }
            elif key == "items":
                result["items"] = _strip_to_generator(value)
            elif _is_aws_idp_key(key):
                continue
            elif key in (sc.SCHEMA_FIELD, sc.ID_FIELD, sc.DEFS_FIELD, sc.REF_FIELD):
                continue
            elif key in _CARRIED_KEYS:
                result[key] = _strip_to_generator(value)
            else:
                result[key] = _strip_to_generator(value)
        return result
    if isinstance(node, list):
        return [_strip_to_generator(v) for v in node]
    return node


def _allow_null_on_optional_leaves(node: Any) -> None:
    """Let optional scalar fields hold ``null`` in the GENERATOR schema.

    IDP config classes are written for extraction, where the convention for a field
    the document does not contain is an explicit null — many field descriptions say
    so outright ("Output null if not shown"). The same schema is then handed to the
    generator, where ``type: "string"`` cannot hold null: the generator follows the
    description, JSON Schema validation rejects the result, the critic bounces it,
    and the document is retried. Observed on a real run: 119 critic rejections and
    138 instances of that single contradiction while producing 11 documents, which
    exhausted the render stage's hour and lost the whole batch.

    Resolved in the direction the config author intended — the instruction stays,
    and the generator schema is widened to permit it — rather than by rewriting
    field descriptions we do not own. Required fields are left alone: those must
    always be present, so a null there is a genuine failure worth a retry.

    Extraction is unaffected; this widens only the copy handed to the generator.
    """
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            required_here = node.get("required") or ()
            for name, sub in props.items():
                if isinstance(sub, dict):
                    _allow_null_on_optional_leaves(sub)
                    if name in required_here:
                        continue
                    sub_type = sub.get("type")
                    # Scalars only. Widening an object or array to null would let the
                    # generator drop whole structures rather than a single value.
                    if isinstance(sub_type, str) and sub_type not in (
                        "object",
                        "array",
                        "null",
                    ):
                        sub["type"] = [sub_type, "null"]
                        # An enum constrains the value independently of type, so
                        # widening type alone leaves null invalid and the field back in
                        # the critic-retry loop this exists to break.
                        choices = sub.get("enum")
                        if isinstance(choices, list) and None not in choices:
                            sub["enum"] = [*choices, None]
        items = node.get("items")
        if isinstance(items, dict):
            _allow_null_on_optional_leaves(items)


def config_class_to_generator_schema(
    class_dict: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert an IDP config-class schema into a generator ``schema.json`` dict.

    Steps: inline ``$defs``/``$ref`` -> strip ``x-aws-idp-*`` annotations ->
    swap the ``$schema`` draft -> set ``title`` from the document type. Every
    leaf field name is preserved exactly (verified by :func:`leaf_field_paths`).
    """
    if not isinstance(class_dict, dict):
        raise TypeError("class_dict must be a dict (a single IDP document class)")

    doc_type = (
        class_dict.get(sc.X_AWS_IDP_DOCUMENT_TYPE)
        or class_dict.get(sc.ID_FIELD)
        or class_dict.get("title")
    )
    if not doc_type:
        raise ValueError(
            "config class is missing a document type "
            f"({sc.X_AWS_IDP_DOCUMENT_TYPE}/{sc.ID_FIELD}/title)"
        )

    inlined = inline_refs(class_dict)
    stripped = _strip_to_generator(inlined)

    generator_schema: Dict[str, Any] = {
        "$schema": _GENERATOR_SCHEMA_DRAFT,
        "title": str(doc_type),
        "type": stripped.get("type", "object"),
    }
    if "description" in stripped:
        generator_schema["description"] = stripped["description"]
    if "required" in stripped:
        generator_schema["required"] = stripped["required"]
    generator_schema["properties"] = stripped.get("properties", {})
    # Reconcile the extraction convention ("output null if absent") with what the
    # generator's validator accepts. See _allow_null_on_optional_leaves.
    _allow_null_on_optional_leaves(generator_schema)
    return generator_schema


def leaf_field_paths(schema: Dict[str, Any]) -> Set[str]:
    """Return the set of dotted leaf field paths in a schema (either form).

    A *leaf* is a property whose value is not itself an object-with-properties
    and not an array-of-objects. Array-of-objects and nested objects recurse so
    the path set captures every name that will appear in ``inference_result``.
    ``$ref`` is resolved first so both schema forms yield comparable paths.

    Examples of returned paths::

        "Account Number"
        "Account Holder Address.City"
        "Transactions[].Amount"
    """
    resolved = inline_refs(schema) if schema.get(sc.DEFS_FIELD) else schema

    def _walk(node: Dict[str, Any], prefix: str, out: Set[str]) -> None:
        props = node.get("properties")
        if not isinstance(props, dict):
            return
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            spec = inline_refs(spec) if spec.get(sc.REF_FIELD) else spec
            path = f"{prefix}{name}"
            node_type = spec.get("type")
            if node_type == "object" and isinstance(spec.get("properties"), dict):
                _walk(spec, f"{path}.", out)
            elif node_type == "array" and isinstance(spec.get("items"), dict):
                items = spec["items"]
                items = items if not items.get(sc.REF_FIELD) else inline_refs(items)
                if isinstance(items.get("properties"), dict):
                    _walk(items, f"{path}[].", out)
                else:
                    out.add(f"{path}[]")
            else:
                out.add(path)

    paths: Set[str] = set()
    _walk(resolved, "", paths)
    return paths


def field_names(schema: Dict[str, Any]) -> List[str]:
    """Flat, sorted list of every leaf field name (last path segment).

    Convenience for the runtime validation gate that compares generated
    baseline keys against the schema's field names.
    """
    names: Set[str] = set()
    for path in leaf_field_paths(schema):
        seg = path.rstrip("[]").split(".")[-1].rstrip("[]")
        names.add(seg)
    return sorted(names)
