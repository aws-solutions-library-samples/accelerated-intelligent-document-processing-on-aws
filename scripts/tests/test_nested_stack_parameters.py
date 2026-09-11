# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parent/nested-stack parameter wiring, asserted against SOURCE templates.

cfn-lint has a rule for this (E3043, "Specified parameter doesn't exist in nested
stack template") but it cannot work in either environment we care about:

* In **CI** the parent's ``TemplateURL`` points at
  ``<dir>/.aws-sam/packaged.yaml`` — a *build* artifact absent from a fresh
  checkout. cfn-lint logs "Template file not found" and silently skips the check.
* In a **built local tree** the file exists but may predate a ``Parameters``
  change, so the rule reports false positives. Chasing five of those is what
  proved the rule was useless here.

So E3043 is disabled in ``make cfn-lint`` and this test does the job instead,
reading each nested stack's **source** ``template.yaml`` rather than its packaged
output. That is strictly better than the lint rule: it works on a clean checkout,
it cannot go stale, and it also catches the reverse direction (a required nested
parameter the parent never passes) which E3043 does not check at all.

Both failure modes are real deployment breakers:

* passing a parameter the nested template does not declare →
  ``Parameters: [X] do not exist in the template`` at CREATE/UPDATE time;
* omitting one the nested template requires (no ``Default``) →
  ``Parameters: [X] must have values``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PARENT_TEMPLATE = "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(rel_path: str) -> dict:
    return yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}


def _source_template_for(template_url: Any) -> str | None:
    """Map a nested stack's TemplateURL to the SOURCE template it is built from.

    ``./feature-platform/main-stack-extensions/.aws-sam/packaged.yaml``
    -> ``feature-platform/main-stack-extensions/template.yaml``
    """
    if not isinstance(template_url, str):
        return None
    match = re.match(r"^\./(.+?)/\.aws-sam/packaged\.ya?ml$", template_url.strip())
    if not match:
        return None
    for candidate in (
        f"{match.group(1)}/template.yaml",
        f"{match.group(1)}/template.yml",
    ):
        if (REPO_ROOT / candidate).is_file():
            return candidate
    return None


def _nested_stacks() -> list[tuple[str, str]]:
    """(logical id, source template path) for every nested stack in the parent."""
    resources = _load(PARENT_TEMPLATE).get("Resources", {}) or {}
    found = []
    for name, body in resources.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        source = _source_template_for((body.get("Properties") or {}).get("TemplateURL"))
        if source:
            found.append((name, source))
    return found


@pytest.mark.unit
def test_nested_stacks_are_discovered() -> None:
    """Guard the discovery itself — a silent zero would make the rest vacuous.

    This is the failure mode that made E3043 worthless: no templates resolved, no
    findings, green build.
    """
    stacks = _nested_stacks()
    assert len(stacks) >= 5, (
        f"expected at least 5 nested stacks in {PARENT_TEMPLATE}, found "
        f"{len(stacks)}: {stacks}. If TemplateURL no longer points at "
        f"'<dir>/.aws-sam/packaged.yaml', update _source_template_for()."
    )


@pytest.mark.unit
@pytest.mark.parametrize("logical_id,source", _nested_stacks(), ids=lambda v: str(v))
def test_parent_passes_only_parameters_the_nested_template_declares(
    logical_id: str, source: str
) -> None:
    """Passing an undeclared parameter fails the stack operation outright."""
    parent = _load(PARENT_TEMPLATE)
    passed = set(
        (
            (parent["Resources"][logical_id].get("Properties") or {}).get("Parameters")
            or {}
        )
    )
    declared = set(_load(source).get("Parameters", {}) or {})

    undeclared = sorted(passed - declared)
    assert not undeclared, (
        f"{PARENT_TEMPLATE} passes parameter(s) to {logical_id} that "
        f"{source} does not declare: {undeclared}. CloudFormation rejects this "
        f"with 'Parameters: [...] do not exist in the template'."
    )


@pytest.mark.unit
@pytest.mark.parametrize("logical_id,source", _nested_stacks(), ids=lambda v: str(v))
def test_parent_passes_every_required_nested_parameter(
    logical_id: str, source: str
) -> None:
    """A nested parameter with no Default must be supplied by the parent.

    E3043 does not check this direction at all.
    """
    parent = _load(PARENT_TEMPLATE)
    passed = set(
        (
            (parent["Resources"][logical_id].get("Properties") or {}).get("Parameters")
            or {}
        )
    )
    declared = _load(source).get("Parameters", {}) or {}
    required = {
        name
        for name, spec in declared.items()
        if isinstance(spec, dict) and "Default" not in spec
    }

    missing = sorted(required - passed)
    assert not missing, (
        f"{source} requires parameter(s) with no Default that "
        f"{PARENT_TEMPLATE} does not pass to {logical_id}: {missing}. "
        f"CloudFormation rejects this with 'Parameters: [...] must have values'."
    )
