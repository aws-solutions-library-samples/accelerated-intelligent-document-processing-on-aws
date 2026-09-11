# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ``LogLevel`` parameter must default to ``WARN`` in every template.

At ``INFO`` the accelerator can write S3 presigned URLs, document contents and
PII into CloudWatch Logs — the residual risk behind AppSec finding #9
(*TRACE/DEBUG Logging Enabled*), whose acceptance rested on ``INFO`` being a
safe product default. ``docs/well-architected.md`` has told operators to set
``WARN`` or ``ERROR`` for production since v0.3, so ``INFO`` also disagreed with
our own published guidance.

This is a defaults regression guard, not a style check: the failure mode is
silent (a deployment that never touches the parameter simply logs more than it
should), so nothing else in the build would notice a revert.

``patterns/unified/template.yaml`` is included deliberately — it already
defaulted to ``WARN`` before the root template did, and is the evidence that the
safer default is operationally acceptable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

SAFE_DEFAULT = "WARN"

# Templates a customer can deploy directly, so their own default is what a
# customer actually gets. Nested templates the root always passes ``LogLevel``
# to are covered by the root's default and are not listed here.
TEMPLATES = (
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/bedrockkb/template.yaml",
)


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _parameters(rel_path: str) -> dict:
    doc = yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}
    return doc.get("Parameters") or {}


@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_log_level_defaults_to_warn(rel_path: str) -> None:
    param = _parameters(rel_path).get("LogLevel")
    assert param is not None, f"{rel_path} declares no LogLevel parameter"

    default = param.get("Default")
    assert default == SAFE_DEFAULT, (
        f"{rel_path}: LogLevel Default is {default!r}, expected {SAFE_DEFAULT!r}. "
        "INFO and DEBUG can emit presigned URLs, document contents and PII to "
        "CloudWatch (AppSec finding #9)."
    )


@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_safe_default_is_an_allowed_value(rel_path: str) -> None:
    """A Default outside AllowedValues fails at deploy time, not at lint time."""
    allowed = _parameters(rel_path)["LogLevel"].get("AllowedValues")
    assert allowed is not None, f"{rel_path}: LogLevel has no AllowedValues"
    assert SAFE_DEFAULT in allowed, f"{rel_path}: {SAFE_DEFAULT} not in {allowed}"
