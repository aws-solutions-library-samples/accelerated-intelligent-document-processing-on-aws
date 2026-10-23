# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The CloudFormation service role must be able to deploy a NESTED-stack solution.

`validate_service_role_permissions.py` checks that the role holds a permission for
every resource type the templates declare, and `test_validate_service_role_permissions.py`
pins that the check cannot degrade to an empty comparison. Neither can see the class of
defect this file covers: a grant whose *condition* excludes the way CloudFormation
actually makes the call.

This solution is built from nested stacks. A nested stack inherits its parent's service
role, which CloudFormation propagates by passing the role **to itself** — an
`iam:PassRole` whose `iam:PassedToService` is `cloudformation`. The hardening in #927
scoped `iam:PassRole` with a `StringEqualsIfExists` list of 14 services and did not
include `cloudformation`, so the Resource matched, the condition did not, and the
**first** nested stack failed with

    <role>/AWSCloudFormation is not authorized to perform: iam:PassRole on resource:
    arn:aws:iam::<account>:role/<stack>-iam-CFServiceRole because no identity-based
    policy allows the iam:PassRole action

taking 59 sibling resources down with it. Every deploy through this role failed, in
every hosting variant — and it shipped, because nothing in the repository ever
deployed a stack *through* the role, only asserted things about its policy document.

The trap that makes this worth a named test: a `PassServiceRoleToCloudFormationOnly`
statement granting exactly this pass **does** exist in the same template, in the
`<StackName>-PassRolePolicy` managed policy. That policy is documented for an
administrator to attach to the deploying *human*. It is not attached to the role, so
it does nothing for the role passing itself, and its presence makes the role's own
missing entry read as already handled.

This is a static check and it does not replace a deploy. The real coverage is the
deploy-variant tier (`make stacktest-*`), which now exercises the role end to end;
this file is the cheap gate that fails in CI in under a second instead.

⚠️ **The `cloudformation` entry was the instance; the missing entry is the class.**
The test above names one service, so it went green while the *same* defect recurred
with a different one. `AWS::GuardDuty::MalwareProtectionPlan` passes `MalwareScanRole`
to `malware-protection-plan.guardduty.amazonaws.com`, which was not in the list, so
the 2026-10-13 nightly deploy reached `ROLLBACK_COMPLETE` with

    is not authorized to perform: iam:PassRole on resource:
    arn:aws:iam::<account>:role/<stack>-MalwareScanRole-<suffix>
    because no identity-based policy allows the iam:PassRole action

— the identical sentence, one service along. Note also that the failure's own
suggested remedy ("scope PassRole to guardduty.amazonaws.com") is **wrong**: the
plan's sub-service principal is what the role trusts and what populates
`iam:PassedToService`, so a `guardduty.` entry reads as correct and still denies.

`test_every_service_a_template_passes_a_role_to_is_allowed` is therefore derived
rather than enumerated: it reads the trust policy of every role the solution
templates hand to a service and requires each principal to appear in the list. A new
resource that passes a role cannot reintroduce this without failing here.

Two bounds on that derivation, stated so a green run is not over-read:

* It resolves a role **referenced in the same template** (`!GetAtt X.Arn` or
  `!Ref X`). A role arriving cross-stack as a parameter is unresolvable statically
  and is not covered.
* It reads `Principal.Service` only. A **federated** principal (the Cognito
  identity-pool roles) yields no requirement, which is correct — those are not
  `iam:PassRole` targets — but it means the absence of a finding is not evidence
  about them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROLE = (
    REPO_ROOT
    / "iam-roles"
    / "cloudformation-management"
    / "IDP-Cloudformation-Service-Role.yaml"
)


class _CfnLoader(yaml.SafeLoader):
    """Load CloudFormation YAML, keeping short-form intrinsics as inert data."""


def _intrinsic(loader, tag_suffix, node):  # pragma: no cover - exercised via load
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


def _statements_of_role():
    """Every statement of every inline policy on the service role itself."""
    doc = yaml.load(SERVICE_ROLE.read_text(encoding="utf-8"), Loader=_CfnLoader)
    role = doc["Resources"]["CloudFormationServiceRole"]
    out = []
    for policy in role["Properties"].get("Policies", []):
        out.extend(policy["PolicyDocument"]["Statement"])
    return out


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _sub_text(value):
    """The literal text of a value that may be a plain string or an `Fn::Sub`."""
    if isinstance(value, dict):
        inner = value.get("Fn::Sub")
        if isinstance(inner, str):
            return inner
        if isinstance(inner, list) and inner:
            return str(inner[0])
        return ""
    return str(value)


@pytest.mark.unit
def test_role_may_pass_itself_to_cloudformation_for_nested_stacks() -> None:
    passed_to = set()
    found_passrole = False

    for stmt in _statements_of_role():
        if stmt.get("Effect") != "Allow":
            continue
        actions = [str(a) for a in _as_list(stmt.get("Action"))]
        if not any(a == "iam:PassRole" for a in actions):
            continue
        found_passrole = True
        condition = stmt.get("Condition") or {}
        for operator, keys in condition.items():
            if "StringEquals" not in operator:
                continue
            for key, value in (keys or {}).items():
                if key.lower() != "iam:passedtoservice":
                    continue
                for entry in _as_list(value):
                    passed_to.add(_sub_text(entry))

    assert found_passrole, (
        "no Allow of iam:PassRole found on the service role's own inline policies — "
        "this test can no longer see the grant it exists to check, so it would pass "
        "vacuously. Re-point it at wherever the grant moved."
    )

    assert any(entry.startswith("cloudformation.") for entry in sorted(passed_to)), (
        "the service role's iam:PassRole condition does not allow passing a role to "
        "cloudformation, so CloudFormation cannot hand this role to a nested stack "
        "and the FIRST nested stack of every deploy fails with 'no identity-based "
        "policy allows the iam:PassRole action'. Add "
        "!Sub 'cloudformation.${AWS::URLSuffix}' to iam:PassedToService. Note the "
        "PassRolePolicy managed policy is NOT this grant: it is for the deploying "
        f"human, not the role. Services currently allowed: {sorted(passed_to)}"
    )


@pytest.mark.unit
def test_the_nested_stack_resource_type_is_actually_used() -> None:
    """Guard the premise: if nothing nests, the test above is arguing about nothing."""
    main = (REPO_ROOT / "template.yaml").read_text(encoding="utf-8")
    assert "AWS::CloudFormation::Stack" in main, (
        "template.yaml no longer declares a nested stack, so the reasoning in this "
        "module's docstring no longer holds — re-derive it before trusting either test."
    )


# ---------------------------------------------------------------------------
# The class: every service a solution template hands a role to.
# ---------------------------------------------------------------------------

# The templates this role deploys: the main stack, the nested stacks it creates,
# the processing pattern, and the feature-platform extensions installed into it.
# Expressed as globs rather than a list so a new nested stack or feature is
# covered the day it lands — the hardcoded three-template universe in
# validate_service_role_permissions.py is what let a new template go unchecked.
#
# scripts/sdlc/cfn/ is outside this set and that is a scope boundary, not a
# waiver: those templates are the CI pipeline's OWN infrastructure, deployed by
# an administrator rather than through this role, and the role cannot be asked
# to pass a role in them. (codepipeline.amazonaws.com is the one principal they
# contribute, and it is correctly absent from the role's list.)
SOLUTION_TEMPLATE_GLOBS = (
    "template.yaml",
    "nested/*/template.yaml",
    "patterns/*/template.yaml",
    "feature-platform/*/template.yaml",
)

# Properties that hand a role to the service creating the resource. Each takes a
# role ARN or name, so a value here is a genuine iam:PassRole at deploy time.
ROLE_BEARING_PROPERTIES = frozenset(
    {
        "Role",
        "RoleArn",
        "ServiceRole",
        "ServiceRoleArn",
        "ExecutionRoleArn",
        "JobRoleArn",
    }
)


def _solution_templates():
    seen = {}
    for pattern in SOLUTION_TEMPLATE_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            seen[path] = True
    return sorted(seen)


def _referenced_logical_id(value):
    """The logical id a role-bearing property points at, if it points in-template."""
    if not isinstance(value, dict):
        return None
    if "Ref" in value and isinstance(value["Ref"], str):
        return value["Ref"]
    attr = value.get("Fn::GetAtt")
    if isinstance(attr, str):
        parts = attr.split(".")
    elif isinstance(attr, list):
        parts = [str(part) for part in attr]
    else:
        return None
    if len(parts) >= 2 and parts[-1] == "Arn":
        return parts[0]
    return None


def _trusted_service_principals(role):
    """The `Principal.Service` entries of a role's trust policy, as literal text."""
    document = (role.get("Properties") or {}).get("AssumeRolePolicyDocument") or {}
    statements = document.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    out = set()
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal")
        if not isinstance(principal, dict):
            continue
        for entry in _as_list(principal.get("Service")):
            text = _sub_text(entry)
            if text:
                out.add(text)
    return out


def _role_passes_in_solution_templates():
    """Map service principal -> the call sites that pass a role to it."""
    passes: dict[str, list[str]] = {}
    for path in _solution_templates():
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_CfnLoader)
        if not isinstance(document, dict):
            continue
        resources = document.get("Resources") or {}
        roles = {
            logical_id: body
            for logical_id, body in resources.items()
            if isinstance(body, dict) and body.get("Type") == "AWS::IAM::Role"
        }
        for logical_id, body in resources.items():
            if not isinstance(body, dict):
                continue
            properties = body.get("Properties")
            if not isinstance(properties, dict):
                continue
            for prop, value in properties.items():
                if prop not in ROLE_BEARING_PROPERTIES:
                    continue
                target = _referenced_logical_id(value)
                if target is None or target not in roles:
                    continue
                for principal in _trusted_service_principals(roles[target]):
                    site = (
                        f"{path.relative_to(REPO_ROOT)}: "
                        f"{logical_id}.{prop} -> {target}"
                    )
                    passes.setdefault(principal, []).append(site)
    return passes


def _allowed_passed_to_services():
    """The iam:PassedToService values the role's own PassRole grants permit."""
    allowed = set()
    found_passrole = False
    for statement in _statements_of_role():
        if statement.get("Effect") != "Allow":
            continue
        actions = [str(action) for action in _as_list(statement.get("Action"))]
        if "iam:PassRole" not in actions:
            continue
        found_passrole = True
        for operator, keys in (statement.get("Condition") or {}).items():
            if "StringEquals" not in operator:
                continue
            for key, value in (keys or {}).items():
                if key.lower() != "iam:passedtoservice":
                    continue
                for entry in _as_list(value):
                    allowed.add(_sub_text(entry))
    return allowed, found_passrole


def _service_prefix(principal):
    """`lambda.${AWS::URLSuffix}` -> `lambda.` — compare without the suffix form.

    The templates spell the suffix as `${AWS::URLSuffix}` and could equally spell
    it `amazonaws.com`; comparing the leading service label makes the check
    independent of which, and of the partition.
    """
    text = principal.split("${")[0]
    return text if text.endswith(".") else text + "."


@pytest.mark.unit
def test_every_service_a_template_passes_a_role_to_is_allowed() -> None:
    passes = _role_passes_in_solution_templates()
    allowed, found_passrole = _allowed_passed_to_services()

    assert found_passrole, (
        "no Allow of iam:PassRole found on the service role's own inline policies, "
        "so this check cannot see the grant it exists to verify."
    )
    assert passes, (
        "no in-template role-pass was derived from "
        f"{list(SOLUTION_TEMPLATE_GLOBS)} — the derivation is broken or the globs "
        "no longer match any template, so this test would pass vacuously."
    )

    allowed_prefixes = {_service_prefix(entry) for entry in allowed}
    missing = {
        principal: sites
        for principal, sites in passes.items()
        if _service_prefix(principal) not in allowed_prefixes
    }

    assert not missing, (
        "the service role's iam:PassRole condition does not allow passing a role to "
        "every service the solution templates hand one to, so CloudFormation denies "
        "the pass and the stack rolls back with 'no identity-based policy allows the "
        "iam:PassRole action'. Add each principal below to iam:PassedToService on the "
        "PassStackRolesToStackServices statement — spelled EXACTLY as the role's trust "
        "policy spells it, since a parent service name (guardduty. for "
        "malware-protection-plan.guardduty.) reads as correct and still denies.\n"
        + "\n".join(
            f"  {principal}\n"
            + "\n".join(f"      {site}" for site in sorted(set(sites)))
            for principal, sites in sorted(missing.items())
        )
        + f"\n\nCurrently allowed: {sorted(allowed)}"
    )


@pytest.mark.unit
def test_the_malware_plan_role_pass_is_one_of_the_derived_sites() -> None:
    """Pin the site that #1371's successor exposed, so the derivation keeps seeing it.

    The general check above is only as good as its ability to resolve a role
    reference. This names the one call site that actually broke a deploy, so a
    refactor of the resolver that silently stops matching `Role: !GetAtt X.Arn`
    fails here rather than going quiet.
    """
    passes = _role_passes_in_solution_templates()
    guardduty = {
        principal: sites
        for principal, sites in passes.items()
        if "guardduty." in principal
    }
    assert guardduty, (
        "the derivation no longer finds MalwareProtectionPlan.Role -> MalwareScanRole. "
        "Either the resource was removed (update this test) or the resolver stopped "
        "matching it (fix the resolver — the general check is now blind to that site)."
    )
    assert any(
        "MalwareProtectionPlan" in site
        for sites in guardduty.values()
        for site in sites
    )
