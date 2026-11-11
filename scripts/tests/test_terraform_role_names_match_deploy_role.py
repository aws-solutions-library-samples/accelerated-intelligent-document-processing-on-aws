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

What this covers is a NEW deployment, which is what every CI run makes. The module
keeps a role's generated name for the role's lifetime (`ignore_changes` on `name`
and `name_prefix`, because renaming a role replaced resources that reference it),
so an existing deployment upgraded to this module keeps whatever names its roles
were created with, Terraform's `terraform-<timestamp>` names included.
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


def _interpolation_end(text: str, start: int) -> int:
    """Index just past the ``}`` closing the ``${`` at ``start``, skipping quoted
    strings (which may hold interpolations of their own)."""
    depth, i, quoted = 0, start + 2, False
    while i < len(text):
        ch = text[i]
        if quoted:
            if ch == "\\":
                i += 1
            elif ch == '"':
                quoted = False
            elif text.startswith("${", i):
                i = _interpolation_end(text, i) - 1
        elif ch == '"':
            quoted = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                return i + 1
            depth -= 1
        i += 1
    raise ValueError(f"unterminated interpolation in {text!r}")


_SUBSTR = re.compile(r'^substr\("(?P<inner>.*)", 0, (?P<length>\d+)\)$', re.S)


def _render_template(literal: str) -> tuple[str, bool]:
    """(rendered text, complete) for an HCL template body, CI stack name filled in.

    Understands the two interpolations the translator writes at the start of a
    role name: ``${var.stack_name}`` and ``${substr("<template>", 0, N)}``. At
    any other interpolation the rendering stops there and ``complete`` is False:
    what follows (a hash, a timestamp suffix) can only make the name longer, not
    change how it starts.
    """
    out, i = [], 0
    while i < len(literal):
        if literal.startswith("${", i):
            end = _interpolation_end(literal, i)
            body = literal[i + 2 : end - 1].strip()
            if body == "var.stack_name":
                out.append(CI_STACK)
            elif match := _SUBSTR.match(body):
                inner, complete = _render_template(match.group("inner"))
                length = int(match.group("length"))
                if not complete and len(inner) < length:
                    out.append(inner)
                    return "".join(out), False
                out.append(inner[:length])
            else:
                return "".join(out), False
            i = end
        else:
            out.append(literal[i])
            i += 1
    return "".join(out), True


def _rendered_prefix(expression: str) -> str | None:
    """The literal start of a name expression, rendered for the CI stack name.

    Only a quoted template is read; anything else (a function call, a bare
    reference) returns None, which the test reports rather than guesses at.
    """
    if not (expression.startswith('"') and expression.endswith('"')):
        return None
    return _render_template(expression[1:-1])[0]


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


@pytest.mark.parametrize(
    ("expression", "prefix"),
    [
        ('"${var.stack_name}BastionHost-Role"', f"{CI_STACK}BastionHost-Role"),
        (
            '"${substr("${var.stack_name}-CfnBridge", 0, 55)}-'
            '${substr(md5("${var.stack_name}-CfnBridge"), 0, 8)}"',
            f"{CI_STACK}-CfnBridge-",
        ),
        (
            '"${substr("${var.stack_name}-SomeLongLogicalIdRole", 0, 20)}-x"',
            f"{CI_STACK}-Some"[:20] + "-x",
        ),
        ('"cfnbridge-"', "cfnbridge-"),
        ('"${local.prefix}-x"', ""),
        ('format("%s", var.stack_name)', None),
    ],
)
def test_the_name_renderer(expression, prefix):
    """The renderer the check above relies on, against the shapes it reads."""
    assert _rendered_prefix(expression) == prefix


def test_the_patterns_are_read_from_the_deploy_role():
    """Non-vacuity for the check above: the globs it uses exist and are bounded."""
    for action in ("iam:CreateRole", "iam:PassRole"):
        globs = _role_name_globs(action)
        assert all(g.endswith(f"{MANAGED_PREFIX}*") for g in globs), globs
        assert not any(fnmatch.fnmatchcase("cfnbridge-x", g) for g in globs)
        assert any(fnmatch.fnmatchcase(f"{CI_STACK}-Role-abc", g) for g in globs)
