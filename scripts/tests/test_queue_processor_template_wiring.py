# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The template wiring the queue processor and the reporting Lambda depend on.

Two changes in the handlers need the template to deliver something, and neither
handler fails when it is missing:

- The queue processor and ``SaveReportingDataFunctionV2`` read a document's
  pinned configuration revision (#1619). Without ``CONFIGURATION_BUCKET`` or
  ``s3:GetObject`` on the revision prefix, the read fails and both fall back to
  the profile head with a warning, which is the behaviour the change replaced.
  The processChanges resolver (``ProcessChangesResolverFunction`` in
  ``nested/api-resolvers/template.yaml``) reads the same revision to decide
  whether a requeued run goes down the BDA branch; without the wiring it refuses
  every section and page change on a pinned document, which only a live stack
  would show.
- The queue processor stops an execution it has just started when the document
  was aborted in the window before its post-start write (#1612). Without
  ``states:StopExecution`` the stop fails, and that is one ERROR log line.

Reverting ``template.yaml`` to its state before both changes left every other
test under ``scripts/tests`` passing, so the wiring is asserted here directly.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template.yaml"
API_RESOLVERS_TEMPLATE = REPO_ROOT / "nested/api-resolvers/template.yaml"
REVISIONS_MODULE = REPO_ROOT / "lib/idp_common_pkg/idp_common/config/revisions.py"

# (template, function, the logical name the bucket is reached by in that template)
REVISION_BODY_READERS = (
    (TEMPLATE, "QueueProcessor", "ConfigurationBucket"),
    (TEMPLATE, "SaveReportingDataFunctionV2", "ConfigurationBucket"),
    (
        API_RESOLVERS_TEMPLATE,
        "ProcessChangesResolverFunction",
        "ConfigurationBucketName",
    ),
)
READER_IDS = [name for _, name, _ in REVISION_BODY_READERS]
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


_LOADED: dict = {}


def _resources_of(template: Path) -> dict:
    if template not in _LOADED:
        _LOADED[template] = yaml.load(template.read_text(), Loader=_CfnLoader)[
            "Resources"
        ]
    return _LOADED[template]


@pytest.fixture(scope="module")
def resources() -> dict:
    return _resources_of(TEMPLATE)


def _revision_prefix() -> str:
    """``REVISION_S3_PREFIX`` as the revision store defines it, read from source."""
    tree = ast.parse(REVISIONS_MODULE.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "REVISION_S3_PREFIX"
                for t in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError(f"REVISION_S3_PREFIX not found in {REVISIONS_MODULE}")


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


@pytest.mark.parametrize(
    ("template", "name", "bucket"), REVISION_BODY_READERS, ids=READER_IDS
)
def test_the_reader_is_told_the_configuration_bucket(template, name, bucket):
    function = _resources_of(template)[name]
    variables = function["Properties"]["Environment"]["Variables"]
    assert variables.get("CONFIGURATION_BUCKET") == {"Fn::Ref": bucket}


@pytest.mark.parametrize(
    ("template", "name", "bucket"), REVISION_BODY_READERS, ids=READER_IDS
)
def test_the_reader_can_get_exactly_the_revision_bodies(template, name, bucket):
    prefix = _revision_prefix()
    assert prefix, "the revision store's prefix is empty"
    grants = _granting(_resources_of(template)[name], "s3:GetObject")
    resources_granted = [_sub(s.get("Resource")) for s in grants]
    assert resources_granted == [
        f"arn:${{AWS::Partition}}:s3:::${{{bucket}}}/{prefix}/*"
    ], grants
    assert all(s.get("Effect") == "Allow" and "Condition" not in s for s in grants)


@pytest.mark.parametrize(
    ("template", "name", "bucket"), REVISION_BODY_READERS, ids=READER_IDS
)
def test_the_reader_holds_no_whole_bucket_template_on_the_configuration_bucket(
    template, name, bucket
):
    """A SAM S3 policy template on the bucket would grant every object in it."""
    templates = [
        (key, value)
        for policy in _resources_of(template)[name]["Properties"].get("Policies") or []
        if isinstance(policy, dict)
        for key, value in policy.items()
        if key.startswith("S3") and "ConfigurationBucket" in repr(value)
    ]
    assert templates == [], templates


@pytest.mark.parametrize(
    ("template", "name", "bucket"), REVISION_BODY_READERS, ids=READER_IDS
)
def test_the_readers_list_grant_is_limited_to_the_revision_prefix(
    template, name, bucket
):
    grants = _granting(_resources_of(template)[name], "s3:ListBucket")
    assert [(_sub(s.get("Resource")), s.get("Condition")) for s in grants] == [
        (
            f"arn:${{AWS::Partition}}:s3:::${{{bucket}}}",
            {"StringLike": {"s3:prefix": f"{_revision_prefix()}/*"}},
        )
    ], grants


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
