# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``ManageApiGatewayAccountSettings`` decides who sets the Region's API Gateway role.

``AWS::ApiGateway::Account`` is one setting per account and Region: the CloudWatch
role API Gateway uses to write REST API stage logs. Every deployment that declares
it points it at its own role, so two deployments in one Region overwrite each
other, and a Terraform deployment shows the other's write as drift on every plan.
The parameter lets one deployment per Region own the setting and every other one
leave it alone.

What is asserted, by evaluating the nested template's conditions and ``Fn::If``
branches for each combination rather than by reading the YAML:

* with the parameter ``true`` (the default) the stack is what it was before the
  parameter existed: the role and the account setting are created whenever
  access logging is on, and both log groups are ordered after the setting;
* with it ``false`` neither is created, while access and execution logging on
  the stage are still configured;
* in every combination, no surviving resource names a resource that the same
  combination drops — the deployment-breaking shape a ``DependsOn`` on the
  account setting would have (CloudFormation refuses ``DependsOn`` naming a
  resource whose condition is false);
* the parent declares the parameter defaulting to ``true`` and passes it through,
  and the headless transform, which removes the API stack, removes it too.
"""

from __future__ import annotations

import copy
import itertools
import re

import gate_premises
import pytest

pytestmark = pytest.mark.unit

PARAMETER = "ManageApiGatewayAccountSettings"
NESTED = gate_premises.load_template("nested/api-resolvers/template.yaml")
PARENT = gate_premises.load_template("template.yaml")
ACCOUNT = "ApiGatewayAccount"
ROLE = "ApiGatewayCloudWatchRole"
LOG_GROUPS = ("HttpApiAccessLogGroup", "HttpApiExecutionLogGroup")
STAGE = "HttpApiStage"
PSEUDO = {
    "AWS::Region": "us-east-1",
    "AWS::Partition": "aws",
    "AWS::AccountId": "123456789012",
    "AWS::URLSuffix": "amazonaws.com",
    "AWS::StackName": "stack",
    "AWS::StackId": "stack-id",
}
_NO_VALUE = object()


def _parameter_values(**overrides: str) -> dict[str, object]:
    """Every nested parameter, at its default or a placeholder, plus overrides."""
    values: dict[str, object] = {}
    for name, spec in NESTED["Parameters"].items():
        default = spec.get("Default", "placeholder")
        if spec.get("Type") == "CommaDelimitedList" or "List<" in str(spec.get("Type")):
            default = str(default).split(",")
        values[name] = default
    values.update(overrides)
    return values


class _Evaluator:
    def __init__(self, params: dict[str, object]) -> None:
        self.params = params
        self.cache: dict[str, bool] = {}

    def condition(self, name: str) -> bool:
        if name not in self.cache:
            self.cache[name] = bool(self.value(NESTED["Conditions"][name]))
        return self.cache[name]

    def value(self, node: object) -> object:
        if isinstance(node, dict) and len(node) == 1:
            key, arg = next(iter(node.items()))
            if key in ("Ref", "Fn::Ref"):
                if arg == "AWS::NoValue":
                    return _NO_VALUE
                if arg in PSEUDO:
                    return PSEUDO[arg]
                return self.params.get(arg, f"<ref {arg}>")
            if key == "Fn::Equals":
                left, right = (self.value(a) for a in arg)
                return str(left) == str(right)
            if key == "Fn::Not":
                return not self.value(arg[0])
            if key == "Fn::And":
                return all(self.value(a) for a in arg)
            if key == "Fn::Or":
                return any(self.value(a) for a in arg)
            if key == "Fn::Condition":
                return self.condition(arg)
            if key == "Fn::Join":
                sep, items = arg
                items = self.value(items)
                return sep.join(str(self.value(i)) for i in items)
        if isinstance(node, list):
            return [self.value(i) for i in node]
        return node

    def resolve(self, node: object) -> object:
        """Select every ``Fn::If`` branch and drop ``AWS::NoValue``."""
        if isinstance(node, dict):
            if len(node) == 1 and "Fn::If" in node:
                cond, yes, no = node["Fn::If"]
                return self.resolve(yes if self.condition(cond) else no)
            if node == {"Fn::Ref": "AWS::NoValue"} or node == {"Ref": "AWS::NoValue"}:
                return _NO_VALUE
            out = {}
            for key, val in node.items():
                resolved = self.resolve(val)
                if resolved is not _NO_VALUE:
                    out[key] = resolved
            return out
        if isinstance(node, list):
            return [r for r in (self.resolve(i) for i in node) if r is not _NO_VALUE]
        return node

    def resources(self) -> dict[str, dict]:
        """The resources this combination creates, with branches resolved."""
        created: dict[str, dict] = {}
        for name, body in NESTED["Resources"].items():
            if "Condition" in body and not self.condition(body["Condition"]):
                continue
            resolved = self.resolve(copy.deepcopy(body))
            assert isinstance(resolved, dict), name
            created[name] = resolved
        return created


def _names_referenced(node: object) -> set[str]:
    """Logical ids a resolved subtree names by Ref, GetAtt or Sub."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, val in node.items():
            if key in ("Ref", "Fn::Ref") and isinstance(val, str):
                found.add(val)
            elif key == "Fn::GetAtt":
                found.add((val.split(".")[0] if isinstance(val, str) else val[0]))
            elif key == "Fn::Sub":
                text = val if isinstance(val, str) else val[0]
                found |= {m.split(".")[0] for m in re.findall(r"\$\{([^}!]+)\}", text)}
                if not isinstance(val, str):
                    found |= _names_referenced(val[1])
            else:
                found |= _names_referenced(val)
    elif isinstance(node, list):
        for item in node:
            found |= _names_referenced(item)
    return found


def _depends_on(body: dict) -> set[str]:
    dep = body.get("DependsOn", [])
    return {dep} if isinstance(dep, str) else set(dep)


COMBINATIONS = list(
    itertools.product(("true", "false"), ("DEBUG", "INFO", "WARN", "ERROR"))
)


def _resources(manage: str, level: str) -> dict[str, dict]:
    return _Evaluator(
        _parameter_values(**{PARAMETER: manage, "LogLevel": level})
    ).resources()


@pytest.mark.parametrize(("manage", "level"), COMBINATIONS)
def test_the_account_setting_and_its_role_exist_only_when_managed_and_logging(
    manage: str, level: str
) -> None:
    resources = _resources(manage, level)
    expected = manage == "true" and level in ("DEBUG", "INFO")
    assert (ACCOUNT in resources) is expected
    assert (ROLE in resources) is expected


@pytest.mark.parametrize(("manage", "level"), COMBINATIONS)
def test_stage_logging_follows_log_level_not_the_parameter(
    manage: str, level: str
) -> None:
    resources = _resources(manage, level)
    logging = level in ("DEBUG", "INFO")
    for group in LOG_GROUPS:
        assert (group in resources) is logging
    stage = resources[STAGE]["Properties"]
    assert ("AccessLogSetting" in stage) is logging
    assert ("MethodSettings" in stage) is logging


@pytest.mark.parametrize("level", ("DEBUG", "INFO"))
def test_a_managing_deployment_orders_both_log_groups_after_the_setting(
    level: str,
) -> None:
    """The stage refuses AccessLogSetting until the Region has a role set."""
    resources = _resources("true", level)
    assert STAGE in resources
    stage_refs = _names_referenced(resources[STAGE])
    assert "HttpApiAccessLogGroup" in stage_refs
    assert "HttpApiExecutionLogGroup" in stage_refs
    for group in LOG_GROUPS:
        body = resources[group]
        assert ACCOUNT in _names_referenced(body) | _depends_on(body), group
        tags = body["Properties"]["Tags"]
        assert isinstance(tags, list), group
        assert all(set(tag) == {"Key", "Value"} for tag in tags), group


@pytest.mark.parametrize(("manage", "level"), COMBINATIONS)
def test_no_resource_names_one_the_same_combination_drops(
    manage: str, level: str
) -> None:
    resources = _resources(manage, level)
    declared = set(NESTED["Resources"])
    for name, body in resources.items():
        named = (_names_referenced(body) | _depends_on(body)) & declared
        dangling = named - set(resources)
        assert not dangling, (
            f"{name} names {sorted(dangling)} ({PARAMETER}={manage}, {level})"
        )


def test_the_logging_role_serves_only_the_account_setting() -> None:
    """Gating the role with the setting is safe only if nothing else uses it."""
    users = {
        name
        for name, body in NESTED["Resources"].items()
        if name != ROLE and ROLE in (_names_referenced(body) | _depends_on(body))
    }
    assert users == {ACCOUNT}


def test_the_parent_declares_the_parameter_defaulting_to_managed_and_passes_it() -> (
    None
):
    spec = PARENT["Parameters"][PARAMETER]
    assert spec["Default"] == "true"
    assert sorted(spec["AllowedValues"]) == ["false", "true"]
    passed = PARENT["Resources"]["APIRESOLVERSTACK"]["Properties"]["Parameters"]
    assert passed[PARAMETER] == {"Fn::Ref": PARAMETER}
    nested = NESTED["Parameters"][PARAMETER]
    assert nested["Default"] == "true"
    assert spec["Type"] == nested["Type"] == "String"
    assert sorted(nested["AllowedValues"]) == ["false", "true"]
    grouped = {
        p
        for group in PARENT["Metadata"]["AWS::CloudFormation::Interface"][
            "ParameterGroups"
        ]
        for p in group["Parameters"]
    }
    assert PARAMETER in grouped


def test_the_headless_transform_drops_the_parameter_with_the_api_stack() -> None:
    from idp_sdk._core.template_transform import HeadlessTemplateTransformer

    out = HeadlessTemplateTransformer().apply_transforms(copy.deepcopy(PARENT))
    assert "APIRESOLVERSTACK" not in out["Resources"]
    assert PARAMETER not in out["Parameters"]
