# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A cleared classification model resolves to the stack's own Region's profile.

The deploy-time swap in ``src/lambda/update_configuration`` rewrites the stored
siblings of a cleared call for the stack's Region, but the value
``default_empty_model`` substitutes is supplied while the profile is read, after
the swap. Without the translation an EU stack sent a ``us.`` profile, which Bedrock
rejects there. The expected ids are the swap's own answers, held by
``test_the_code_fallbacks_agree_with_the_swap_tables`` in
``src/lambda/update_configuration/test_eu_region.py``.
"""

from __future__ import annotations

import pytest

from idp_common.config.models import (
    DEFAULT_CLASSIFICATION_MODEL,
    ClassificationModelSettings,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def region(monkeypatch: pytest.MonkeyPatch):
    def _set(aws_region: str | None, default_region: str | None = None) -> None:
        for name, value in (
            ("AWS_REGION", aws_region),
            ("AWS_DEFAULT_REGION", default_region),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)

    return _set


@pytest.mark.parametrize(
    ("aws_region", "expected"),
    [
        ("us-east-1", "us.amazon.nova-2-lite-v1:0"),
        ("us-west-2", "us.amazon.nova-2-lite-v1:0"),
        ("eu-central-1", "eu.amazon.nova-2-lite-v1:0"),
        ("eu-west-1", "eu.amazon.nova-2-lite-v1:0"),
        # The swap leaves Nova 2 Lite alone in Canada: its us. profile is callable.
        ("ca-central-1", "us.amazon.nova-2-lite-v1:0"),
    ],
)
@pytest.mark.parametrize("cleared", [None, "", "   "])
def test_a_cleared_model_is_the_regions_default(region, aws_region, expected, cleared):
    region(aws_region)
    assert ClassificationModelSettings(model=cleared).model == expected


def test_a_local_session_region_is_used_when_aws_region_is_unset(region):
    region(None, "eu-west-3")
    assert ClassificationModelSettings(model="").model == "eu.amazon.nova-2-lite-v1:0"


def test_aws_region_wins_over_the_session_default(region):
    region("us-east-1", "eu-west-3")
    assert ClassificationModelSettings(model="").model == "us.amazon.nova-2-lite-v1:0"


def test_no_region_is_the_shipped_default(region):
    region(None)
    assert ClassificationModelSettings(model="").model == DEFAULT_CLASSIFICATION_MODEL


def test_a_named_model_is_not_translated(region):
    region("eu-west-1")
    assert (
        ClassificationModelSettings(model="us.amazon.nova-2-lite-v1:0").model
        == "us.amazon.nova-2-lite-v1:0"
    )


def test_the_warning_names_the_model_substituted(region, caplog):
    region("eu-west-1")
    with caplog.at_level("WARNING"):
        ClassificationModelSettings(model="")
    assert "eu.amazon.nova-2-lite-v1:0" in caplog.text
    assert "us.amazon.nova-2-lite-v1:0" not in caplog.text
