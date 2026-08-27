# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Static regression coverage for IAM Permissions Boundary propagation.

Background
----------
Some customer accounts enforce a Service Control Policy (SCP) that *requires*
every IAM role to be created with a Permissions Boundary. To support them, the
templates take an optional ``PermissionsBoundaryArn`` parameter and, when set,
attach it to every IAM role they create.

A real customer bug prompted these tests: the Feature Platform nested stack
(``feature-platform/main-stack-extensions``) neither declared the parameter nor
put a boundary on its ``FeaturePlatformLambdaRole``, AND the main template
didn't forward ``PermissionsBoundaryArn`` into that nested stack. In an
SCP-enforced account, ``iam:CreateRole`` was denied and the nested stack rolled
back on creation. Sibling feature templates had the same gap on their
SAM-auto-role functions (``FeatureApiFunction`` / ``ClaimStatusHookFunction``).

These tests fail loudly if any deployed-stack template regresses on three
invariants:

1. Every ``AWS::IAM::Role`` (and every ``AWS::Serverless::Function`` that relies
   on a SAM-auto-generated role) sets ``PermissionsBoundary``.
2. Every template that creates IAM roles declares the ``PermissionsBoundaryArn``
   parameter and the ``HasPermissionsBoundary`` condition.
3. Every nested ``AWS::CloudFormation::Stack`` forwards ``PermissionsBoundaryArn``
   to any child template that declares that parameter.

The check is deliberately static (no AWS, no deploy) so it runs in the fast
unit gate and catches the regression at author time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from idp_sdk._core.cfn_yaml import load_cfn_template

pytestmark = pytest.mark.unit


def _repo_root() -> Path:
    """Walk up until we find the repo root (contains the main template.yaml)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "template.yaml").is_file() and (parent / "publish.py").is_file():
            return parent
    raise RuntimeError("Could not locate repo root containing template.yaml")


# Every template that is part of a *deployed* stack (the main stack, its nested
# stacks, and the feature-platform templates a customer installs). Bootstrap /
# CI-only templates under scripts/ and iam-roles/ are intentionally excluded --
# they are not deployed by the accelerator and have their own lifecycle.
DEPLOYED_TEMPLATES = [
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/bedrockkb/template.yaml",
    "nested/multi-doc-discovery/template.yaml",
    "nested/api-resolvers/template.yaml",
    "feature-platform/main-stack-extensions/template.yaml",
    "feature-platform/feature-template/template.yaml",
    "feature-platform/sample-feature/template.yaml",
    "feature-platform/sample-health-insurance-review/template.yaml",
    # Deployed standalone into an AWS Marketplace SELLER account rather than by
    # the main stack, so `test_nested_stacks_forward_permissions_boundary` does
    # not apply to it — but it creates an IAM role, so the boundary assertions
    # do. It was omitted at first and the suite passed anyway, giving false
    # assurance; that is exactly the failure mode this list has to avoid.
    "feature-platform/seller-entitlement-service/template.yaml",
]


def _load(rel_path: str) -> dict:
    # Intrinsics come back as {"!Tag": value} so the assertions below can tell a
    # !Ref from a literal; see idp_sdk._core.cfn_yaml for the safety rationale.
    return load_cfn_template(_repo_root() / rel_path)


def _resources(template: dict) -> dict:
    return {
        name: body
        for name, body in (template.get("Resources") or {}).items()
        if isinstance(body, dict)
    }


def _needs_boundary(body: dict) -> bool:
    """True if this resource creates an IAM role that must carry a boundary.

    - AWS::IAM::Role: always creates a role.
    - AWS::Serverless::Function WITHOUT an explicit ``Role``: SAM generates an
      execution role for it, so the ``PermissionsBoundary`` property must be set
      on the function to flow onto that generated role. A function that points at
      an explicit ``Role`` (!GetAtt SomeRole.Arn) inherits that role's boundary
      and must NOT set its own.
    """
    rtype = body.get("Type")
    props = body.get("Properties") or {}
    if rtype == "AWS::IAM::Role":
        return True
    if rtype == "AWS::Serverless::Function":
        return "Role" not in props
    return False


def test_deployed_templates_are_discoverable():
    """Guard: every template we intend to check actually exists on disk."""
    root = _repo_root()
    missing = [t for t in DEPLOYED_TEMPLATES if not (root / t).is_file()]
    assert not missing, (
        "DEPLOYED_TEMPLATES lists templates that no longer exist: "
        f"{missing}. Update this test's template list."
    )


@pytest.mark.parametrize("rel_path", DEPLOYED_TEMPLATES)
def test_every_iam_role_sets_permissions_boundary(rel_path):
    """Every role-creating resource attaches the optional permissions boundary."""
    template = _load(rel_path)
    offenders = []
    for name, body in _resources(template).items():
        if not _needs_boundary(body):
            continue
        props = body.get("Properties") or {}
        if "PermissionsBoundary" not in props:
            offenders.append(f"{name} ({body.get('Type')})")

    assert not offenders, (
        f"{rel_path}: these role-creating resources are missing a "
        "'PermissionsBoundary' property. In SCP-enforced accounts their "
        "IAM role creation is denied and the stack rolls back. Add:\n"
        "    PermissionsBoundary: !If [HasPermissionsBoundary, "
        "!Ref PermissionsBoundaryArn, !Ref AWS::NoValue]\n"
        f"Offenders: {offenders}"
    )


@pytest.mark.parametrize("rel_path", DEPLOYED_TEMPLATES)
def test_templates_with_roles_declare_boundary_plumbing(rel_path):
    """A template that creates roles must declare the param + condition it uses."""
    template = _load(rel_path)
    creates_roles = any(
        _needs_boundary(body) for name, body in _resources(template).items()
    )
    if not creates_roles:
        pytest.skip(f"{rel_path} creates no IAM roles")

    params = template.get("Parameters") or {}
    conditions = template.get("Conditions") or {}
    assert "PermissionsBoundaryArn" in params, (
        f"{rel_path} creates IAM roles but does not declare the "
        "'PermissionsBoundaryArn' parameter."
    )
    assert "HasPermissionsBoundary" in conditions, (
        f"{rel_path} creates IAM roles but does not declare the "
        "'HasPermissionsBoundary' condition guarding the boundary."
    )


def _template_url_to_source(url: str) -> str:
    """Map a nested-stack TemplateURL to its committed source template.

    Nested stacks reference the built artifact
    (``./nested/foo/.aws-sam/packaged.yaml``); the committed source lives at
    ``nested/foo/template.yaml``.
    """
    cleaned = url.replace("./", "", 1) if url.startswith("./") else url
    base = cleaned.split("/.aws-sam/")[0]
    return f"{base}/template.yaml"


def test_nested_stacks_forward_permissions_boundary():
    """Every nested stack forwards PermissionsBoundaryArn to children that need it.

    This is the exact defect that broke FeaturePlatformStack: the child template
    declared (or needed) the parameter, but the parent's
    ``AWS::CloudFormation::Stack`` resource did not pass it through, so the child
    silently got the empty-string default and created boundary-less roles.
    """
    root = _repo_root()
    main = _load("template.yaml")
    offenders = []
    for name, body in _resources(main).items():
        if body.get("Type") != "AWS::CloudFormation::Stack":
            continue
        props = body.get("Properties") or {}
        url = props.get("TemplateURL")
        if not isinstance(url, str):
            continue
        child_src = _template_url_to_source(url)
        if not (root / child_src).is_file():
            # Child template isn't a committed source we can introspect; skip.
            continue
        child_params = _load(child_src).get("Parameters") or {}
        if "PermissionsBoundaryArn" not in child_params:
            continue  # child doesn't take the param -> nothing to forward
        passed = props.get("Parameters") or {}
        if "PermissionsBoundaryArn" not in passed:
            offenders.append(f"{name} -> {child_src}")

    assert not offenders, (
        "These nested stacks accept a 'PermissionsBoundaryArn' parameter but the "
        "parent template.yaml does not forward it (child gets the empty default "
        "and creates boundary-less roles -> fails in SCP-enforced accounts). Add "
        "'PermissionsBoundaryArn: !Ref PermissionsBoundaryArn' to the nested "
        f"stack's Parameters. Offenders: {offenders}"
    )
