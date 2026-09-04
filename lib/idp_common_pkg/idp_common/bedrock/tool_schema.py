# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Make a document-class JSON Schema usable as a Bedrock Converse tool schema.

Bedrock rejects ``toolSpec.inputSchema`` property keys that do not match
``^[a-zA-Z0-9_.-]{1,64}$``::

    ValidationException: tools.0.custom.input_schema.properties:
    Property keys should match pattern '^[a-zA-Z0-9_.-]{1,64}$'

Document classes are authored for humans, so names like ``"Account Number"`` and
``"Purchase Date and Time"`` are normal — four shipped presets contain them
(GitHub #709). Nothing sends a toolSpec today, which is exactly why this went
unnoticed; it blocks any work that puts the class schema on the wire.

Two things this module is deliberate about.

**It sanitizes recursively.** Bedrock's own check is *top level only* — a bad key
nested inside an object property, inside ``array.items``, or inside a ``$defs``
entry is accepted today. Sanitizing only the top level would therefore work, and
would be a trap twice over: wrapping a class in a list would silently start
sending unsanitized names, and the day AWS makes the check recursive, configs
that worked would begin failing. Depending on a validator being shallow is not a
contract.

**It is reversible, and the reverse mapping does not travel on the wire.**
``sanitize_tool_schema`` returns the clean schema plus a ``NameMap`` mirroring its
shape; ``restore_names`` walks a model response against that map and puts the
authored names back, so ``inference_result`` still uses the names the config
declares and nothing downstream (evaluation baselines, Athena columns, the UI,
the SDK ``fields`` contract) sees a renamed field. Embedding the original name in
the schema instead would have been simpler but sends keys Bedrock did not ask
for.

Collisions are resolved deterministically rather than raised, because the whole
point is to unblock schemas that already exist: ``"Total (USD)"`` and
``"Total USD"`` both reduce to ``Total_USD_``/``Total_USD``, so the second gets a
numeric suffix. The map records it, so the response still restores exactly.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The pattern Bedrock enforces on ``toolSpec.inputSchema`` property keys.
TOOL_PROPERTY_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

#: Max property-name length Bedrock accepts.
MAX_PROPERTY_NAME_LENGTH = 64

_INVALID = re.compile(r"[^a-zA-Z0-9_.-]")

# Schema keywords whose values are subschemas we must descend into. Anything not
# listed is left untouched: this module renames PROPERTY NAMES, never keywords,
# never `enum` values, never description text.
_SUBSCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")

#: Keywords that identify a schema DOCUMENT rather than constrain a value, and
#: which must not be sent inside a ``toolSpec.inputSchema``.
#:
#: Bedrock meta-validates the tool schema and enforces that ``$id`` is an RFC 3986
#: URI-reference. An IDP class schema sets ``$id`` to the document-class name, so a
#: class called ``Policy Application Form`` produces
#: ``$id: "Policy Application Form"`` — spaces, therefore not a URI-reference, and
#: Converse rejects the whole request with:
#:
#:     ValidationException: The json schema definition at
#:     toolConfig.tools.0.toolSpec.inputSchema is invalid. ... $.$id: does not
#:     match the uri-reference pattern
#:
#: These keywords carry no constraint, so dropping them cannot change what the
#: model is asked for.
_DOCUMENT_METADATA_KEYS = frozenset({"$id", "$schema", "$anchor", "$comment", "id"})

#: Prefix of the accelerator's own schema extensions (few-shot examples, per-class
#: prompt and model overrides, instance-array flags). They are instructions to THIS
#: codebase, not to the model, and some hold large example payloads — so sending
#: them would spend tokens on a directive the model cannot act on.
_IDP_EXTENSION_PREFIX = "x-aws-idp-"


def strip_non_wire_keywords(node: Any) -> Any:
    """Recursively drop schema-document metadata and ``x-aws-idp-*`` extensions.

    Applied to every node, not just the root: ``$id``/``$comment`` are legal on any
    subschema, and a nested one would fail the same Bedrock meta-validation. Values
    that are not dicts/lists pass through untouched.
    """
    if isinstance(node, list):
        return [strip_non_wire_keywords(v) for v in node]
    if not isinstance(node, dict):
        return node
    out: Dict[str, Any] = {}
    for key, value in node.items():
        if key in _DOCUMENT_METADATA_KEYS or key.startswith(_IDP_EXTENSION_PREFIX):
            continue
        # `properties` keys are user-authored FIELD names, which may legitimately
        # be spelled like a metadata keyword — never filter inside them.
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: strip_non_wire_keywords(v) for k, v in value.items()}
        else:
            out[key] = strip_non_wire_keywords(value)
    return out


def is_valid_tool_property_name(name: str) -> bool:
    """True if ``name`` can be sent as a toolSpec property key as-is."""
    return bool(name) and bool(TOOL_PROPERTY_NAME_PATTERN.match(name))


@dataclass
class NameMap:
    """Sanitized-name -> (original-name, child map) for one subschema level.

    Mirrors the schema's shape rather than flattening to dotted paths, because a
    property name may legitimately contain a ``.`` (the pattern allows it), which
    would make a dotted key ambiguous.
    """

    #: sanitized property name -> original property name
    renamed: Dict[str, str] = field(default_factory=dict)
    #: sanitized property name -> that property's own NameMap
    children: Dict[str, "NameMap"] = field(default_factory=dict)
    #: the map for ``items`` (arrays), when the item schema has properties
    items: Optional["NameMap"] = None

    def is_empty(self) -> bool:
        """True when nothing anywhere beneath this level was renamed."""
        if self.renamed:
            return False
        if self.items is not None and not self.items.is_empty():
            return False
        return all(child.is_empty() for child in self.children.values())


def sanitize_property_name(name: str, taken: set[str]) -> str:
    """Reduce one property name to Bedrock's character set, avoiding ``taken``.

    Illegal characters become underscores and the result is truncated to 64
    characters. A name that is already valid is returned unchanged, so a schema
    Bedrock already accepts is sent byte-identical and nothing has to be mapped
    back.
    """
    if is_valid_tool_property_name(name) and name not in taken:
        return name

    candidate = _INVALID.sub("_", name) or "field"
    candidate = candidate[:MAX_PROPERTY_NAME_LENGTH]
    if candidate not in taken:
        return candidate

    # Deterministic de-duplication. Reserve room for the suffix so the result
    # still fits in 64 characters.
    for n in range(2, 1000):
        suffix = f"_{n}"
        trimmed = candidate[: MAX_PROPERTY_NAME_LENGTH - len(suffix)]
        attempt = f"{trimmed}{suffix}"
        if attempt not in taken:
            return attempt
    raise ValueError(  # pragma: no cover - needs 1000 colliding names
        f"could not find a unique tool-schema name for {name!r}"
    )


def _sanitize_node(node: Any) -> Tuple[Any, NameMap]:
    """Recursively sanitize one subschema. Returns (clean node, its NameMap)."""
    name_map = NameMap()
    if not isinstance(node, dict):
        return node, name_map

    out: Dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            clean_props: Dict[str, Any] = {}
            taken: set[str] = set()
            for prop_name, prop_schema in value.items():
                safe = sanitize_property_name(str(prop_name), taken)
                taken.add(safe)
                if safe != prop_name:
                    name_map.renamed[safe] = str(prop_name)
                child_clean, child_map = _sanitize_node(prop_schema)
                clean_props[safe] = child_clean
                if not child_map.is_empty():
                    name_map.children[safe] = child_map
            out[key] = clean_props
            continue

        if key == "items":
            child_clean, child_map = _sanitize_node(value)
            out[key] = child_clean
            if not child_map.is_empty():
                name_map.items = child_map
            continue

        if key in _SUBSCHEMA_LISTS and isinstance(value, list):
            # Branches share the parent's property space; merge their maps so a
            # response can be restored without knowing which branch matched.
            cleaned = []
            for branch in value:
                branch_clean, branch_map = _sanitize_node(branch)
                cleaned.append(branch_clean)
                name_map.renamed.update(branch_map.renamed)
                name_map.children.update(branch_map.children)
                if branch_map.items is not None:
                    name_map.items = branch_map.items
            out[key] = cleaned
            continue

        if key == "$defs" and isinstance(value, dict):
            # $defs entries are referenced from elsewhere, so their internal
            # property names must be sanitized too. The DEFINITION names
            # themselves are not property keys, so they are left alone and $ref
            # strings keep resolving.
            clean_defs = {}
            for def_name, def_schema in value.items():
                def_clean, def_map = _sanitize_node(def_schema)
                clean_defs[def_name] = def_clean
                if not def_map.is_empty():
                    # Keyed by definition name; merged in at use sites by
                    # restore_names via the $ref-resolved child map below.
                    name_map.children[f"$defs/{def_name}"] = def_map
            out[key] = clean_defs
            continue

        if key == "required" and isinstance(value, list):
            out[key] = value  # rewritten by the caller, which knows the mapping
            continue

        out[key] = value

    # `required` names the pre-sanitization keys; rewrite them to match.
    if isinstance(out.get("required"), list) and name_map.renamed:
        reverse = {orig: safe for safe, orig in name_map.renamed.items()}
        out["required"] = [reverse.get(str(r), r) for r in out["required"]]

    return out, name_map


def sanitize_tool_schema(schema: Dict[str, Any]) -> Tuple[Dict[str, Any], NameMap]:
    """Return ``(schema safe to send as a toolSpec, map to restore names)``.

    The input is not mutated. When nothing needed renaming the returned map is
    empty (``NameMap.is_empty()``), and ``restore_names`` is then a no-op — so a
    schema Bedrock already accepts costs nothing and is sent unchanged.
    """
    # Strip first: metadata keys can never be property names, and removing them
    # before the rename walk keeps the two concerns separate.
    clean, name_map = _sanitize_node(strip_non_wire_keywords(schema))
    if name_map.renamed or name_map.children or name_map.items:
        logger.debug(
            "Sanitized %d top-level tool-schema property name(s) for Bedrock",
            len(name_map.renamed),
        )
    return clean, name_map


def restore_names(value: Any, name_map: Optional[NameMap]) -> Any:
    """Put the authored property names back into a model response.

    Walks ``value`` against ``name_map``. Keys the map does not mention are left
    exactly as they are — a model that echoed an unexpected key must not have it
    silently dropped, because that is how a hallucinated field becomes invisible
    instead of reviewable.
    """
    if name_map is None or name_map.is_empty():
        return value
    if isinstance(value, list):
        return [restore_names(v, name_map.items or name_map) for v in value]
    if not isinstance(value, dict):
        return value

    out: Dict[str, Any] = {}
    for key, val in value.items():
        original = name_map.renamed.get(key, key)
        child = name_map.children.get(key)
        if child is None:
            # Fall back to a $defs map if exactly one is available and the value
            # is a container: a $ref'd child's names live there.
            child = (
                _sole_defs_child(name_map) if isinstance(val, (dict, list)) else None
            )
        out[original] = restore_names(val, child) if child is not None else val
    return out


def _sole_defs_child(name_map: NameMap) -> Optional[NameMap]:
    """The single ``$defs`` child map, when there is exactly one.

    A ``$ref``'d property's names are recorded under the definition, not under
    the property, so restoring a ``$ref``'d value needs that map. With one
    definition this is unambiguous; with several it is not, so nothing is
    guessed — those names simply stay sanitized rather than being restored to
    the wrong field.
    """
    defs = [m for k, m in name_map.children.items() if k.startswith("$defs/")]
    return defs[0] if len(defs) == 1 else None


def find_invalid_property_names(schema: Any, _path: str = "") -> List[str]:
    """Every property name in ``schema`` that Bedrock would reject, with its path.

    Diagnostic helper for tests and config validation. Reports names at ALL
    depths, not just the top level Bedrock currently checks.
    """
    bad: List[str] = []
    if not isinstance(schema, dict):
        return bad
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            for prop_name, prop_schema in value.items():
                here = f"{_path}.{prop_name}" if _path else str(prop_name)
                if not is_valid_tool_property_name(str(prop_name)):
                    bad.append(here)
                bad += find_invalid_property_names(prop_schema, here)
        elif key == "items":
            bad += find_invalid_property_names(value, f"{_path}[]")
        elif key == "$defs" and isinstance(value, dict):
            for def_name, def_schema in value.items():
                bad += find_invalid_property_names(def_schema, f"$defs/{def_name}")
        elif key in _SUBSCHEMA_LISTS and isinstance(value, list):
            for branch in value:
                bad += find_invalid_property_names(branch, _path)
    return bad


def find_document_metadata_keywords(schema: Any, _path: str = "") -> List[str]:
    """Paths of schema-DOCUMENT keywords that must not reach a toolSpec.

    The read-only counterpart to :func:`strip_non_wire_keywords`, used by the
    Bedrock client to fail locally with an actionable message instead of letting
    Converse reject the request. Walks the same subschema keywords as
    :func:`find_invalid_property_names`, and — like it — never looks *inside*
    ``properties`` keys, because a user-authored field may legitimately be named
    like a keyword.
    """
    found: List[str] = []
    if isinstance(schema, list):
        for i, item in enumerate(schema):
            found += find_document_metadata_keywords(item, f"{_path}[{i}]")
        return found
    if not isinstance(schema, dict):
        return found
    for key, value in schema.items():
        here = f"{_path}.{key}" if _path else str(key)
        if key in _DOCUMENT_METADATA_KEYS or key.startswith(_IDP_EXTENSION_PREFIX):
            found.append(here)
            continue
        if key == "properties" and isinstance(value, dict):
            for prop_name, prop_schema in value.items():
                sub = f"{_path}.{prop_name}" if _path else str(prop_name)
                found += find_document_metadata_keywords(prop_schema, sub)
        elif key == "items":
            found += find_document_metadata_keywords(value, f"{_path}[]")
        elif key == "$defs" and isinstance(value, dict):
            for def_name, def_schema in value.items():
                found += find_document_metadata_keywords(
                    def_schema, f"$defs/{def_name}"
                )
        elif key in _SUBSCHEMA_LISTS and isinstance(value, list):
            for branch in value:
                found += find_document_metadata_keywords(branch, _path)
    return found
