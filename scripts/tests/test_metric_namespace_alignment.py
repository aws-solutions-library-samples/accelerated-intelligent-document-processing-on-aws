# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression test — every Lambda whose PutMetricData grant is scoped
by ``cloudwatch:namespace = !Ref AWS::StackName`` MUST also set
``METRIC_NAMESPACE: !Ref AWS::StackName`` in its Environment.Variables.

Round-27 review blocker: 4 Lambdas (ChatWithDocumentProcessorFunction,
ChatStreamProcessorFunction, DiscoveryProcessorFunction,
BlueprintOptimizationFunction) had the IAM condition without the env
var, so ``idp_common.metrics.put_metric`` defaulted to the ``GENAIDP``
namespace and every emission silently AccessDenied at runtime. This
test pins the alignment so the class of bug — deploy passes, first
Bedrock call gets `AccessDenied` in a logger.warning — can't reappear
silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_TEMPLATE = REPO_ROOT / "template.yaml"
UNIFIED_TEMPLATE = REPO_ROOT / "patterns" / "unified" / "template.yaml"
# The nested-stack template references StackName as a Parameter (via
# `!Ref StackName`) rather than the intrinsic `AWS::StackName` — the
# invariant is the same but the Ref target differs. Kept as a per-file
# expectation so the test doesn't false-positive across templates.
_STACKNAME_REF_BY_TEMPLATE = {
    MAIN_TEMPLATE: "AWS::StackName",
    UNIFIED_TEMPLATE: "StackName",
}


def _cfn_tag_loader() -> Any:
    """A YAML loader that leaves CFN intrinsics (!Ref, !Sub, …) as
    dict values of the form ``{"<tag>": <value>}`` rather than
    rejecting them."""
    loader = yaml.SafeLoader

    def _make_ctor(tag_name: str):
        def _ctor(_loader: Any, node: Any) -> Any:
            if isinstance(node, yaml.ScalarNode):
                return {tag_name: _loader.construct_scalar(node)}
            if isinstance(node, yaml.SequenceNode):
                return {tag_name: _loader.construct_sequence(node)}
            if isinstance(node, yaml.MappingNode):
                return {tag_name: _loader.construct_mapping(node)}
            return {tag_name: None}

        return _ctor

    for tag in (
        "Ref",
        "Sub",
        "GetAtt",
        "Join",
        "If",
        "Not",
        "Equals",
        "And",
        "Or",
        "Select",
        "Split",
        "Base64",
        "Cidr",
        "FindInMap",
        "GetAZs",
        "ImportValue",
        "Condition",
    ):
        yaml.add_constructor(f"!{tag}", _make_ctor(f"!{tag}"), Loader=loader)
    return loader


def _find_namespace_scoped_lambdas(
    template: Dict[str, Any], stackname_ref: str
) -> List[str]:
    """Return the logical IDs of every Lambda whose PutMetricData grant
    is scoped to ``cloudwatch:namespace = !Ref <stackname_ref>``.

    We look inside each function's inline ``Policies`` for a
    ``Statement`` with Action ``cloudwatch:PutMetricData`` AND a
    Condition ``StringEquals.cloudwatch:namespace`` referencing the
    stack name via CFN Ref. ``stackname_ref`` is ``AWS::StackName`` in
    the root template and the ``StackName`` parameter name in the
    nested-stack templates.
    """
    hits: List[str] = []
    resources = template.get("Resources", {}) or {}
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict):
            continue
        if resource.get("Type") not in (
            "AWS::Serverless::Function",
            "AWS::Lambda::Function",
        ):
            continue
        props = resource.get("Properties", {}) or {}
        for policy in props.get("Policies", []) or []:
            if not isinstance(policy, dict):
                continue
            statements = policy.get("Statement", [])
            if isinstance(statements, dict):
                statements = [statements]
            for stmt in statements or []:
                if not isinstance(stmt, dict):
                    continue
                actions = stmt.get("Action", [])
                if isinstance(actions, str):
                    actions = [actions]
                if "cloudwatch:PutMetricData" not in actions:
                    continue
                condition = stmt.get("Condition", {}) or {}
                se = condition.get("StringEquals", {}) or {}
                for key, value in se.items():
                    if key != "cloudwatch:namespace":
                        continue
                    if isinstance(value, dict) and value.get("!Ref") == stackname_ref:
                        hits.append(logical_id)
    return hits


def _get_env_var(resource: Dict[str, Any], name: str) -> Any:
    """Return the value of ``Environment.Variables[name]`` on a Lambda,
    or ``None`` if the var is not set."""
    props = resource.get("Properties", {}) or {}
    env = props.get("Environment", {}) or {}
    return (env.get("Variables", {}) or {}).get(name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "template_path",
    [MAIN_TEMPLATE, UNIFIED_TEMPLATE],
    ids=lambda p: p.name if p.parent.name != "unified" else "patterns/unified/template.yaml",
)
class TestMetricNamespaceAlignment:
    def test_every_stackname_scoped_putmetricdata_lambda_sets_env(
        self, template_path: Path
    ) -> None:
        """The core invariant: any Lambda that scopes its PutMetricData
        grant to cloudwatch:namespace = !Ref <StackName> MUST also
        set METRIC_NAMESPACE: !Ref <StackName> in its env, so
        idp_common.metrics.put_metric doesn't default to GENAIDP and
        silently AccessDenied. Round-27 blocker regression pin. Runs
        against BOTH the root template AND the nested unified template
        (adversarial-round #3 fix: scanning only the root would let a
        namespace-scoped condition on a unified-template Lambda pass
        CI silently)."""
        template = yaml.load(template_path.read_text(), Loader=_cfn_tag_loader())
        stackname_ref = _STACKNAME_REF_BY_TEMPLATE[template_path]
        scoped_lambdas = _find_namespace_scoped_lambdas(template, stackname_ref)
        resources = template.get("Resources", {}) or {}
        misaligned: List[str] = []
        for logical_id in scoped_lambdas:
            resource = resources.get(logical_id, {}) or {}
            env_val = _get_env_var(resource, "METRIC_NAMESPACE")
            if not (isinstance(env_val, dict) and env_val.get("!Ref") == stackname_ref):
                misaligned.append(f"{logical_id}: env METRIC_NAMESPACE={env_val!r}")
        assert not misaligned, (
            f"Namespace/env mismatch in {template_path.name} — these Lambdas "
            f"have PutMetricData scoped to `!Ref {stackname_ref}` but their "
            f"METRIC_NAMESPACE env var doesn't match, so "
            f"idp_common.metrics.put_metric will default to GENAIDP and "
            f"every emission silently AccessDenied:\n  - "
            + "\n  - ".join(misaligned)
        )


@pytest.mark.unit
class TestRound27SpecificFourLambdas:
    """The 4 Lambdas the round-27 reviewer flagged by name live in the
    root template — this is a targeted regression pin, kept separate
    from the parametrized invariant test above so a rename of any of
    the 4 shows up with the exact Lambda name."""

    def test_all_four_have_env(self) -> None:
        template = yaml.load(MAIN_TEMPLATE.read_text(), Loader=_cfn_tag_loader())
        required = {
            "ChatWithDocumentProcessorFunction",
            "ChatStreamProcessorFunction",
            "DiscoveryProcessorFunction",
            "BlueprintOptimizationFunction",
        }
        resources = template.get("Resources", {}) or {}
        missing: Set[str] = set()
        for logical_id in required:
            resource = resources.get(logical_id)
            if resource is None:
                missing.add(f"{logical_id} (resource not found — was it renamed?)")
                continue
            env_val = _get_env_var(resource, "METRIC_NAMESPACE")
            if not (isinstance(env_val, dict) and env_val.get("!Ref") == "AWS::StackName"):
                missing.add(f"{logical_id} (env is {env_val!r})")
        assert not missing, (
            "Round-27 blocker regression — these Lambdas MUST set "
            "METRIC_NAMESPACE: !Ref AWS::StackName:\n  - "
            + "\n  - ".join(sorted(missing))
        )
