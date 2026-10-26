# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The template wiring the queue processor's abort handling depends on.

The queue processor stops an execution it has just started when the document
was aborted in the window before its post-start write (#1612). Without
``states:StopExecution`` the stop fails, and that is one ERROR log line, so no
handler test notices. Reverting ``template.yaml`` left every other test under
``scripts/tests`` passing, so the grant is asserted here directly.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template.yaml"

STATE_MACHINE_NAME = {"Fn::GetAtt": "PATTERNSTACK.Outputs.StateMachineName"}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def resources() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)["Resources"]


def _statements(function: dict) -> list[dict]:
    out = []
    for policy in function["Properties"].get("Policies") or []:
        if isinstance(policy, dict) and "Statement" in policy:
            out.extend(policy["Statement"])
    return out


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action")
    actions = action if isinstance(action, list) else [action]
    return [a for a in actions if isinstance(a, str)]


def _granting(function: dict, action: str) -> list[dict]:
    """Every inline statement whose action patterns cover ``action``.

    IAM action names are case-insensitive and take ``*`` and ``?`` wildcards, so
    ``states:*`` or ``s3:Get*`` counts as granting the action. This reads inline
    ``Statement`` lists only: a managed policy, a policy template with another
    bucket name, an ``Fn::If`` around a statement or action, ``NotAction``, or a
    separate ``AWS::IAM::Policy`` on the role is not seen. The tests here assert
    that the grants these changes add are present and exactly scoped; they are not
    a general check that nothing else widens the role. Several such shapes were
    measured passing, and following them one spelling at a time is the pattern this
    repository's backlog hazards record as a stop signal.
    """
    return [
        s
        for s in _statements(function)
        if any(fnmatch.fnmatchcase(action.lower(), a.lower()) for a in _actions(s))
    ]


def _sub(node: Any) -> Any:
    return node.get("Fn::Sub") if isinstance(node, dict) else None


def test_the_queue_processor_can_stop_only_its_own_executions(resources):
    function = resources["QueueProcessor"]
    (grant,) = _granting(function, "states:StopExecution")
    assert grant.get("Effect") == "Allow" and "Condition" not in grant
    assert _actions(grant) == ["states:StopExecution"]
    assert _sub(grant["Resource"]) == [
        "arn:${AWS::Partition}:states:${AWS::Region}:${AWS::AccountId}"
        ":execution:${StateMachineName}:*",
        {"StateMachineName": STATE_MACHINE_NAME},
    ]
    # The machine it may stop is the machine it starts.
    started = [
        p["StepFunctionsExecutionPolicy"]["StateMachineName"]
        for p in function["Properties"]["Policies"]
        if isinstance(p, dict) and "StepFunctionsExecutionPolicy" in p
    ]
    assert started == [STATE_MACHINE_NAME]
    assert function["Properties"]["Environment"]["Variables"]["STATE_MACHINE_ARN"] == {
        "Fn::GetAtt": "PATTERNSTACK.Outputs.StateMachineArn"
    }
