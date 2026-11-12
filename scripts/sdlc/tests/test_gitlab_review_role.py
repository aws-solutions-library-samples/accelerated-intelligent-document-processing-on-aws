# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The GitLab AI review job's own role: Bedrock only, vended by the runner fleet.

``GitLabReviewRole`` in ``credential-vendor.yml`` is what ``ai_mr_review`` holds in
place of the integration-test role. These tests hold its grant to the GitHub
review role's, derived by reading ``github-oidc-review-role.yml`` rather than
restated, hold its trust to the runner fleet gated on both project tags, and tie
the job's ``REVIEW_ROLE_NAME`` to the role name the template renders.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

CFN = Path(__file__).resolve().parents[1] / "cfn"
REPO_ROOT = Path(__file__).resolve().parents[3]


class _Loader(yaml.SafeLoader):
    """Reads a short-form intrinsic as ``{"!Tag": value}`` so it can be compared."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {f"!{suffix}": value}


_Loader.add_multi_constructor("!", _tagged)


def _load(name: str) -> dict:
    with (CFN / name).open(encoding="utf-8") as handle:
        loader = _Loader(handle)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


@pytest.fixture(scope="module")
def vendor() -> dict:
    return _load("credential-vendor.yml")


@pytest.fixture(scope="module")
def review_role(vendor: dict) -> dict:
    return vendor["Resources"]["GitLabReviewRole"]["Properties"]


@pytest.mark.unit
def test_the_grant_is_the_github_review_role_s_grant(review_role: dict) -> None:
    github = _load("github-oidc-review-role.yml")["Resources"]["GitHubReviewRole"]
    assert review_role["Policies"] == github["Properties"]["Policies"]
    actions = {
        action
        for policy in review_role["Policies"]
        for statement in policy["PolicyDocument"]["Statement"]
        for action in statement["Action"]
    }
    assert actions == {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
    assert "ManagedPolicyArns" not in review_role
    assert "PermissionsBoundary" not in review_role


@pytest.mark.unit
def test_only_the_runner_fleet_with_both_project_tags_can_assume_it(
    vendor: dict, review_role: dict
) -> None:
    gitlab_trust = vendor["Resources"]["GitLabRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]["Statement"]
    fleet = gitlab_trust[0]["Principal"]["AWS"]
    statements = review_role["AssumeRolePolicyDocument"]["Statement"]
    assert len(statements) == 1, statements
    (statement,) = statements
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"AWS": fleet}
    assert sorted(statement["Action"]) == ["sts:AssumeRole", "sts:TagSession"]
    assert statement["Condition"] == {
        "StringEquals": {
            "aws:PrincipalTag/GitLab:Group": {"!Ref": "GitLabGroupName"},
            "aws:PrincipalTag/GitLab:Project": {"!Ref": "GitLabProjectName"},
        }
    }


@pytest.mark.unit
def test_the_job_requires_the_name_the_template_renders(
    vendor: dict, review_role: dict
) -> None:
    defaults = {key: spec.get("Default") for key, spec in vendor["Parameters"].items()}
    rendered = review_role["RoleName"]["!Sub"]
    for key, value in defaults.items():
        rendered = rendered.replace("${" + key + "}", str(value))
    jobs = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    variables = jobs["ai_mr_review"]["variables"]
    assert variables["REVIEW_ROLE_NAME"] == rendered == "idp-sdlc-GitLabReview"
    assert variables["AWS_CREDS_TARGET_ROLE"] == "$AI_REVIEW_GITLAB_ROLE_ARN"
    assert "GitLabReviewRoleArn" in vendor["Outputs"]


@pytest.mark.unit
def test_the_review_job_is_the_only_job_naming_the_review_role() -> None:
    """The UX jobs stay on the integration-test role, which keeps its own Bedrock
    grant for them; only ``ai_mr_review`` moves."""
    jobs = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    naming = sorted(
        name
        for name, job in jobs.items()
        if isinstance(job, dict)
        and "AI_REVIEW_GITLAB_ROLE_ARN" in str(job.get("variables", ""))
    )
    assert naming == ["ai_mr_review"]
