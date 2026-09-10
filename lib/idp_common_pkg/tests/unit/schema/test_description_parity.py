# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#836: every `description` a class author writes must reach the tool schema
the Advanced (agentic) extraction agent sees.

That schema is `model_json_schema()` of the Pydantic model generated from the
class schema, not the class schema itself. Pydantic sources an object's
`description` from the class docstring, and datamodel-code-generator only emits
docstrings when asked (`use_schema_description=True`), so before the fix the
root description was dropped on EVERY class and `$defs` group descriptions with
it — four of the six lending classes then carried no description at all. The
prose copy of the schema in the task prompt masked this; it stops being masked
the moment that prose is reduced (#710/#774).
"""

from __future__ import annotations

import glob
import os
from typing import Any

import pytest
import yaml

from idp_common.schema.pydantic_generator import (
    create_pydantic_model_from_json_schema,
    nullable_leaves_for_transport,
)

pytestmark = pytest.mark.unit

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 5))
PRESET_CONFIGS = sorted(
    glob.glob(os.path.join(REPO, "config_library", "unified", "*", "config.yaml"))
)


def _referenced_defs(schema: dict) -> set[str]:
    """Names of `$defs` entries some `$ref` in the schema actually points at."""
    import json
    import re

    return set(re.findall(r'"\$ref": "#/\$defs/([^"]+)"', json.dumps(schema)))


def descriptions(
    node: Any, path: str = "$", *, reachable_defs: set[str] | None = None
) -> dict[str, str]:
    """Every `description` string in a JSON Schema, keyed by its location.

    An unreferenced `$defs` entry is dead in the class schema too (nothing can
    reach it), so its descriptions are excluded when ``reachable_defs`` is given;
    the generator rightly does not emit a model for it.
    """
    out: dict[str, str] = {}
    if isinstance(node, dict):
        if isinstance(node.get("description"), str):
            out[path] = node["description"]
        for key in ("properties", "$defs", "definitions"):
            for name, sub in (node.get(key) or {}).items():
                if (
                    key != "properties"
                    and reachable_defs is not None
                    and name not in reachable_defs
                ):
                    continue
                out.update(
                    descriptions(
                        sub, f"{path}.{key}[{name}]", reachable_defs=reachable_defs
                    )
                )
        items = node.get("items")
        # Known remaining gap: a description on the ITEMS of a scalar array
        # (`items: {type: string, description: ...}`) has no home in `list[str]`
        # and is dropped; the array field's own description survives. Object
        # items ($ref / inline object) are checked.
        if isinstance(items, dict) and not (
            isinstance(items.get("type"), str) and items["type"] in _SCALARS
        ):
            out.update(
                descriptions(items, f"{path}.items", reachable_defs=reachable_defs)
            )
    return out


_SCALARS = {"string", "number", "integer", "boolean"}


def _wire_schema(class_schema: dict) -> dict:
    """What the extraction service hands the agent (ExtractionService._transport_model)."""
    model = create_pydantic_model_from_json_schema(
        schema=nullable_leaves_for_transport(class_schema),
        class_label=class_schema.get("x-aws-idp-document-type") or "cls",
        clean_schema=False,
    )
    return model.model_json_schema()


def _classes():
    for path in PRESET_CONFIGS:
        cfg = yaml.safe_load(open(path)) or {}
        for cls in cfg.get("classes") or []:
            if isinstance(cls, dict) and cls.get("properties"):
                label = cls.get("x-aws-idp-document-type") or cls.get("$id")
                yield pytest.param(
                    cls, id=f"{os.path.basename(os.path.dirname(path))}:{label}"
                )


@pytest.mark.parametrize("class_schema", list(_classes()))
def test_every_shipped_class_description_reaches_the_wire_schema(class_schema):
    src = descriptions(class_schema, reachable_defs=_referenced_defs(class_schema))
    wire_values = set(descriptions(_wire_schema(class_schema)).values())
    lost = {p: d for p, d in src.items() if d not in wire_values}
    assert not lost, (
        f"{len(lost)}/{len(src)} descriptions missing from the tool schema: {sorted(lost)}"
    )


def test_root_and_nested_group_descriptions_survive_synthetic():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "paystub",
        "type": "object",
        "description": "BOUNDARY: one pay statement per employee per period.",
        "properties": {
            "Employee": {"$ref": "#/$defs/Person", "description": "Who was paid."},
            "Address": {
                "type": "object",
                "description": "Inline group: mailing address.",
                "properties": {"City": {"type": "string", "description": "City name."}},
            },
            "Lines": {
                "type": "array",
                "description": "Earnings lines.",
                "items": {"$ref": "#/$defs/Line"},
            },
        },
        "required": ["Employee"],
        "$defs": {
            "Person": {
                "type": "object",
                "description": "A named person.",
                "properties": {"Name": {"type": "string", "description": "Full name."}},
            },
            "Line": {
                "type": "object",
                "description": "One earnings line.",
                "properties": {
                    "Amount": {"type": "number", "description": "Line amount."}
                },
            },
        },
    }
    wire = _wire_schema(schema)
    wire_values = set(descriptions(wire).values())
    for expected in descriptions(schema).values():
        assert expected in wire_values, expected
    # The root description specifically lands on the root object.
    assert (
        wire.get("description")
        == "BOUNDARY: one pay statement per employee per period."
    )


def test_root_description_survives_the_validation_subclass_path():
    """Advanced constraints (here `pattern`) route the model through the
    ModelWithValidation subclass; a subclass has no __doc__ unless copied."""
    schema = {
        "type": "object",
        "description": "Root guidance for a constrained class.",
        "properties": {"Code": {"type": "string", "pattern": "^[A-Z]{3}$"}},
    }
    model = create_pydantic_model_from_json_schema(schema, "constrained")
    assert (
        model.model_json_schema().get("description")
        == "Root guidance for a constrained class."
    )


def test_class_without_descriptions_gets_none_invented():
    schema = {"type": "object", "properties": {"A": {"type": "string"}}}
    wire = create_pydantic_model_from_json_schema(schema, "bare").model_json_schema()
    assert "description" not in wire
