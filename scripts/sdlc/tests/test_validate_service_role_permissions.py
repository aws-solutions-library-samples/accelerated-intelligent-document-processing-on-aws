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


def test_an_instance_profile_demands_its_own_lifecycle_actions(tmp_path):
    """An InstanceProfile is a separate IAM resource type with its own actions.

    The bastion host is the only feature that declares one, so a role that
    covered every AWS::IAM::Role and nothing else deployed every stack except
    that variant — a gap only the operator who enabled it would ever hit.
    """
    template = _write(
        tmp_path,
        "profile.yaml",
        """
Resources:
  Profile:
    Type: AWS::IAM::InstanceProfile
    Properties:
      Roles:
        - !Ref SomeRole
""",
    )
    derived = validator.extract_cfn_control_plane_iam_actions(template)

    assert validator.INSTANCE_PROFILE_ACTIONS <= derived
    # No AWS::IAM::Role here, so the role actions must not be demanded.
    assert "iam:UpdateAssumeRolePolicy" not in derived


def test_the_shipped_role_covers_the_bastion_instance_profile():
    """The grant exists in the template we ship, not just in the requirement."""
    granted = validator.extract_permissions_from_role(validator.SERVICE_ROLE_TEMPLATE)

    missing = {
        action
        for action in validator.INSTANCE_PROFILE_ACTIONS
        if action not in granted
    }
    assert not missing, f"service role is missing {sorted(missing)}"


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


def test_deny_statements_are_not_counted_as_grants(tmp_path):
    """A guardrail Deny must not read as a permission the role holds."""
    template = _write(
        tmp_path,
        "deny.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action: iam:CreateRole
                Resource: '*'
              - Effect: Deny
                Action: iam:CreateUser
                Resource: '*'
""",
    )
    granted = validator.extract_iam_permissions_from_role(template)
    assert granted == {"iam:CreateRole"}


# --- The other direction: is the role BROADER than it needs to be? ------------
# Everything above checks the role is strong enough to deploy. These check it is
# not a transitive account administrator, which is the gap issue #927 reported:
# the shipped role had IAM writes on Resource: "*" and PassRole on "*", and this
# validator reported success.
def test_iam_write_on_all_resources_without_boundary_is_reported(tmp_path):
    template = _write(
        tmp_path,
        "unbounded.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Unbounded
                Effect: Allow
                Action:
                  - iam:CreateRole
                  - iam:AttachRolePolicy
                Resource: '*'
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any("iam:permissionsboundary" in f for f in findings), findings


def test_iam_write_on_all_resources_with_boundary_condition_passes(tmp_path):
    """The permissions-boundary condition is what makes the wide resource safe."""
    template = _write(
        tmp_path,
        "bounded.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Bounded
                Effect: Allow
                Action:
                  - iam:CreateRole
                Resource: '*'
                Condition:
                  StringEquals:
                    iam:PermissionsBoundary: !Ref Boundary
""",
    )
    assert validator.find_service_role_hardening_findings(template) == []


def test_iam_reads_on_all_resources_are_not_reported(tmp_path):
    """Get/List cannot change anything, so `Resource: "*"` is fine for them."""
    template = _write(
        tmp_path,
        "reads.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Reads
                Effect: Allow
                Action:
                  - iam:GetRole
                  - iam:ListRoles
                  - iam:SimulatePrincipalPolicy
                Resource: '*'
""",
    )
    assert validator.find_service_role_hardening_findings(template) == []


def test_explicit_deny_on_all_resources_is_not_a_finding(tmp_path):
    """A Deny on `*` is a guardrail; flagging it would punish the fix."""
    template = _write(
        tmp_path,
        "guardrail.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Guardrail
                Effect: Deny
                Action:
                  - iam:DeleteRolePermissionsBoundary
                  - iam:CreateUser
                Resource: '*'
""",
    )
    assert validator.find_service_role_hardening_findings(template) == []


def test_pass_role_on_all_resources_is_reported(tmp_path):
    template = _write(
        tmp_path,
        "passrole-star.yaml",
        """
Resources:
  Policy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Sid: WidePass
            Effect: Allow
            Action: iam:PassRole
            Resource: '*'
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any('iam:PassRole on Resource: "*"' in f for f in findings), findings


def test_pass_role_without_passed_to_service_is_reported(tmp_path):
    """Scoping the role ARN is not enough: bound the destination service too."""
    template = _write(
        tmp_path,
        "passrole-noservice.yaml",
        """
Resources:
  Policy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Sid: ScopedButUnconditioned
            Effect: Allow
            Action: iam:PassRole
            Resource: !GetAtt SomeRole.Arn
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any("iam:passedtoservice" in f for f in findings), findings
    # ...and it is not ALSO reported as a Resource: "*" finding.
    assert not any('Resource: "*"' in f for f in findings), findings


def test_pass_role_scoped_with_passed_to_service_passes(tmp_path):
    template = _write(
        tmp_path,
        "passrole-ok.yaml",
        """
Resources:
  Policy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Sid: Scoped
            Effect: Allow
            Action: iam:PassRole
            Resource: !GetAtt SomeRole.Arn
            Condition:
              StringEquals:
                iam:PassedToService: !Sub 'lambda.${AWS::URLSuffix}'
""",
    )
    assert validator.find_service_role_hardening_findings(template) == []


def test_null_guard_does_not_satisfy_the_boundary_requirement(tmp_path):
    """`Null` asserts a key is ABSENT, so it cannot stand in for a comparison.

    Without this distinction the boundary check would be trivially bypassable by
    writing `Null: {iam:PermissionsBoundary: true}` — a condition that matches
    precisely when no boundary is attached.
    """
    template = _write(
        tmp_path,
        "null-guard.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: FakeBoundary
                Effect: Allow
                Action: iam:CreateRole
                Resource: '*'
                Condition:
                  'Null':
                    iam:PermissionsBoundary: 'true'
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any("iam:permissionsboundary" in f for f in findings), findings


def test_boundary_removal_is_never_granted(tmp_path):
    template = _write(
        tmp_path,
        "strip-boundary.yaml",
        """
Resources:
  Role:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: P
          PolicyDocument:
            Statement:
              - Sid: Strip
                Effect: Allow
                Action: iam:DeleteRolePermissionsBoundary
                Resource: !Sub 'arn:${AWS::Partition}:iam::x:role/idp*'
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any("must never be granted" in f for f in findings), findings


def test_shipped_service_role_has_no_hardening_findings():
    """The role we ship must satisfy the check, not just the templates' needs."""
    assert validator.find_service_role_hardening_findings(SERVICE_ROLE) == []


def test_the_hardening_check_fails_on_the_pre_fix_service_role(tmp_path):
    """Mutation guard for the gate itself (issue #927).

    An extended gate nobody proved can fail is the same defect class as the one
    it was added to catch, so this pins the pre-fix shape of the shipped role —
    IAM writes on `Resource: "*"`, PassRole on `"*"` with no condition, and
    iam:DeleteRolePermissionsBoundary — and asserts all three are reported.
    """
    template = _write(
        tmp_path,
        "pre-fix.yaml",
        """
Resources:
  CloudFormationServiceRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: CloudFormationPermissions
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action:
                  - iam:CreateRole
                  - iam:PutRolePermissionsBoundary
                  - iam:DeleteRolePermissionsBoundary
                  - iam:AttachRolePolicy
                  - iam:PassRole
                Resource: '*'
  PassRolePolicy:
    Type: AWS::IAM::ManagedPolicy
    Properties:
      PolicyDocument:
        Statement:
          - Effect: Allow
            Action:
              - iam:PassRole
            Resource: !GetAtt CloudFormationServiceRole.Arn
""",
    )
    findings = validator.find_service_role_hardening_findings(template)
    assert any("must never be granted" in f for f in findings), findings
    assert any("iam:permissionsboundary" in f for f in findings), findings
    assert any('iam:PassRole on Resource: "*"' in f for f in findings), findings
    assert sum("iam:passedtoservice" in f for f in findings) == 2, findings
