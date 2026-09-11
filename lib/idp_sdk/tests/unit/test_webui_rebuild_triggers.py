# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static regression coverage for the Web UI rebuild trigger.

Background
----------
The Web UI reads a number of settings as ``import.meta.env.VITE_*``. Vite
substitutes those **textually at build time** (``npm run build``) and freezes the
values into the JS bundle uploaded to ``WebUIBucket``. The values come from the
``EnvironmentVariables`` of ``UICodeBuildProject`` in the main template, several
of which derive from a CloudFormation *parameter*.

The load-bearing subtlety: mutating a CodeBuild project definition does **not**
run a build. The only thing that runs one is the ``CodeBuildRun`` custom resource
(which calls ``codebuild:StartBuild``). CloudFormation invokes a custom resource
on UPDATE only when one of its *properties* changes. So if a build-time-baked
parameter is not reflected in ``CodeBuildRun``'s properties, a parameter-only
stack update:

  1. correctly updates ``UICodeBuildProject``'s env var,
  2. produces no property diff on ``CodeBuildRun`` -> no Update event, no build,
  3. leaves the previously-built bundle in place -> the change silently has no
     effect in the UI.

A fresh stack *create* always runs the build once, which is why this gate is
invisible to create-only end-to-end testing.

Regression that prompted these tests: adding ``AllowedSignUpEmailDomain`` to an
existing stack correctly flipped the backend (UserPool
``AllowAdminCreateUserOnly=false`` plus the ``PreSignUp`` domain-verify Lambda)
but the login page kept hiding the Sign Up tab, because
``VITE_SHOULD_HIDE_SIGN_UP`` was still ``"true"`` in the stale bundle.

The tests below derive the *required* set of parameters from the template itself
(every parameter referenced by a ``VITE_*``/build-affecting CodeBuild env var),
so a newly-added baked parameter fails the test rather than quietly regressing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Set

import pytest

from idp_sdk._core.cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


@pytest.fixture(scope="module")
def template() -> dict:
    # Intrinsics come back as {"!Tag": value}; see idp_sdk._core.cfn_yaml for
    # the safety rationale.
    return load_cfn_template(_repo_root() / "template.yaml")


# CodeBuild env vars that affect the *built artifact* but are not named VITE_*.
# WEB_UI_HOSTING selects the CloudFront-invalidation branch of the buildspec.
_NON_VITE_BUILD_AFFECTING = {"WEB_UI_HOSTING"}

# Parameters that reach a build-time env var but must NOT force a rebuild,
# with the reason. Keep this list tiny and justified.
_EXEMPT: dict[str, str] = {}


def _collect_names(node: Any, refs: Set[str], conditions: Set[str]) -> None:
    """Collect referenced names under `node`.

    Adds to `refs` every `!Ref <Name>` and every `${Name}` inside a `!Sub`, and
    to `conditions` every condition name reached via `!If` / `!Condition`.
    Following conditions matters: a parameter often reaches an env var *only*
    through a condition (``VITE_SHOULD_HIDE_SIGN_UP`` is
    ``!If [ShouldAllowSignUpEmailDomain, "false", "true"]`` — it never names
    ``AllowedSignUpEmailDomain`` directly). Callers filter `refs` against the
    template's declared Parameters, so pseudo-parameters and resource
    references drop out.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "!Ref" and isinstance(value, str):
                refs.add(value)
            elif key == "!Condition" and isinstance(value, str):
                conditions.add(value)
            elif key == "!If" and isinstance(value, list) and value:
                # First element is the condition name, not a value to walk.
                if isinstance(value[0], str):
                    conditions.add(value[0])
                for item in value[1:]:
                    _collect_names(item, refs, conditions)
            else:
                _collect_names(value, refs, conditions)
    elif isinstance(node, list):
        for item in node:
            _collect_names(item, refs, conditions)
    elif isinstance(node, str):
        # !Sub "${Foo}" also references Foo.
        for match in re.findall(r"\$\{([A-Za-z0-9:.]+)\}", node):
            refs.add(match)


def _resolve(template: dict, node: Any) -> Set[str]:
    """Every declared Parameter that `node` depends on, through conditions."""
    declared = set(template.get("Parameters", {}))
    all_conditions = template.get("Conditions", {})

    refs: Set[str] = set()
    pending: Set[str] = set()
    _collect_names(node, refs, pending)

    # Transitively expand condition names (conditions reference conditions).
    seen: Set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in all_conditions:
            continue
        seen.add(name)
        _collect_names(all_conditions[name], refs, pending)

    return refs & declared


def _baked_parameters(template: dict) -> Set[str]:
    """Parameters whose values Vite freezes into the bundle at build time."""
    env_vars = template["Resources"]["UICodeBuildProject"]["Properties"]["Environment"][
        "EnvironmentVariables"
    ]

    baked: Set[str] = set()
    for env_var in env_vars:
        name = env_var["Name"]
        if not (name.startswith("VITE_") or name in _NON_VITE_BUILD_AFFECTING):
            continue
        baked |= _resolve(template, env_var.get("Value"))

    return baked - set(_EXEMPT)


def _rebuild_trigger_parameters(template: dict) -> Set[str]:
    """Parameters referenced by the CodeBuildRun custom resource's properties."""
    return _resolve(template, template["Resources"]["CodeBuildRun"]["Properties"])


class TestCodeBuildRunIsSensitiveToBakedParameters:
    def test_every_baked_parameter_forces_a_rebuild(self, template):
        """The load-bearing assertion.

        Any CFN parameter baked into the bundle must appear in CodeBuildRun's
        properties, or changing it on an existing stack will not rebuild the UI
        and the change will silently not take effect.
        """
        baked = _baked_parameters(template)
        triggers = _rebuild_trigger_parameters(template)

        missing = sorted(baked - triggers)
        assert not missing, (
            "These CloudFormation parameters are baked into the Web UI bundle at "
            "build time but are NOT referenced by the CodeBuildRun custom "
            f"resource: {missing}. Changing one on an existing stack updates "
            "UICodeBuildProject's env var but produces no property diff on "
            "CodeBuildRun, so CloudFormation never re-invokes it, CodeBuild "
            "never runs, and the stale bundle stays deployed — the change "
            "silently has no effect in the UI. Add each to the UIBuildInputs "
            "property of CodeBuildRun (or add a justified entry to _EXEMPT)."
        )

    def test_the_signup_regression_parameter_is_covered(self, template):
        """Explicit guard for the specific bug this test file was written for."""
        assert "AllowedSignUpEmailDomain" in _rebuild_trigger_parameters(template), (
            "AllowedSignUpEmailDomain no longer triggers a UI rebuild; adding it "
            "to an existing stack will flip the backend but leave the login page "
            "hiding the Sign Up tab (stale VITE_SHOULD_HIDE_SIGN_UP)"
        )

    def test_baked_parameter_detection_actually_finds_something(self, template):
        """Guard the guard: if the detector silently returns {} it proves nothing."""
        baked = _baked_parameters(template)
        assert "AllowedSignUpEmailDomain" in baked, (
            "the baked-parameter detector did not find AllowedSignUpEmailDomain "
            "via VITE_SHOULD_HIDE_SIGN_UP — the detector is broken, so the "
            "coverage assertion above is vacuous"
        )
        # Sanity floor: several parameters are known to be baked in.
        assert len(baked) >= 4, f"suspiciously few baked parameters found: {baked}"

    def test_runtime_resolved_settings_are_not_rebuild_triggers(self, template):
        """Don't force needless ~30-minute UI rebuilds.

        ConsoleTitle / DefaultFeatureId reach the UI through the SSM settings
        parameter (read at runtime by useParameterStore), not through a VITE_*
        build-time constant, so they must not be listed on CodeBuildRun.
        """
        triggers = _rebuild_trigger_parameters(template)
        for param in ("ConsoleTitle", "DefaultFeatureId"):
            assert param not in triggers, (
                f"{param} is resolved at runtime from the SSM settings "
                "parameter, so listing it on CodeBuildRun only forces an "
                "unnecessary UI rebuild on every change"
            )
