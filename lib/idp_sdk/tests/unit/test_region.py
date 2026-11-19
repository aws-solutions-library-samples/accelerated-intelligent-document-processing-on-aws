# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp_sdk.resolve_region`, the one way the SDK and CLI work out a Region.

The callers' behaviour is tested where each one lives; this file covers what
the function itself promises and no caller exercises.
"""

from unittest.mock import MagicMock

import boto3
import pytest

from idp_sdk import resolve_region


@pytest.fixture(autouse=True)
def no_ambient_region(monkeypatch):
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.setenv(name, "set-by-fixture")
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)


@pytest.mark.unit
def test_an_explicit_region_wins(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")

    assert resolve_region("eu-central-1") == "eu-central-1"


@pytest.mark.unit
def test_aws_region_alone_resolves_nothing(monkeypatch):
    """boto3 does not read `AWS_REGION`, so neither does the resolver."""
    monkeypatch.setenv("AWS_REGION", "eu-central-1")

    assert resolve_region(None) is None


@pytest.mark.unit
def test_a_session_double_with_no_real_region_resolves_nothing(monkeypatch):
    """A substituted session's `region_name` is not a string, so it is not one."""
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", MagicMock())

    assert resolve_region(None) is None
