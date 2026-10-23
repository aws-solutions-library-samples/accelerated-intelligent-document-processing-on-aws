# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nested-stack parameters whose value, not just whose presence, decides a feature.

``nested_parameter_defaults.py`` reports a defaulted nested parameter the parent does
not pass at all. That is a presence check: it is satisfied by any value, including
``""`` for the MLflow logger ARN, which reproduces the defect exactly. So for the two
parameters that check found unpassed, this module asserts what the parent passes.

* ``MLflowLoggerFunctionArn`` (api-resolvers) must be the pattern stack's
  ``MLflowLoggerFunctionArn`` output, behind a condition that is true exactly when the
  pattern stack creates that output. Passing it unconditionally fails the deploy when
  MLflow is off (the output does not exist); passing a constant leaves the feature
  inert. Issue 1553.
"""

from __future__ import annotations

from typing import Any

import gate_premises
import pytest

PARENT = gate_premises.PARENT_TEMPLATE
PATTERN = "patterns/unified/template.yaml"


def _template(path: str) -> dict:
    return gate_premises.load_template(path)


def _ref(node: Any) -> str | None:
    """The parameter name behind ``Ref``/``!Ref``, else None."""
    if isinstance(node, dict) and len(node) == 1:
        key, value = next(iter(node.items()))
        if key in ("Ref", "Fn::Ref") and isinstance(value, str):
            return value
    return None


def _get_att(node: Any) -> tuple[str, str] | None:
    """``(resource, attribute)`` for ``Fn::GetAtt`` in either spelling, else None."""
    if not (isinstance(node, dict) and len(node) == 1 and "Fn::GetAtt" in node):
        return None
    value = node["Fn::GetAtt"]
    if isinstance(value, str):
        resource, _, attribute = value.partition(".")
        return resource, attribute
    if isinstance(value, list) and len(value) == 2:
        return value[0], value[1]
    return None


def _normalise(node: Any) -> Any:
    """``Ref`` and ``Fn::Ref`` are the same intrinsic; compare them as one."""
    if isinstance(node, dict):
        return {
            ("Ref" if key == "Fn::Ref" else key): _normalise(value)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_normalise(item) for item in node]
    return node


def _substitute_refs(node: Any, mapping: dict[str, Any]) -> Any:
    """Rewrite a nested template's ``Ref <param>`` into what the parent passes."""
    name = _ref(node)
    if name is not None:
        if name not in mapping:
            pytest.fail(
                f"the pattern stack's MLflow condition reads {name}, which {PARENT} "
                "does not pass to PATTERNSTACK, so the two conditions cannot be shown "
                "to agree"
            )
        return mapping[name]
    if isinstance(node, dict):
        return {key: _substitute_refs(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_substitute_refs(item, mapping) for item in node]
    return node


def _stack_parameters(parent: dict, stack: str) -> dict:
    return parent["Resources"][stack]["Properties"].get("Parameters") or {}


@pytest.mark.unit
def test_mlflow_logger_arn_is_the_pattern_output_exactly_when_it_exists() -> None:
    parent = _template(PARENT)
    pattern = _template(PATTERN)

    passed = _stack_parameters(parent, "APIRESOLVERSTACK").get(
        "MLflowLoggerFunctionArn"
    )
    assert passed is not None, (
        f"{PARENT} does not pass MLflowLoggerFunctionArn to APIRESOLVERSTACK, so the "
        "test-results resolver never invokes the MLflow logger (issue 1553)"
    )
    branches = passed.get("Fn::If") if isinstance(passed, dict) else None
    assert isinstance(branches, list) and len(branches) == 3, (
        "MLflowLoggerFunctionArn must be an Fn::If: the pattern stack's output exists "
        f"only when MLflow is enabled, so an unconditional value is either inert or "
        f"a deploy failure. Got {passed!r}"
    )
    condition, when_true, when_false = branches
    assert _get_att(when_true) == ("PATTERNSTACK", "Outputs.MLflowLoggerFunctionArn"), (
        "the enabled branch must be PATTERNSTACK.Outputs.MLflowLoggerFunctionArn, got "
        f"{when_true!r}"
    )
    assert when_false == "", f"the disabled branch must be '', got {when_false!r}"

    output = pattern["Outputs"]["MLflowLoggerFunctionArn"]
    pattern_condition = pattern["Conditions"][output["Condition"]]
    pattern_inputs = _stack_parameters(parent, "PATTERNSTACK")
    expected = _normalise(_substitute_refs(pattern_condition, pattern_inputs))
    actual = _normalise(parent["Conditions"][condition])
    assert actual == expected, (
        f"{PARENT} gates MLflowLoggerFunctionArn on {condition} = {actual!r}, but the "
        f"pattern stack creates the output under {output['Condition']}, which with "
        f"the values {PARENT} passes it is {expected!r}. When the two disagree the "
        "GetAtt names an output that does not exist, or the ARN is withheld while "
        "the logger exists."
    )
