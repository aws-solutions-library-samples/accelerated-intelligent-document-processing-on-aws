# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every alarm must reach a topic that somebody is subscribed to.

Background
----------
``AlertsTopic`` is the destination for every CloudWatch alarm the solution
creates but one — ``patterns/unified/template.yaml`` and every ``nested/*`` stack
declare zero alarms, so alerting funnels through this one topic in the parent
template. Eleven of the twelve alarms publish to it directly; the twelfth,
``BedrockServiceOutageAlarm``, publishes to ``CircuitBreakerTopic`` and reaches
``AlertsTopic`` via the circuit-breaker manager Lambda. For many releases
*nothing was subscribed to* ``AlertsTopic``.
The topic ARN was emitted as the ``SNSAlertsTopicARN`` stack output and an
operator was tacitly expected to go and subscribe by hand; on a default
deployment every alarm transitioned to ``ALARM`` and notified nobody (GitHub
issue #922).

This is the third instance of the same failure shape in this subsystem, which is
why it gets a test rather than only a fix:

* #746 — ``WorkflowErrorsAlarm`` watched a metric name that does not exist, so it
  sat in ``INSUFFICIENT_DATA`` through real failures
  (``lib/idp_sdk/tests/unit/test_cloudwatch_alarms.py``);
* the KMS grant for CloudWatch on the topic's customer-managed key was gated on
  ``CircuitBreakerEnabled=true``, so on the default ``false`` every alarm action
  failed authorization (see the ``Allow CloudWatch Alarms to use the key for
  SNS`` statement in ``template.yaml``);
* and this one: a correct alarm, publishing successfully, to a topic with no
  subscribers.

All three present identically — quiet console, no error, no notification — and
none of them is something cfn-lint can see: a topic with no subscription is a
perfectly valid template. So the checks here are structural and read the
committed templates directly (no AWS, no deploy, no build artifacts), in the
style of ``scripts/tests/test_nested_stack_parameters.py``.

Two of those three prior instances were about *conditions*, not structure, so the
repo-wide check below compares alarm and subscription conditions rather than only
matching resources up; and it discovers templates by content rather than naming
the parent, because a guard that counts in one file leaves the same defect open in
the other 29. See ``_subscription_can_serve`` and ``_discover_templates`` for what
each of those does and does not decide.

What is deliberately *not* asserted: that alerting actually works end to end. An
SNS email subscription is created in ``PendingConfirmation`` and delivers nothing
until the recipient clicks the confirmation link, which no static test can
observe. See ``docs/monitoring.md``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_SCRIPT = REPO_ROOT / "scripts" / "discover_templates.sh"
PARENT_TEMPLATE = "template.yaml"
ALERTS_TOPIC = "AlertsTopic"
ADMIN_EMAIL_PARAM = "AdminEmail"
# The condition that must guard the subscription, and the pre-existing condition
# its second leg negates. Both are asserted by name: "some condition is present"
# is satisfied by a condition that is false on every deployment, which silently
# restores #922 (see test_admin_email_is_subscribed_to_alerts).
GUARD_CONDITION = "ShouldSubscribeAdminToAlerts"
SENTINEL_CONDITION = "SuppressAdminInvite"

# Subscription conditions accepted as compatible with an *unconditionally* created
# alarm, beyond "no condition" and "the alarm's own condition". Each entry is a
# deliberate operator opt-out: true on a default deployment, false only because the
# deployer supplied an input that means "do not notify this address". Keep this to
# conditions whose definition is separately pinned by a test in this file -- an
# unpinned name here would be a hole exactly the size of the bug the file exists to
# catch. Name -> why it is an opt-out rather than an accident.
OPERATOR_OPT_OUT_GUARDS = {
    GUARD_CONDITION: (
        "true on a default deployment; false only when AdminEmail is empty or is "
        "the citest@suppress.welcome.email CI sentinel, which can never confirm a "
        "subscription. Its definition is pinned by "
        "test_admin_email_is_subscribed_to_alerts."
    ),
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
    return yaml.load((REPO_ROOT / rel_path).read_text(), Loader=_CfnLoader) or {}


def _discover_templates() -> list[str]:
    """Every CloudFormation template in the repo, as repo-relative paths.

    Discovered by *content*, not named. The first version of this file pinned
    ``PARENT_TEMPLATE`` and nothing else, which closed #922 inside
    ``template.yaml`` and left it open across the other 29 templates: injecting an
    unsubscribed topic plus an alarm targeting it into
    ``patterns/unified/template.yaml`` kept this suite at 4 passed and
    ``lib/idp_sdk/tests/unit/test_cloudwatch_alarms.py`` at 68 passed. That is
    latent rather than live today -- the unified pattern declares no alarms and no
    topics -- but the whole premise of this file is that a topic with no
    subscriber is invisible until something counts, so counting in one file only
    reproduces the defect one directory over.

    ``scripts/discover_templates.sh`` is the repository's content-based discovery,
    already shared by ``make cfn-lint`` and ``make check-arn-partitions`` and
    pinned by ``scripts/tests/test_discover_templates.py``; reusing it (as
    ``test_discover_templates.py`` itself does, by subprocess) means these gates
    cannot drift apart, and an alarm added to a template created after this test
    is covered with no list to update.
    """
    out = subprocess.run(
        [str(DISCOVERY_SCRIPT), "cfn"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def _resources(template: dict) -> dict[str, dict]:
    return {
        name: body
        for name, body in (template.get("Resources") or {}).items()
        if isinstance(body, dict)
    }


def _of_type(template: dict, cfn_type: str) -> dict[str, dict]:
    return {
        name: body
        for name, body in _resources(template).items()
        if body.get("Type") == cfn_type
    }


def _referenced_names(node: Any) -> set[str]:
    """Logical names reached by Ref / Fn::Ref / Fn::GetAtt anywhere under ``node``.

    The loader above normalises ``!Ref X`` to ``{"Fn::Ref": "X"}`` and long-form
    ``Ref: X`` stays ``{"Ref": "X"}``, so both spellings are handled — otherwise a
    style change in the template would silently empty these sets and make every
    assertion below vacuous.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("Ref", "Fn::Ref") and isinstance(value, str):
                found.add(value)
            elif key in ("Fn::GetAtt", "GetAtt"):
                if isinstance(value, str):
                    found.add(value.split(".")[0])
                elif isinstance(value, list) and value and isinstance(value[0], str):
                    found.add(value[0])
            found |= _referenced_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= _referenced_names(item)
    return found


def _subscriptions_by_topic(template: dict) -> dict[str, dict[str, str | None]]:
    """Topic logical name -> {subscription resource name: its ``Condition``}.

    The condition is carried along because a subscription that is not created on
    the deployments where the alarm *is* created notifies nobody, and matching
    alarm to topic to subscription purely structurally cannot see that. See
    ``_subscription_can_serve``.
    """
    by_topic: dict[str, dict[str, str | None]] = {}
    subscriptions = _of_type(template, "AWS::SNS::Subscription")
    for name, body in subscriptions.items():
        topic_arn = (body.get("Properties") or {}).get("TopicArn")
        condition = body.get("Condition")
        for topic in _referenced_names(topic_arn):
            by_topic.setdefault(topic, {})[name] = (
                condition if isinstance(condition, str) else None
            )
    return by_topic


def _subscription_can_serve(
    alarm_condition: str | None, subscription_condition: str | None
) -> bool:
    """Could this subscription exist on a deployment where the alarm exists?

    WHAT THIS DECIDES. A subscription cannot serve an alarm when it carries a
    ``Condition`` that is neither absent, nor the alarm's own ``Condition``, nor a
    named operator opt-out (``OPERATOR_OPT_OUT_GUARDS``). That is the gap a
    mutation exposed: re-pointing ``CircuitBreakerTopicSubscription`` at
    ``SuppressAdminInvite`` -- false on every normal deployment -- leaves
    ``BedrockServiceOutageAlarm`` and ``CircuitBreakerTopic`` both present and the
    subscription absent, so the alarm publishes into a void. Before this rule the
    suite reported 4 passed on that mutated template, which is the same
    report-OK-by-silence shape as the ``CircuitBreakerEnabled``-gated KMS grant
    named in this module's docstring.

    WHAT THIS DOES NOT DECIDE. General CloudFormation condition satisfiability. It
    does not evaluate ``Fn::And`` / ``Fn::Or`` / ``Fn::Equals``, does not know what
    parameter values a deployer will pass, and cannot tell that two
    differently-named conditions are logically equivalent or that one implies the
    other. Such a pair is reported as incompatible, and the resolution is to name
    the subscription's condition in ``OPERATOR_OPT_OUT_GUARDS`` with a
    justification -- not to loosen this rule. That is deliberate: a satisfiability
    solver here would be a second implementation of CloudFormation to keep correct,
    and a check that over-claims is worse than a narrow one that says what it
    covers.
    """
    if subscription_condition is None:
        # Created on every deployment, so it exists wherever the alarm does.
        return True
    if subscription_condition == alarm_condition:
        # Identical guard: the two resources appear and disappear together.
        return True
    return subscription_condition in OPERATOR_OPT_OUT_GUARDS


def _intrinsic(node: Any, name: str) -> Any:
    """Operand(s) of intrinsic ``name`` on ``node``, or ``None``.

    Accepts the short-form spelling the loader normalises to ``Fn::<name>`` and
    the long-form ``<name>:`` key, so a purely stylistic rewrite of the template
    cannot turn these checks into no-ops.
    """
    if not isinstance(node, dict):
        return None
    if f"Fn::{name}" in node:
        return node[f"Fn::{name}"]
    return node.get(name)


def _negated(node: Any) -> Any:
    """The single operand of an ``Fn::Not``, or ``None`` if ``node`` is not one."""
    operand = _intrinsic(node, "Not")
    if isinstance(operand, list) and len(operand) == 1:
        return operand[0]
    return None


def _is_admin_email_empty_test(node: Any) -> bool:
    """True for ``!Equals [!Ref AdminEmail, '']``, in either operand order."""
    operands = _intrinsic(node, "Equals")
    if not isinstance(operands, list) or len(operands) != 2:
        return False
    left, right = operands
    return any(
        _referenced_names(a) == {ADMIN_EMAIL_PARAM} and b == ""
        for a, b in ((left, right), (right, left))
    )


def _is_condition_ref(node: Any, condition_name: str) -> bool:
    """True for ``!Condition <condition_name>``."""
    return _intrinsic(node, "Condition") == condition_name


@pytest.mark.unit
def test_alerts_topic_exists() -> None:
    """Guard the discovery — a rename would make everything below vacuous."""
    topics = _of_type(_load(PARENT_TEMPLATE), "AWS::SNS::Topic")
    assert ALERTS_TOPIC in topics, (
        f"{PARENT_TEMPLATE} no longer declares an AWS::SNS::Topic named "
        f"{ALERTS_TOPIC!r} (found: {sorted(topics)}). If the alert topic was "
        f"renamed, update ALERTS_TOPIC here and in docs/monitoring.md."
    )


@pytest.mark.unit
def test_alerts_topic_has_a_subscription() -> None:
    """#922: a topic every alarm publishes to, and nobody receives."""
    template = _load(PARENT_TEMPLATE)
    subscribers = _subscriptions_by_topic(template).get(ALERTS_TOPIC, {})
    assert subscribers, (
        f"{ALERTS_TOPIC} has no AWS::SNS::Subscription targeting it, so every "
        f"alarm in {PARENT_TEMPLATE} publishes into a topic with zero "
        f"subscribers and no operator is notified (GitHub issue #922). Emitting "
        f"the topic ARN as a stack output is not a substitute — nobody reads "
        f"outputs looking for homework."
    )


@pytest.mark.unit
def test_admin_email_is_subscribed_to_alerts() -> None:
    """The stack already collects an operator address; it must be the subscriber.

    Pinning ``AdminEmail`` specifically (not just "some subscription exists")
    keeps the default deployment self-sufficient: it is the only address the
    stack knows, and wiring it is what removes the manual post-deploy step.

    The guard condition is pinned by name *and* by the meaning of its definition.
    Asserting only that the resource carries some ``Condition`` is vacuous: it
    passes for a condition that is false on every possible deployment, and for
    ``SuppressAdminInvite``, which is the intended guard inverted. Both of those
    reinstate #922 — no subscriber, or only an unreachable one — while a
    "has a condition" check reports success.
    """
    template = _load(PARENT_TEMPLATE)
    subscriptions = _of_type(template, "AWS::SNS::Subscription")
    matching: dict[str, dict] = {}
    for name, body in subscriptions.items():
        props = body.get("Properties") or {}
        topics = _referenced_names(props.get("TopicArn"))
        endpoints = _referenced_names(props.get("Endpoint"))
        if ALERTS_TOPIC in topics and ADMIN_EMAIL_PARAM in endpoints:
            matching[name] = body

    assert matching, (
        f"no AWS::SNS::Subscription subscribes the AdminEmail parameter to "
        f"{ALERTS_TOPIC}. Found subscriptions: {sorted(subscriptions)}."
    )

    for name, body in matching.items():
        props = body.get("Properties") or {}
        assert props.get("Protocol") == "email", (
            f"{name} subscribes AdminEmail to {ALERTS_TOPIC} with Protocol "
            f"{props.get('Protocol')!r}; an email address needs 'email'."
        )
        # The guard exists so an empty or CI-sentinel AdminEmail does not create a
        # subscription that can never confirm, and so the --headless transform has
        # a condition to strip alongside the removed AdminEmail parameter.
        #
        # Assert the guard *by name*, not merely that some Condition is present.
        # "Has a condition" is satisfied by a condition that is false on every
        # possible deployment (the subscription is then never created, which is
        # #922 again), and by SuppressAdminInvite itself, which is the exact
        # inversion of the intended guard: it would subscribe only the
        # unreachable CI sentinel and nobody else.
        assert body.get("Condition") == GUARD_CONDITION, (
            f"{name} is guarded by Condition {body.get('Condition')!r}, expected "
            f"{GUARD_CONDITION!r}. The guard must skip the subscription only when "
            f"{ADMIN_EMAIL_PARAM} is empty or is the CI sentinel. A different "
            f"condition can be false on every deployment — which never creates "
            f"the subscription and silently restores #922 — or, if it is "
            f"{SENTINEL_CONDITION!r}, inverts the intent and subscribes only the "
            f"unconfirmable sentinel address."
        )

    # ...and the guard's *definition* has to actually mean what its name says.
    # Checked structurally (not as a string match on serialised YAML) so
    # reformatting is free but a change of meaning is not. This asserts the two
    # legs are present; it does not forbid additional legs, so a future third
    # leg is allowed and must be reviewed on its own merits.
    conditions = template.get("Conditions") or {}
    assert GUARD_CONDITION in conditions, (
        f"{PARENT_TEMPLATE} has no {GUARD_CONDITION!r} entry in its Conditions "
        f"block, so the subscription references a condition that does not exist "
        f"and CloudFormation rejects the whole template. Found: "
        f"{sorted(conditions)}."
    )

    definition = conditions[GUARD_CONDITION]
    legs = _intrinsic(definition, "And")
    assert isinstance(legs, list), (
        f"{GUARD_CONDITION} is defined as {definition!r}, which is not an "
        f"Fn::And. It must AND a non-empty {ADMIN_EMAIL_PARAM} test with a "
        f"negation of {SENTINEL_CONDITION}. Any other expression — in "
        f"particular a comparison of two unequal literals — is false on every "
        f"deployment, so the subscription is never created and every alarm "
        f"notifies nobody again (#922)."
    )

    assert any(_is_admin_email_empty_test(_negated(leg)) for leg in legs), (
        f"{GUARD_CONDITION} has no leg negating an empty {ADMIN_EMAIL_PARAM}: "
        f"expected !Not [ !Equals [ !Ref {ADMIN_EMAIL_PARAM}, '' ] ] among its "
        f"Fn::And legs, got {legs!r}. Without it, relaxing "
        f"{ADMIN_EMAIL_PARAM}'s AllowedPattern would create an SNS subscription "
        f"with an empty Endpoint."
    )

    assert any(_is_condition_ref(_negated(leg), SENTINEL_CONDITION) for leg in legs), (
        f"{GUARD_CONDITION} has no leg negating the {SENTINEL_CONDITION} "
        f"condition: expected !Not [ !Condition {SENTINEL_CONDITION} ] among its "
        f"Fn::And legs, got {legs!r}. Without it, CI and automated multi-stack "
        f"deploys subscribe the citest@suppress.welcome.email sentinel, whose "
        f"domain does not resolve, queueing a confirmation email on every deploy "
        f"to an address that can never confirm. Note that negating it is "
        f"required: using {SENTINEL_CONDITION} unnegated subscribes only the "
        f"sentinel."
    )


@pytest.mark.unit
def test_every_alarm_notifies_a_subscribed_topic() -> None:
    """An alarm with no AlarmActions, or one aimed at a dead topic, is silence.

    Adding a subscriber to ``AlertsTopic`` while an alarm sits unwired only half
    closes #922, so this walks every ``AWS::CloudWatch::Alarm`` in *every*
    template the repository ships, not just the parent one. All 12 alarms live in
    ``template.yaml`` today, which is precisely why the check must not name it: a
    thirteenth added to a nested or feature-platform stack would otherwise be
    covered by nothing (see ``_discover_templates``).

    Three properties per alarm: it sets ``AlarmActions``; every target is an
    ``AWS::SNS::Topic`` declared in the same template; and that topic has at least
    one subscription, of any protocol, whose ``Condition`` is compatible with the
    alarm's (``_subscription_can_serve``).

    ``BedrockServiceOutageAlarm`` is the one alarm that does not target
    ``AlertsTopic``: it publishes to ``CircuitBreakerTopic``, which is subscribed
    by ``CircuitBreakerManagerFunction`` (protocol ``lambda``) — and that function
    in turn publishes a human-readable message to ``AlertsTopic``. Both carry the
    same ``CircuitBreakerEnabledCondition``, so they satisfy the rule here.
    """
    templates = _discover_templates()
    assert len(templates) >= 20, (
        f"content-based discovery returned only {len(templates)} template(s): "
        f"{templates}. The repository ships around 30, so a collapsed count means "
        f"scripts/discover_templates.sh broke and this check has gone vacuous — "
        f"not that the templates went away."
    )

    # A stale opt-out entry is a permanent hole, so require each to still name a
    # real condition somewhere in the tree.
    declared_conditions = {
        name
        for rel in templates
        for name in (_load(rel).get("Conditions") or {})
        if isinstance(name, str)
    }
    stale = sorted(set(OPERATOR_OPT_OUT_GUARDS) - declared_conditions)
    assert not stale, (
        f"OPERATOR_OPT_OUT_GUARDS names condition(s) no template declares: "
        f"{stale}. A renamed or deleted condition left in that dict silently "
        f"widens what counts as an acceptable subscription guard. Remove the "
        f"entry, or point it at the condition's new name."
    )

    total_alarms = 0
    findings: list[str] = []
    for rel in templates:
        template = _load(rel)
        alarms = _of_type(template, "AWS::CloudWatch::Alarm")
        if not alarms:
            continue
        total_alarms += len(alarms)
        topics = _of_type(template, "AWS::SNS::Topic")
        subscriptions = _subscriptions_by_topic(template)

        for name, body in sorted(alarms.items()):
            alarm_condition = body.get("Condition")
            alarm_condition = (
                alarm_condition if isinstance(alarm_condition, str) else None
            )
            actions = (body.get("Properties") or {}).get("AlarmActions") or []
            targets = sorted(_referenced_names(actions))
            if not targets:
                findings.append(
                    f"{rel}: {name} has no AlarmActions naming a resource, so it "
                    f"can only ever be noticed by someone already looking at the "
                    f"console."
                )
                continue
            for target in targets:
                if target not in topics:
                    # Fail closed. Every alarm in the repo today points at a topic
                    # declared beside it, so this cannot be reached without a new
                    # wiring shape (a topic ARN passed in as a parameter, say)
                    # whose subscriber this file has no way to see.
                    findings.append(
                        f"{rel}: {name} points AlarmActions at {target!r}, which "
                        f"is not an AWS::SNS::Topic declared in that template "
                        f"(topics there: {sorted(topics)}). This check can only "
                        f"follow a topic it can see, so extend it to cover the new "
                        f"wiring rather than assuming somebody is subscribed."
                    )
                    continue
                candidates = subscriptions.get(target, {})
                usable = sorted(
                    sub
                    for sub, cond in candidates.items()
                    if _subscription_can_serve(alarm_condition, cond)
                )
                if usable:
                    continue
                findings.append(
                    f"{rel}: {name} (Condition {alarm_condition!r}) publishes to "
                    f"{target!r}, which has no AWS::SNS::Subscription that exists "
                    f"on the deployments where the alarm does, so it notifies "
                    f"nobody (#922). Subscriptions on that topic and their "
                    f"conditions: {candidates or 'none'}. A subscription counts "
                    f"only if it has no Condition, carries the alarm's own "
                    f"Condition, or is one of the reviewed operator opt-outs "
                    f"{sorted(OPERATOR_OPT_OUT_GUARDS)}."
                )

    assert total_alarms >= 10, (
        f"expected the repository to declare at least 10 CloudWatch alarms, found "
        f"{total_alarms} across {len(templates)} templates. A collapsed count "
        f"means the discovery broke, not that the alarms went away."
    )
    assert not findings, "alarm(s) notify nobody:\n  " + "\n  ".join(findings)


# ---------------------------------------------------------------------------
# The --headless variant: an accepted gap, so documentation IS the mitigation
# ---------------------------------------------------------------------------
# A --headless deployment keeps AlertsTopic and every alarm but creates no
# subscription, because the transform removes the AdminEmail parameter with the
# Cognito resources and a subscription cannot Ref a parameter that is gone. Adding
# an optional AlertsEmail parameter was considered and rejected (issue #984):
# headless operators are automating and mostly attach a pager, chat webhook or
# existing operational topic through their own IaC, so the parameter would be one
# most of them never set.
#
# That decision makes documentation the entire mitigation for a failure that is
# otherwise silent -- an unsubscribed topic accepts every publish successfully. A
# mitigation that consists of three sentences in three files is exactly the kind
# that rots, so it is checked here rather than trusted. The pairing is the point:
# the tests below fail if the transform stops removing the subscription (the docs
# would then be wrong in the other direction) or if any of the three documents
# stops telling the operator to subscribe.
HEADLESS_TRANSFORM = (
    REPO_ROOT / "lib" / "idp_sdk" / "idp_sdk" / "_core" / "template_transform.py"
)

#: Each document that must tell a headless operator to set up delivery, with a
#: phrase that carries the *obligation* rather than merely mentioning the topic.
#: Matched against the whitespace-flattened text, because all three wrap at ~80
#: columns and a line-oriented search would miss a sentence that spans a break.
HEADLESS_DELIVERY_DOCS = {
    "docs/headless-deployment.md": "required post-deploy step",
    "docs/monitoring.md": "creates no subscription on `AlertsTopic`",
    "docs/govcloud-operations.md": "creates no subscription at all",
}


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.unit
def test_headless_transform_removes_the_subscription_and_keeps_the_topic() -> None:
    """Pin the shape the documentation describes, from the transform itself.

    Both halves matter. If the subscription stopped being removed, the headless
    template would carry a resource referencing a deleted parameter, which is a hard
    error at validate time -- and the documentation telling the operator to
    subscribe by hand would be wrong. If the *topic* or the ARN output were removed,
    the documented instruction would be impossible to follow, because there would be
    nothing to subscribe to.
    """
    import sys

    sdk = str(REPO_ROOT / "lib" / "idp_sdk")
    if sdk not in sys.path:
        sys.path.insert(0, sdk)
    from idp_sdk._core.template_transform import HeadlessTemplateTransformer

    transformer = HeadlessTemplateTransformer()
    removed = transformer.all_resources_to_remove

    assert "AlertsTopicAdminEmailSubscription" in removed, (
        "the headless transform no longer removes AlertsTopicAdminEmailSubscription. "
        "It must: AdminEmail is removed with the Cognito resources, and a resource "
        "left Ref'ing a deleted parameter is a hard template error. If the parameter "
        "is now retained, the documentation in HEADLESS_DELIVERY_DOCS is wrong and "
        "issue #984 has been reopened by implementation rather than by decision."
    )
    assert ALERTS_TOPIC not in removed, (
        f"the headless transform removes {ALERTS_TOPIC}. The documented remedy is to "
        f"subscribe to the SNSAlertsTopicARN output by hand, which is impossible "
        f"without the topic."
    )
    for output in ("SNSAlertsTopicARN", "SNSAlertsTopicConsoleURL"):
        assert output not in transformer.outputs_to_remove, (
            f"the headless transform removes the {output} output, which is how the "
            f"documentation tells an operator to find the topic to subscribe to."
        )


@pytest.mark.unit
def test_headless_alert_delivery_is_documented() -> None:
    """Documentation is the whole mitigation here, so it is a checked artifact.

    Each phrase is chosen to carry the obligation, not just the subject. "The
    ``AlertsTopic`` SNS topic" appearing somewhere in a page proves nothing -- a page
    can name the topic while leaving a reader believing the stack subscribes
    something, which is what two of these three pages did before #984 was decided.
    """
    missing = []
    for rel, phrase in HEADLESS_DELIVERY_DOCS.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"{rel}: file not found")
            continue
        if phrase.lower() not in _flat(path.read_text(encoding="utf-8")).lower():
            missing.append(f"{rel}: does not state {phrase!r}")

    assert not missing, (
        "the --headless alert-delivery gap is an ACCEPTED gap (issue #984), which "
        "means these documents are the only thing standing between an operator and "
        "alarms that notify nobody:\n  " + "\n  ".join(missing) + "\n"
        "Restore the statement, or -- if the gap was closed in the template instead "
        "(an AlertsEmail parameter, or any subscription the headless variant "
        "creates) -- delete the entry here and say so in #984, because the "
        "documentation would then be describing a limitation that no longer exists."
    )


@pytest.mark.unit
def test_the_transform_states_no_alarm_count() -> None:
    """The comment next to the removal must not restate a number nothing derives.

    It used to read "the topic and all 12 alarms stay". ``template.yaml`` has since
    grown more alarms, so the comment was wrong and nothing noticed, because a
    number in a comment is not connected to the thing it counts. This is the same
    defect class the derived-at-test-time document guards exist for; the cheapest
    fix at this site is to state no count at all, and this test keeps it that way.
    """
    text = _flat(HEADLESS_TRANSFORM.read_text(encoding="utf-8"))
    import re

    offenders = re.findall(r"all \d+ alarms|\d+ alarms stay", text)
    assert not offenders, (
        f"{HEADLESS_TRANSFORM.relative_to(REPO_ROOT)} states an alarm count in a "
        f"comment: {offenders}. Nothing derives it, so it goes stale the next time "
        f"an alarm is added -- which is exactly what happened to 'all 12 alarms'. "
        f"Say 'every alarm' instead, or derive the number in a test."
    )
