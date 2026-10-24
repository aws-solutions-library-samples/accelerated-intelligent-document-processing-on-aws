# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two nested-stack parameters whose value, not just whose presence, decides a feature.

``nested_parameter_defaults.py`` reports a defaulted nested parameter the parent does
not pass at all. That is a presence check: it is satisfied by any value, including
``""`` for the MLflow logger ARN, which reproduces the defect exactly. So for the two
parameters that check found unpassed, this module asserts what the parent passes.

* ``MLflowLoggerFunctionArn`` (api-resolvers) must be the pattern stack's
  ``MLflowLoggerFunctionArn`` output, behind a condition that is true exactly when the
  pattern stack creates that output. Passing it unconditionally fails the deploy when
  MLflow is off (the output does not exist); passing a constant leaves the feature
  inert. Issue 1553.
* ``pEmbedModel`` (bedrockkb) must come from a root parameter offering the same
  choices and the same default, so every model the nested stack supports is
  selectable and an existing deployment keeps the model it was created with.
  Issue 1558.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import gate_premises
import pytest

PARENT = gate_premises.PARENT_TEMPLATE
PATTERN = "patterns/unified/template.yaml"
API = "nested/api-resolvers/template.yaml"
KB = "nested/bedrockkb/template.yaml"


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


@pytest.mark.unit
def test_the_pattern_output_is_the_mlflow_logger_function() -> None:
    """The output the parent forwards must be the logger, created under its condition.

    The parent-side test above proves the output is forwarded; it cannot see what the
    output names. Pointing it at another function deploys cleanly and invokes the
    wrong Lambda on every test run.
    """
    pattern = _template(PATTERN)
    output = pattern["Outputs"]["MLflowLoggerFunctionArn"]
    target = _get_att(output.get("Value"))
    assert target is not None and target[1] == "Arn", (
        f"the MLflowLoggerFunctionArn output must be a GetAtt <function>.Arn, got "
        f"{output.get('Value')!r}"
    )
    function = pattern["Resources"].get(target[0]) or {}
    environment = (
        ((function.get("Properties") or {}).get("Environment") or {}).get("Variables")
    ) or {}
    assert "MLFLOW_TRACKING_URI" in environment, (
        f"the output names {target[0]}, which is not the MLflow logger: it has no "
        "MLFLOW_TRACKING_URI in its environment"
    )
    assert function.get("Condition") == output.get("Condition"), (
        f"{target[0]} is created under {function.get('Condition')!r} but the output "
        f"under {output.get('Condition')!r}; the output would name a function that "
        "does not exist, or the function would exist with no output"
    )


@pytest.mark.unit
def test_the_api_stack_hands_the_received_arn_to_the_resolver() -> None:
    """The receiving side: a non-empty ARN must reach the resolver's environment.

    ``nested/api-resolvers`` gates the environment variable and the invoke grant on
    a condition derived from the parameter. Inverting that condition leaves the
    feature inert exactly as the unpassed parameter did, with every parent-side
    assertion still green.
    """
    nested = _template(API)
    expected = _normalise(
        {"Fn::Not": [{"Fn::Equals": [{"Ref": "MLflowLoggerFunctionArn"}, ""]}]}
    )
    uses: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get("MLFLOW_LOGGER_FUNCTION_ARN")
            if value is not None:
                branches = value.get("Fn::If") if isinstance(value, dict) else None
                assert (
                    isinstance(branches, list)
                    and len(branches) == 3
                    and _ref(branches[1]) == "MLflowLoggerFunctionArn"
                    and branches[2] == ""
                ), (
                    "MLFLOW_LOGGER_FUNCTION_ARN must be Fn::If [<condition>, Ref "
                    f"MLflowLoggerFunctionArn, ''], got {value!r}"
                )
                actual = _normalise(nested["Conditions"][branches[0]])
                assert actual == expected, (
                    f"MLFLOW_LOGGER_FUNCTION_ARN is gated on {branches[0]} = "
                    f"{actual!r}; it must be true exactly when the ARN is non-empty"
                )
                uses.append(branches[0])
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(nested.get("Resources") or {})
    assert uses, f"no resource in {API} sets MLFLOW_LOGGER_FUNCTION_ARN"


@pytest.mark.unit
def test_kb_embedding_model_is_selectable_from_the_root_with_the_nested_choices() -> (
    None
):
    parent = _template(PARENT)
    nested = _template(KB)["Parameters"]["pEmbedModel"]

    passed = _stack_parameters(parent, "DOCUMENTKB").get("pEmbedModel")
    root_name = _ref(passed)
    assert root_name is not None, (
        f"{PARENT} must pass pEmbedModel to DOCUMENTKB as a Ref to a root parameter, "
        f"got {passed!r}; without it the nested default decides for every "
        "deployment (issue 1558)"
    )
    root = parent["Parameters"].get(root_name)
    assert isinstance(root, dict), f"{root_name} is not a root parameter"

    assert nested.get("AllowedValues"), "pEmbedModel declares no AllowedValues"
    assert set(root.get("AllowedValues") or []) == set(nested["AllowedValues"]), (
        f"{root_name} offers {root.get('AllowedValues')} but pEmbedModel accepts "
        f"{nested['AllowedValues']}: a value only the root offers is rejected by the "
        "nested stack, and one only the nested stack accepts cannot be chosen"
    )
    assert root.get("Default") == nested.get("Default"), (
        f"{root_name} defaults to {root.get('Default')!r} but pEmbedModel to "
        f"{nested.get('Default')!r}. The nested default is what every existing "
        "deployment was created with, and changing an existing knowledge base's "
        "embedding model is not supported, so the root default must match it."
    )


def _refs(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("Ref", "Fn::Ref") and isinstance(value, str):
                found.add(value)
            elif key == "Fn::Sub":
                text = value[0] if isinstance(value, list) else value
                if isinstance(text, str):
                    found.update(re.findall(r"\$\{([A-Za-z0-9:]+)[.}]", text))
            found |= _refs(value)
    elif isinstance(node, list):
        for item in node:
            found |= _refs(item)
    return found


@pytest.mark.unit
def test_headless_drops_every_root_parameter_only_the_knowledge_base_reads() -> None:
    """A parameter read only by DOCUMENTKB has nothing to configure once it is gone.

    The headless transform removes the knowledge-base stack. A root parameter whose
    only reader was that stack would survive as a deploy-time choice that does
    nothing, so it must be in ``parameters_to_remove``.
    """
    sdk_root = gate_premises.REPO_ROOT / "lib" / "idp_sdk"
    if str(sdk_root) not in sys.path:
        sys.path.insert(0, str(sdk_root))
    from idp_sdk._core.template_transform import HeadlessTemplateTransformer

    template = _template(PARENT)
    transformer = HeadlessTemplateTransformer()
    stripped = transformer.kb_resources
    resources = template["Resources"]
    elsewhere = _refs(
        {
            key: value
            for key, value in template.items()
            if key not in ("Resources", "Metadata", "Parameters")
        }
    ) | _refs({key: value for key, value in resources.items() if key not in stripped})
    only_kb = (
        _refs({key: resources[key] for key in stripped if key in resources})
        & set(template["Parameters"])
    ) - elsewhere
    assert only_kb, (
        "no root parameter is read only by the knowledge-base stack; this test has "
        "lost its subject (pEmbedModel's root parameter should be one)"
    )
    kept = sorted(only_kb - transformer.parameters_to_remove)
    assert not kept, (
        f"{kept} are read only by {sorted(stripped)}, which the headless transform "
        "removes, and are not in parameters_to_remove, so a headless deployment "
        "would offer a choice that configures nothing"
    )
