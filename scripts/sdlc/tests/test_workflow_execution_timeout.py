"""#757: the execution-level workflow timeout is a parameter that reaches the state
machine, and tripping it is alarmable.

Pins the plumbing that a fresh-deploy smoke test cannot see: the parent declares
`WorkflowExecutionTimeoutSeconds`, passes it to the pattern stack, the pattern
stack substitutes it into the ASL's top-level `TimeoutSeconds`, and a
`WorkflowTimeoutsAlarm` watches `ExecutionsTimedOut` — the metric a timed-out
execution emits instead of `ExecutionsFailed`, which is why `WorkflowErrorsAlarm`
alone cannot see it.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
PARENT = REPO_ROOT / "template.yaml"
PATTERN = REPO_ROOT / "patterns" / "unified" / "template.yaml"
PARAM = "WorkflowExecutionTimeoutSeconds"


class _CfnSafeLoader(yaml.SafeLoader):
    pass


def _tagged(loader: Any, tag_suffix: str, node: Any) -> Any:  # noqa: ANN401
    if isinstance(node, yaml.ScalarNode):
        return {f"!{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"!{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"!{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnSafeLoader.add_multi_constructor("!", _tagged)


def _load(path: pathlib.Path) -> dict:
    with path.open() as fh:
        loader = _CfnSafeLoader(fh)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


@pytest.fixture(scope="module")
def parent() -> dict:
    return _load(PARENT)


@pytest.fixture(scope="module")
def pattern() -> dict:
    return _load(PATTERN)


def test_parent_declares_a_generous_default_and_groups_it(parent):
    p = parent["Parameters"][PARAM]
    assert p["Type"] == "Number"
    assert p["Default"] == 21600, "default is 6 hours; see the measurement in the PR"
    assert p["MinValue"] >= 300
    groups = {
        g["Label"]["default"]: g["Parameters"]
        for g in parent["Metadata"]["AWS::CloudFormation::Interface"]["ParameterGroups"]
    }
    assert PARAM in groups["General Configuration"]
    assert (
        PARAM in parent["Metadata"]["AWS::CloudFormation::Interface"]["ParameterLabels"]
    )


def test_parent_passes_it_to_the_pattern_stack(parent):
    params = parent["Resources"]["PATTERNSTACK"]["Properties"]["Parameters"]
    assert params[PARAM] == {"!Ref": PARAM}


def test_pattern_stack_substitutes_it_into_the_state_machine(pattern):
    assert pattern["Parameters"][PARAM]["Default"] == 21600
    sm = next(
        r
        for r in pattern["Resources"].values()
        if r.get("Type") == "AWS::Serverless::StateMachine"
        and "workflow.asl.json" in str(r["Properties"].get("DefinitionUri", ""))
    )
    subs = sm["Properties"]["DefinitionSubstitutions"]
    assert subs[PARAM] == {"!Ref": PARAM}


def test_a_timed_out_execution_is_alarmable(parent):
    alarm = parent["Resources"]["WorkflowTimeoutsAlarm"]
    assert alarm["Type"] == "AWS::CloudWatch::Alarm"
    props = alarm["Properties"]
    assert props["MetricName"] == "ExecutionsTimedOut"
    assert props["Namespace"] == "AWS/States"
    assert props["Threshold"] == 1
    assert {"!Ref": "AlertsTopic"} in props["AlarmActions"]
    # The status-change rule that drives the workflow tracker must include TIMED_OUT,
    # or the timed-out document keeps its slot and its RUNNING status.
    rule = parent["Resources"]["WorkflowStateChangeRule"]["Properties"]["EventPattern"]
    assert "TIMED_OUT" in rule["detail"]["status"]


def test_the_dashboard_plots_timed_out_executions(parent):
    """A TIMED_OUT run emits neither ExecutionsFailed nor ExecutionTime, so the
    executions widget must carry its own series or the dashboard shows nothing."""
    dashboards = [
        r
        for r in parent["Resources"].values()
        if r.get("Type") == "AWS::CloudWatch::Dashboard"
    ]
    assert dashboards, "parent template declares a dashboard"
    body = str(dashboards[0]["Properties"]["DashboardBody"])
    assert "ExecutionsTimedOut" in body
    assert "Timed out per Minute" in body
