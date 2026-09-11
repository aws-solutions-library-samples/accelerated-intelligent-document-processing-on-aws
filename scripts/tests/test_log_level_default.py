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

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

SAFE_DEFAULT = "WARN"

# Templates a customer can deploy directly, so their own default is what a
# customer actually gets. The nested stacks the root passes ``LogLevel`` to are
# listed too: the root's value wins when deployed through the root, but each is
# also deployable standalone (and ``main-stack-extensions`` was the template
# whose enum rejected the root's new default — see the superset test below).
#
# NOT covered, deliberately: the six installable ``feature-platform/*`` feature
# stacks. Their templates default to ``INFO``, but changing that would be a
# no-op — each feature's ``feature.yaml`` pins ``defaultParameters.LogLevel:
# INFO``, which the installer passes explicitly, so the template default is
# never reached. Fixing those means editing the ``feature.yaml`` values (six
# features, ``pii-anonymizer`` first), which is a separate change with its own
# blast radius. Finding #9's residual risk survives there until then; add them
# to ``TEMPLATES`` when it is fixed. The scaffold (``feature-template/``) IS
# fixed, so new features start safe.
TEMPLATES = (
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/bedrockkb/template.yaml",
    "nested/multi-doc-discovery/template.yaml",
    "feature-platform/main-stack-extensions/template.yaml",
    "feature-platform/feature-template/template.yaml",
)

ROOT_TEMPLATE = "template.yaml"
LOG_LEVEL = "LogLevel"


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


def _parameters(rel_path: str) -> dict:
    return _load(rel_path).get("Parameters") or {}


def _source_template_for(template_url: object) -> str | None:
    """``./nested/x/.aws-sam/packaged.yaml`` -> ``nested/x/template.yaml``.

    Same mapping as ``test_nested_stack_parameters.py``: the parent points at a
    build artifact, so read the source template it is built from instead.
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


def _nested_stacks_receiving_log_level() -> list[tuple[str, str]]:
    """(logical id, source template) for every nested stack passed ``!Ref LogLevel``."""
    resources = _load(ROOT_TEMPLATE).get("Resources") or {}
    found = []
    for name, body in resources.items():
        if not isinstance(body, dict):
            continue
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        props = body.get("Properties") or {}
        passed = (props.get("Parameters") or {}).get(LOG_LEVEL)
        if passed != {"Fn::Ref": LOG_LEVEL}:
            continue
        source = _source_template_for(props.get("TemplateURL"))
        if source:
            found.append((name, source))
    return found


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
    """A Default outside AllowedValues fails at deploy time, not at lint time.

    A template with no ``AllowedValues`` accepts any string, so there is nothing
    to check for it (``nested/multi-doc-discovery`` is one).
    """
    allowed = _parameters(rel_path)[LOG_LEVEL].get("AllowedValues")
    if allowed is None:
        return
    assert SAFE_DEFAULT in allowed, f"{rel_path}: {SAFE_DEFAULT} not in {allowed}"


def test_root_passes_log_level_to_nested_stacks() -> None:
    """Guard the discovery — a silent zero would make the superset test vacuous."""
    stacks = _nested_stacks_receiving_log_level()
    assert len(stacks) >= 5, (
        f"expected at least 5 nested stacks in {ROOT_TEMPLATE} to receive "
        f"'LogLevel: !Ref LogLevel', found {len(stacks)}: {stacks}. If the "
        "wiring changed, update _nested_stacks_receiving_log_level()."
    )


@pytest.mark.parametrize(
    "logical_id,source", _nested_stacks_receiving_log_level(), ids=lambda v: str(v)
)
def test_nested_enum_accepts_every_root_value(logical_id: str, source: str) -> None:
    """Each nested ``AllowedValues`` must be a superset of the root's.

    The root forwards its own ``LogLevel`` verbatim, so any value the root
    accepts reaches the nested template and is validated against *its* enum.
    A nested enum that spells the level differently (``WARNING`` where the root
    says ``WARN``) fails CreateStack with "Parameter LogLevel failed to satisfy
    constraint" — and with ``WARN`` as the root default, it fails a deployment
    that never touched the parameter. ``feature-platform/main-stack-extensions``
    had exactly that mismatch; this test fails on it.

    The nested template's own ``Default`` is irrelevant here (the root always
    passes a value), so this test does not look at it.
    """
    root_param = _parameters(ROOT_TEMPLATE)[LOG_LEVEL]
    root_allowed = set(root_param["AllowedValues"])
    assert root_param["Default"] in root_allowed

    nested_param = _parameters(source).get(LOG_LEVEL)
    assert nested_param is not None, (
        f"{ROOT_TEMPLATE} passes LogLevel to {logical_id} but {source} declares no "
        "LogLevel parameter"
    )
    nested_allowed = nested_param.get("AllowedValues")
    if nested_allowed is None:
        return  # unconstrained: accepts anything the root sends

    rejected = sorted(root_allowed - set(nested_allowed))
    assert not rejected, (
        f"{source} (nested stack {logical_id}) rejects root LogLevel value(s) "
        f"{rejected}: its AllowedValues are {sorted(nested_allowed)} but "
        f"{ROOT_TEMPLATE} allows {sorted(root_allowed)} and forwards them as-is. "
        "CreateStack fails with 'Parameter LogLevel failed to satisfy constraint'."
    )
