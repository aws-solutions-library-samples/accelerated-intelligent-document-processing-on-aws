# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every IAM role the Terraform module creates is one the CI deploy role may create.

The integration driver's Terraform runs create the deployment as the per-run
`<stack>-iam-TFDeployRole` (terraform_deploy_role_template in
scripts/sdlc/codebuild_deployment.py), which carries the CloudFormation service
role's IAM scoping: `iam:CreateRole` and `iam:PassRole` only on roles named with
the managed stack-name prefix, and `iam:CreateRole` only with the run's
permissions boundary attached. A role the module names otherwise (a fixed
`cfnbridge-` prefix, or no name, which Terraform fills with `terraform-<time>`), or
creates without the boundary, is refused at apply time in CI.

So the module is translated offline from `template.yaml`, the way the publish
does it, and every `aws_iam_role` is held to the patterns read from the derived
deploy-role template itself, for a stack named the way CI names one.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TRANSLATOR = REPO_ROOT / "terraform" / "translator"
SERVICE_ROLE_TEMPLATE = (
    REPO_ROOT
    / "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)
#: A stack named the way generate_stack_name names a CI run.
CI_STACK = "idp-0101-000000"
#: What the driver passes as ManagedStackNamePrefix (MANAGED_STACK_NAME_PREFIX).
MANAGED_PREFIX = "idp"


def _driver():
    sdlc = str(REPO_ROOT / "scripts" / "sdlc")
    if sdlc not in sys.path:
        sys.path.insert(0, sdlc)
    import codebuild_deployment

    return codebuild_deployment


def _role_name_globs(action: str) -> list[str]:
    """The role-name globs the deploy role allows ``action`` on, read off it."""
    cbd = _driver()
    assert cbd.MANAGED_STACK_NAME_PREFIX == MANAGED_PREFIX
    doc = cbd.terraform_deploy_role_template(SERVICE_ROLE_TEMPLATE.read_text())
    globs = []
    for policy in doc["Resources"]["TerraformDeployRole"]["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            actions = [actions] if isinstance(actions, str) else actions
            if statement.get("Effect") != "Allow" or action not in actions:
                continue
            resources = statement["Resource"]
            for resource in resources if isinstance(resources, list) else [resources]:
                text = resource["Fn::Sub"] if isinstance(resource, dict) else resource
                text = text.replace("${ManagedStackNamePrefix}", MANAGED_PREFIX)
                globs.append(text.split(":role/", 1)[1])
    assert globs, f"the deploy role grants {action} on no role"
    return globs


def _blocks(text: str, kind: str):
    """(name, body) of each top-level ``resource "<kind>"`` block, brace-matched
    outside quoted strings."""
    for match in re.finditer(rf'^resource "{kind}" "([^"]+)" \{{', text, re.M):
        depth, i, quoted = 1, match.end(), False
        while depth and i < len(text):
            ch = text[i]
            if quoted:
                if ch == "\\":
                    i += 1
                elif ch == '"':
                    quoted = False
            elif ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        yield match.group(1), text[match.end() : i - 1]


def _top_level_attribute(body: str, name: str) -> str | None:
    match = re.search(rf"^  {name}\s+=\s+(.+)$", body, re.M)
    return match.group(1).strip() if match else None


def _rendered_prefix(expression: str) -> str | None:
    """The literal start of a name expression, with the stack name filled in.

    Only a quoted template is read; anything else (a function call, a bare
    reference) returns None, which the test reports rather than guesses at.
    """
    if not expression.startswith('"'):
        return None
    literal = expression[1 : expression.rfind('"')]
    literal = re.sub(r"\$\{[^}]*stack_name[^}]*\}", CI_STACK, literal)
    return literal.split("${", 1)[0]


@pytest.fixture(scope="module")
def module_roles(tmp_path_factory):
    missing = [
        m for m in ("samtranslator", "yaml") if importlib.util.find_spec(m) is None
    ]
    if missing:
        pytest.skip(f"the translator's dependencies are not installed: {missing}")
    out = tmp_path_factory.mktemp("module")
    subprocess.run(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-m",
            "idp_tf.cli",
            "../../template.yaml",
            "--offline",
            "--out",
            str(out),
        ],
        cwd=TRANSLATOR,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    roles = []
    for path in sorted(out.rglob("*.tf")):
        for name, body in _blocks(path.read_text(), "aws_iam_role"):
            roles.append((f"{path.relative_to(out)}:{name}", body))
    assert roles, "the translated module declares no aws_iam_role at all"
    return roles


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PR A (the translator fix) names unnamed roles <stack_name>-<LogicalId>-<hash> "
        "and gives the bridge roles stack-prefixed names and the boundary; until it "
        "is merged into this branch, those roles fall outside the deploy role's "
        "patterns. Remove this marker after that merge."
    ),
)
def test_every_module_role_is_one_the_deploy_role_may_create_and_pass(module_roles):
    create = _role_name_globs("iam:CreateRole")
    pass_ = _role_name_globs("iam:PassRole")
    problems = []
    for where, body in module_roles:
        expression = _top_level_attribute(body, "name") or _top_level_attribute(
            body, "name_prefix"
        )
        if expression is None:
            problems.append(f"{where}: no name, so Terraform names it terraform-<time>")
            continue
        prefix = _rendered_prefix(expression)
        if prefix is None:
            problems.append(f"{where}: name {expression} is not a readable template")
            continue
        probe = prefix + "x"
        if not any(fnmatch.fnmatchcase(probe, g) for g in create):
            problems.append(f"{where}: {prefix!r}... is outside CreateRole {create}")
        if not any(fnmatch.fnmatchcase(probe, g) for g in pass_):
            problems.append(f"{where}: {prefix!r}... is outside PassRole {pass_}")
        boundary = _top_level_attribute(body, "permissions_boundary") or ""
        if "permissions_boundary_arn" not in boundary:
            problems.append(
                f"{where}: no permissions boundary from the stack parameter"
            )
    assert not problems, "\n".join(problems)


def test_the_patterns_are_read_from_the_deploy_role():
    """Non-vacuity for the check above: the globs it uses exist and are bounded."""
    for action in ("iam:CreateRole", "iam:PassRole"):
        globs = _role_name_globs(action)
        assert all(g.endswith(f"{MANAGED_PREFIX}*") for g in globs), globs
        assert not any(fnmatch.fnmatchcase("cfnbridge-x", g) for g in globs)
        assert any(fnmatch.fnmatchcase(f"{CI_STACK}-Role-abc", g) for g in globs)
