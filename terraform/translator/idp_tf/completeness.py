"""Every CloudFormation property value reaches the Terraform it became.

The translator fails on a property it has no mapping for, but a mapper that
*reads* a property can still drop part of it: a sub-key it never looks at, a
value it replaces with a default, a parameter passed to a nested module as
``""``. That is the shape of a deployment that works, matches on every check
that compares what is there, and is softer than CloudFormation, because the
missing value was a guardrail, a retention period or a key.

This check reads each resource's properties and the HCL emitted for it, and
requires every value to be visible in that HCL:

- a non-empty literal (string, number, boolean) appears, as HCL would spell it;
- a ``Ref`` to a parameter appears as its variable, a ``Ref`` or ``Fn::GetAtt``
  to a resource or nested stack names that resource or module, a condition as
  its local, and the literal text of an ``Fn::Sub`` appears around its
  references.

It is a presence check, not a proof of equivalence: a value that appears in the
wrong attribute passes. What it does catch is a value that appears nowhere.
Each value that legitimately does not appear is listed in ``NOT_APPLICABLE``
with its reason, keyed by (type, property path); an entry that no longer
matches anything fails, so the list cannot outlive what it described.

Nested-stack ``Parameters`` are checked the same way on the ``module`` block, so
a parameter the parent passes but the module call drops fails here too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

from . import hcl

if TYPE_CHECKING:
    from .translate import Translator

#: (CloudFormation type, property path) -> why the value is not in the HCL.
#: Paths are "/"-separated property names; list positions are not part of them.
NOT_APPLICABLE: dict[tuple[str, str], str] = {
    ("AWS::S3::Bucket", "AccessControl"): (
        "only Private is accepted (any other value fails the translation), and "
        "Private is the provider's default ACL, so nothing is written"
    ),
    ("AWS::Lambda::Function", "Code/ZipFile"): (
        "inline source is written into the module's zips/ as index.py or "
        "index.js, and the function deploys that zip by its hash"
    ),
    ("AWS::DynamoDB::Table", "KeySchema/KeyType"): (
        "HASH and RANGE become the attribute names hash_key and range_key; any "
        "other key type fails the translation"
    ),
    ("AWS::DynamoDB::Table", "GlobalSecondaryIndexes/KeySchema/KeyType"): (
        "HASH and RANGE become the attribute names hash_key and range_key; any "
        "other key type fails the translation"
    ),
    ("AWS::DynamoDB::Table", "SSESpecification/SSEType"): (
        "KMS is the only type DynamoDB accepts and the one the provider sends "
        "when encryption is enabled; any other value fails the translation"
    ),
}

_SUB_REF = re.compile(r"\$\{([^}]*)\}")


@dataclass(frozen=True)
class Missing:
    stack: str
    logical_id: str
    cfn_type: str
    path: str
    value: str

    def describe(self) -> str:
        where = f"{self.stack}/{self.logical_id}" if self.stack else self.logical_id
        return (
            f"{where} ({self.cfn_type}) {self.path} = {self.value}: not in the "
            "emitted Terraform"
        )


def _literal_forms(v: Any) -> list[str]:
    """How a literal can be spelled in the HCL emitted for it."""
    if isinstance(v, bool):
        return ["true", "false"] if v else ["false", "true"]
    if isinstance(v, (int, float)):
        text = repr(v)
        return [text, text.removesuffix(".0")]
    text = str(v)
    forms = [hcl.escape_template(text), text]
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            minified = json.dumps(json.loads(stripped), separators=(",", ":"))
            forms += [minified, hcl.escape_template(minified)]
        except ValueError:
            pass
    # Inside a jsonencode()d document or an escaped JSON string.
    forms.append(json.dumps(text)[1:-1])
    forms.append(hcl.escape_template(json.dumps(text)[1:-1]))
    if text.lower() in ("true", "false"):
        forms.append(text.lower())
    return [f for f in forms if f]


def _needles(tr: Translator, v: Any) -> Iterator[tuple[str, list[str]]]:
    """(description, alternatives) per atom of a value; one alternative of each
    atom must appear."""
    from .translate import NO_VALUE, is_intrinsic

    if v is None or v == "" or v == NO_VALUE:
        return
    if isinstance(v, list):
        for x in v:
            yield from _needles(tr, x)
        return
    if isinstance(v, dict) and not is_intrinsic(v):
        for k, x in v.items():
            # Map keys the template writes (tags, environment variables) are
            # values too, and appear quoted.
            if isinstance(k, str) and not k[:1].isupper():
                yield (repr(k), _literal_forms(k))
            yield from _needles(tr, x)
        return
    if not isinstance(v, dict):
        yield (repr(v)[:80], _literal_forms(v))
        return
    fn, arg = next(iter(v.items()))
    if fn == "Ref":
        yield from _ref_needle(tr, arg)
    elif fn == "Fn::GetAtt":
        name = arg.split(".", 1)[0] if isinstance(arg, str) else arg[0]
        yield from _ref_needle(tr, name)
    elif fn == "Fn::Sub":
        text, variables = (arg if isinstance(arg, list) else [arg, {}])[:2]
        pos = 0
        for m in _SUB_REF.finditer(text):
            frag = text[pos : m.start()]
            if frag.strip():
                yield (repr(frag)[:80], _literal_forms(frag))
            name = m.group(1).strip()
            if name.startswith("!"):
                pass
            elif name in variables:
                yield from _needles(tr, variables[name])
            else:
                yield from _ref_needle(tr, name.split(".", 1)[0])
            pos = m.end()
        tail = text[pos:]
        if tail.strip():
            yield (repr(tail)[:80], _literal_forms(tail))
    elif fn == "Fn::If":
        cond, a, b = arg
        yield (f"Condition {cond}", [f"c_{hcl.snake(cond)}"])
        yield from _needles(tr, a)
        yield from _needles(tr, b)
    elif fn == "Condition":
        yield (f"Condition {arg}", [f"c_{hcl.snake(arg)}"])
    elif fn == "Fn::FindInMap":
        yield (f"FindInMap {arg[0]}", [hcl.string(str(arg[0]))])
        for x in arg[1:]:
            yield from _needles(tr, x)
    else:
        for x in arg if isinstance(arg, list) else [arg]:
            yield from _needles(tr, x)


def _ref_needle(tr: Translator, name: str) -> Iterator[tuple[str, list[str]]]:
    if name.startswith("AWS::") or name.startswith("IDP::"):
        return
    if name in tr.param_vars:
        var = tr.param_vars[name]
        yield (f"Ref {name}", [f"var.{var}"])
        return
    plan = tr.plans.get(name)
    if plan is None:
        return
    # Mappers often reach a resource through one of its companions (a role's
    # policy, a bucket's configuration), all labelled after it.
    yield (f"reference to {name}", [plan.name])


def _walk(v: Any, path: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    """(path, value) of each property value; descends into literal objects so a
    path can be declared not-applicable at any depth."""
    from .translate import is_intrinsic

    if isinstance(v, dict) and not is_intrinsic(v):
        for k, x in v.items():
            yield from _walk(x, (*path, k))
    elif isinstance(v, list) and not any(
        isinstance(x, dict) and not is_intrinsic(x) for x in v
    ):
        yield path, v
    elif isinstance(v, list):
        for x in v:
            yield from _walk(x, path)
    else:
        yield path, v


def _declared(cfn_type: str, path: tuple[str, ...]) -> tuple[str, str] | None:
    for n in range(len(path), 0, -1):
        key = (cfn_type, "/".join(path[:n]))
        if key in NOT_APPLICABLE:
            return key
    return None


def check(
    tr: Translator, stack: str = "", used: set[tuple[str, str]] | None = None
) -> tuple[list[Missing], set[tuple[str, str]]]:
    """Missing values across the tree, and the NOT_APPLICABLE keys that matched."""
    used = set() if used is None else used
    missing: list[Missing] = []
    for lid, plan in tr.plans.items():
        text = tr.emitted.get(lid)
        if text is None:
            continue
        props = dict(plan.props)
        if plan.kind == "module":
            props.pop("TemplateURL", None)
        for path, value in _walk(props, ()):
            for desc, forms in _needles(tr, value):
                if any(f in text for f in forms):
                    continue
                key = _declared(plan.cfn_type, path)
                if key is not None:
                    used.add(key)
                    continue
                missing.append(Missing(stack, lid, plan.cfn_type, "/".join(path), desc))
    for lid, plan in tr.plans.items():
        if plan.kind == "module":
            child_stack = f"{stack}/{lid}" if stack else lid
            more, _ = check(tr.children[plan.name], child_stack, used)
            missing.extend(more)
    return missing, used


def stale_declarations(used: set[tuple[str, str]]) -> list[tuple[str, str]]:
    return sorted(set(NOT_APPLICABLE) - used)
