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
from urllib.parse import unquote

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

#: Keywords whose dict KEYS are user-authored names, not schema vocabulary.
_USER_KEYED_KEYWORDS = frozenset(
    {
        "properties",
        "$defs",
        "patternProperties",
        "dependentSchemas",
        "dependentRequired",
    }
)


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
        # The KEYS of these keywords are user data — field names (`properties`,
        # `dependentSchemas`, `dependentRequired`), definition names (`$defs`) or
        # regexes (`patternProperties`) — and may legitimately be spelled like a
        # metadata keyword (`id`, `$comment`). Never filter inside them. Before
        # this a group called `id` was deleted from `$defs` outright while the
        # `$ref` to it stayed, leaving a pointer to nothing.
        if key in _USER_KEYED_KEYWORDS and isinstance(value, dict):
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
    #: sanitized property name -> the container kind(s) its schema declares
    #: (``{"object"}``, ``{"array"}`` or both), resolved THROUGH ``$ref`` and
    #: combinators. Absent when the schema is not a container OR also permits a
    #: plain string. ``restore_names`` may JSON-parse a string under one of these
    #: keys — a group the model serialized (#783) — but only into the declared
    #: kind, and never for a genuine string field that happens to hold JSON text.
    container_kinds: Dict[str, frozenset[str]] = field(default_factory=dict)
    #: sanitized ``$defs`` definition name -> original definition name, for the
    #: ``$defs`` block AT THIS LEVEL only (a nested block lives on its property's
    #: child map). Per level because sanitization is per block: the same original
    #: name can legitimately map to different safe names in different blocks, so
    #: one flat map would cross-wire pointers. Not consulted by ``restore_names``
    #: — a definition name never appears in a model response.
    defs_renamed: Dict[str, str] = field(default_factory=dict)

    def _beneath(self) -> List["NameMap"]:
        """Every distinct map reachable from here, this one first.

        After ``_link_maps`` a ``$ref``'d definition's map is shared by every
        property that references it, and a recursive definition makes the graph
        cyclic — so the aggregates below walk by identity, once per map.
        """
        seen: Dict[int, "NameMap"] = {}
        stack: List["NameMap"] = [self]
        while stack:
            m = stack.pop()
            if id(m) in seen:
                continue
            seen[id(m)] = m
            stack.extend(m.children.values())
            if m.items is not None:
                stack.append(m.items)
        return list(seen.values())

    def total_definition_renames(self) -> int:
        """Definition renames at this level and every level beneath (audit count).

        A shared (``$ref``-linked) map is counted once, however many properties
        reference it."""
        return sum(len(m.defs_renamed) for m in self._beneath())

    def is_empty(self) -> bool:
        """True when nothing anywhere beneath this level was renamed — property
        names OR definition names. A schema whose `$defs` key changed did change on
        the wire, even if every property name was already valid."""
        return not any(m.renamed or m.defs_renamed for m in self._beneath())


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


_CONTAINER = frozenset({"object", "array"})
_MAX_REF_DEPTH = 32


def _declared_types(node: Dict[str, Any]) -> List[str]:
    declared = node.get("type")
    if isinstance(declared, str):
        return [declared]
    return (
        [x for x in declared if isinstance(x, str)]
        if isinstance(declared, list)
        else []
    )


class _KindResolver:
    """Container-kind and permits-string queries over ONE sanitized schema.

    Memoized by node identity, so a definition reached through many ``$ref``s (a
    DAG) is examined once instead of once per path — the un-memoized recursion
    was exponential in a chain of ``anyOf``-doubled pointers. A node re-entered
    while it is still being resolved (a ``$ref`` cycle) contributes nothing on
    that path, which is also what terminates the recursion.
    """

    def __init__(self, root: Any) -> None:
        self.root = root
        self._kinds: Dict[int, frozenset[str]] = {}
        self._strings: Dict[int, bool] = {}
        self._in_progress: set[int] = set()

    def container_kinds(self, node: Any) -> frozenset[str]:
        """The container kind(s) a property's schema declares, or empty.

        Resolves ``$ref`` against the sanitized root and looks through combinator
        branches, so a ``$ref`` to a shared *string* definition — an enum or a
        formatted code factored into ``$defs`` — is a string, not a group. Empty
        when the schema is not a container OR also permits a plain string: that
        exclusion mirrors coercion's rule for the same repair, because a field
        declared ``["object", "string"]`` legitimately holds text and a
        JSON-looking string under it must stay a string. Without the exclusion
        the two layers disagreed and ``restore_names`` (which runs first) won.
        """
        if self.permits_string(node):
            return frozenset()
        return self.kinds(node)

    def permits_string(self, node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        key = id(node)
        if key in self._strings:
            return self._strings[key]
        if key in self._in_progress:
            return False
        self._in_progress.add(key)
        try:
            result = "string" in _declared_types(node)
            if not result and "$ref" in node:
                result = self.permits_string(_resolve_pointer(self.root, node["$ref"]))
            if not result:
                for k in _SUBSCHEMA_LISTS:
                    branches = node.get(k)
                    if isinstance(branches, list) and any(
                        self.permits_string(b) for b in branches
                    ):
                        result = True
                        break
        finally:
            self._in_progress.discard(key)
        self._strings[key] = result
        return result

    def kinds(self, node: Any) -> frozenset[str]:
        if not isinstance(node, dict):
            return frozenset()
        key = id(node)
        if key in self._kinds:
            return self._kinds[key]
        if key in self._in_progress:
            return frozenset()
        self._in_progress.add(key)
        try:
            found: set[str] = set(_CONTAINER & set(_declared_types(node)))
            if "properties" in node:
                found.add("object")
            if "items" in node or "prefixItems" in node:
                found.add("array")
            if "$ref" in node:
                target = _resolve_pointer(self.root, node["$ref"])
                if target is None:
                    # A pointer we cannot follow (non-local, or into a keyword we
                    # do not track): unknown, so keep the pre-#794 reading of
                    # "$ref = a group".
                    found |= _CONTAINER
                else:
                    found |= self.kinds(target)
            for k in _SUBSCHEMA_LISTS:
                branches = node.get(k)
                if isinstance(branches, list):
                    for b in branches:
                        found |= self.kinds(b)
        finally:
            self._in_progress.discard(key)
        result = frozenset(found)
        self._kinds[key] = result
        return result


def _container_kinds(node: Any, root: Any) -> frozenset[str]:
    """One-off form of :meth:`_KindResolver.container_kinds` (tests, probes)."""
    return _KindResolver(root).container_kinds(node)


def _resolve_pointer(root: Any, ref: Any) -> Any:
    """The node a LOCAL JSON pointer (``#/a/b/0``) addresses in ``root``, or None.

    Segments are percent-decoded and RFC 6901-unescaped; a list is indexed by
    integer segment. Anything that does not resolve is None — never a guess.
    """
    segs = _pointer_segments(ref)
    if segs is None:
        return None
    node = root
    for seg in segs:
        key = _decode_segment(seg)
        if isinstance(node, dict):
            if key not in node:
                return None
            node = node[key]
        elif isinstance(node, list):
            try:
                node = node[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


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
                # Container kinds and `$ref` links are recorded by ``_link_maps``
                # once the whole schema is sanitized — both need the root.
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
            # property names must be sanitized too — AND so must the DEFINITION
            # names. Bedrock's validation rule does not cover them, which is why
            # they were originally left alone, but the model has to RESOLVE the
            # `$ref` pointer that names them, and Claude Sonnet 5 does not resolve
            # `#/$defs/Account Holder Address`: it emits the group as a serialized
            # JSON string instead of an object, making every section carrying a
            # spaced group name schema-invalid (#783; Sonnet 4.6 resolved it, which
            # is why this went unnoticed). Renaming the definitions and rewriting
            # every `$ref` that targets them (see ``_rewrite_refs``) keeps the wire
            # schema free of anything that has to resolve by luck.
            clean_defs = {}
            taken_defs: set[str] = set()
            for def_name, def_schema in value.items():
                safe_def = sanitize_property_name(str(def_name), taken_defs)
                taken_defs.add(safe_def)
                if safe_def != def_name:
                    name_map.defs_renamed[safe_def] = str(def_name)
                def_clean, def_map = _sanitize_node(def_schema)
                clean_defs[safe_def] = def_clean
                if not def_map.is_empty():
                    # Keyed by the (sanitized) definition name. ``_link_maps``
                    # attaches this same map to every property whose `$ref`
                    # resolves to the definition, so restore_names finds it where
                    # the VALUE appears.
                    name_map.children[f"$defs/{safe_def}"] = def_map
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
    empty (``NameMap.is_empty()``) and ``restore_names`` is a no-op. The wire
    schema is then unchanged except that each ``$ref`` node gains its
    definition's ``type`` (see ``_annotate_ref_types``); property and definition
    NAMES are byte-identical.
    """
    # Strip first: metadata keys can never be property names, and removing them
    # before the rename walk keeps the two concerns separate.
    clean, name_map = _sanitize_node(strip_non_wire_keywords(schema))
    if not name_map.is_empty():
        clean = _rewrite_refs(clean, name_map)
        _link_maps(clean, name_map)
    clean = _annotate_ref_types(clean)
    if not name_map.is_empty():
        logger.debug(
            "Sanitized %d top-level tool-schema property name(s) and %d $defs "
            "definition name(s) for Bedrock",
            len(name_map.renamed),
            name_map.total_definition_renames(),
        )
    return clean, name_map


_DEF_SEGMENT = "$defs"


def _pointer_segments(ref: Any) -> Optional[List[str]]:
    """Split a LOCAL JSON pointer (``#/...``) into its raw segments, or None.

    Raw means percent-encoding and RFC 6901 escapes are left as written, so a
    segment can be matched against a definition name spelled either way.
    """
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    return ref[2:].split("/")


def _decode_segment(seg: str) -> str:
    """Percent-decode, then undo RFC 6901 escapes (``~1`` -> ``/``, ``~0`` -> ``~``)."""
    return unquote(seg).replace("~1", "/").replace("~0", "~")


def _encode_segment(name: str) -> str:
    """RFC 6901-escape a definition name for use as a pointer segment.

    Sanitized names contain only ``[a-zA-Z0-9_.-]``, so this is the identity for
    every name this module produces; kept for correctness if that ever changes.
    """
    return name.replace("~", "~0").replace("/", "~1")


def _rewrite_refs(node: Any, root_map: NameMap) -> Any:
    """Rewrite every local ``$ref`` so each pointer segment names the SANITIZED key
    at that exact location.

    The pointer is walked against the ``NameMap`` tree the sanitizer built, so a
    ``properties`` segment is mapped through that level's property renames, a
    ``$defs`` segment through THAT block's definition renames (per block — two
    blocks may sanitize the same original name differently), ``items`` descends
    into the item map, and a combinator index passes through. Segments are
    percent-decoded and RFC 6901-unescaped before matching — the order a
    conforming resolver (and ``jsonschema``) uses, so a pointer that could name
    either of two definitions differing only by encoding governs the same one
    before and after sanitizing — with a RAW match as the fallback so a name that
    literally contains ``%`` still resolves; the result is RFC 6901-escaped.

    Pointers this cannot follow (unknown keywords, non-local URIs) are returned
    unchanged rather than guessed at.
    """
    EMPTY = NameMap()

    def _map_name(lookup: Dict[str, str], raw: str) -> Optional[str]:
        # ``lookup`` is safe -> original; invert once per call (small maps).
        reverse = {orig: safe for safe, orig in lookup.items()}
        decoded = _decode_segment(raw)
        if decoded in reverse:
            return reverse[decoded]
        if raw in reverse:
            return reverse[raw]
        # Not renamed at this level: the segment is already its own safe spelling
        # (or unknown to us); keep it verbatim.
        return None

    def _rewrite_pointer(ref: str) -> str:
        segs = _pointer_segments(ref)
        if not segs:
            return ref
        out = list(segs)
        level: NameMap = root_map
        i = 0
        while i < len(segs):
            seg = segs[i]
            if seg == "properties" and i + 1 < len(segs):
                safe = _map_name(level.renamed, segs[i + 1])
                if safe is not None:
                    out[i + 1] = _encode_segment(safe)
                key = safe if safe is not None else _decode_segment(segs[i + 1])
                level = level.children.get(key, EMPTY)
                i += 2
            elif seg == _DEF_SEGMENT and i + 1 < len(segs):
                safe = _map_name(level.defs_renamed, segs[i + 1])
                if safe is not None:
                    out[i + 1] = _encode_segment(safe)
                key = safe if safe is not None else _decode_segment(segs[i + 1])
                level = level.children.get(f"$defs/{key}", EMPTY)
                i += 2
            elif seg == "items":
                level = level.items or EMPTY
                i += 1
            elif seg in _SUBSCHEMA_LISTS and i + 1 < len(segs):
                # Branch maps were merged into the parent; stay at this level.
                i += 2
            else:
                # A keyword we do not track (additionalProperties, patternProperties,
                # ...). Names below it were not sanitized by us either, so stop
                # mapping but keep the remaining segments verbatim.
                break
        return "#/" + "/".join(out)

    def _walk(n: Any) -> Any:
        if isinstance(n, list):
            return [_walk(v) for v in n]
        if not isinstance(n, dict):
            return n
        result: Dict[str, Any] = {}
        for key, value in n.items():
            if key == "$ref" and isinstance(value, str):
                result[key] = _rewrite_pointer(value)
            elif key == "properties" and isinstance(value, dict):
                # Property names are user data and may be spelled "$ref"; only
                # the VALUES are subschemas.
                result[key] = {k: _walk(v) for k, v in value.items()}
            else:
                result[key] = _walk(value)
        return result

    return _walk(node)


def _annotate_ref_types(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a referenced definition's ``type`` onto each ``$ref`` node lacking one.

    Belt-and-braces for the same #783 failure: with an explicit ``"type":
    "object"`` beside the ``$ref``, Sonnet 5 returned an object even for the
    pointer it otherwise mis-resolved. Legal in JSON Schema 2020-12 (``$ref`` is
    an ordinary keyword that combines with its siblings), redundant when the
    pointer resolves, and cheap. Only ``type`` is copied — never the definition's
    body — so the schema grows by one key per ``$ref``, not by the definition.

    Only a definition with a single string ``type`` is copied; a list-typed
    definition (``["object", "null"]``) is left alone, because the point is to give
    the model an unambiguous ``object`` hint, and a union is not that. Runs AFTER
    ``_rewrite_refs`` and resolves each local pointer against the sanitized
    schema — a root ``$defs`` entry or a definition in a nested block alike.
    """

    def _walk(node: Any) -> Any:
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        out: Dict[str, Any] = {}
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                out[key] = {k: _walk(v) for k, v in value.items()}
            else:
                out[key] = _walk(value)
        if "type" not in node and "$ref" in node:
            definition = _resolve_pointer(schema, node["$ref"])
            depth = 0
            # Follow a chain of bare $refs (A -> B -> the typed definition).
            while (
                isinstance(definition, dict)
                and "type" not in definition
                and isinstance(definition.get("$ref"), str)
                and depth < _MAX_REF_DEPTH
            ):
                definition = _resolve_pointer(schema, definition["$ref"])
                depth += 1
            if isinstance(definition, dict) and isinstance(definition.get("type"), str):
                out["type"] = definition["type"]
        return out

    return _walk(schema)


_MAX_LINK_PASSES = 64

#: Keywords that give a schema node structure of its own, beyond a bare ``$ref``.
_STRUCTURE_KEYS = (
    "properties",
    "items",
    "prefixItems",
    "$defs",
    "patternProperties",
    "additionalProperties",
)


def _has_own_structure(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if any(isinstance(node.get(k), (dict, list)) for k in _STRUCTURE_KEYS):
        return True
    for key in _SUBSCHEMA_LISTS:
        branches = node.get(key)
        if isinstance(branches, list) and any(_has_own_structure(b) for b in branches):
            return True
    return False


def _link_maps(clean: Any, root_map: NameMap) -> None:
    """Attach each ``$ref``'d definition's map where its VALUE will appear, and
    record every property's container kind(s).

    The sanitizer records a definition's inner names under ``children["$defs/X"]``
    because that is where the *schema* keeps them; a model *response* keeps the
    value under the property that references the definition. Walking the
    sanitized schema against the map tree, this resolves each ``$ref`` (already
    rewritten to sanitized segments) to the definition's map and links it under
    the referencing property (or as ``items`` for a list of them), so
    ``restore_names`` follows the response without guessing. A pointer it cannot
    follow leaves those names sanitized — never restored to a wrong field.

    Two rules keep the links honest:

    * **A definition's map is never mutated from a use site.** A site that is a
      bare ``$ref`` (possibly ``anyOf``'d with ``null``) SHARES the definition's
      map by identity. A site with structure of its own — inline properties
      beside the ``$ref``, two combinator branches pointing at different
      definitions, an alias already holding a shared map that now needs more —
      gets a site-owned map that ABSORBS the definitions instead, so nothing
      leaks from one ``$ref`` site into every other ``$ref`` to that definition.
    * **Linking runs to a fixpoint.** A rename-free definition that only
      references renamed ones has no map until a pass creates one, and a chain
      of such definitions needs one pass per link; a created map is attached
      before its subtree is walked so pointers into it resolve within the pass.
      Passes stop when a pass changes nothing (bounded by ``_MAX_LINK_PASSES``).
    """
    _Linker(root_map).run(clean)


class _Linker:
    def __init__(self, root_map: NameMap) -> None:
        self.root_map = root_map
        self._kinds: Optional[_KindResolver] = None
        self.definition_ids: set[int] = set()
        self.changed = False
        for m in root_map._beneath():
            for key, child in m.children.items():
                if key.startswith(f"{_DEF_SEGMENT}/"):
                    self.definition_ids.add(id(child))

    def run(self, clean: Any) -> None:
        self._kinds = _KindResolver(clean)
        for _ in range(_MAX_LINK_PASSES):
            self.changed = False
            self._walk(clean, self.root_map)
            if not self.changed:
                return
        logger.warning(
            "tool-schema name-map linking did not converge in %d passes; some "
            "$ref'd names may stay sanitized",
            _MAX_LINK_PASSES,
        )

    def _walk(self, node: Any, level: NameMap) -> None:
        if not isinstance(node, dict):
            return

        defs = node.get(_DEF_SEGMENT)
        if isinstance(defs, dict):
            for safe_def, body in defs.items():
                key = f"{_DEF_SEGMENT}/{safe_def}"
                self._site(body, level, level.children, key, is_definition=True)

        props = node.get("properties")
        if isinstance(props, dict):
            for safe, sub in props.items():
                assert self._kinds is not None
                kinds = self._kinds.container_kinds(sub)
                if kinds:
                    level.container_kinds[safe] = kinds
                self._site(sub, level, level.children, safe)

        items = node.get("items")
        if isinstance(items, dict):
            self._site(items, level, None, "items")

        for key in _SUBSCHEMA_LISTS:
            branches = node.get(key)
            if isinstance(branches, list):
                for branch in branches:
                    # Branch maps were merged into this level by the sanitizer.
                    self._walk(branch, level)

    def _site(
        self,
        sub: Any,
        level: NameMap,
        slot: Optional[Dict[str, NameMap]],
        key: str,
        *,
        is_definition: bool = False,
    ) -> None:
        """Link one schema node (a property, definition or ``items`` schema)."""

        def get() -> Optional[NameMap]:
            return slot.get(key) if slot is not None else level.items

        def put(m: Optional[NameMap]) -> None:
            if slot is not None:
                if m is None:
                    slot.pop(key, None)
                else:
                    slot[key] = m
            else:
                level.items = m

        targets: List[NameMap] = []
        for ref in _refs_in(sub):
            m = _resolve_map(self.root_map, ref)
            if m is not None and all(m is not x for x in targets):
                targets.append(m)
        pure = len(targets) == 1 and not _has_own_structure(sub)

        existing = get()
        created = False
        if existing is None:
            if pure:
                put(targets[0])  # share by identity
                self.changed = True
                return
            child = NameMap()
            created = True
            put(child)  # before the walk, so pointers INTO it resolve this pass
            if is_definition:
                self.definition_ids.add(id(child))
        elif (
            not is_definition
            and id(existing) in self.definition_ids
            and not (pure and existing is targets[0])
        ):
            # A use site holding a shared definition map that now needs structure
            # of its own (a second branch, inline properties): fork a site-owned
            # copy rather than writing into the definition.
            child = NameMap()
            _absorb(child, existing)
            put(child)
            self.changed = True
        else:
            child = existing
            if pure and existing is targets[0]:
                return  # already linked; a bare $ref has nothing to walk

        before = _signature(child)
        for target in targets:
            if target is not child:
                _absorb(child, target)
        self._walk(sub, child)
        if created and child.is_empty():
            put(None)
            self.definition_ids.discard(id(child))
        elif _signature(child) != before:
            self.changed = True


def _signature(m: NameMap) -> Tuple[int, int, int, bool]:
    return (
        len(m.renamed),
        len(m.children),
        len(m.container_kinds),
        m.items is not None,
    )


def _refs_in(node: Any) -> List[str]:
    """Local ``$ref`` pointers on ``node`` itself or its combinator branches."""
    if not isinstance(node, dict):
        return []
    refs: List[str] = []
    ref = node.get("$ref")
    if isinstance(ref, str):
        refs.append(ref)
    for key in _SUBSCHEMA_LISTS:
        branches = node.get(key)
        if isinstance(branches, list):
            for b in branches:
                refs += _refs_in(b)
    return refs


def _resolve_map(root_map: NameMap, ref: str) -> Optional[NameMap]:
    """The ``NameMap`` for the schema node a (rewritten) local pointer addresses."""
    segs = _pointer_segments(ref)
    if not segs:
        return None
    level: Optional[NameMap] = root_map
    i = 0
    while i < len(segs) and level is not None:
        seg = segs[i]
        if seg == "properties" and i + 1 < len(segs):
            level = level.children.get(_decode_segment(segs[i + 1]))
            i += 2
        elif seg == _DEF_SEGMENT and i + 1 < len(segs):
            level = level.children.get(f"$defs/{_decode_segment(segs[i + 1])}")
            i += 2
        elif seg == "items":
            level = level.items
            i += 1
        elif seg in _SUBSCHEMA_LISTS and i + 1 < len(segs):
            i += 2
        else:
            return None
    return level


def _absorb(into: NameMap, other: NameMap) -> None:
    """Copy ``other``'s names into ``into`` (a site-owned map). Sub-maps are
    shared by reference, never the top-level dicts, so ``other`` is never written."""
    into.renamed.update(other.renamed)
    into.children.update(other.children)
    into.container_kinds.update(other.container_kinds)
    into.defs_renamed.update(other.defs_renamed)
    if into.items is None:
        into.items = other.items


def restore_names(value: Any, name_map: Optional[NameMap]) -> Any:
    """Put the authored property names back into a model response.

    Walks ``value`` against ``name_map``. Keys the map does not mention are left
    exactly as they are — a model that echoed an unexpected key must not have it
    silently dropped, because that is how a hallucinated field becomes invisible
    instead of reviewable. A ``$ref``'d group's names are found through the link
    ``_link_maps`` recorded under the referencing property; where no link exists
    (a pointer the sanitizer could not follow) the inner names stay sanitized
    rather than being restored against a guessed map.
    """
    if name_map is None or name_map.is_empty():
        return value
    if isinstance(value, list):
        item_map = name_map.items
        out_list = []
        for v in value:
            if isinstance(v, str) and item_map is not None:
                # A list whose ELEMENTS the model serialized. An items map exists
                # only when the item schema has (renamed) properties, i.e. it is
                # an object, so only an object parse is accepted.
                parsed = _parse_serialized_container(v)
                if isinstance(parsed, dict):
                    v = parsed
            out_list.append(restore_names(v, item_map or name_map))
        return out_list
    if not isinstance(value, dict):
        return value

    out: Dict[str, Any] = {}
    for key, val in value.items():
        original = name_map.renamed.get(key, key)
        kinds = name_map.container_kinds.get(key)
        if isinstance(val, str) and kinds:
            # A group the model serialized as a JSON STRING (Sonnet 5 did this for
            # a `$ref` it could not resolve, #783). Its inner keys are the
            # SANITIZED spellings the model was given, so if it is left as text
            # for coercion to parse later, the parsed object carries wire names
            # that nothing restores — and they leak into inference_result. Parse
            # it here, where the map is, so the inner names come back too. ONLY
            # under a key the schema declares as a container (never one that also
            # permits a string), and ONLY into the kind it declares: `"[1, 2]"`
            # in an object field is not a repair coercion would make either.
            parsed = _parse_serialized_container(val)
            parsed_kind = (
                "object"
                if isinstance(parsed, dict)
                else "array"
                if isinstance(parsed, list)
                else None
            )
            if parsed_kind is not None and parsed_kind in kinds:
                logger.info(
                    "Parsed a serialized %s the model returned as a string for "
                    "tool-schema field %r and restored its inner names",
                    parsed_kind,
                    original,
                )
                val = parsed
        child = name_map.children.get(key)
        out[original] = restore_names(val, child) if child is not None else val
    return out


def _parse_serialized_container(text: str) -> Any:
    """``text`` as a dict/list if it is exactly the JSON of one, else None.

    Only when a name map is in play (callers check), and only a lossless parse:
    no NaN/Infinity constants, no duplicate keys, no non-finite overflow.
    """
    import json
    import math

    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None

    def _no_constants(_name: str) -> Any:
        raise ValueError("non-finite constant")

    def _no_dupes(pairs: Any) -> Dict[str, Any]:
        seen: Dict[str, Any] = {}
        for k, v in pairs:
            if k in seen:
                raise ValueError("duplicate key")
            seen[k] = v
        return seen

    def _finite(v: Any) -> bool:
        if isinstance(v, float):
            return math.isfinite(v)
        if isinstance(v, dict):
            return all(_finite(x) for x in v.values())
        if isinstance(v, list):
            return all(_finite(x) for x in v)
        return True

    try:
        parsed = json.loads(
            stripped, parse_constant=_no_constants, object_pairs_hook=_no_dupes
        )
        # Inside the try: this Python recursion trips RecursionError (~500
        # levels) long before json's C scanner does (~10,000).
        finite = isinstance(parsed, (dict, list)) and _finite(parsed)
    except (ValueError, RecursionError):
        return None
    return parsed if finite else None


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
                here = f"$defs/{def_name}"
                # A definition name is not a property key, and Bedrock accepts
                # any spelling — but a `$ref` pointer containing a space is not
                # resolved by Sonnet 5 (#783), so it is reported here too.
                if not is_valid_tool_property_name(str(def_name)):
                    bad.append(here)
                bad += find_invalid_property_names(def_schema, here)
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
