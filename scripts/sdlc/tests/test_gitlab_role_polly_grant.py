# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The GitLab CI role's Amazon Polly grant, for the MR UX test's narration.

credential-vendor.yml gains exactly one statement for the advisory ai_ux_test job:
polly:SynthesizeSpeech on "*", because a request with no lexicons is authorized
against "*" rather than a lexicon ARN, so the resource cannot be narrowed. The
action is what can be narrowed, and the role is shared with the integration
deploy and the AI code review, so these tests hold the grant to that one action
in that one statement, confirm the code review's Bedrock grant beside it did not
move, and close the role's whole grant over an audited inventory, so that no other
action can arrive beside it unreviewed either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "cfn" / "credential-vendor.yml"


class _TagTolerantLoader(yaml.SafeLoader):
    """SafeLoader that reads a CloudFormation short-form tag as its plain value.

    `!Sub "arn:..."` becomes the string, `!Ref X` the name, and so on. Which
    actions a statement grants is all these tests compare, and no tag carries
    any of that, so the intrinsic is dropped rather than modelled.
    """


def _untagged(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    raise TypeError(f"unexpected YAML node {node!r}")


_TagTolerantLoader.add_multi_constructor("!", _untagged)


def _template() -> dict:
    with TEMPLATE.open(encoding="utf-8") as handle:
        loader = _TagTolerantLoader(handle)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


def _policies() -> list[dict]:
    return _template()["Resources"]["GitLabRole"]["Properties"]["Policies"]


def _statements(document: dict) -> list[dict]:
    statements = document["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _actions(statement: dict) -> list[str]:
    action = statement.get("Action", [])
    return [action] if isinstance(action, str) else list(action)


def _every_statement() -> list[tuple[str, dict]]:
    role = _template()["Resources"]["GitLabRole"]["Properties"]
    found = [
        ("AssumeRolePolicyDocument", statement)
        for statement in _statements(role["AssumeRolePolicyDocument"])
    ]
    for policy in role["Policies"]:
        found += [
            (policy["PolicyName"], statement)
            for statement in _statements(policy["PolicyDocument"])
        ]
    return found


@pytest.mark.unit
def test_exactly_one_statement_grants_polly_and_only_synthesize_speech():
    polly = [
        (policy, statement)
        for policy, statement in _every_statement()
        if any(action.lower().startswith("polly:") for action in _actions(statement))
    ]

    assert len(polly) == 1, f"polly is granted by {[name for name, _ in polly]}"
    ((policy, statement),) = polly
    assert policy == "PollySynthesizeForUxTest"
    assert statement["Sid"] == "SynthesizeSpeechForAdvisoryUxTest"
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == ["polly:SynthesizeSpeech"]
    assert statement["Resource"] == "*"
    assert set(statement) == {"Sid", "Effect", "Action", "Resource"}


@pytest.mark.unit
def test_no_other_statement_grants_polly_or_a_wildcard_action():
    for policy, statement in _every_statement():
        actions = [action.lower() for action in _actions(statement)]
        assert "*" not in actions, f"{policy} grants every action"
        if policy != "PollySynthesizeForUxTest":
            assert not [a for a in actions if a.startswith("polly:")], (
                f"{policy} gained a polly action"
            )


@pytest.mark.unit
def test_the_code_reviews_bedrock_grant_is_unchanged():
    (bedrock,) = [
        p for p in _policies() if p["PolicyName"] == "BedrockInvokeForMrReview"
    ]
    (statement,) = _statements(bedrock["PolicyDocument"])

    assert statement["Sid"] == "InvokeOpusFamilyForAdvisoryMrReview"
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
    ]
    assert len(statement["Resource"]) == 3


@pytest.mark.unit
def test_the_polly_policy_sits_between_the_bedrock_and_source_upload_policies():
    """Where the stack update diff will show it, beside the grant it parallels."""
    names = [policy["PolicyName"] for policy in _policies()]
    assert "PollySynthesizeForUxTest" in names
    polly = names.index("PollySynthesizeForUxTest")

    assert names[polly - 1] == "BedrockInvokeForMrReview"
    assert names[polly + 1] == "S3PutObjectPolicy"
    assert len(names) == len(set(names))


@pytest.mark.unit
def test_every_action_the_gitlab_role_holds_was_audited_into_this_test():
    """A grant added anywhere on the shared role, not only a polly one, fails here.

    The inventory is every (policy, action) pair across the trust policy and the
    inline policies, written out below. An action that appears is unaccounted
    until somebody reviews it in, and one that disappears makes the inventory
    stale, so the list is edited in the same change as the template either way.
    """
    audited = {
        ("AssumeRolePolicyDocument", "sts:AssumeRole"),
        ("AssumeRolePolicyDocument", "sts:TagSession"),
        ("SelfAssumeForCredentialRefresh", "sts:AssumeRole"),
        ("SelfAssumeForCredentialRefresh", "sts:TagSession"),
        ("BedrockInvokeForMrReview", "bedrock:InvokeModel"),
        ("BedrockInvokeForMrReview", "bedrock:InvokeModelWithResponseStream"),
        ("PollySynthesizeForUxTest", "polly:SynthesizeSpeech"),
        ("S3PutObjectPolicy", "s3:PutObject"),
        ("CodePipelineMonitorPolicy", "codepipeline:GetPipelineState"),
        ("CodePipelineMonitorPolicy", "codepipeline:GetPipeline"),
        ("CodePipelineMonitorPolicy", "codepipeline:GetPipelineExecution"),
        ("CodePipelineMonitorPolicy", "codepipeline:ListPipelineExecutions"),
        ("CodePipelineMonitorPolicy", "codepipeline:ListPipelines"),
        ("CodePipelineMonitorPolicy", "codepipeline:ListActionExecutions"),
        ("CodeBuildAccessPolicy", "codebuild:BatchGetBuilds"),
        ("CloudWatchLogsAccessPolicy", "logs:GetLogEvents"),
        ("CloudWatchLogsAccessPolicy", "logs:DescribeLogStreams"),
        ("CloudWatchLogsAccessPolicy", "logs:DescribeLogGroups"),
    }
    granted = {
        (policy, action)
        for policy, statement in _every_statement()
        for action in _actions(statement)
    }
    unaccounted = sorted(granted - audited)
    assert not unaccounted, (
        f"GitLabRole holds actions nobody audited into this test: {unaccounted}"
    )
    stale = sorted(audited - granted)
    assert not stale, f"the audited inventory names actions GitLabRole lost: {stale}"
    widening = [
        (policy, sorted(set(statement) & {"NotAction", "NotResource", "NotPrincipal"}))
        for policy, statement in _every_statement()
        if set(statement) & {"NotAction", "NotResource", "NotPrincipal"}
    ]
    assert not widening, f"a statement grants by exclusion: {widening}"


@pytest.mark.unit
def test_nothing_attaches_actions_to_the_gitlab_role_outside_its_inline_policies():
    """Managed policies and separate policy resources would add actions the
    inventory above never reads, so the role and the template hold neither."""
    template = _template()
    role = template["Resources"]["GitLabRole"]["Properties"]
    assert "ManagedPolicyArns" not in role
    assert "PermissionsBoundary" not in role
    attaching = sorted(
        name
        for name, resource in template["Resources"].items()
        if resource.get("Type")
        in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::RolePolicy")
    )
    assert not attaching, f"these resources can attach actions to roles: {attaching}"
