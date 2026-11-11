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
