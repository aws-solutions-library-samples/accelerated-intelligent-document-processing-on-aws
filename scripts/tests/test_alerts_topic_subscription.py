# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every alarm must reach a topic that somebody is subscribed to.

Background
----------
``AlertsTopic`` is the single destination for all CloudWatch alarms the solution
creates — ``patterns/unified/template.yaml`` and every ``nested/*`` stack declare
zero alarms, so all alerting for the whole solution funnels through this one
topic in the parent template. For many releases *nothing was subscribed to it*.
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

What is deliberately *not* asserted: that alerting actually works end to end. An
SNS email subscription is created in ``PendingConfirmation`` and delivers nothing
until the recipient clicks the confirmation link, which no static test can
observe. See ``docs/monitoring.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PARENT_TEMPLATE = "template.yaml"
ALERTS_TOPIC = "AlertsTopic"


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


def _subscriptions_by_topic(template: dict) -> dict[str, list[str]]:
    """Topic logical name -> subscription resources whose TopicArn points at it."""
    by_topic: dict[str, list[str]] = {}
    subscriptions = _of_type(template, "AWS::SNS::Subscription")
    for name, body in subscriptions.items():
        topic_arn = (body.get("Properties") or {}).get("TopicArn")
        for topic in _referenced_names(topic_arn):
            by_topic.setdefault(topic, []).append(name)
    return by_topic


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
    subscribers = _subscriptions_by_topic(template).get(ALERTS_TOPIC, [])
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
    """
    template = _load(PARENT_TEMPLATE)
    subscriptions = _of_type(template, "AWS::SNS::Subscription")
    matching: dict[str, dict] = {}
    for name, body in subscriptions.items():
        props = body.get("Properties") or {}
        topics = _referenced_names(props.get("TopicArn"))
        endpoints = _referenced_names(props.get("Endpoint"))
        if ALERTS_TOPIC in topics and "AdminEmail" in endpoints:
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
        assert body.get("Condition"), (
            f"{name} has no Condition. It must be guarded so it is skipped when "
            f"AdminEmail is empty or is the CI sentinel — see "
            f"ShouldSubscribeAdminToAlerts in {PARENT_TEMPLATE}."
        )


@pytest.mark.unit
def test_every_alarm_notifies_a_subscribed_topic() -> None:
    """An alarm with no AlarmActions, or one aimed at a dead topic, is silence.

    Adding a subscriber to ``AlertsTopic`` while an alarm sits unwired only half
    closes #922, so this walks every ``AWS::CloudWatch::Alarm`` in the parent
    template. ``BedrockServiceOutageAlarm`` is the one that does not target
    ``AlertsTopic``: it publishes to ``CircuitBreakerTopic``, which is subscribed
    by ``CircuitBreakerManagerFunction`` (protocol ``lambda``) — and that function
    in turn publishes a human-readable message to ``AlertsTopic``. That still
    satisfies the rule checked here, which is that no alarm points at a topic with
    no subscriber of any protocol.
    """
    template = _load(PARENT_TEMPLATE)
    alarms = _of_type(template, "AWS::CloudWatch::Alarm")
    assert len(alarms) >= 10, (
        f"expected the parent template to declare at least 10 alarms, found "
        f"{len(alarms)}: {sorted(alarms)}. A collapsed count means the "
        f"discovery broke, not that the alarms went away."
    )

    subscribed_topics = set(_subscriptions_by_topic(template))

    unwired: list[str] = []
    unsubscribed: dict[str, list[str]] = {}
    for name, body in alarms.items():
        actions = (body.get("Properties") or {}).get("AlarmActions") or []
        targets = _referenced_names(actions)
        if not targets:
            unwired.append(name)
            continue
        dead = sorted(t for t in targets if t not in subscribed_topics)
        if dead:
            unsubscribed[name] = dead

    assert not unwired, (
        f"alarm(s) in {PARENT_TEMPLATE} have no AlarmActions, so they can only "
        f"ever be noticed by someone already looking at the console: {unwired}."
    )
    assert not unsubscribed, (
        "alarm(s) publish to a topic with no AWS::SNS::Subscription, which "
        f"notifies nobody: {unsubscribed}. Subscribed topics: "
        f"{sorted(subscribed_topics)}."
    )
