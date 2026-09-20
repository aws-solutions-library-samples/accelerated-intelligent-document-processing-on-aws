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

The set of templates this enforces is **derived** from the tree rather than
listed, so a template declaring ``LogLevel`` is covered the moment it exists. See
``LOG_LEVEL_DEFAULT_EXEMPT`` for why that matters.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import gate_premises

REPO_ROOT = Path(__file__).resolve().parents[2]

SAFE_DEFAULT = "WARN"

ROOT_TEMPLATE = "template.yaml"
LOG_LEVEL = "LogLevel"

# Every template that declares a ``LogLevel`` parameter is enforced, unless it is
# named in ``LOG_LEVEL_DEFAULT_EXEMPT`` below. Membership is DERIVED --
# :func:`_templates_declaring_log_level` reads it out of the tree -- so a new
# template cannot be added without a decision being made about it.
#
# This replaced a hardcoded six-entry tuple whose exclusion lived only in a prose
# comment. The comment said the excluded ``feature-platform/*`` stacks were covered
# because "each feature's ``feature.yaml`` pins ``defaultParameters.LogLevel:
# INFO``", and it named six of them. There are five such manifests. The sixth
# directory, ``seller-entitlement-service/``, has no ``feature.yaml`` at all, so its
# template ``Default`` was what a seller actually got -- precisely the regression
# this module exists to prevent, sitting inside the sentence explaining why it could
# not happen. A prose exclusion cannot be evaluated per member, and that is the
# whole defect: one justification attached to a set, where the justification is a
# property of each member.
#
# So the exclusion is now a constant, its members are checked one at a time against
# :func:`gate_premises.installer_manifest_pins_parameter`, and a directory with no
# manifest fails.
#
# A template that declares ``LogLevel`` with NO ``Default`` is accepted rather than
# exempted: the absence of a default is not an unsafe default (CloudFormation
# requires the caller to supply a value). ``nested/api-resolvers`` is the one such
# template today, and it was outside the old hardcoded tuple as well. Adding
# ``Default: INFO`` to it later still fails.
LOG_LEVEL_DEFAULT_EXEMPT: dict[str, str] = {
    # The five catalog features. Each is installed into a customer account as its
    # own stack, and the console install path folds the manifest's
    # ``defaultParameters`` into the launch URL (see
    # main-stack-extensions/lambdas/get_feature_launch_url), so the value reaching
    # CloudFormation there is the manifest's, not the template's.
    #
    # The premise checked per member is the narrow, verifiable one: a manifest
    # exists and pins ``LogLevel``. It is deliberately NOT the broader claim that
    # "the template default is never reached", because that broader claim does not
    # hold on every install path -- ``idp-feature-cli deploy`` passes ``LogLevel``
    # only when ``--log-level`` is given, so without it the template default IS what
    # reaches CloudFormation. That residual is recorded here rather than papered
    # over; closing it belongs with the installer, not with this gate.
    "feature-platform/confbench-testset": (
        "manifest pins LogLevel for the console install path"
    ),
    "feature-platform/idp-data-generator": (
        "manifest pins LogLevel for the console install path"
    ),
    "feature-platform/pii-anonymizer": (
        "manifest pins LogLevel for the console install path"
    ),
    "feature-platform/sample-feature": (
        "manifest pins LogLevel for the console install path"
    ),
    "feature-platform/sample-health-insurance-review": (
        "manifest pins LogLevel for the console install path"
    ),
}


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


def _templates_declaring_log_level() -> tuple[str, ...]:
    """Every template in the tree that declares a ``LogLevel`` parameter.

    Discovered through git (see :func:`gate_premises.tracked_files`) rather than by
    walking the filesystem, so build output under ``.aws-sam/`` and sibling
    worktrees under ``.claude/`` cannot contribute findings CI can never reproduce.
    Untracked-but-not-ignored files are included for the same reason
    ``scripts/discover_templates.sh`` includes them: a template a contributor has
    added and not yet committed is exactly the one this gate needs to see.
    """
    found = []
    for rel in gate_premises.tracked_files("*.yaml", "*.yml", include_untracked=True):
        try:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not re.search(r"^AWSTemplateFormatVersion", text, re.M):
            continue
        if LOG_LEVEL in _parameters(rel):
            found.append(rel)
    return tuple(found)


def _exempt_template_dirs() -> dict[str, str]:
    """``{template path: exempting directory}`` for the exempt feature stacks."""
    return {f"{d}/template.yaml": d for d in LOG_LEVEL_DEFAULT_EXEMPT}


def _enforced_templates() -> tuple[str, ...]:
    exempt = _exempt_template_dirs()
    return tuple(t for t in _templates_declaring_log_level() if t not in exempt)


ENFORCED = _enforced_templates()


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


@pytest.mark.parametrize("rel_path", ENFORCED)
def test_log_level_defaults_to_warn(rel_path: str) -> None:
    param = _parameters(rel_path).get("LogLevel")
    assert param is not None, f"{rel_path} declares no LogLevel parameter"

    if "Default" not in param:
        # No default at all: CloudFormation requires the caller to supply a value,
        # so there is no unsafe default to regress from. Accepted, not exempted --
        # adding `Default: INFO` here later still fails this test.
        return

    default = param["Default"]
    assert default == SAFE_DEFAULT, (
        f"{rel_path}: LogLevel Default is {default!r}, expected {SAFE_DEFAULT!r}. "
        "INFO and DEBUG can emit presigned URLs, document contents and PII to "
        "CloudWatch (AppSec finding #9)."
    )


def test_the_root_template_pins_the_safe_default_explicitly() -> None:
    """The root's default is a product promise, so absence is not good enough here.

    Everything below the root inherits this value, and a customer who never touches
    the parameter gets it. The test above tolerates a missing ``Default`` because an
    absent default cannot be unsafe; for the root, an absent default would also mean
    every deployment had to name a level, which is not what we publish.
    """
    default = _parameters(ROOT_TEMPLATE)[LOG_LEVEL].get("Default")
    assert default == SAFE_DEFAULT, (
        f"{ROOT_TEMPLATE}: LogLevel Default is {default!r}, expected {SAFE_DEFAULT!r}"
    )


def test_every_template_declaring_log_level_is_enforced_or_exempt() -> None:
    """Universe closure: no template may sit outside both sets.

    This is the assertion the previous prose comment could not make. The enforced
    set was a hardcoded tuple of six and the exclusion was a sentence, so seven
    templates were in neither -- and the gate looked exactly as green as it does
    now. Deriving the universe is what makes the exemption list trustworthy: every
    member of it was put there by someone who had to write down a reason.
    """
    universe = set(_templates_declaring_log_level())
    assert len(universe) >= 12, (
        f"suspiciously few templates declare {LOG_LEVEL}: {sorted(universe)}. "
        "Discovery returning a subset would make this whole module vacuous."
    )

    exempt = _exempt_template_dirs()
    unaccounted = sorted(universe - set(ENFORCED) - set(exempt))
    assert not unaccounted, (
        f"these templates declare {LOG_LEVEL} but are neither enforced nor exempt: "
        f"{unaccounted}"
    )

    vanished = sorted(set(exempt) - universe)
    assert not vanished, (
        f"LOG_LEVEL_DEFAULT_EXEMPT names {vanished}, which no longer declares "
        f"{LOG_LEVEL}. A dead exemption is a standing licence for whatever next "
        "occupies that path -- delete the entry."
    )


@pytest.mark.parametrize("feature_dir", sorted(LOG_LEVEL_DEFAULT_EXEMPT))
def test_each_exempt_feature_has_a_manifest_that_pins_log_level(
    feature_dir: str,
) -> None:
    """The exemption's premise, evaluated for ONE member at a time.

    Read as an aggregate -- "do the feature stacks pin this in their manifests?" --
    the old comment was true enough to pass review. Per member it is false for a
    directory with no manifest, and that was the member whose template default a
    human actually got. So this asserts per member, and the failure names the
    member rather than the set.
    """
    holds, explanation = gate_premises.installer_manifest_pins_parameter(
        feature_dir, LOG_LEVEL
    )
    assert holds, (
        f"{feature_dir} is exempt from the {LOG_LEVEL} default check on the stated "
        f"ground that its installer manifest supplies the value, but {explanation}. "
        f"Either fix the template's own Default (to {SAFE_DEFAULT!r}) and drop the "
        "exemption, or correct the manifest."
    )


@pytest.mark.parametrize("feature_dir", sorted(LOG_LEVEL_DEFAULT_EXEMPT))
def test_no_exempt_feature_is_a_nested_stack_of_the_root(feature_dir: str) -> None:
    """A nested stack receives the root's value, so the manifest premise cannot apply.

    This is the same structural mistake that put a nested stack into an X-Ray
    exemption list on the ground that it was independently deployed. Checking it
    here costs one call and closes that shape for this list too.
    """
    holds, explanation = gate_premises.not_a_nested_stack_of_parent(feature_dir)
    assert holds, (
        f"{feature_dir} is exempt as an independently installed feature, but "
        f"{explanation}. An installer manifest does not enter into it."
    )


@pytest.mark.parametrize("rel_path", ENFORCED)
def test_safe_default_is_an_allowed_value(rel_path: str) -> None:
    """A Default outside AllowedValues fails at deploy time, not at lint time.

    A template with no ``AllowedValues`` accepts any string, so there is nothing
    to check for it (``nested/multi-doc-discovery`` is one).
    """
    allowed = _parameters(rel_path)[LOG_LEVEL].get("AllowedValues")
    if allowed is None:
        return
    assert SAFE_DEFAULT in allowed, f"{rel_path}: {SAFE_DEFAULT} not in {allowed}"


def test_scaffold_manifest_pins_safe_default() -> None:
    """The installer passes ``feature.yaml`` ``defaultParameters``, not the template Default.

    So for a feature stack the manifest pin is the value that actually reaches
    CloudFormation; guarding only the scaffold template's ``Default`` would let a
    revert of the pin ship every newly scaffolded feature at ``INFO``.
    """
    manifest = yaml.safe_load(
        (REPO_ROOT / "feature-platform/feature-template/feature.yaml").read_text()
    )
    pinned = (manifest.get("defaultParameters") or {}).get(LOG_LEVEL)
    assert pinned == SAFE_DEFAULT, (
        f"feature-template/feature.yaml pins defaultParameters.LogLevel={pinned!r}; "
        f"expected {SAFE_DEFAULT!r} so newly scaffolded features start safe."
    )


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
