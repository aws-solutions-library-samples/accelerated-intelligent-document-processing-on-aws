"""Read the awscc provider schema into type hints the emitter can use.

The schema is the output of ``terraform providers schema -json`` for a pinned
``hashicorp/awscc`` version. awscc is generated from the CloudFormation
registry, so an awscc attribute name is the CloudFormation property name in
snake_case; matching on :func:`hcl.norm` is exact, not fuzzy, and an ambiguous
match is an error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hcl import norm


@dataclass
class Hint:
    kind: str  # str | num | bool | list | set | map | obj | any
    el: Hint | None = None
    attrs: dict[str, tuple[str, Hint]] = field(default_factory=dict)
    computed_only: set[str] = field(default_factory=set)

    def attr(self, cfn_name: str) -> tuple[str, Hint] | None:
        return self.attrs.get(norm(cfn_name))


ANY = Hint("any")


def _from_type(t: Any) -> Hint:
    if isinstance(t, str):
        return Hint({"string": "str", "number": "num", "bool": "bool"}.get(t, "any"))
    if isinstance(t, list) and t:
        head = t[0]
        if head in ("list", "set", "map"):
            return Hint(head, el=_from_type(t[1]))
        if head == "object":
            return _object({k: {"type": v} for k, v in t[1].items()})
    return ANY


def _object(attributes: dict[str, Any]) -> Hint:
    hint = Hint("obj")
    for name, spec in attributes.items():
        if "nested_type" in spec:
            nested = spec["nested_type"]
            inner = _object(nested.get("attributes", {}))
            mode = nested.get("nesting_mode", "single")
            child = inner if mode == "single" else Hint(mode, el=inner)
        else:
            child = _from_type(spec.get("type"))
        key = norm(name)
        if key in hint.attrs:
            raise ValueError(f"ambiguous awscc attribute key {key!r}")
        hint.attrs[key] = (name, child)
        if (
            spec.get("computed")
            and not spec.get("optional")
            and not spec.get("required")
        ):
            hint.computed_only.add(key)
    return hint


#: How a resource's tag attribute is spelled in the awscc schema, which decides
#: how the module's ``tags`` variable is merged into it:
#:
#: - ``kv``: a list or set of ``{key, value}`` objects (the CloudFormation
#:   ``Tags: [{Key, Value}]`` shape, most types);
#: - ``map``: ``map(string)`` (types whose registry schema declares a JSON
#:   object of tags, such as SSM parameters);
#: - ``json``: a ``string`` holding a JSON object (types whose registry schema
#:   declares ``Tags`` as ``Json``, such as Glue crawlers).
TAG_SHAPES = ("kv", "map", "json")


def _tag_shape(spec: dict[str, Any]) -> str | None:
    """The :data:`TAG_SHAPES` member an attribute spec is, or None."""
    nested = spec.get("nested_type")
    if nested is not None:
        attrs = nested.get("attributes", {})
        if nested.get("nesting_mode") in ("list", "set") and set(attrs) == {
            "key",
            "value",
        }:
            if all(a.get("type") == "string" for a in attrs.values()):
                return "kv"
        return None
    t = spec.get("type")
    if t == ["map", "string"]:
        return "map"
    if t == "string":
        return "json"
    return None


def _is_tag_attribute(name: str) -> bool:
    """CloudFormation names a resource's tag property ``Tags`` or
    ``<Something>Tags`` (``IdentityPoolTags``, ``UserPoolTags``); awscc spells
    the property in snake_case."""
    return name == "tags" or name.endswith("_tags")


def tag_attribute_of(attributes: dict[str, Any], label: str) -> tuple[str, str] | None:
    """``(awscc attribute, shape)`` of a resource type's settable tag attribute,
    from the type's top-level schema ``attributes``; None when the schema gives
    the type no tag attribute, or only a computed one. Read from the schema, so
    a type gains or loses the merge with the provider version the schema is for.

    Raises ValueError for a tag attribute in a shape the translator cannot merge
    into, or for a type with two: either would otherwise leave the resource
    without the module's tags and nothing said. ``label`` names the type in the
    message.
    """
    found = []
    for name, spec in attributes.items():
        if not _is_tag_attribute(name):
            continue
        if not (spec.get("optional") or spec.get("required")):
            continue  # computed only: the provider does not accept a value
        shape = _tag_shape(spec)
        if shape is None:
            raise ValueError(
                f"{label}: tag attribute {name!r} has a shape the translator "
                "cannot merge the module's tags into"
            )
        found.append((name, shape))
    if len(found) > 1:
        raise ValueError(f"{label}: more than one tag attribute: {found}")
    return found[0] if found else None


def awscc_type_name(cfn_type: str) -> str:
    _, service, resource = cfn_type.split("::")
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", resource)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()
    return f"awscc_{service.lower()}_{s}"


class AwsccSchema:
    def __init__(self, path: Path):
        data = json.loads(path.read_text())
        providers = data["provider_schemas"]
        key = next(k for k in providers if k.endswith("/awscc"))
        self._resources = providers[key]["resource_schemas"]
        self.version = data.get("awscc_version", "")
        self._cache: dict[str, Hint] = {}

    def has(self, cfn_type: str) -> bool:
        return awscc_type_name(cfn_type) in self._resources

    def hint(self, cfn_type: str) -> Hint:
        name = awscc_type_name(cfn_type)
        if name not in self._cache:
            block = self._resources[name]["block"]
            self._cache[name] = _object(block.get("attributes", {}))
        return self._cache[name]

    def tag_attribute(self, cfn_type: str) -> tuple[str, str] | None:
        """``(awscc attribute, shape)`` of the type's settable tag attribute; see
        :func:`tag_attribute_of`."""
        name = awscc_type_name(cfn_type)
        return tag_attribute_of(
            self._resources[name]["block"].get("attributes", {}), cfn_type
        )
