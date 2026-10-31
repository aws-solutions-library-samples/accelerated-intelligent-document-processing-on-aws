# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI config schema is template data, so it has to obey template rules.

`UpdateSchemaConfig`'s `Schema` property in `patterns/unified/template.yaml` is
the schema that drives the configuration panel. It is authored as free-form YAML
inside a CloudFormation resource property, and that is the trap: **`cfn-lint`
does not look inside it**, because to cfn-lint it is an opaque value, so a
construct that is fine as YAML and fine as JSON can still be rejected by
CloudFormation itself at deploy time.

⚠️ **A literal `null` is the live case.** CloudFormation refuses `'null' values
are not allowed in templates` anywhere in a template, including deep inside a
property value nothing else validates. `showWhen: [{field: "granularity",
values: ["", null]}]` — a perfectly ordinary way to spell "empty or absent" —
passed `cfn-lint`, passed every unit test, and failed the nested pattern stack's
CREATE with that message. The only gate that caught it was a real deploy, which
is the slowest and most expensive signal in the repository and is no longer a
merge gate (see CLAUDE.md on `integration_tests`).

So the check lives here instead, where it costs nothing: walk the schema and
refuse a `null` anywhere in it. The remedy at the call site is to express the
condition some other way — for the `showWhen` case, `notValues` says "has not
been set to one of these" without needing a `null` to compare against, and is
also the *correct* semantics, since an absent value does not equal `""` either.
"""

from __future__ import annotations

import pathlib
from typing import Any, Iterator, List

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
UNIFIED_TEMPLATE = REPO / "patterns" / "unified" / "template.yaml"


class _Intrinsic:
    """Stand-in for a CloudFormation short-form intrinsic (``!If``, ``!Ref``, …).

    ⚠️ **Not ``None``.** The obvious loader for a template full of `!`-tags
    returns ``None`` for each one, and that is exactly wrong for this file: the
    `model` enums in this template carry `!If` entries, so a null-hunting walk
    over a ``None``-producing parse reports two findings that are intrinsics
    rather than nulls — a test that fails on correct templates and would be
    "fixed" by deleting the check it exists to make.
    """

    __slots__ = ("tag",)

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return f"<!{self.tag}>"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form intrinsic tags."""


def _intrinsic_tag(loader: Any, tag_suffix: str, node: Any) -> _Intrinsic:  # noqa: ARG001
    return _Intrinsic(tag_suffix)


_CfnLoader.add_multi_constructor("!", _intrinsic_tag)


def _config_schemas(template: Any) -> Iterator[tuple[str, Any]]:
    """Every ``Schema`` property of a config-schema custom resource."""
    resources = (template or {}).get("Resources") or {}
    for name, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        schema = (resource.get("Properties") or {}).get("Schema")
        if schema is not None:
            yield name, schema


def _nulls(node: Any, path: str = "") -> List[str]:
    """Paths at which ``node`` holds a literal null."""
    found: List[str] = []
    if node is None:
        found.append(path or "<root>")
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(_nulls(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_nulls(value, f"{path}/{index}"))
    return found


@pytest.fixture(scope="module")
def unified_template() -> Any:
    if not UNIFIED_TEMPLATE.exists():  # pragma: no cover - packaged install
        pytest.skip(f"{UNIFIED_TEMPLATE} not present")
    return yaml.load(UNIFIED_TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.mark.unit
def test_an_intrinsic_is_not_mistaken_for_a_null(unified_template):
    """The loader's own guard. The `model` enums in this template carry `!If`
    entries, so a loader that produced ``None`` for an intrinsic would make the
    null check fail on a correct template — and the obvious next move is to
    weaken the check rather than the loader."""
    intrinsics = [
        path
        for _, schema in _config_schemas(unified_template)
        for path in _intrinsics(schema)
    ]
    assert intrinsics, (
        "No CloudFormation intrinsic found inside any config schema. The null "
        "check below is only trustworthy while this holds, because it is what "
        "proves intrinsics and nulls are being told apart. If the `!If` model "
        "entries were removed, delete this test deliberately rather than "
        "leaving it asserting nothing."
    )


def _intrinsics(node: Any, path: str = "") -> List[str]:
    """Paths at which ``node`` holds a CloudFormation intrinsic."""
    found: List[str] = []
    if isinstance(node, _Intrinsic):
        found.append(path or "<root>")
    elif isinstance(node, dict):
        for key, value in node.items():
            found.extend(_intrinsics(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_intrinsics(value, f"{path}/{index}"))
    return found


@pytest.mark.unit
def test_a_config_schema_is_present_to_check(unified_template):
    """Non-vacuity: if the resource is renamed, this file must fail rather than
    quietly checking nothing."""
    assert list(_config_schemas(unified_template)), (
        "No resource in patterns/unified/template.yaml has a Properties.Schema. "
        "If the config-schema custom resource was renamed or restructured, point "
        "_config_schemas at the new shape — a check that finds nothing to examine "
        "reports success for the wrong reason."
    )


@pytest.mark.unit
def test_no_config_schema_holds_a_literal_null(unified_template):
    """CloudFormation refuses a null anywhere in a template, and cfn-lint cannot
    see inside this property to tell you so."""
    failures = []
    for name, schema in _config_schemas(unified_template):
        for path in _nulls(schema):
            failures.append(f"{name}.Properties.Schema{path}")

    assert not failures, (
        "A literal null in a config schema deploys as "
        "\"'null' values are not allowed in templates\" and fails the nested "
        "stack's CREATE. cfn-lint does not look inside this property, so a real "
        "deploy is otherwise the first thing that catches it.\n"
        "Null at: " + ", ".join(failures) + "\n"
        "For a showWhen condition meaning 'has not been set', use "
        'notValues: [<the set values>] rather than values: ["", null] — it '
        "needs no null AND is the correct semantics, since an absent value does "
        'not equal "" either.'
    )


@pytest.mark.unit
def test_every_show_when_condition_is_well_formed(unified_template):
    """A `showWhen` entry with no `field` is ignored at render time rather than
    hiding the field, so a typo there is silent in the UI. The schema is
    reviewed; the rendered form is not."""
    failures = []
    for name, schema in _config_schemas(unified_template):
        for path, condition in _show_when_conditions(schema):
            if not isinstance(condition, dict):
                failures.append(f"{name}{path}: not an object ({condition!r})")
                continue
            if not isinstance(condition.get("field"), str) or not condition["field"]:
                failures.append(f"{name}{path}: no 'field' ({condition!r})")
                continue
            if not any(
                key in condition for key in ("value", "values", "not", "notValues")
            ):
                failures.append(
                    f"{name}{path}: names a field but no value to compare it "
                    f"against ({condition!r})"
                )

    assert not failures, "Malformed showWhen condition(s):\n" + "\n".join(failures)


def _show_when_conditions(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Every entry of every ``showWhen`` list in the schema."""
    if isinstance(node, dict):
        show_when = node.get("showWhen")
        if isinstance(show_when, list):
            for index, condition in enumerate(show_when):
                yield f"{path}/showWhen/{index}", condition
        elif show_when is not None:
            yield f"{path}/showWhen", show_when
        for key, value in node.items():
            if key == "showWhen":
                continue
            yield from _show_when_conditions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _show_when_conditions(value, f"{path}/{index}")
