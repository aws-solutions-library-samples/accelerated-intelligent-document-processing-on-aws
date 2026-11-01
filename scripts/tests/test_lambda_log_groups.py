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
5. A custom-resource handler with a declared log group has a role that denies
   ``logs:CreateLogGroup`` on that group (#1754), so the last log lines of its
   stack-delete invocation, which Lambda delivers after the group is deleted,
   cannot recreate it with no retention. See ``_check_rule_5``.

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

Earlier revisions of this gate could be defeated in several ways (the
parametrized cases below pin 14, plus 2 standalone bypass tests) — ``Fn::Join``
and the list form of ``Fn::Sub`` slipped rule 3; a ``LoggingConfig`` naming a
non-existent log group, or one merely *mentioning* a real one
(``!Sub '${G}-suffix'``), satisfied rule 1; ``RetentionInDays: ~`` and
``!Ref 'AWS::NoValue'`` satisfied rule 2; an ``Fn::If`` branch yielding
``AWS::NoValue`` bypassed rules 1 and 4; and the exemption check accepted any
export whose value merely mentioned the function.
``test_gate_catches_known_bypasses`` pins each of those closed so the rules
cannot silently weaken again.

Two seams remain, both known and both currently harmless:

* **Rule 2 fails open on shapes it does not recognise**, where rule 1 fails
  closed. ``RetentionInDays: !Ref SomeUndeclaredParam``, ``''``, ``7777`` (not a
  valid CloudWatch value) and a malformed 2-arity ``Fn::If`` all pass here.
  CloudFormation or cfn-lint rejects each at deploy, so nothing ships. Since
  ``make cfn-lint`` joined ``lint-cicd`` these are also caught in CI, which
  weakens but does not remove the concern: cfn-lint validates a *value*, this gate
  validates the *shape*, and only the latter sees an intrinsic that resolves
  differently per branch. Note also that five templates declare ``LogRetentionDays`` with
  no ``AllowedValues`` (two of them defaulting to 365), so the ``!Ref`` leg is
  not as constrained as it looks.
* **``Fn::ForEach`` / ``Transform: AWS::LanguageExtensions`` is invisible.** A
  ``Fn::ForEach::X`` key under ``Resources`` maps to a list, so ``_functions``
  and ``_log_groups`` skip it and every rule silently passes. The repo does not
  use it today (checked); if that changes, this gate must be taught about it
  rather than trusted.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from repo_files import tracked_paths


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
    "samples/lambda-hook-inference/GENAIIDP-cohere-parse-hook/template.yaml",
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


def _check_rule_1(doc: dict, exempt: set[str]) -> list[str]:
    """Rule 1's problems for one parsed template.

    Takes no ``rel_path``: it used to, and never read it — every entry returned
    here names the *resource*, and both callers prepend the template themselves.
    The synthetic caller had to invent a ``"<synthetic>"`` placeholder to satisfy
    the signature, which is the clearest sign the argument was vestigial. Now
    symmetric with ``_check_rule_2``, ``_check_rule_3`` and ``_check_rule_4``,
    which have always taken just the document.
    """
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
    problems = _check_rule_1(_load(rel_path), CUSTOM_RESOURCE_ONLY.get(rel_path, set()))
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


def custom_resource_only_violation(rel_path: str, name: str) -> str | None:
    """Why Lambda ``name`` in ``rel_path`` is NOT custom-resource-only, else None.

    An exempt function must be reachable only during a stack operation: a
    ``ServiceToken`` target in this template, or an install hook whose ARN is
    exported via a direct ``GetAtt`` on that function. It must also have no
    event source of any kind.

    Public, and it loads the template itself, because
    ``test_log_group_encryption.py`` reuses it to justify that gate's *own*
    exemption list rather than keeping a second copy of this reasoning that
    could drift from it. Loading by ``rel_path`` is deliberate: every helper
    here keys on the long intrinsic form (``Fn::GetAtt``) because that is what
    this module's loader emits, and a document parsed by a loader that
    preserves the short form (``!GetAtt``) would resolve no references at all
    and make every function look unjustified. Taking a pre-parsed document
    would make that silent; taking a path cannot.

    Structural and not airtight: it proves the function is reachable only
    through a stack operation as far as *this template* declares, which is the
    strongest statement a template-only check can make.
    """
    doc = _load(rel_path)
    resources = doc.get("Resources", {}) or {}
    outputs = doc.get("Outputs", {}) or {}

    if name not in resources:
        return f"{name}: not a resource in {rel_path}"

    # (a) ServiceToken target of a custom resource in this template.
    is_service_token = any(
        name
        in _referenced_logical_ids((body.get("Properties") or {}).get("ServiceToken"))
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
        return f"{name}: event_sources={event_sources or None}"
    return None


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", sorted(CUSTOM_RESOURCE_ONLY))
def test_exemptions_are_really_custom_resource_only(rel_path: str) -> None:
    """The exemption list is verified, not trusted."""
    resources = _load(rel_path).get("Resources", {}) or {}

    unjustified = []
    for name in sorted(CUSTOM_RESOURCE_ONLY[rel_path]):
        assert name in resources, (
            f"{rel_path}: CUSTOM_RESOURCE_ONLY names {name!r}, which no longer "
            f"exists. Remove the stale entry."
        )
        violation = custom_resource_only_violation(rel_path, name)
        if violation:
            unjustified.append(violation)

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


def _custom_resource_log_groups(doc: dict) -> dict[str, set[str]]:
    """``{function: log groups}`` for every custom-resource handler with a group.

    A function counts when a ``ServiceToken`` in this template names it and its
    ``LoggingConfig`` resolves to an ``AWS::Logs::LogGroup`` declared here. An
    ``Fn::If`` between two groups yields both, and rule 5 needs a deny on each. CloudFormation sends each such function a ``Delete`` while it
    deletes the stack, so these are the groups rule 5 is about. The functions in
    ``CUSTOM_RESOURCE_ONLY`` declare no group and drop out on the second leg.
    """
    resources = doc.get("Resources", {}) or {}
    log_groups = set(_log_groups(resources))
    tokens: set[str] = set()
    for body in resources.values():
        if isinstance(body, dict):
            tokens |= _referenced_logical_ids(
                (body.get("Properties") or {}).get("ServiceToken")
            )
    found: dict[str, set[str]] = {}
    for name, body in _functions(resources).items():
        if name not in tokens:
            continue
        groups = set()
        for branch in _branches(_logging_config_target(body)):
            if _resolves_exactly_to(branch, log_groups):
                groups |= _referenced_logical_ids(branch) & log_groups
        if groups:
            found[name] = groups
    return found


def _unconditional_statements(policy_items: Any) -> list[dict]:
    """Statements in a ``Policies`` list that deploy on every branch.

    Handles both shapes: a SAM function's items (``{Statement: [...]}``) and an
    ``AWS::IAM::Role``'s (``{PolicyName, PolicyDocument: {Statement: [...]}}``).
    Anything reached only through ``Fn::If`` is left out, because a statement on
    one branch is absent on the other. That needs no test of its own: an
    ``Fn::If`` wrapper has neither ``PolicyDocument`` nor ``Statement`` nor
    ``Effect``, so nothing under it is read as a statement.
    """
    statements: list[dict] = []
    if not isinstance(policy_items, list):
        return statements
    for item in policy_items:
        if not isinstance(item, dict):
            continue
        document = item.get("PolicyDocument", item)
        if not isinstance(document, dict):
            continue
        listed = document.get("Statement")
        listed = listed if isinstance(listed, list) else [listed]
        statements.extend(s for s in listed if isinstance(s, dict))
    return statements


def _denies_creating(statement: dict, group: str) -> bool:
    """True if ``statement`` alone denies ``logs:CreateLogGroup`` on ``group``."""
    if statement.get("Effect") != "Deny" or "Condition" in statement:
        return False
    actions = statement.get("Action")
    actions = actions if isinstance(actions, list) else [actions]
    if not any(
        isinstance(a, str) and fnmatch.fnmatchcase("logs:createloggroup", a.lower())
        for a in actions
    ):
        return False
    resources = statement.get("Resource")
    resources = resources if isinstance(resources, list) else [resources]
    arn = (
        "arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}"
        f":log-group:${{{group}}}"
    )
    # Both spellings of the group's ARN, because which one IAM evaluates for
    # this call was not observed: the CloudWatch Logs documentation names the
    # bare log-group ARN, and ``GetAtt <group>.Arn`` is the ``:*`` form.
    bare = suffixed = False
    for resource in resources:
        if resource == "*":
            return True
        if not isinstance(resource, dict):
            continue
        attr = resource.get("Fn::GetAtt")
        sub = resource.get("Fn::Sub")
        if attr in (f"{group}.Arn", [group, "Arn"]) or sub == f"{arn}:*":
            suffixed = True
        if sub == arn:
            bare = True
    return bare and suffixed


def _check_rule_5(doc: dict) -> list[str]:
    """Rule 5 (#1754): a custom-resource handler cannot recreate its own group.

    Deleting the stack sends the handler a ``Delete``; Lambda delivers that
    invocation's last lines a few seconds after CloudFormation has deleted the
    function, its role and its group, and the delivery recreates the group under
    the function's role, with no retention and no KMS key. The function's own
    ordering cannot prevent it: it already depends on its group through
    ``LoggingConfig``, so it is deleted first. The role denying
    ``logs:CreateLogGroup`` on the group costs nothing while the stack lives,
    because CloudFormation creates the group before the function, and naming
    the group in the role's policy makes the role depend on it, so the group is
    deleted after the role. On one live delete that moved each group's deletion
    from about 4 to about 15 seconds after its function's, and no group came
    back; the deny itself was never exercised there, so whether IAM still
    applies it to a deleted role's session is unobserved. The delay is a margin,
    not a guarantee, and the deny is the backstop for a delivery later still.
    """
    resources = doc.get("Resources", {}) or {}
    problems = []
    for name, groups in sorted(_custom_resource_log_groups(doc).items()):
        properties = resources[name].get("Properties") or {}
        role = properties.get("Role")
        if role is None:
            owner, items = name, properties.get("Policies")
        else:
            ids = _referenced_logical_ids(role)
            role_id = next(iter(ids)) if len(ids) == 1 else None
            role_body = resources.get(role_id) if role_id else None
            if (
                not isinstance(role_body, dict)
                or role_body.get("Type") != "AWS::IAM::Role"
            ):
                problems.append(
                    f"{name}: its Role is not an AWS::IAM::Role in this template, "
                    f"so nothing here can show it denies recreating {sorted(groups)}"
                )
                continue
            owner, items = role_id, (role_body.get("Properties") or {}).get("Policies")
        statements = _unconditional_statements(items)
        for group in sorted(groups):
            if not any(_denies_creating(s, group) for s in statements):
                problems.append(
                    f"{name}: {owner}'s policies do not deny logs:CreateLogGroup "
                    f"on {group}"
                )
    return problems


@pytest.mark.unit
@pytest.mark.parametrize("rel_path", TEMPLATES)
def test_custom_resource_functions_cannot_recreate_their_log_group(
    rel_path: str,
) -> None:
    """Rule 5 (#1754): deleting the stack must not leave the group behind."""
    problems = _check_rule_5(_load(rel_path))
    assert not problems, (
        f"{rel_path}: a custom-resource function's last log lines arrive after "
        f"the stack delete removed its log group, and recreate it with no "
        f"retention and no KMS key: {problems}. Add a Deny on "
        f"logs:CreateLogGroup naming the group to the function's role (see "
        f".claude/skills/infrastructure.md)."
    )


@pytest.mark.unit
def test_rule_5_reaches_real_functions() -> None:
    """Rule 5's universe is closed: every handler with a group is in it.

    Walked independently of ``_custom_resource_log_groups``: any function some
    resource's ``ServiceToken`` names that also has a ``LoggingConfig`` naming a
    declared group must be in the universe with every such group. A derivation
    that dropped members would leave rule 5 green over fewer functions.
    """
    total = 0
    for rel_path in TEMPLATES:
        doc = _load(rel_path)
        resources = doc.get("Resources", {}) or {}
        declared = set(_log_groups(resources))
        universe = _custom_resource_log_groups(doc)
        for name, body in _functions(resources).items():
            targeted = any(
                isinstance(other, dict)
                and name
                in _referenced_logical_ids(
                    (other.get("Properties") or {}).get("ServiceToken")
                )
                for other in resources.values()
            )
            named = _referenced_logical_ids(_logging_config_target(body)) & declared
            if targeted and named:
                assert universe.get(name) == named, (rel_path, name, universe.get(name))
                total += 1
        assert len(universe) == sum(
            1 for name in universe if name in _functions(resources)
        )
    assert total, "rule 5 reaches no function at all"


_CR = """
Resources:
  Handler:
    Type: AWS::Serverless::Function
    Properties:
      Handler: index.handler
      LoggingConfig:
        LogGroup: !Ref G
{policies}
  G:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
  Other:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
  Thing:
    Type: Custom::Thing
    Properties:
      ServiceToken: !GetAtt Handler.Arn
{extra}
"""

_DENY = """        - Effect: Deny
          Action: {action}
          Resource: {resource}"""


def _sam(statement: str) -> str:
    return "      Policies:\n        - Statement:\n" + statement.replace(
        "\n", "\n  "
    ).replace("        - Effect", "          - Effect", 1)


_ROLE = """  R:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: p
          PolicyDocument:
            Statement:
{statement}"""


def _role_shape(statement: str) -> dict:
    return {
        "policies": "      Role: !GetAtt R.Arn",
        "extra": _ROLE.format(
            statement=statement.replace("        - ", "              - ").replace(
                "\n          ", "\n                "
            )
        ),
    }


_BOTH = (
    '[!GetAtt G.Arn, !Sub "arn:${AWS::Partition}:logs:${AWS::Region}'
    ':${AWS::AccountId}:log-group:${G}"]'
)
_GOOD = _DENY.format(action="logs:CreateLogGroup", resource=_BOTH)


@pytest.mark.unit
@pytest.mark.parametrize(
    "shape",
    [
        {"policies": _sam(_GOOD), "extra": ""},
        {
            "policies": _sam(
                _DENY.format(action="logs:CreateLogGroup", resource="'*'")
            ),
            "extra": "",
        },
        _role_shape(_GOOD),
    ],
)
def test_rule_5_accepts_a_deny_on_the_group(shape: dict) -> None:
    """Positive control, so the bypass cases below fail for their own reason."""
    assert _check_rule_5(_load_text(_CR.format(**shape))) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,shape",
    [
        ("no-policies", {"policies": "", "extra": ""}),
        (
            "allow-not-deny",
            {"policies": _sam(_GOOD.replace("Deny", "Allow")), "extra": ""},
        ),
        (
            "deny-other-group",
            {
                "policies": _sam(
                    _GOOD.replace("G.Arn", "Other.Arn").replace("${G}", "${Other}")
                ),
                "extra": "",
            },
        ),
        (
            "getatt-plus-a-sub-that-only-mentions-the-group",
            {
                "policies": _sam(
                    _DENY.format(
                        action="logs:CreateLogGroup",
                        resource="[!GetAtt G.Arn, !Sub '${G}-suffix']",
                    )
                ),
                "extra": "",
            },
        ),
        (
            "getatt-names-another-group",
            {"policies": _sam(_GOOD.replace("G.Arn", "Other.Arn")), "extra": ""},
        ),
        (
            "only-the-getatt-form",
            {
                "policies": _sam(
                    _DENY.format(action="logs:CreateLogGroup", resource="!GetAtt G.Arn")
                ),
                "extra": "",
            },
        ),
        (
            "only-the-bare-arn",
            {
                "policies": _sam(
                    _DENY.format(
                        action="logs:CreateLogGroup",
                        resource='!Sub "arn:${AWS::Partition}:logs:${AWS::Region}'
                        ':${AWS::AccountId}:log-group:${G}"',
                    )
                ),
                "extra": "",
            },
        ),
        (
            "deny-mentions-group",
            {
                "policies": _sam(
                    _DENY.format(
                        action="logs:CreateLogGroup",
                        resource="!Sub '${G}-suffix'",
                    )
                ),
                "extra": "",
            },
        ),
        (
            "deny-other-action",
            {
                "policies": _sam(_GOOD.replace("CreateLogGroup", "CreateLogStream")),
                "extra": "",
            },
        ),
        (
            "deny-with-condition",
            {
                "policies": _sam(
                    _GOOD + "\n          Condition: {Bool: {aws:ViaAWSService: 'true'}}"
                ),
                "extra": "",
            },
        ),
        (
            "deny-behind-fn-if",
            {
                "policies": "      Policies:\n        - !If\n          - C\n"
                "          - Statement:\n"
                + _GOOD.replace("        - ", "              - ").replace(
                    "\n          ", "\n                "
                )
                + "\n          - !Ref AWS::NoValue",
                "extra": "",
            },
        ),
        ("role-without-deny", _role_shape(_GOOD.replace("Deny", "Allow"))),
        (
            "role-not-in-template",
            {"policies": "      Role: !ImportValue SomeRole", "extra": ""},
        ),
    ],
)
def test_rule_5_catches_bypasses(case: str, shape: dict) -> None:
    """Each shape leaves the group recreatable and must be reported."""
    assert _check_rule_5(_load_text(_CR.format(**shape))), case


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
        1: _check_rule_1(doc, exempt),
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


def _discover_unlisted_templates(root: Path | None = None) -> list[str]:
    """Every Lambda-declaring template in the repo that TEMPLATES omits.

    Globs both ``*.yaml`` and ``*.yml``, and does not assume the filename is
    ``template.yaml`` — ``notebooks/examples/demo-lambda/template.yml`` was
    invisible to an earlier ``template.yaml``-only sweep.
    """
    root = (root or REPO_ROOT).resolve()
    listed = {root / rel for rel in TEMPLATES}
    # `scratch/` and `.claude/` are the repo's gitignored local-work directories; both
    # hold whole git worktrees (`scratch/wt-*/`, `.claude/worktrees/agent-*/`), which
    # are full copies of every template. `tracked_paths` excludes them by asking git,
    # which is also what lets this sweep run from *inside* one of those worktrees:
    # matching these names against a file's ABSOLUTE path discards the whole checkout,
    # because the worktree itself lives under `.claude/`. This sweep has no self-guard,
    # so that discarded everything and passed VACUOUSLY — a registration check that
    # registers nothing. See repo_files.py. The set is kept as a second filter on the
    # repo-RELATIVE path so the fallback walk and the git listing agree.
    skip_dirs = {
        ".aws-sam",
        "node_modules",
        ".venv",
        "build",
        "dist",
        ".git",
        "scratch",
        ".claude",
        # The advisory reviewer's inputs: `.ai-review/base/` is a whole copy of
        # the tree at the merge base, and unlike the two above it is a copy this
        # repository's own tooling creates. Kept in step with the sibling gates.
        ".ai-review",
    }
    unlisted = []
    for path in tracked_paths(root, "*.yaml", "*.yml"):
        # The meta-tests below write synthetic probe templates INSIDE the repo
        # and delete them in a `finally`. Skip them, for two reasons: under a
        # parallel run (`pytest -n auto`) this sweep otherwise sees another
        # worker's probe and fails; and if a run is hard-killed between write
        # and unlink, a leaked probe would fail every later run.
        if path.name.startswith("_") and path.name.endswith(
            ("_probe.yaml", "_probe.yml")
        ):
            continue
        if path in listed:
            continue
        if any(part in skip_dirs for part in path.relative_to(root).parts):
            continue
        try:
            doc = _load_text(path.read_text())
        except (yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        if _functions(doc.get("Resources") or {}):
            unlisted.append(str(path.relative_to(root)))
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
def test_discovery_actually_finds_an_unlisted_template(tmp_path: Path) -> None:
    """The discovery sweep must be able to fail.

    Without this, deleting the body of ``_discover_unlisted_templates`` leaves
    the suite green and the coverage guarantee silently gone — one of two
    mechanisms a prior review found unprotected.

    The probe goes in ``tmp_path``, **not** in the repo. An earlier revision wrote
    it into ``scripts/tests/`` and the repo-wide sweep in
    ``test_every_template_with_lambdas_is_listed`` then found *this* test's probe
    under ``pytest -n 8`` — a deterministic parallel-run failure, reproduced 3/3.
    A hard kill between write and unlink also leaked the file and broke every
    later run.
    """
    probe = tmp_path / "some-service" / "template.yml"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "Resources:\n"
        "  ProbeFn:\n"
        "    Type: AWS::Serverless::Function\n"
        "    Properties:\n"
        "      Handler: index.handler\n"
    )
    assert _discover_unlisted_templates(root=tmp_path) == ["some-service/template.yml"]


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


@pytest.mark.unit
def test_rule_5_needs_a_deny_on_every_group_an_fn_if_can_pick() -> None:
    """``LogGroup: !If [C, !Ref G, !Ref Other]`` logs to either group."""
    one = _CR.format(policies=_sam(_GOOD), extra="").replace(
        "        LogGroup: !Ref G", "        LogGroup: !If [C, !Ref G, !Ref Other]"
    )
    assert "!If [C, !Ref G, !Ref Other]" in one
    assert _check_rule_5(_load_text(one)) == [
        "Handler: Handler's policies do not deny logs:CreateLogGroup on Other"
    ]
    other = _GOOD.replace("G.Arn", "Other.Arn").replace("${G}", "${Other}")
    both = _CR.format(policies=_sam(_GOOD + "\n" + other), extra="").replace(
        "        LogGroup: !Ref G", "        LogGroup: !If [C, !Ref G, !Ref Other]"
    )
    assert _check_rule_5(_load_text(both)) == [], both
