# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The app's Cognito role groups, read from template.yaml.

Every live check in this directory needs the same set — to create the groups on a
throwaway pool, to filter Cognito's auto-created per-provider group out of a
comparison, or to print the stack parameters that map them. Each script used to
carry its own literal list, and when `Annotator` was added to the deployment all
of them silently kept describing four roles (#968). Deriving the set from the
`AWS::Cognito::UserPoolGroup` resources means a sixth group is picked up with no
edit here, and a renamed one cannot go unnoticed.

This module deliberately has no fallback list: a check that cannot read the
template would otherwise run against a stale set and report PASS.
"""

from __future__ import annotations

import pathlib
import re

import yaml

# scripts/security/live_checks/<this file> -> repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATE = REPO_ROOT / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation's short-form tags.

    `!Ref Foo` and friends are not valid YAML tags, so each is collapsed to its
    plain scalar payload — enough to read resource types and property literals.
    """


def _passthrough(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_CfnLoader.add_multi_constructor("!", _passthrough)


def load_template(path: pathlib.Path | None = None) -> dict:
    """Parse template.yaml. Raises if it is unreadable — never returns a guess."""
    path = path or TEMPLATE
    with open(path) as f:
        template = yaml.load(f, Loader=_CfnLoader)
    if not isinstance(template, dict) or "Resources" not in template:
        raise RuntimeError(f"{path} does not look like a CloudFormation template")
    return template


def cognito_role_groups(template: dict | None = None) -> set[str]:
    """Every `AWS::Cognito::UserPoolGroup` name the stack creates.

    Ordered-by-precedence output is available from `cognito_role_groups_ordered`;
    use that where the order is user-visible.
    """
    template = template if template is not None else load_template()
    groups = {
        res["Properties"]["GroupName"]
        for res in template["Resources"].values()
        if res.get("Type") == "AWS::Cognito::UserPoolGroup"
    }
    if not groups:
        raise RuntimeError(
            "no AWS::Cognito::UserPoolGroup resources found in template.yaml; "
            "refusing to run a check against an empty role set"
        )
    return groups


def cognito_role_groups_ordered(template: dict | None = None) -> list[str]:
    """The role groups in `Precedence` order, most privileged first."""
    template = template if template is not None else load_template()
    ranked = [
        (int(res["Properties"].get("Precedence", 99)), res["Properties"]["GroupName"])
        for res in template["Resources"].values()
        if res.get("Type") == "AWS::Cognito::UserPoolGroup"
    ]
    if not ranked:
        raise RuntimeError(
            "no AWS::Cognito::UserPoolGroup resources found in template.yaml; "
            "refusing to run a check against an empty role set"
        )
    return [name for _, name in sorted(ranked)]


def idp_group_env(idp_group_name=lambda role: f"IdP-{role}s") -> dict[str, str]:
    """`{<ROLE>_GROUP_NAME: <IdP group name>}` for the pre-token trigger.

    Keys are read from the trigger's own `Environment` in template.yaml, so a
    check configures exactly the variables the shipped handler reads.
    """
    template = load_template()
    variables = template["Resources"]["ExternalIdPGroupMappingFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    env = {}
    for key, value in variables.items():
        if not key.endswith("_GROUP_NAME"):
            continue
        env[key] = idp_group_name(external_idp_role(value))
    if not env:
        raise RuntimeError(
            "ExternalIdPGroupMappingFunction declares no *_GROUP_NAME variables"
        )
    return env


def external_idp_role(parameter_name: str) -> str:
    """`ExternalIdPAnnotatorGroupName` -> `Annotator`."""
    match = re.fullmatch(r"ExternalIdP(?P<role>\w+)GroupName", parameter_name)
    if not match:
        raise RuntimeError(f"unexpected group parameter name: {parameter_name}")
    return match.group("role")


def external_idp_group_parameters(
    idp_group_name=lambda role: f"IdP-{role}s",
) -> list[tuple[str, str]]:
    """`[(ExternalIdP<Role>GroupName, <IdP group name>)]`, in precedence order."""
    template = load_template()
    variables = template["Resources"]["ExternalIdPGroupMappingFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    by_role = {
        external_idp_role(value): value
        for key, value in variables.items()
        if key.endswith("_GROUP_NAME")
    }
    return [
        (by_role[role], idp_group_name(role))
        for role in cognito_role_groups_ordered(template)
        if role in by_role
    ]
