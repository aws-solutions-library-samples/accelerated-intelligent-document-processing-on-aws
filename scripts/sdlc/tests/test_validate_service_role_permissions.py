"""Unit tests for the CI service-role permission validator (issue #632).

`scripts/sdlc/validate_service_role_permissions.py` runs in the GitLab
`security_review`-adjacent stage on every MR to develop. Before this suite it
had been failing OPEN: the IAM half of the check crashed on the first
`Fn::If`-wrapped inline policy it met, and the broad `except` printed the
AttributeError and returned an EMPTY action set — so "no missing IAM
permissions" meant "no IAM permissions were ever compared". That is why the
missing `iam:UpdateAssumeRolePolicy` reached a release.

These tests pin both halves: the walk must survive intrinsics, and it must not
silently degrade to an empty result.
"""

from __future__ import annotations

import pytest
import validate_service_role_permissions as validator

MAIN_TEMPLATE = "template.yaml"
SERVICE_ROLE = (
    "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch):
    """The validator resolves its template paths relative to the repo root."""
    from pathlib import Path

    monkeypatch.chdir(Path(__file__).resolve().parents[3])


def _write(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body)
    return str(path)


# --- The regression: extraction must not silently return nothing --------------
def test_main_template_yields_iam_actions():
    """A non-empty result is the whole precondition for the comparison below."""
    actions = validator.extract_iam_actions_from_template(MAIN_TEMPLATE)
    assert actions, (
        "No actions extracted from template.yaml — the validator is passing "
        "vacuously again (see module docstring)."
    )


def test_intrinsics_in_a_policy_do_not_abort_the_walk(tmp_path):
    """One Fn::If'd policy must not hide the actions of its siblings.

    This is the exact shape that used to zero out the whole scan: CFNLoader
    collapses the `!If` to None, and the old code called `.get` on it.
    """
    template = _write(
        tmp_path,
        "conditional.yaml",
        """
Resources:
  RoleWithConditionalPolicy:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - !If
          - SomeCondition
          - PolicyName: Conditional
            PolicyDocument:
              Statement:
                - Effect: Allow
                  Action: s3:GetObject
                  Resource: '*'
          - !Ref AWS::NoValue
        - PolicyName: Unconditional
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action:
                  - iam:PassRole
                Resource: '*'
""",
    )
    assert validator.extract_iam_actions_from_template(template) == {"iam:PassRole"}


def test_statement_list_as_intrinsic_is_skipped_not_fatal(tmp_path):
    template = _write(
        tmp_path,
        "if-statements.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: Whole statement list is an Fn::If
          PolicyDocument:
            Statement: !If [C, [{Effect: Allow, Action: 's3:*'}], []]
  Other:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action: iam:GetRole
""",
    )
    assert validator.extract_iam_actions_from_template(template) == {"iam:GetRole"}


def test_unparseable_template_raises_instead_of_reporting_success(tmp_path):
    """Fail the gate loudly; never degrade to 'nothing required'."""
    template = _write(tmp_path, "broken.yaml", "Resources: {: not: valid: yaml")
    with pytest.raises(Exception):
        validator.extract_iam_actions_from_template(template)


# --- Control-plane derivation -------------------------------------------------
def test_control_plane_actions_track_the_role_features_declared(tmp_path):
    template = _write(
        tmp_path,
        "roles.yaml",
        """
Resources:
  Plain:
    Type: AWS::IAM::Role
    Properties:
      AssumeRolePolicyDocument: {}
  WithInline:
    Type: AWS::IAM::Role
    Properties:
      Policies: []
  WithManaged:
    Type: AWS::IAM::Role
    Properties:
      ManagedPolicyArns: []
  WithBoundary:
    Type: AWS::IAM::Role
    Properties:
      PermissionsBoundary: !If [HasPermissionsBoundary, !Ref Arn, !Ref 'AWS::NoValue']
""",
    )
    derived = validator.extract_cfn_control_plane_iam_actions(template)

    # Trust policies are only mutable via this action; it is the #632 regression.
    assert "iam:UpdateAssumeRolePolicy" in derived
    assert validator.ROLE_LIFECYCLE_ACTIONS <= derived
    assert validator.INLINE_POLICY_ACTIONS <= derived
    assert validator.MANAGED_POLICY_ATTACH_ACTIONS <= derived
    # Derived through an Fn::If — property presence is what matters, so this
    # works even though the loader drops the intrinsic's value.
    assert validator.BOUNDARY_ACTIONS <= derived


def test_control_plane_actions_are_not_demanded_without_roles(tmp_path):
    """The requirement is derived, not hardcoded: no roles, no role actions."""
    template = _write(
        tmp_path,
        "no-roles.yaml",
        """
Resources:
  Bucket:
    Type: AWS::S3::Bucket
""",
    )
    assert validator.extract_cfn_control_plane_iam_actions(template) == set()


def test_shipped_service_role_satisfies_the_main_stack():
    """End-to-end: the role we ship covers what our templates need."""
    required_wildcards, required_iam = (
        validator.extract_required_permissions_from_templates(
            [
                MAIN_TEMPLATE,
                "patterns/unified/template.yaml",
                "nested/bedrockkb/template.yaml",
            ]
        )
    )
    assert "iam:UpdateAssumeRolePolicy" in required_iam

    missing_wildcards, missing_iam = validator.validate_permissions(
        validator.extract_permissions_from_role(SERVICE_ROLE),
        required_wildcards,
        required_iam,
        validator.extract_iam_permissions_from_role(SERVICE_ROLE),
    )
    assert not missing_wildcards
    assert not missing_iam


def test_missing_action_is_reported():
    """Mutation guard: the comparison must actually flag an absent action."""
    granted = validator.extract_iam_permissions_from_role(SERVICE_ROLE)
    granted.discard("iam:UpdateAssumeRolePolicy")
    _, missing_iam = validator.validate_permissions(
        set(), set(), {"iam:UpdateAssumeRolePolicy"}, granted
    )
    assert missing_iam == {"iam:UpdateAssumeRolePolicy"}


def test_blanket_iam_wildcard_satisfies_specific_requirements():
    _, missing_iam = validator.validate_permissions(
        set(), set(), {"iam:UpdateAssumeRolePolicy"}, {"iam:*"}
    )
    assert missing_iam == set()
