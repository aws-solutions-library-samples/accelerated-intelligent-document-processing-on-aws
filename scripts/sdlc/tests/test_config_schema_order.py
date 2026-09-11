# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The UI ConfigSchema's ``order`` values must be unique among siblings.

``ConfigBuilder.tsx`` sorts each property group with
``sort((a, b) => a.order - b.order)``. ``Array.prototype.sort`` is stable, so two
siblings sharing an ``order`` fall back to whatever order the object's keys
happened to be in — which means the panel silently reorders when an unrelated
edit moves a key, and two settings the schema author intended to sit apart end up
adjacent with no signal that anything is wrong.

This has actually happened twice: ``extraction.coercion``/``validation`` were
added on slots already taken by ``model``/``model_lambda_hook_arn``, and
``extraction.image`` shared slot 15 with the 1S-TopK prompt. Both were found by
reading the rendered order by hand. Fractional slots (``4.1``) are the intended
escape hatch — ``getOrderWeight`` parses with ``parseFloat`` — so there is never a
reason to reuse an integer.
"""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

_TEMPLATES = [
    "patterns/unified/template.yaml",
    "template.yaml",
]


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


class _CfnSafeLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form intrinsics.

    The schema block itself is plain data, but the surrounding template is full of
    ``!Ref`` / ``!Sub`` / ``!GetAtt``, which SafeLoader rejects outright.
    """


def _any_tag(loader: Any, tag_suffix: str, node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnSafeLoader.add_multi_constructor("!", _any_tag)


def _iter_property_groups(node: Any, path: str = ""):
    """Yield ``(path, {name: order})`` for every ``properties`` mapping."""
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict):
        orders: dict[str, Any] = {}
        for name, sub in props.items():
            if isinstance(sub, dict) and "order" in sub:
                orders[name] = sub["order"]
        if orders:
            yield (path or "<root>", orders)
    for key, sub in node.items():
        # `properties` is scaffolding, not a name a reader would use — report
        # `extraction.coercion`, not `properties.extraction.properties.coercion`.
        child_path = path if key == "properties" else f"{path}.{key}" if path else key
        if isinstance(sub, dict):
            yield from _iter_property_groups(sub, child_path)
        elif isinstance(sub, list):
            for idx, item in enumerate(sub):
                if isinstance(item, dict):
                    yield from _iter_property_groups(item, f"{child_path}[{idx}]")


def _schemas():
    """Every ``Schema:`` block found in the templates, with its source label."""
    found = []
    for rel in _TEMPLATES:
        path = _repo_root() / rel
        if not path.exists():  # pragma: no cover
            continue
        # Strip the AWS::Include-style transform lines the loader still trips on.
        doc = yaml.load(path.read_text(), Loader=_CfnSafeLoader)
        for res_name, res in (doc.get("Resources") or {}).items():
            if not isinstance(res, dict):
                continue
            schema = (res.get("Properties") or {}).get("Schema")
            if isinstance(schema, dict):
                found.append((f"{rel}::{res_name}", schema))
    return found


def test_at_least_one_schema_is_discovered():
    """Guard the guard: a loader change must not silently make this vacuous."""
    schemas = _schemas()
    assert schemas, "no ConfigSchema block found — this test would pass vacuously"
    labels = [label for label, _ in schemas]
    assert any("patterns/unified/template.yaml" in label for label in labels), labels


def test_sibling_order_values_are_unique():
    problems: list[str] = []
    for label, schema in _schemas():
        for path, orders in _iter_property_groups(schema):
            seen: dict[float, list[str]] = {}
            for name, raw in orders.items():
                try:
                    weight = float(raw)
                except (TypeError, ValueError):
                    problems.append(
                        f"{label} {path}.{name}: order {raw!r} is not a number, so "
                        f"parseFloat yields NaN and the field sorts last"
                    )
                    continue
                seen.setdefault(weight, []).append(name)
            for weight, names in seen.items():
                if len(names) > 1:
                    problems.append(
                        f"{label} {path}: order {weight:g} is shared by "
                        f"{sorted(names)} — the panel order is then whatever the "
                        f"key order happens to be. Use a fractional slot "
                        f"(e.g. {weight:g}1) instead."
                    )
    assert not problems, "\n".join(problems)


def test_the_extraction_group_is_covered():
    """The group both real collisions were in, named explicitly.

    A structural sweep can drift away from the thing it was written for; this
    fails loudly if `extraction` stops being discovered at all.
    """
    for label, schema in _schemas():
        if "patterns/unified" not in label:
            continue
        groups = dict(_iter_property_groups(schema))
        extraction = groups.get("extraction")
        assert extraction, f"{label}: extraction property group not discovered"
        for expected in ("coercion", "validation", "image", "model"):
            assert expected in extraction, (expected, sorted(extraction))
        return
    pytest.fail("patterns/unified/template.yaml was not scanned")


def test_fractional_slots_are_actually_supported_by_the_ui():
    """The escape hatch this test recommends has to exist in the sorter."""
    builder = (
        _repo_root() / "src/ui/src/components/configuration-layout/ConfigBuilder.tsx"
    )
    if not builder.exists():  # pragma: no cover
        pytest.skip("UI source not present")
    source = builder.read_text()
    assert re.search(r"function getOrderWeight", source), (
        "getOrderWeight is gone; re-check how `order` is parsed before "
        "recommending fractional slots"
    )
    assert "parseFloat" in source, (
        "getOrderWeight no longer uses parseFloat, so fractional order slots "
        "would truncate or fail — this test's advice would be wrong"
    )
