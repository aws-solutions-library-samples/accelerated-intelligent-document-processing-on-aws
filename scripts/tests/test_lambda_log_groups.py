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

Rules enforced:

1. Every Lambda has a ``LoggingConfig`` that **resolves to a real
   ``AWS::Logs::LogGroup`` in the same template** — unless it is exempt (see
   ``CUSTOM_RESOURCE_ONLY``).
2. Every ``AWS::Logs::LogGroup`` sets a non-null ``RetentionInDays``.
3. No ``LogGroupName`` references a Function resource, in **any** intrinsic
   form. This is #818 as a permanent gate.
4. A log group's ``Condition`` matches the ``Condition`` of the function it
   serves, so a group is never created on a stack whose function does not exist
   (and never missing on one where it does).

Rule 1's exemption is for Lambdas that run **only** during a CloudFormation
stack operation: a custom-resource handler, or an install hook invoked by
another stack's custom resource. Those are very low volume and log only stack
operations, so indefinite retention on an auto-created group is a deliberate
accepted cost rather than an oversight.

That exemption is **checked, not merely trusted**
(``test_exemptions_are_really_custom_resource_only``): an exempt function must be
a ``ServiceToken`` target in its own template, or have its ARN exported via a
direct ``GetAtt``, and must have no event source declared in the template — no
SAM ``Events``, ``EventSourceMapping``, ``Lambda::Permission``, ``Events::Rule``
or API Gateway method/integration.

**That check is structural and it is not airtight.** It reasons about the
template, not about what actually invokes a function at runtime. A Lambda that
is invoked *by ARN from configuration* — every hook in
``samples/lambda-hook-inference`` — has no template wiring for the event-source
leg to find, so adding an ``Export`` to its ARN output would make it look like an
install hook and let it be exempted. Nothing in this repo is wrongly exempt
today, and each entry in ``CUSTOM_RESOURCE_ONLY`` carries its justification, but
treat the list as a reviewed decision rather than a proof: **when you add an
entry, confirm by hand that nothing invokes the function outside a stack
operation.**

Earlier revisions of this gate could be defeated in several ways — ``Fn::Join``
and the list form of ``Fn::Sub`` slipped rule 3; a ``LoggingConfig`` naming a
non-existent log group, or one merely *mentioning* a real one
(``!Sub '${G}-suffix'``), satisfied rule 1; ``RetentionInDays: ~`` and
``!Ref 'AWS::NoValue'`` satisfied rule 2; an ``Fn::If`` branch yielding
``AWS::NoValue`` bypassed rules 1 and 4; and the exemption check accepted any
export whose value merely mentioned the function.
``test_gate_catches_known_bypasses`` pins each of those closed so the rules
cannot silently weaken again.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml


def _is_no_value(node: Any) -> bool:
    """``!Ref AWS::NoValue`` — removes whatever property it stands in.

    A log group whose ``RetentionInDays`` resolves to this has NO retention —
    the #826 defect — and cfn-lint does not flag it (verified: exit 0). Same for
    a ``LoggingConfig.LogGroup``: the function silently loses its log group on
    whichever branch yields NoValue.
    """
    return isinstance(node, dict) and any(
        node.get(key) == "AWS::NoValue" for key in ("Ref", "Fn::Ref")
    )


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
    # Customer-deployable sample hook stacks. These are per-document data-plane
    # Lambdas — the IDP pipeline invokes them once per page or document — so
    # their log volume is higher than several functions in the stacks above.
    "samples/lambda-hook-inference/template.yaml",
    "samples/lambda-hook-inference/GENAIIDP-bedrock-proxy/template.yaml",
    "samples/lambda-hook-inference/GENAIIDP-sagemaker-hook/template.yaml",
    "samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook/template.yaml",
    "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/template.yaml",
    "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency/template.yaml",
    # Throwaway verification fixture (make verify-idp-federation). Included so
    # an interrupted run cannot leave a never-expiring log group behind.
    "scripts/security/live_checks/oidc_provider/template.yaml",
    # Note the .yml extension — the discovery meta-test below globs both
    # spellings precisely because this one was invisible to a template.yaml-only
    # sweep.
    "notebooks/examples/demo-lambda/template.yml",
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


def _load_text(text: str) -> dict:
    return yaml.load(text, Loader=_CfnLoader) or {}


def _load(rel_path: str) -> dict:
    return _load_text((REPO_ROOT / rel_path).read_text())


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


def _referenced_logical_ids(node: Any) -> set[str]:
    """Every logical id any intrinsic in ``node`` could resolve to.

    Walks the whole structure so ``Fn::Join``, the list form of ``Fn::Sub``,
    and nesting are all covered — not just a top-level scalar ``Fn::Sub``.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Fn::Sub":
                template = value[0] if isinstance(value, list) and value else value
                if isinstance(template, str):
                    # ${Foo} and ${Foo.Arn} both name Foo.
                    for token in re.findall(r"\$\{([^}]+)\}", template):
                        found.add(token.split(".")[0].strip())
                if isinstance(value, list) and len(value) > 1:
                    found |= _referenced_logical_ids(value[1])
            elif key in {"Ref", "Fn::Ref"} and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt":
                if isinstance(value, str):
                    found.add(value.split(".")[0])
                elif isinstance(value, list) and value:
                    found.add(str(value[0]))
            else:
                found |= _referenced_logical_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _referenced_logical_ids(item)
    return found


def _logging_config_target(function_body: dict) -> Any:
    return ((function_body.get("Properties") or {}).get("LoggingConfig") or {}).get(
        "LogGroup"
    )


def _branches(node: Any) -> list[Any]:
    """Every value ``node`` can evaluate to, expanding ``Fn::If`` branches.

    A rule that inspects only the node itself is blind to
    ``!If [C, !Ref G, !Ref 'AWS::NoValue']``, which on one branch drops the
    property entirely. This repo uses that idiom heavily, so every branch has to
    be checked independently.
    """
    if isinstance(node, dict) and "Fn::If" in node:
        value = node["Fn::If"]
        if isinstance(value, list) and len(value) == 3:
            return [b for branch in value[1:] for b in _branches(branch)]
    return [node]


def _resolves_exactly_to(target: Any, candidates: set[str]) -> bool:
    """True only if ``target`` *is* a reference to one of ``candidates``.

    ``!Sub '${G}-suffix'`` and ``!Join ['', [!Ref G, '-oops']]`` mention ``G``
    but name a log group that does not exist, so the function still falls back
    to Lambda's auto-created group. Membership in
    ``_referenced_logical_ids`` is therefore not enough — the whole value must
    resolve to exactly the group.
    """
    if not isinstance(target, dict):
        return False
    for key in ("Ref", "Fn::Ref"):
        if isinstance(target.get(key), str) and target[key] in candidates:
            return True
    sub = target.get("Fn::Sub")
    template = sub[0] if isinstance(sub, list) and sub else sub
    if isinstance(template, str):
        match = re.fullmatch(r"\$\{([^}]+)\}", template.strip())
        if match and match.group(1).split(".")[0].strip() in candidates:
            return True
    return False


def _check_rule_1(rel_path: str, doc: dict, exempt: set[str]) -> list[str]:
    resources = doc.get("Resources", {}) or {}
    log_groups = set(_log_groups(resources))
    problems = []
    for name, body in _functions(resources).items():
        if name in exempt:
            continue
        target = _logging_config_target(body)
        if not target:
            problems.append(f"{name}: no LoggingConfig")
            continue
        # Every Fn::If branch must independently resolve to a real log group.
        for branch in _branches(target):
            if _is_no_value(branch):
                problems.append(f"{name}: LoggingConfig.LogGroup can be AWS::NoValue")
            elif not _resolves_exactly_to(branch, log_groups):
                problems.append(
                    f"{name}: LoggingConfig.LogGroup does not resolve to an "
                    f"AWS::Logs::LogGroup in this template (got {branch!r})"
                )
    return problems


def _check_rule_2(doc: dict) -> list[str]:
    resources = doc.get("Resources", {}) or {}
    problems = []
    for name, body in _log_groups(resources).items():
        properties = body.get("Properties") or {}
        if "RetentionInDays" not in properties:
            problems.append(f"{name}: no RetentionInDays")
            continue
        for branch in _branches(properties["RetentionInDays"]):
            if branch is None:
                problems.append(f"{name}: RetentionInDays is null")
            elif _is_no_value(branch):
                problems.append(f"{name}: RetentionInDays can be AWS::NoValue")
    return problems


def _check_rule_3(doc: dict) -> list[str]:
    resources = doc.get("Resources", {}) or {}
    function_names = set(_functions(resources))
    problems = []
    for name, body in _log_groups(resources).items():
        log_group_name = (body.get("Properties") or {}).get("LogGroupName")
        if log_group_name is None:
            continue
        offending = _referenced_logical_ids(log_group_name) & function_names
        if offending:
            problems.append(f"{name} -> {sorted(offending)}")
    return problems


def _check_rule_4(doc: dict, exempt: set[str]) -> list[str]:
    """Condition parity between a function and the log group it points at."""
    resources = doc.get("Resources", {}) or {}
    log_groups = _log_groups(resources)
    problems = []
    for name, body in _functions(resources).items():
        if name in exempt:
            continue
        target = _logging_config_target(body)
        if not isinstance(target, dict):
            continue
        # Expand Fn::If so a group referenced from only one branch is still
        # checked, matching rule 1.
        referenced: set[str] = set()
        for branch in _branches(target):
            referenced |= _referenced_logical_ids(branch)
        for group in referenced & set(log_groups):
            fn_condition = body.get("Condition")
            group_condition = log_groups[group].get("Condition")
            if fn_condition != group_condition:
                problems.append(
                    f"{name} (Condition={fn_condition!r}) -> {group} "
                    f"(Condition={group_condition!r})"
                )
    return problems


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_every_lambda_declares_a_real_log_group(rel_path: str) -> None:
    """Rule 1 (#826): LoggingConfig present AND resolving to a real group."""
    problems = _check_rule_1(
        rel_path, _load(rel_path), CUSTOM_RESOURCE_ONLY.get(rel_path, set())
    )
    assert not problems, (
        f"{rel_path}: Lambda(s) would fall back to Lambda's auto-created "
        f"/aws/lambda/<fn> group, which has NO retention, so their logs are "
        f"billed forever: {problems}. Declare a log group (see "
        f".claude/skills/infrastructure.md) or, if the function runs only during "
        f"a stack operation, add it to CUSTOM_RESOURCE_ONLY in this file."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_every_log_group_sets_retention(rel_path: str) -> None:
    """Rule 2: a log group without a real RetentionInDays never expires."""
    problems = _check_rule_2(_load(rel_path))
    assert not problems, f"{rel_path}: log group(s) never expire: {problems}"


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_no_log_group_is_named_after_a_function(rel_path: str) -> None:
    """Rule 3: #818 as a permanent gate, across every intrinsic form."""
    problems = _check_rule_3(_load(rel_path))
    assert not problems, (
        f"{rel_path}: log group name(s) reference a Function resource, which "
        f"inverts the CloudFormation create order and orphans never-expiring "
        f"groups on function replacement (#818): {problems}. Use "
        f"'/${{AWS::StackName}}/lambda/<FunctionLogicalId>' with a matching "
        f"LoggingConfig on the function instead."
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_log_group_condition_matches_its_function(rel_path: str) -> None:
    """Rule 4: a group must exist exactly where its function does."""
    problems = _check_rule_4(_load(rel_path), CUSTOM_RESOURCE_ONLY.get(rel_path, set()))
    assert not problems, (
        f"{rel_path}: log group Condition does not match its function's, so the "
        f"group is created where the function is absent (empty group forever) or "
        f"missing where it is present (stack failure): {problems}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(CUSTOM_RESOURCE_ONLY))
def test_exemptions_are_really_custom_resource_only(rel_path: str) -> None:
    """The exemption list is verified, not trusted.

    An exempt function must be reachable only during a stack operation: a
    ``ServiceToken`` target in this template, or an install hook whose ARN is
    exported via a direct ``GetAtt`` on that function. It must also have no
    event source of any kind.
    """
    doc = _load(rel_path)
    resources = doc.get("Resources", {}) or {}
    outputs = doc.get("Outputs", {}) or {}

    unjustified = []
    for name in sorted(CUSTOM_RESOURCE_ONLY[rel_path]):
        assert name in resources, (
            f"{rel_path}: CUSTOM_RESOURCE_ONLY names {name!r}, which no longer "
            f"exists. Remove the stale entry."
        )

        # (a) ServiceToken target of a custom resource in this template.
        is_service_token = any(
            name
            in _referenced_logical_ids(
                (body.get("Properties") or {}).get("ServiceToken")
            )
            for body in resources.values()
            if isinstance(body, dict)
        )

        # (b) Install hook: ARN exported via a DIRECT GetAtt on this function,
        # not merely a string that mentions it.
        is_exported_hook = False
        for output in outputs.values():
            if not isinstance(output, dict) or not output.get("Export"):
                continue
            value = output.get("Value")
            if isinstance(value, dict) and value.get("Fn::GetAtt"):
                attr = value["Fn::GetAtt"]
                target = attr.split(".")[0] if isinstance(attr, str) else str(attr[0])
                if target == name:
                    is_exported_hook = True

        # (c) No event source of any kind.
        properties = resources[name].get("Properties") or {}
        event_sources = []
        if properties.get("Events"):
            event_sources.append("SAM Events")
        for other, body in resources.items():
            if not isinstance(body, dict) or other == name:
                continue
            kind = body.get("Type")
            if kind in {
                "AWS::Lambda::EventSourceMapping",
                "AWS::Lambda::Permission",
                "AWS::Events::Rule",
                "AWS::ApiGateway::Method",
                "AWS::ApiGatewayV2::Integration",
            } and name in _referenced_logical_ids(body.get("Properties")):
                event_sources.append(f"{other} ({kind})")

        if event_sources or not (is_service_token or is_exported_hook):
            unjustified.append(f"{name}: event_sources={event_sources or None}")

    assert not unjustified, (
        f"{rel_path}: listed in CUSTOM_RESOURCE_ONLY but not custom-resource-only "
        f"— neither a ServiceToken target nor a GetAtt-exported install hook, or "
        f"they have an event source: {unjustified}. They need a real log group "
        f"with retention."
    )


# ---------------------------------------------------------------------------
# Meta-tests: a gate that cannot fail is worthless. Each case below is a real
# bypass an earlier revision of this file accepted.
# ---------------------------------------------------------------------------

_FN = """
Resources:
  MyFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
{extra}
"""


def _rules(text: str, exempt: set[str] | None = None):
    doc = _load_text(text)
    exempt = exempt or set()
    return {
        1: _check_rule_1("<synthetic>", doc, exempt),
        2: _check_rule_2(doc),
        3: _check_rule_3(doc),
        4: _check_rule_4(doc, exempt),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,rule,body",
    [
        (
            "818-Fn-Join",
            3,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
      LogGroupName: !Join ['', ['/aws/lambda/', !Ref MyFn]]""",
        ),
        (
            "818-Fn-Sub-list-form",
            3,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
      LogGroupName: !Sub ['/aws/lambda/${X}', {X: !Ref MyFn}]""",
        ),
        (
            "818-Fn-Sub-scalar",
            3,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
      LogGroupName: !Sub '/aws/lambda/${MyFn}'""",
        ),
        (
            "826-no-logging-config",
            1,
            "",
        ),
        (
            "826-logging-config-typo",
            1,
            """      LoggingConfig:
        LogGroup: !Ref TypoLogGrup
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
        (
            "826-logging-config-bare-string",
            1,
            "      LoggingConfig:\n        LogGroup: /aws/lambda/whatever",
        ),
        (
            "retention-null",
            2,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: ~""",
        ),
        (
            "retention-no-value",
            2,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: !Ref 'AWS::NoValue'""",
        ),
        (
            "retention-if-branch-no-value",
            2,
            """      LoggingConfig:
        LogGroup: !Ref G
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: !If [C, 30, !Ref 'AWS::NoValue']""",
        ),
        (
            "loggroup-if-branch-no-value",
            1,
            """      LoggingConfig:
        LogGroup: !If [C, !Ref G, !Ref 'AWS::NoValue']
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
        (
            "loggroup-mentions-but-does-not-resolve-sub",
            1,
            """      LoggingConfig:
        LogGroup: !Sub '${G}-suffix'
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
        (
            "loggroup-mentions-but-does-not-resolve-join",
            1,
            """      LoggingConfig:
        LogGroup: !Join ['', [!Ref G, '-oops']]
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
        (
            "loggroup-getatt-arn-not-name",
            1,
            """      LoggingConfig:
        LogGroup: !GetAtt G.Arn
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
        (
            "condition-mismatch",
            4,
            """      LoggingConfig:
        LogGroup: !Ref G
    Condition: SomeCondition
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30""",
        ),
    ],
)
def test_gate_catches_known_bypasses(case: str, rule: int, body: str) -> None:
    """Each of these defeated an earlier revision of this gate."""
    problems = _rules(_FN.format(extra=body))
    assert problems[rule], (
        f"bypass {case!r} was NOT caught by rule {rule} — the gate has "
        f"regressed. All rules: {problems}"
    )


@pytest.mark.unit
def test_cross_wired_log_groups_are_caught() -> None:
    """Two functions pointing at each other's groups is a Condition/pairing bug."""
    text = """
Resources:
  A:
    Type: AWS::Serverless::Function
    Condition: CondA
    Properties:
      LoggingConfig:
        LogGroup: !Ref BLog
  B:
    Type: AWS::Serverless::Function
    Properties:
      LoggingConfig:
        LogGroup: !Ref ALog
  ALog:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
  BLog:
    Type: AWS::Logs::LogGroup
    Condition: CondB
    Properties:
      RetentionInDays: 30
"""
    assert _rules(text)[4], "cross-wired groups with mismatched Conditions not caught"


@pytest.mark.unit
def test_exemption_cannot_be_claimed_by_a_loose_export_mention() -> None:
    """An export whose value merely mentions the function must not exempt it."""
    text = """
Resources:
  MyFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
Outputs:
  Hint:
    Value: 'see MyFn for details'
    Export:
      Name: hint
"""
    path = REPO_ROOT / "scripts" / "tests" / "_synthetic_probe.yaml"
    path.write_text(text)
    try:
        CUSTOM_RESOURCE_ONLY["scripts/tests/_synthetic_probe.yaml"] = {"MyFn"}
        with pytest.raises(AssertionError, match="not custom-resource-only"):
            test_exemptions_are_really_custom_resource_only(
                "scripts/tests/_synthetic_probe.yaml"
            )
    finally:
        CUSTOM_RESOURCE_ONLY.pop("scripts/tests/_synthetic_probe.yaml", None)
        path.unlink()


def _discover_unlisted_templates() -> list[str]:
    """Every Lambda-declaring template in the repo that TEMPLATES omits.

    Globs both ``*.yaml`` and ``*.yml``, and does not assume the filename is
    ``template.yaml`` — ``notebooks/examples/demo-lambda/template.yml`` was
    invisible to an earlier ``template.yaml``-only sweep.
    """
    listed = {REPO_ROOT / rel for rel in TEMPLATES}
    skip_dirs = {".aws-sam", "node_modules", ".venv", "build", "dist", ".git"}
    unlisted = []
    for pattern in ("*.yaml", "*.yml"):
        for path in REPO_ROOT.rglob(pattern):
            if any(part in skip_dirs for part in path.parts) or path in listed:
                continue
            try:
                doc = _load_text(path.read_text())
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            if not isinstance(doc, dict):
                continue
            if _functions(doc.get("Resources") or {}):
                unlisted.append(str(path.relative_to(REPO_ROOT)))
    return sorted(unlisted)


@pytest.mark.unit
def test_every_template_with_lambdas_is_listed() -> None:
    """A new template with Lambdas must be added to TEMPLATES, not forgotten.

    ``samples/lambda-hook-inference`` was missed on the first pass and its
    per-document hook Lambdas had the #826 defect;
    ``notebooks/examples/demo-lambda/template.yml`` was then missed because the
    sweep only globbed ``template.yaml``.
    """
    unlisted = _discover_unlisted_templates()
    assert not unlisted, (
        f"template(s) declare Lambda functions but are not in TEMPLATES, so the "
        f"log-group rules do not cover them: {unlisted}"
    )


@pytest.mark.unit
def test_discovery_actually_finds_an_unlisted_template() -> None:
    """The discovery sweep must be able to fail.

    Without this, deleting the body of ``_discover_unlisted_templates`` leaves
    the suite green and the coverage guarantee silently gone — one of two
    mechanisms a prior review found unprotected.
    """
    probe = REPO_ROOT / "scripts" / "tests" / "_unlisted_probe.yml"
    probe.write_text(
        "Resources:\n"
        "  ProbeFn:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      Handler: index.handler\n"
    )
    try:
        assert "scripts/tests/_unlisted_probe.yml" in _discover_unlisted_templates()
    finally:
        probe.unlink()


@pytest.mark.unit
def test_exemption_rejects_a_function_with_an_external_event_source() -> None:
    """The event-source leg of the exemption check must be able to fail.

    A ``ServiceToken`` handler that ALSO has an ``EventSourceMapping`` is not
    custom-resource-only. Without this test the whole leg can be deleted and the
    suite stays green — the second of two mechanisms a prior review found
    unprotected.
    """
    text = """
Resources:
  MyFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
  Custom:
    Type: Custom::Thing
    Properties:
      ServiceToken: !GetAtt MyFn.Arn
  Mapping:
    Type: AWS::Lambda::EventSourceMapping
    Properties:
      FunctionName: !Ref MyFn
      EventSourceArn: arn:aws:sqs:us-east-1:123456789012:q
"""
    probe = REPO_ROOT / "scripts" / "tests" / "_eventsource_probe.yaml"
    probe.write_text(text)
    rel = "scripts/tests/_eventsource_probe.yaml"
    try:
        CUSTOM_RESOURCE_ONLY[rel] = {"MyFn"}
        with pytest.raises(AssertionError, match="not custom-resource-only"):
            test_exemptions_are_really_custom_resource_only(rel)
    finally:
        CUSTOM_RESOURCE_ONLY.pop(rel, None)
        probe.unlink()
