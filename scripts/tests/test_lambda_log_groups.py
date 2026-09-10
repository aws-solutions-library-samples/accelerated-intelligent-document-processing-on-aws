# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Repo-wide gate on Lambda log-group declaration and naming.

Two defects motivated this gate, both of which shipped and both of which a
test would have caught:

* **#818** — 21 log groups were named ``/aws/lambda/${SomeFunction}``. Because
  ``!Sub`` resolves the *function's generated physical name*, the log group
  depended on the function, so CloudFormation created the function first;
  anything invoking it in that window let Lambda auto-create the group and
  CloudFormation's ``CREATE`` then failed ``ResourceAlreadyExists``. The name
  also changed whenever the function was replaced, orphaning the old group with
  no retention policy — 79 such groups, 14.8 MiB, on one long-lived stack.

* **#826** — functions with no log group at all, which log to Lambda's
  auto-created ``/aws/lambda/<fn>`` group. Lambda's auto-create sets **no
  retention**, so those logs accumulate and are billed forever.

The three rules below are therefore:

1. Every Lambda has a ``LoggingConfig`` — unless it is exempt (see
   ``CUSTOM_RESOURCE_ONLY``).
2. Every ``AWS::Logs::LogGroup`` sets ``RetentionInDays``.
3. No ``LogGroupName`` references a Function resource. This is #818 as a
   permanent gate.

Rule 1's exemption is for Lambdas that run **only** during a CloudFormation
stack operation: a custom-resource handler, or an install-hook invoked by
another stack's custom resource. Those are very low volume and log only stack
operations, so indefinite retention on an auto-created group is a deliberate
accepted cost rather than an oversight.

That exemption is **verified, not trusted** (``test_exemptions_are_really_custom_resource_only``):
an exempt function must actually be reachable only that way — a ``ServiceToken``
target in its own template, or an install-hook whose ARN is exported for other
stacks to invoke. A data-plane Lambda cannot be quietly added to the list to
silence rule 1.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES = [
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/api-resolvers/template.yaml",
    "nested/bedrockkb/template.yaml",
    "nested/multi-doc-discovery/template.yaml",
    "feature-platform/main-stack-extensions/template.yaml",
    "feature-platform/confbench-testset/template.yaml",
    "feature-platform/feature-template/template.yaml",
    "feature-platform/idp-data-generator/template.yaml",
    "feature-platform/pii-anonymizer/template.yaml",
    "feature-platform/sample-feature/template.yaml",
    "feature-platform/sample-health-insurance-review/template.yaml",
    "feature-platform/seller-entitlement-service/template.yaml",
]

FUNCTION_TYPES = {"AWS::Serverless::Function", "AWS::Lambda::Function"}

# Lambdas that run ONLY during a CloudFormation stack operation. Each keeps
# Lambda's auto-created log group and its indefinite retention on purpose:
# they are invoked a handful of times per stack operation and log nothing but
# stack operations.
#
# Adding an entry here is not enough to make it true —
# test_exemptions_are_really_custom_resource_only re-derives the property from
# the template and fails if an entry is not actually custom-resource-only.
CUSTOM_RESOURCE_ONLY: dict[str, set[str]] = {
    "template.yaml": {
        # ServiceToken handlers for Custom::/CustomResource resources.
        "InitializeConcurrencyTableLambda",
        "TestSetBucketNotificationFunction",
        "DashboardMergerFunction",
    },
    "nested/bedrockkb/template.yaml": {
        # Every Lambda in this stack is a ServiceToken handler.
        "GetAdjustedStackNameFunction",
        "GetSeedUrlsFunction",
        "S3VectorManagerFunction",
        "CreateOSSIndexLambdaFunction",
        "StartIngestionJobFunction",
    },
    "feature-platform/main-stack-extensions/template.yaml": {
        # Install-hook trio. Not ServiceToken handlers in THIS template — their
        # ARNs are Exported and invoked by a feature stack's own custom resource
        # at install time, so they run only during a feature stack operation.
        "RegisterFeatureFunction",
        "RegisterFeatureHooksFunction",
        "ApplyFeatureConfigPresetFunction",
    },
}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(rel_path: str) -> dict:
    with open(REPO_ROOT / rel_path) as handle:
        return yaml.load(handle, Loader=_CfnLoader) or {}


def _resources(rel_path: str) -> dict:
    return _load(rel_path).get("Resources", {}) or {}


def _functions(resources: dict) -> dict:
    return {
        name: body
        for name, body in resources.items()
        if isinstance(body, dict) and body.get("Type") in FUNCTION_TYPES
    }


def _log_groups(resources: dict) -> dict:
    return {
        name: body
        for name, body in resources.items()
        if isinstance(body, dict) and body.get("Type") == "AWS::Logs::LogGroup"
    }


def _raw(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text()


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_every_lambda_declares_a_log_group(rel_path: str) -> None:
    """Rule 1: a Lambda has LoggingConfig, or is custom-resource-only (#826)."""
    resources = _resources(rel_path)
    exempt = CUSTOM_RESOURCE_ONLY.get(rel_path, set())

    missing = [
        name
        for name, body in _functions(resources).items()
        if name not in exempt and not (body.get("Properties") or {}).get("LoggingConfig")
    ]

    assert not missing, (
        f"{rel_path}: {len(missing)} Lambda(s) have no LoggingConfig, so Lambda "
        f"auto-creates /aws/lambda/<fn> with NO retention and their logs are "
        f"billed forever: {sorted(missing)}. Either declare a log group (see "
        f".claude/skills/infrastructure.md) or, if the function runs only during "
        f"a stack operation, add it to CUSTOM_RESOURCE_ONLY in this file."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_every_log_group_sets_retention(rel_path: str) -> None:
    """Rule 2: a declared log group without RetentionInDays never expires."""
    offenders = [
        name
        for name, body in _log_groups(_resources(rel_path)).items()
        if "RetentionInDays" not in (body.get("Properties") or {})
    ]

    assert not offenders, (
        f"{rel_path}: log group(s) without RetentionInDays never expire: "
        f"{sorted(offenders)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_no_log_group_is_named_after_a_function(rel_path: str) -> None:
    """Rule 3: #818 as a permanent gate.

    ``LogGroupName: !Sub '/aws/lambda/${SomeFunction}'`` inverts the create
    order (the group waits on the function) and renames the group whenever the
    function is replaced, orphaning the old one with no retention.
    """
    resources = _resources(rel_path)
    function_names = set(_functions(resources))

    offenders = []
    for name, body in _log_groups(resources).items():
        log_group_name = (body.get("Properties") or {}).get("LogGroupName")
        if not isinstance(log_group_name, dict):
            continue
        template = log_group_name.get("Fn::Sub")
        if not isinstance(template, str):
            continue
        for referenced in re.findall(r"\$\{([A-Za-z0-9:]+)\}", template):
            if referenced in function_names:
                offenders.append(f"{name} -> ${{{referenced}}}")

    assert not offenders, (
        f"{rel_path}: log group name(s) reference a Function resource, which "
        f"inverts the CloudFormation create order and orphans never-expiring "
        f"groups on function replacement (#818): {sorted(offenders)}. Use "
        f"'/${{AWS::StackName}}/lambda/<FunctionLogicalId>' with a matching "
        f"LoggingConfig on the function instead."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(CUSTOM_RESOURCE_ONLY))
def test_exemptions_are_really_custom_resource_only(rel_path: str) -> None:
    """The exemption list is verified, not trusted.

    An exempt function must actually be reachable only during a stack
    operation: either a ``ServiceToken`` target in this template, or an
    install-hook whose ARN is Exported for another stack's custom resource to
    invoke. This stops a data-plane Lambda being added to
    ``CUSTOM_RESOURCE_ONLY`` to silence rule 1.
    """
    raw = _raw(rel_path)
    resources = _resources(rel_path)
    outputs = _load(rel_path).get("Outputs", {}) or {}
    exported_arns = {
        str(body.get("Value"))
        for body in outputs.values()
        if isinstance(body, dict) and body.get("Export")
    }

    unjustified = []
    for name in sorted(CUSTOM_RESOURCE_ONLY[rel_path]):
        assert name in resources, (
            f"{rel_path}: CUSTOM_RESOURCE_ONLY names {name!r}, which no longer "
            f"exists. Remove the stale entry."
        )

        is_service_token = bool(
            re.search(rf"ServiceToken:\s*!GetAtt\s+{name}\.Arn", raw)
            or re.search(rf"ServiceToken:\s*!Ref\s+{name}\b", raw)
        )
        # Install-hook: ARN exported for other stacks to invoke, and never
        # wired to an event source or API in this template.
        is_exported_hook = any(name in arn for arn in exported_arns)

        properties = resources[name].get("Properties") or {}
        has_event_source = bool(properties.get("Events"))

        if has_event_source or not (is_service_token or is_exported_hook):
            unjustified.append(name)

    assert not unjustified, (
        f"{rel_path}: {unjustified} are listed in CUSTOM_RESOURCE_ONLY but are "
        f"not custom-resource-only — they are neither a ServiceToken target nor "
        f"an exported install hook, or they declare an event source. They need a "
        f"real log group with retention."
    )
