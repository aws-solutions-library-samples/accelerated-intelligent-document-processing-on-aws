# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Python deployers vs the templates they deploy: required parameters covered.

``test_nested_stack_parameters.py`` asserts this contract for CFN parent →
nested stack. Nothing asserted it for the other direction the repo deploys
templates in — a **Python script** calling ``cloudformation:CreateStack`` with a
``TemplateBody`` read from this tree — and that gap shipped a broken CI for every
run between two releases:

``iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml``
gained a *required* ``CreatedRolePermissionsBoundaryArn`` (issue #927, so that
``iam:CreateRole`` could be gated on ``iam:PermissionsBoundary`` and the role
would stop being a transitive account administrator). The template change was
correct and well tested — ``scripts/sdlc/validate_service_role_permissions.py``
and ``scripts/tests/test_iam_privilege_escalation.py`` both defend the policy's
shape. But ``create_iam_resources()`` in ``scripts/sdlc/codebuild_deployment.py``
still called ``create_stack()`` with no ``Parameters`` at all, so every
integration-test run died at step 0 with::

    ValidationError: Parameters: [CreatedRolePermissionsBoundaryArn] must have values

before creating anything — and because the main stack never existed, the failure
summary reported "stack does not exist" rather than the real cause.

The security property was gated; the caller contract was not. This test gates the
caller contract, in both directions:

* a required template parameter (no ``Default``) no caller supplies →
  ``Parameters: [X] must have values`` at CreateStack;
* a parameter a caller passes that the template does not declare →
  ``Parameters: [X] do not exist in the template``.

It also fails when a **new** script calls ``create_stack``/``update_stack``
without being registered below, so the next template-deploying script cannot
quietly reintroduce the gap.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


# Every script under scripts/ that deploys a template from this tree.
#
# ``static=True`` means the script's parameter list is literal enough to compare
# exactly (every ParameterKey is a string literal). ``static=False`` means the
# list is built at runtime — a loop or a comprehension over a dict — so the exact
# set cannot be read from the source; those get the weaker check that each
# required parameter name at least appears somewhere in the file, which still
# catches "template grew a required parameter and its caller never heard".
#
# Adding a script that deploys a template? Add it here. test_registry_is_complete
# fails until you do.
DEPLOYERS = [
    pytest.param(
        "scripts/sdlc/codebuild_deployment.py",
        "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml",
        True,
        id="codebuild_deployment→cfn-service-role",
    ),
    pytest.param(
        "scripts/deploy-vpc-endpoints.py",
        "scripts/vpc-endpoints.yaml",
        # skip_params is looped over to add CreateXxx entries at runtime.
        False,
        id="deploy-vpc-endpoints→vpc-endpoints",
    ),
    pytest.param(
        "scripts/security/live_checks/oidc_provider/deploy.py",
        "scripts/security/live_checks/oidc_provider/template.yaml",
        # Parameters come from a comprehension over rsa_parameters().
        False,
        id="oidc_provider→oidc-template",
    ),
]

REGISTERED_FILES = {p.values[0] for p in DEPLOYERS}


def _template_parameters(rel_path: str) -> dict[str, dict]:
    doc = yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}
    return doc.get("Parameters") or {}


def _required_parameters(rel_path: str) -> set[str]:
    """Parameters with no Default — CloudFormation rejects CreateStack without them."""
    return {
        name
        for name, spec in _template_parameters(rel_path).items()
        if isinstance(spec, dict) and "Default" not in spec
    }


def _literal_parameter_keys(rel_path: str) -> set[str]:
    """String literals used as a "ParameterKey" value anywhere in the file."""
    tree = ast.parse((REPO_ROOT / rel_path).read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "ParameterKey"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                keys.add(value.value)
    return keys


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template", "static"), DEPLOYERS)
def test_required_parameters_are_supplied(script: str, template: str, static: bool):
    """Each required template parameter is supplied by the script that deploys it."""
    required = _required_parameters(template)
    if not required:
        pytest.skip(f"{template} has no parameters without defaults")

    if static:
        supplied = _literal_parameter_keys(script)
        missing = required - supplied
        assert not missing, (
            f"{script} deploys {template} without required parameter(s) "
            f"{sorted(missing)}. CloudFormation will reject CreateStack with "
            f'"Parameters: {sorted(missing)} must have values" before creating '
            "anything. Add them to the Parameters list."
        )
        return

    source = (REPO_ROOT / script).read_text()
    missing = {name for name in required if name not in source}
    assert not missing, (
        f"{script} deploys {template}, which requires {sorted(missing)}, but that "
        f"name does not appear in the script at all. Its parameter list is built "
        "at runtime so this check is by-name only — supply the parameter."
    )


@pytest.mark.unit
@pytest.mark.parametrize(("script", "template", "static"), DEPLOYERS)
def test_supplied_parameters_exist_in_template(
    script: str, template: str, static: bool
):
    """No script passes a parameter the template does not declare."""
    if not static:
        pytest.skip(f"{script} builds its parameter list at runtime")

    declared = set(_template_parameters(template))
    unknown = _literal_parameter_keys(script) - declared
    assert not unknown, (
        f"{script} passes {sorted(unknown)} to {template}, which does not declare "
        f'it. CloudFormation will reject the call with "Parameters: '
        f'{sorted(unknown)} do not exist in the template".'
    )


@pytest.mark.unit
def test_registry_is_complete():
    """Every template-deploying script under scripts/ is registered above.

    Without this, the next script to call create_stack silently escapes the
    parameter-coverage check — the same way create_iam_resources did.
    """
    unregistered = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or path.name.startswith("test_"):
            continue
        if rel in REGISTERED_FILES:
            continue
        source = path.read_text()
        if ".create_stack(" in source or ".update_stack(" in source:
            unregistered.append(rel)

    assert not unregistered, (
        "These scripts deploy a CloudFormation stack but are not in DEPLOYERS, so "
        f"nothing checks that they supply the template's required parameters: "
        f"{unregistered}. Add an entry (see the comment on DEPLOYERS)."
    )
