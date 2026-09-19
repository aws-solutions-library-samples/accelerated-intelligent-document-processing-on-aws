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
