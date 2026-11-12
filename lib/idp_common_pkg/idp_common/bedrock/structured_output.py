# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Turn a JSON Schema into one Bedrock structured outputs will accept.

Bedrock structured outputs (constrained decoding) guarantees that a response is
valid JSON matching a JSON Schema — ``outputConfig.textFormat`` on Converse,
``output_config.format`` on an Anthropic InvokeModel body. It accepts only a
subset of JSON Schema Draft 2020-12, and within that subset it enforces limits a
document-class schema routinely exceeds. This module bridges the two and reports
exactly what it had to change, so a caller can record it.

The rules come from the Bedrock user guide, "Get validated JSON results from
models" (``structured-output.html``):

* **Every object must set** ``additionalProperties: false``. An omitted value is a
  400, not a default.
* **Unsupported keywords are a 400**: numeric constraints (``minimum``,
  ``maximum``, ``multipleOf``), string length (``minLength``, ``maxLength``),
  ``minItems`` above 1, recursive schemas and external ``$ref``. They are removed
  here (``minItems`` is clamped to 1), and the caller's full-schema validation —
  which reads the *original* class schema — is what still checks them.
* **At most 12 optional properties**, counted across every nested object. The
  documented workaround is to list a property in ``required`` and make it
  nullable, and **at most 16** properties may be union-typed (a type array or
  ``anyOf``). This module spends the nullable allowance first — a required
  nullable field the document lacks comes back ``null``, where an optional one was
  measured coming back as ``""`` — leaves the overflow optional, and reports the
  schema as not fitting when the two limits together cannot hold it.

All of the above was confirmed live against Bedrock (us-east-1, 2026-11-11): 13
optional properties fail compilation with "Schema is too complex" after ~80 s, 17
union-typed properties are refused immediately, ``minItems: 3`` is refused, and
every class schema in ``config_library/unified`` that this module reports as
fitting was accepted, with a reply that validated against it.

What is deliberately *not* done here:

* **Property-name sanitizing.** Callers run
  :func:`idp_common.bedrock.tool_schema.sanitize_tool_schema` first, which also
  removes the ``$id``/``$schema``/``x-aws-idp-*`` metadata Bedrock meta-validates,
  and keep its name map so the authored names can be restored on the response.
* **Deciding whether a model supports the feature.** That is
  :func:`idp_common.bedrock.client.structured_output_unsupported_reason`, beside
  every other per-model Converse capability gate.

``$ref`` is **inlined** rather than passed through. The limits are counted
"across all nested objects in the schema", and a ``$defs`` entry referenced from
two places would otherwise be counted once by this module and possibly twice by
Bedrock — inlining makes the count here an exact statement about the tree that is
sent, and a cycle (a recursive schema, which is unsupported) is detected for free.
The response shape is identical either way, so the caller's name map still applies.
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Bedrock's limit on optional properties, counted across the whole schema.
MAX_OPTIONAL_PROPERTIES = 12

#: Bedrock's limit on union-typed properties (a type array or ``anyOf``).
MAX_UNION_PROPERTIES = 16

#: The client read timeout the first request for a new schema needs. Bedrock
#: compiles a grammar on first use, which "can take up to approximately 90
#: seconds", and the user guide asks for a read timeout of at least 120 s.
#: ``BedrockClient.client`` uses ``CONFIDENCE_READ_TIMEOUT_SECONDS``; a test pins
#: that it stays at or above this.
MIN_READ_TIMEOUT_SECONDS = 120

#: String ``format`` values structured outputs supports. Any other value is
#: removed rather than sent, because it is not in the documented subset.
SUPPORTED_STRING_FORMATS = frozenset(
    {
        "date-time",
        "time",
        "date",
        "duration",
        "email",
        "hostname",
        "uri",
        "ipv4",
        "ipv6",
        "uuid",
    }
)

#: Keywords kept on the wire. Everything else is removed: either it is a
#: constraint structured outputs rejects, or it is an annotation the documented
#: subset does not list (``data_type``, ``examples``), and an unlisted keyword can
#: only add a way for the request to fail.
_KEPT_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "allOf",
        "format",
        "pattern",
        "minItems",
        "description",
        "title",
        "default",
    }
)

#: Lookaround and backreferences, which Bedrock's ``pattern`` subset excludes
#: ("Invalid regex in pattern field").
_UNSUPPORTED_REGEX = re.compile(r"\(\?<?[=!]|\\[1-9]|\\k<")

_SCALAR_TYPES = frozenset({"string", "number", "integer", "boolean", "null"})


class SchemaDoesNotFit(Exception):
    """The schema cannot be expressed within structured outputs' subset or limits."""


@dataclass
class PreparedSchema:
    """The result of :func:`prepare_schema`.

    ``schema`` is None exactly when ``fits`` is False, and ``reason`` then says
    why, in words suitable for a section's audit metadata.
    """

    schema: Optional[Dict[str, Any]]
    fits: bool
    reason: Optional[str] = None
    optional_properties: int = 0
    union_properties: int = 0
    #: Optional properties that were already nullable, moved into ``required``.
    made_required: int = 0
    #: Optional properties made required AND nullable to fit the optional limit.
    made_nullable: int = 0
    #: Keyword -> how many times it was removed (or, for ``minItems``, clamped).
    relaxed_keywords: Dict[str, int] = field(default_factory=dict)

    def audit(self) -> Dict[str, Any]:
        """A JSON-safe summary for section metadata."""
        out: Dict[str, Any] = {
            "optional_properties": self.optional_properties,
            "union_properties": self.union_properties,
            "made_required": self.made_required,
            "made_nullable": self.made_nullable,
        }
        if self.relaxed_keywords:
            out["relaxed_keywords"] = dict(sorted(self.relaxed_keywords.items()))
        if self.reason:
            out["reason"] = self.reason
        return out


# ---------------------------------------------------------------------------
# $ref inlining
# ---------------------------------------------------------------------------


def _resolve_local_ref(root: Dict[str, Any], ref: str) -> Any:
    if not ref.startswith("#/"):
        raise SchemaDoesNotFit(
            f"external $ref {ref!r} is not supported by structured outputs"
        )
    node: Any = root
    for raw in ref[2:].split("/"):
        seg = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or seg not in node:
            raise SchemaDoesNotFit(f"$ref {ref!r} does not resolve")
        node = node[seg]
    return node


def _inline_refs(node: Any, root: Dict[str, Any], stack: Tuple[str, ...]) -> Any:
    if isinstance(node, list):
        return [_inline_refs(x, root, stack) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node and isinstance(node["$ref"], str):
        ref = node["$ref"]
        if ref in stack:
            raise SchemaDoesNotFit(
                f"recursive schema ($ref {ref!r} refers to itself) is not supported "
                f"by structured outputs"
            )
        target = _resolve_local_ref(root, ref)
        merged = dict(target) if isinstance(target, dict) else {}
        # Sibling keywords (a description beside the $ref, or the type annotation
        # sanitize_tool_schema adds) describe this use of the definition and win.
        merged.update({k: v for k, v in node.items() if k != "$ref"})
        return _inline_refs(merged, root, stack + (ref,))
    return {
        k: _inline_refs(v, root, stack)
        for k, v in node.items()
        if k not in ("$defs", "definitions")
    }


# ---------------------------------------------------------------------------
# Keyword subset
# ---------------------------------------------------------------------------


def _is_object(node: Dict[str, Any]) -> bool:
    t = node.get("type")
    return (
        t == "object"
        or (isinstance(t, list) and "object" in t)
        or ("properties" in node)
    )


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _restrict(node: Any, relaxed: Counter) -> Any:
    """Return ``node`` restricted to the supported keyword subset."""
    if not isinstance(node, dict):
        return node
    out: Dict[str, Any] = {}
    for key, value in node.items():
        if key not in _KEPT_KEYWORDS:
            if key == "oneOf" and isinstance(value, list):
                # For generation, "exactly one of" and "at least one of" admit the
                # same instances whenever the branches are disjoint, which is the
                # only way a class schema uses it. anyOf is supported; oneOf is not.
                out["anyOf"] = [_restrict(v, relaxed) for v in value]
                relaxed["oneOf"] += 1
                continue
            relaxed[key] += 1
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _restrict(v, relaxed) for name, v in value.items()}
        elif key == "items":
            if isinstance(value, list):
                raise SchemaDoesNotFit(
                    "tuple-form 'items' (a list of schemas) is not supported by "
                    "structured outputs"
                )
            out[key] = _restrict(value, relaxed)
        elif key in ("anyOf", "allOf") and isinstance(value, list):
            out[key] = [_restrict(v, relaxed) for v in value]
        elif key == "format":
            if value in SUPPORTED_STRING_FORMATS:
                out[key] = value
            else:
                relaxed["format"] += 1
        elif key == "pattern":
            if isinstance(value, str) and not _UNSUPPORTED_REGEX.search(value):
                out[key] = value
            else:
                relaxed["pattern"] += 1
        elif key == "minItems":
            try:
                n = int(value)
            except (TypeError, ValueError):
                relaxed["minItems"] += 1
                continue
            if n > 1:
                # Clamped, not dropped: "at least one" is still enforceable and is
                # strictly weaker than the authored bound, which full-schema
                # validation of the original schema still checks.
                out[key] = 1
                relaxed["minItems"] += 1
            else:
                out[key] = max(n, 0)
        elif key == "enum":
            if not isinstance(value, list) or not all(
                v is None or isinstance(v, (str, int, float, bool)) for v in value
            ):
                raise SchemaDoesNotFit(
                    "an 'enum' with non-scalar values is not supported by "
                    "structured outputs"
                )
            out[key] = list(value)
        elif key == "required":
            out[key] = (
                [r for r in value if isinstance(r, str)]
                if isinstance(value, list)
                else []
            )
        elif key == "additionalProperties":
            # Replaced below for every object; anything but false is a 400.
            continue
        else:
            out[key] = value
    if isinstance(out.get("type"), list) and "enum" in out:
        # Bedrock rejects an enum beside a type array ("Enum value ... does not
        # match declared type"). The enum alone says the same thing once it is
        # filtered to the values the type admitted, and is accepted.
        types = out.pop("type")
        out["enum"] = [
            v
            for v in out["enum"]
            if _json_type(v) in types
            or (_json_type(v) == "integer" and "number" in types)
        ]
        relaxed["type"] += 1
    if _is_object(out):
        props = out.get("properties")
        if isinstance(props, dict):
            if "required" in out:
                out["required"] = [r for r in out["required"] if r in props]
        else:
            out.pop("required", None)
        out["additionalProperties"] = False
    return out


# ---------------------------------------------------------------------------
# Optional / union counting
# ---------------------------------------------------------------------------


def _is_union(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    t = node.get("type")
    return (isinstance(t, list) and len(t) > 1) or "anyOf" in node


def _is_nullable(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    t = node.get("type")
    if t == "null" or (isinstance(t, list) and "null" in t):
        return True
    if any(
        isinstance(b, dict) and b.get("type") == "null" for b in node.get("anyOf", [])
    ):
        return True
    return isinstance(node.get("enum"), list) and None in node["enum"]


def _count_unions(node: Any) -> int:
    if isinstance(node, list):
        return sum(_count_unions(x) for x in node)
    if not isinstance(node, dict):
        return 0
    n = 1 if _is_union(node) else 0
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            n += sum(_count_unions(v) for v in value.values())
        elif key in ("items", "anyOf", "allOf"):
            n += _count_unions(value)
    return n


def _property_sites(node: Any) -> List[Tuple[Dict[str, Any], str]]:
    """Every ``(parent_object, property_name)`` in depth-first schema order."""
    sites: List[Tuple[Dict[str, Any], str]] = []
    if isinstance(node, list):
        for x in node:
            sites.extend(_property_sites(x))
        return sites
    if not isinstance(node, dict):
        return sites
    props = node.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            sites.append((node, name))
            sites.extend(_property_sites(child))
    for key in ("items", "anyOf", "allOf"):
        if key in node:
            sites.extend(_property_sites(node[key]))
    return sites


def _is_optional(parent: Dict[str, Any], name: str) -> bool:
    return name not in (parent.get("required") or [])


def _make_required(parent: Dict[str, Any], name: str) -> None:
    req = list(parent.get("required") or [])
    if name not in req:
        req.append(name)
    parent["required"] = req


def _nullable(node: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``node`` widened to also accept null, as ONE union-typed property.

    A plain scalar takes a type array (``["string", "null"]``). Everything else —
    objects, arrays, consts, untyped schemas and **enums** — is wrapped in
    ``anyOf: [node, {"type": "null"}]``. Enums are wrapped rather than given a type
    array because Bedrock rejects that combination: measured live,
    ``{"type": ["string", "null"], "enum": ["A", "B", null]}`` returns
    ``Enum value 'A' does not match declared type`` (us-east-1, 2026-11-11), while
    the ``anyOf`` form is accepted.
    """
    if "anyOf" in node:
        widened = dict(node)
        widened["anyOf"] = list(node["anyOf"]) + [{"type": "null"}]
        return widened
    t = node.get("type")
    if "const" not in node and "enum" not in node:
        if isinstance(t, str) and t in _SCALAR_TYPES:
            return dict(node, type=[t, "null"])
        if isinstance(t, list) and all(x in _SCALAR_TYPES for x in t):
            return dict(node, type=list(t) + ["null"])
    # Keep the description on the outside, where a reader of the property looks.
    inner = {k: v for k, v in node.items() if k != "description"}
    wrapped: Dict[str, Any] = {"anyOf": [inner, {"type": "null"}]}
    if "description" in node:
        wrapped["description"] = node["description"]
    return wrapped


def _optional_count(schema: Dict[str, Any]) -> int:
    return sum(1 for p, n in _property_sites(schema) if _is_optional(p, n))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def prepare_schema(
    schema: Dict[str, Any],
    *,
    max_optional: int = MAX_OPTIONAL_PROPERTIES,
    max_union: int = MAX_UNION_PROPERTIES,
) -> PreparedSchema:
    """Prepare ``schema`` for structured outputs, or say why it cannot be.

    ``schema`` is not mutated. The result is deterministic for a given input,
    which matters because Bedrock caches a compiled grammar per identical schema
    for 24 hours: a schema that differed run to run would pay the first-request
    compilation (up to ~90 s) on every request.

    Pass a schema that has already been through ``sanitize_tool_schema``.
    """
    relaxed: Counter = Counter()
    try:
        if not isinstance(schema, dict):
            raise SchemaDoesNotFit("the schema is not a JSON object")
        work = _inline_refs(copy.deepcopy(schema), schema, ())
        work = _restrict(work, relaxed)
        if not _is_object(work):
            raise SchemaDoesNotFit("the root of the schema is not an object")
        work["type"] = "object"
        work["additionalProperties"] = False
        work.setdefault("properties", {})
    except SchemaDoesNotFit as exc:
        return PreparedSchema(
            schema=None, fits=False, reason=str(exc), relaxed_keywords=dict(relaxed)
        )

    made_required = 0
    made_nullable = 0

    # 1. An optional property that already accepts null costs nothing to require:
    #    it is union-typed either way, and requiring it takes it off the optional
    #    count.
    for parent, name in _property_sites(work):
        if _is_optional(parent, name) and _is_nullable(parent["properties"][name]):
            _make_required(parent, name)
            made_required += 1

    # 2. Require-and-nullify optional properties, in schema order, while the
    #    union-typed allowance lasts; whatever is left stays optional and must fit
    #    the optional limit. Nullable is spent FIRST, not only on the overflow,
    #    because the two are not equivalent answers for a field the document does
    #    not contain: measured live (Claude Haiku 5.5, us-east-1, 2026-11-11), a
    #    required nullable string came back null while an optional string came back
    #    as "" — so an optional field is filled with an empty string rather than
    #    left out, and nullable is the form that matches the prompt path's "use
    #    null for a field the document does not contain".
    #
    #    The sites are re-read after every change: widening an object property
    #    wraps it in a new ``anyOf`` node, so a site list taken before the change
    #    would hold parents that are no longer in the tree. Bounded by the number
    #    of property sites, so the loop terminates whatever a conversion does.
    for _ in range(len(_property_sites(work))):
        if _count_unions(work) >= max_union:
            break
        site = next(
            ((p, n) for p, n in _property_sites(work) if _is_optional(p, n)), None
        )
        if site is None:
            break
        parent, name = site
        parent["properties"][name] = _nullable(parent["properties"][name])
        _make_required(parent, name)
        made_nullable += 1

    optional = _optional_count(work)
    unions = _count_unions(work)
    result = PreparedSchema(
        schema=work,
        fits=True,
        optional_properties=optional,
        union_properties=unions,
        made_required=made_required,
        made_nullable=made_nullable,
        relaxed_keywords=dict(relaxed),
    )
    if optional > max_optional or unions > max_union:
        result.schema = None
        result.fits = False
        result.reason = (
            f"the schema needs {optional} optional and {unions} union-typed "
            f"properties after preparation; structured outputs allows at most "
            f"{max_optional} optional and {max_union} union-typed (nullable) "
            f"properties"
        )
    return result


def converse_output_config(
    schema: Dict[str, Any], name: str, description: Optional[str] = None
) -> Dict[str, Any]:
    """The Converse ``outputConfig`` that constrains the reply to ``schema``.

    Converse carries the schema as a JSON **string** (``jsonSchema.schema`` is a
    ``string`` in the service model).

    Keys are NOT sorted. Constrained decoding emits properties in schema order,
    and the order changes what the model writes: measured live on 30 RealKIE FCC
    invoices (Claude Sonnet 5.5, us-east-1, 2026-11-11), alphabetized properties
    put ``LineItemDays`` ahead of ``LineItemDescription`` and the model folded the
    air time into the days field on every line item of the largest invoice. The
    authored order is preserved instead, and it is just as deterministic — a dict
    keeps insertion order — so an unchanged schema still produces an identical
    string and hits Bedrock's 24-hour grammar cache.
    """
    json_schema: Dict[str, Any] = {
        "schema": json.dumps(schema, separators=(",", ":")),
        "name": name,
    }
    if description:
        json_schema["description"] = description
    return {
        "textFormat": {
            "type": "json_schema",
            "structure": {"jsonSchema": json_schema},
        }
    }


def invoke_model_output_config(schema: Dict[str, Any]) -> Dict[str, Any]:
    """The ``output_config`` member of an Anthropic InvokeModel request body.

    Unlike Converse, the Anthropic-native body takes the schema as a JSON
    **object**. Merge this into the body's ``output_config`` — which is also where
    reasoning ``effort`` goes — rather than replacing it.

    ``BedrockClient.invoke_model`` sends Converse requests only, so this is for
    callers that build an InvokeModel body themselves.
    """
    return {"format": {"type": "json_schema", "schema": copy.deepcopy(schema)}}
