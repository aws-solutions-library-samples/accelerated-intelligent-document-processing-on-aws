# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The dispatcher's field->function map fits its SSM parameter, worst case.

``HttpApiFieldFunctionMapParam`` in ``nested/api-resolvers/template.yaml`` is an
Advanced-tier SSM String parameter, whose value limit is 8,192 bytes. Its value
is a ``!Sub`` of a JSON object, one key per resolver Lambda, so its deployed size
depends on what the placeholders resolve to. This test resolves every
placeholder to the longest value it can take:

* ``${LogicalId}`` of an in-stack function resolves (``Ref``) to its function
  name, which Lambda bounds at 64 characters;
* a variable whose name ends in ``Arn``, or one defined in the ``!Sub`` list
  form's variable map, resolves to a full Lambda ARN in the longest partition
  (``aws-us-gov``) and region name used here, with a 64-character name.

It then checks both the literal string SSM stores and its minified JSON form
against the limit. A new resolver Lambda adds one key; extra fields of an
existing Lambda go through ``FIELD_ALIASES`` in the dispatcher instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
NESTED_TEMPLATE = REPO_ROOT / "nested/api-resolvers/template.yaml"
PARAM_LOGICAL_ID = "HttpApiFieldFunctionMapParam"

SSM_ADVANCED_VALUE_LIMIT_BYTES = 8192
WORST_CASE_NAME = "f" * 64
WORST_CASE_ARN = (
    "arn:aws-us-gov:lambda:us-gov-west-1:123456789012:function:" + WORST_CASE_NAME
)

_PLACEHOLDER = re.compile(r"\$\{([^}!]+)\}")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load_template(path: Path = NESTED_TEMPLATE) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader) or {}


def _sub_body_and_variables(value: Any) -> tuple[str, dict]:
    """The ``!Sub`` body and its variable map, for scalar and list forms."""
    assert isinstance(value, dict) and "Fn::Sub" in value, (
        f"{PARAM_LOGICAL_ID}.Properties.Value is no longer a !Sub; update this gate"
    )
    sub = value["Fn::Sub"]
    if isinstance(sub, str):
        return sub, {}
    assert isinstance(sub, list) and sub and isinstance(sub[0], str), sub
    variables = sub[1] if len(sub) > 1 and isinstance(sub[1], dict) else {}
    return sub[0], variables


def resolve_worst_case(template: dict) -> tuple[str, list[str]]:
    """The map value with every placeholder at its longest; plus unknown names."""
    resources = template.get("Resources", {}) or {}
    parameters = template.get("Parameters", {}) or {}
    body, variables = _sub_body_and_variables(
        resources[PARAM_LOGICAL_ID]["Properties"]["Value"]
    )
    unknown: list[str] = []

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        base = name.split(".", 1)[0]
        if name in variables or name.endswith("Arn"):
            if (
                name not in variables
                and base not in parameters
                and base not in resources
            ):
                unknown.append(name)
            return WORST_CASE_ARN
        if base in resources:
            return WORST_CASE_NAME
        unknown.append(name)
        return WORST_CASE_NAME

    return _PLACEHOLDER.sub(_replace, body), unknown


@pytest.fixture(scope="module")
def resolved() -> tuple[str, list[str]]:
    return resolve_worst_case(_load_template())


@pytest.mark.unit
class TestHttpApiFieldMapSize:
    def test_every_placeholder_names_something_the_template_defines(self, resolved):
        _, unknown = resolved
        assert not unknown, (
            f"{PARAM_LOGICAL_ID} references names the nested template does not "
            f"define: {unknown}"
        )

    def test_resolved_value_is_valid_json_of_strings(self, resolved):
        value, _ = resolved
        mapping = json.loads(value)
        assert isinstance(mapping, dict) and mapping
        assert all(isinstance(v, str) for v in mapping.values())

    def test_minified_worst_case_fits_the_advanced_tier_limit(self, resolved):
        value, _ = resolved
        minified = json.dumps(json.loads(value), separators=(",", ":")).encode()
        print(
            f"field map minified worst case: {len(minified)} of "
            f"{SSM_ADVANCED_VALUE_LIMIT_BYTES} bytes "
            f"(headroom {SSM_ADVANCED_VALUE_LIMIT_BYTES - len(minified)})"
        )
        assert len(minified) <= SSM_ADVANCED_VALUE_LIMIT_BYTES

    def test_stored_worst_case_fits_the_advanced_tier_limit(self, resolved):
        # SSM stores the literal string, whitespace included.
        value, _ = resolved
        stored = value.encode("utf-8")
        print(
            f"field map stored worst case: {len(stored)} of "
            f"{SSM_ADVANCED_VALUE_LIMIT_BYTES} bytes "
            f"(headroom {SSM_ADVANCED_VALUE_LIMIT_BYTES - len(stored)})"
        )
        assert len(stored) <= SSM_ADVANCED_VALUE_LIMIT_BYTES


@pytest.mark.unit
class TestResolver:
    """The resolver itself, on synthetic templates."""

    @staticmethod
    def _template(value: Any, resources=None, parameters=None) -> dict:
        res = {PARAM_LOGICAL_ID: {"Properties": {"Value": value}}}
        res.update(resources or {})
        return {"Resources": res, "Parameters": parameters or {}}

    def test_scalar_form_resolves_names_and_arns(self):
        tpl = self._template(
            {"Fn::Sub": '{"a": "${Fn1}", "b": "${OtherArn}"}'},
            resources={"Fn1": {}},
            parameters={"OtherArn": {}},
        )
        value, unknown = resolve_worst_case(tpl)
        assert unknown == []
        assert json.loads(value) == {"a": WORST_CASE_NAME, "b": WORST_CASE_ARN}

    def test_list_form_variables_resolve_to_arns(self):
        tpl = self._template({"Fn::Sub": ['{"a": "${Var}"}', {"Var": "x"}]})
        value, unknown = resolve_worst_case(tpl)
        assert unknown == [] and json.loads(value) == {"a": WORST_CASE_ARN}

    def test_undefined_names_are_reported(self):
        tpl = self._template({"Fn::Sub": '{"a": "${Missing}", "b": "${GoneArn}"}'})
        _, unknown = resolve_worst_case(tpl)
        assert unknown == ["Missing", "GoneArn"]
