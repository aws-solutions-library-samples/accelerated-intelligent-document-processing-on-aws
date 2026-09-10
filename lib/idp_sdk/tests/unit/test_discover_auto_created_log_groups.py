# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``StackDeployer._discover_auto_created_log_groups``.

This function had **no test coverage at all**, which is how it came to carry
prefixes that matched nothing. Two rounds of review on #826 found ten dead
entries between them:

* six per-function prefixes like ``/aws/lambda/<stack>-ListInstalledFeaturesFunction``
  for Lambdas that actually live in a *nested* stack, so their real groups are
  ``/aws/lambda/<stack>-FeaturePlatformStack-<fn>-<hash>``;
* then the replacement ``/aws/lambda/<stack>-FeaturePlatformStack-`` itself, which
  is 26 characters for a 4-character stack name and gets truncated away by
  CloudFormation's 64-char Lambda name cap once the stack name is longer;
* and four ``PATTERN1STACK``/``PATTERN2STACK`` prefixes naming logical ids that no
  longer exist since the pattern stacks were unified.

A zero-match prefix is indistinguishable from "there were no orphans", so every
one of those failed silently. These tests pin the behaviour that matters: the
sweep must find a nested stack's orphans regardless of how long the parent stack
name is, and must not touch a sibling stack whose name merely starts the same.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk._core.stack import StackDeployer


def _deployer_with_log_groups(names: list[str]) -> tuple[StackDeployer, MagicMock]:
    """A StackDeployer whose Logs paginator returns ``names`` filtered by prefix."""
    logs = MagicMock()
    paginator = MagicMock()

    def paginate(logGroupNamePrefix: str, **_kwargs):
        return [
            {
                "logGroups": [
                    {"logGroupName": n}
                    for n in names
                    if n.startswith(logGroupNamePrefix)
                ]
            }
        ]

    paginator.paginate.side_effect = paginate
    logs.get_paginator.return_value = paginator
    logs.describe_log_groups.return_value = {
        "logGroups": [{"logGroupName": n} for n in names]
    }

    deployer = StackDeployer.__new__(StackDeployer)
    deployer.region = "us-west-2"
    return deployer, logs


def _discover(stack_name: str, names: list[str]) -> list[str]:
    deployer, logs = _deployer_with_log_groups(names)
    with patch("boto3.client", return_value=logs):
        return deployer._discover_auto_created_log_groups(stack_name)


@pytest.mark.unit
@pytest.mark.parametrize("stack_name", ["IDP1", "IDP-DEV", "idp-production-east-1"])
def test_finds_nested_stack_orphans_for_any_stack_name_length(stack_name: str) -> None:
    """The regression that shipped twice: a prefix too long to survive truncation.

    ``IDP1-FeaturePlatformStack-`` happens to fit in the ~26 characters
    CloudFormation leaves for the stack-name segment. ``IDP-DEV-...`` does not, so
    a nested-stack-scoped prefix silently found nothing. Discovery must not depend
    on the parent stack name being short.
    """
    orphan = (
        f"/aws/lambda/{stack_name}-FeaturePlatformSt-CheckFeatureEntitl-32Q2qRNU35FU"
    )
    assert orphan in _discover(stack_name, [orphan])


@pytest.mark.unit
def test_finds_orphans_of_the_parent_stack_itself() -> None:
    orphan = "/aws/lambda/IDP1-BatchPreProcessorFunction-AbC123xyz789"
    assert orphan in _discover("IDP1", [orphan])


@pytest.mark.unit
def test_does_not_match_a_sibling_stack_with_a_longer_name() -> None:
    """``IDP1`` must never sweep ``IDP10``'s log groups.

    The hyphen after the stack name is the only thing preventing this, so it needs
    a test — deleting another live deployment's logs would be unrecoverable.
    """
    mine = "/aws/lambda/IDP1-BatchPreProcessorFunction-AbC123xyz789"
    sibling = "/aws/lambda/IDP10-BatchPreProcessorFunction-ZzZ999yyy111"
    discovered = _discover("IDP1", [mine, sibling])
    assert mine in discovered
    assert sibling not in discovered


@pytest.mark.unit
def test_finds_stack_scoped_nested_log_groups() -> None:
    """The `/<nested-stack>/lambda/<Fn>` convention, not Lambda's default."""
    names = [
        "/IDP1-PATTERNSTACK-A1B2C3/lambda/OCRFunction",
        "/IDP1-APIRESOLVERSTACK-D4E5F6/lambda/GetDocument",
        "/IDP1-FeaturePlatformStack-G7H8I9/lambda/ListCatalogFeaturesFunction",
    ]
    discovered = _discover("IDP1", names)
    for name in names:
        assert name in discovered, f"{name} not discovered"


@pytest.mark.unit
def test_unrelated_log_groups_are_left_alone() -> None:
    unrelated = [
        "/aws/lambda/SomeOtherStack-Function-abc",
        "/aws/lambda/prod-billing-worker",
        "/some/other/group",
    ]
    assert _discover("IDP1", unrelated) == []


@pytest.mark.unit
def test_a_logs_api_error_does_not_abort_discovery() -> None:
    """One failing prefix must not lose the matches from the others.

    Note the trade-off this consolidation introduced: a single generic
    ``/aws/lambda/<stack>-`` prefix now carries every Lambda orphan, so a
    transient failure on *that* prefix loses them all for this run rather than
    just some. The error is logged as a warning rather than raised, so teardown
    still completes — but a genuinely throttled sweep needs re-running.
    """
    wanted = "/IDP1-PATTERNSTACK-A1B2C3/lambda/OCRFunction"
    deployer, logs = _deployer_with_log_groups([wanted])
    paginator = logs.get_paginator.return_value
    original = paginator.paginate.side_effect

    def flaky(logGroupNamePrefix: str, **kwargs):
        # Fail the generic Lambda prefix; the stack-scoped ones must still run.
        if logGroupNamePrefix == "/aws/lambda/IDP1-":
            raise RuntimeError("throttled")
        return original(logGroupNamePrefix=logGroupNamePrefix, **kwargs)

    paginator.paginate.side_effect = flaky
    with patch("boto3.client", return_value=logs):
        discovered = deployer._discover_auto_created_log_groups("IDP1")

    assert wanted in discovered
