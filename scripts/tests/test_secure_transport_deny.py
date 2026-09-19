# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every queue and bucket must carry a resource policy refusing non-TLS requests.

**The gap this closes (issue #964).** Counting the whole repository, ten of the
eleven ``AWS::SQS::QueuePolicy`` resources carried an explicit ``Deny``
conditioned on ``aws:SecureTransport: false``; ``DataMartRollupDLQPolicy`` in
``template.yaml`` did not. Counting ``template.yaml`` alone, the split was nine
of ten — the eleventh policy lives in ``patterns/unified/template.yaml``. Both
denominators appear below and in ``CHANGELOG.md``, so each is stated with the
scope it is measured over. All fifteen ``AWS::S3::BucketPolicy`` resources
carried the equivalent deny. So the control was applied by convention and
omitted once, and nothing could tell the difference — because no gate enumerated
the class.

**Coverage is now 1:1 on both sides, and that is itself asserted.** Ten of the
eleven original queue policies existed for no purpose other than to carry this
deny — they grant nothing. So a queue without a policy is the same omission as
#964 with the resource missing entirely rather than a statement missing from it,
and eight queues were in that state: ``DiscoveryQueue``, ``DiscoveryDLQ``,
``JobTrackerDLQ``, ``TestResultCacheUpdateQueue``, ``TestFileCopierFunctionDLQ``
and ``TestSetFileCopierFunctionDLQ`` in ``template.yaml``, plus
``BootstrapQueue`` and ``BootstrapDLQ`` in
``feature-platform/idp-data-generator/template.yaml``. Those eight policies were
added alongside this gate, so all 19 queues and all 15 buckets are covered, and
``test_every_sqs_queue_has_a_policy`` / ``test_every_s3_bucket_has_a_policy``
keep it that way. Without those two rules the gate would have graded 11 of 19
queues and reported nothing about the rest.

**Honest bound on the impact.** SQS and S3 endpoints are HTTPS and every AWS SDK
uses TLS unless deliberately reconfigured, so the deny is defence in depth
rather than the only thing standing between a caller and cleartext. A queue with
no resource policy also cannot be reached cross-account at all, which bounds the
exposure of the eight further. Its value is that it makes the guarantee explicit
and auditable: without it a caller pointed at an HTTP endpoint would succeed and
nothing in the template would refuse.

**Why this file enumerates rather than lists.** The one-line template fix closes
the instance; this closes the class. Policies are discovered by parsing every
template ``scripts/discover_templates.sh cfn`` reports, which finds templates by
*content* — anything declaring ``AWSTemplateFormatVersion``. A queue policy added
to a template that does not exist yet is therefore covered on the day it lands,
with no list here to update. Same reasoning as
``scripts/tests/test_lambda_log_groups.py`` and
``scripts/tests/test_iam_privilege_escalation.py``.

**Assertions are on semantics, not on source text.** A grep for the string
``SecureTransport`` would pass on a statement whose ``Effect`` is ``Allow``, or
whose condition tests ``true``, or whose ``Resource`` names a different queue —
each of which is a policy that enforces nothing. So each deny is checked for
``Effect: Deny``, a principal covering *every* caller, an action covering the
whole service namespace, a ``Resource`` resolving to the policy's own target, and
a ``Condition`` that is *only* a ``Bool``/``BoolIfExists`` test of
``aws:SecureTransport`` against false. The meta-tests at the bottom mutate each
of those properties in turn and require the gate to fail, so the assertion cannot
rot into a tautology.

Four of those checks are stricter than the obvious implementation, because the
obvious implementation passes a policy that enforces nothing:

* **The target is matched on resolved logical ids, not on a substring.** A
  substring test passes ``Resource: !GetAtt DocumentQueueDLQ.Arn`` on
  ``DocumentQueuePolicy``, whose target is ``DocumentQueue`` — and this repo has
  three prefix-nested pairs where that is a live risk (``DocumentQueue`` /
  ``DocumentQueueDLQ``, ``TestFileCopyQueue`` / ``TestFileCopyQueueDLQ``,
  ``TestSetFileCopyQueue`` / ``TestSetFileCopyQueueDLQ``) plus the same shape on
  the S3 side. Copying a DLQ's policy and forgetting to drop ``DLQ`` from the
  ``Resource`` line yields a policy that denies nothing. Matching logical ids
  means ``Fn::Sub`` placeholders have to be resolved too, since eleven of the
  fifteen bucket policies write ``!Sub "${SomeBucket.Arn}/*"``.

* **A required control must hold on every ``Fn::If`` leg.** Unioning both legs
  and asking ``any()`` is right for detecting a *bad* statement — either leg
  deploys — but exactly inverted for proving a *required* statement present:
  ``Statement: !If [C, [<deny>], [<allow only>]]`` has no deny at all on the
  false leg. ``_guaranteed_deny`` therefore recurses with ``all()`` across legs
  and ``any()`` across list members, and every per-field check requires all of
  its branches to satisfy the requirement. Where an ``Fn::If`` sits somewhere
  this gate cannot prove uniform, it is **refused** with a message asking the
  author to teach the gate the shape, rather than passed. Nothing in the
  repository uses ``Statement: !If`` today, so this is latent by design.

* **A second condition key narrows the deny to nothing.** IAM ANDs condition
  keys, so ``Bool: {aws:SecureTransport: false}`` plus ``StringEquals:
  {aws:SourceVpc: vpc-…}`` refuses cleartext *only from that VPC* and leaves it
  undenied everywhere else. Adding a VPC or source-IP restriction to an existing
  deny is a plausible edit, so the ``Condition`` must contain the
  ``SecureTransport`` test and nothing else. Every one of the 26 policies in the
  tree already does.

* **The principal check is key-aware.** ``Principal: {Service: "*"}`` flattens to
  the same scalar leaf as ``"*"`` but denies only AWS *service* principals,
  leaving an IAM user or an anonymous HTTP caller undenied. ``"*"`` and
  ``{AWS: "*"}`` are the two spellings IAM treats as every caller; nothing else
  qualifies.

**One rule here is about deployability, not security.** A policy must carry the
same CloudFormation ``Condition`` as its target, or it deploys when the target
does not and fails the stack operation. That is the failure mode of adding these
policies in bulk — ``JobTrackerDLQ`` sits behind ``DeployApiGateway`` — and
``make cfn-lint`` does not catch it: dropping the ``Condition`` from
``JobTrackerDLQPolicy`` raises two advisory ``W1001`` warnings, a class that gate
does not fail on, so the mistake would first appear at deploy time.

**A template that will not parse is a failure, not a skip.** Swallowing a
``YAMLError`` would turn "this template declares nothing" into a pass, which is
precisely the vacuity the gate exists to prevent.

Known seams, shared with the sibling gates: ``Fn::If`` is honoured only in its
3-arity form and only where every leg can be proved to satisfy the requirement
(anywhere else it is refused, not passed), and ``Fn::ForEach`` /
``Transform: AWS::LanguageExtensions`` is invisible. The repo does not use
``Fn::ForEach``; if that changes, teach this gate about it rather than trusting
it. A policy targeting a resource declared in a *different* template is also
invisible to the coverage rules, which pair a resource with a policy inside the
same file — CloudFormation cannot express that pairing anyway.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVER = REPO_ROOT / "scripts" / "discover_templates.sh"

# Resource types whose policy document must carry the deny, and the service
# prefix an action has to cover for the deny to apply to the whole API.
POLICY_TYPES = {
    "AWS::SQS::QueuePolicy": "sqs",
    "AWS::S3::BucketPolicy": "s3",
}

# The property naming what the policy is attached to. SQS takes a list, S3 a
# single value; both are normalized to a list of logical ids below.
TARGET_PROPERTY = {
    "AWS::SQS::QueuePolicy": "Queues",
    "AWS::S3::BucketPolicy": "Bucket",
}

# Which policy type must exist for each resource type. Drives the coverage rules
# that stop a queue simply having no policy at all — the shape eight queues were
# in when #964 was filed, and one this gate would otherwise say nothing about.
POLICY_FOR_RESOURCE = {
    "AWS::SQS::Queue": "AWS::SQS::QueuePolicy",
    "AWS::S3::Bucket": "AWS::S3::BucketPolicy",
}

# Condition operators that can express "the request did not arrive over TLS".
# BoolIfExists is included because aws:SecureTransport is always present on an
# SQS or S3 request, so the two are equivalent here.
BOOL_OPERATORS = {"bool", "boolifexists"}

CONDITION_KEY = "aws:securetransport"

# Floors, not exact counts, so adding a queue or bucket does not edit this file.
# Measured on the tree that fixed #964, over every template
# ``scripts/discover_templates.sh cfn`` reports: 19 queues with 19 queue
# policies, and 15 buckets with 15 bucket policies. A drop below these means
# either discovery broke — which would make every assertion below pass
# vacuously — or resources really were deleted.
#
# No per-template breakdown is transcribed here on purpose. It would go stale
# the moment a queue moved between templates, and no gate reads this comment, so
# a wrong split could sit here indefinitely — which is the same defect this file
# exists to catch, one level down. Re-derive it when you need it:
# ``_resources_of_type(_repo_templates(), "AWS::SQS::Queue")``.
MINIMUM_DISCOVERED = {
    "AWS::SQS::Queue": 19,
    "AWS::SQS::QueuePolicy": 19,
    "AWS::S3::Bucket": 15,
    "AWS::S3::BucketPolicy": 15,
}

# ``${Name}`` / ``${Name.Attr}`` inside an Fn::Sub template string.
_SUB_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags.

    Subclasses ``SafeLoader``, so the ``python/object`` constructors that make
    ``yaml.load`` dangerous are never registered, and the multi-constructor
    below only ever returns plain scalars, lists and dicts.
    """


def _tag_to_python(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> dict:
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


def _load(path: Path) -> dict:
    """Parse one template. Deliberately does not catch ``YAMLError``."""
    loader = _CfnLoader(path.read_text(encoding="utf-8"))
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


def _repo_templates() -> list[Path]:
    """Every CloudFormation template in the repo, found by content.

    Delegates to the same script ``make cfn-lint`` and
    ``make check-arn-partitions`` use, so the three gates cannot drift apart
    over which files count as templates. Invocation matches
    ``scripts/tests/test_discover_templates.py``.
    """
    out = subprocess.run(
        [str(DISCOVER), "cfn"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


def _branches(node: Any) -> list[Any]:
    """Every value ``node`` can evaluate to, expanding a top-level ``Fn::If``.

    Callers decide what to do with more than one branch. Detecting a *bad*
    statement wants ``any``; proving a *required* statement present wants
    ``all``, because a control that vanishes on one leg is not enforced.
    """
    if isinstance(node, dict) and "Fn::If" in node:
        value = node["Fn::If"]
        if isinstance(value, list) and len(value) == 3:
            return [b for branch in value[1:] for b in _branches(branch)]
    return [node]


def _contains_fn_if(node: Any) -> bool:
    """True if ``Fn::If`` appears anywhere under ``node``.

    Used to *refuse* a shape rather than guess at it: a required control hidden
    behind a condition this gate cannot expand uniformly is reported, not
    passed.
    """
    if isinstance(node, dict):
        return "Fn::If" in node or any(_contains_fn_if(v) for v in node.values())
    if isinstance(node, list):
        return any(_contains_fn_if(v) for v in node)
    return False


def _as_list(node: Any) -> list[Any]:
    """Normalize a scalar-or-list property, expanding ``Fn::If`` on the way.

    ``Statement`` may be a single object or an array (IAM grammar), and so may
    ``Action`` and ``Resource``. Iterating the single-object form directly would
    yield its keys.
    """
    items: list[Any] = []
    for branch in _branches(node):
        if branch is None:
            continue
        candidates = branch if isinstance(branch, list) else [branch]
        for candidate in candidates:
            items.extend(b for b in _branches(candidate) if b is not None)
    return items


def _strings(node: Any) -> Iterator[str]:
    """Every scalar leaf under ``node``, as a string."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, (bool, int, float)):
        yield str(node)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _flat(node: Any) -> str:
    """``node`` flattened to one searchable string.

    Only used where the *text* matters — telling an object ARN (``…/*``) from a
    bucket ARN, and spotting a bare ``"*"``. Identity of the target resource is
    decided by :func:`_logical_ids`, not by searching this string.
    """
    return " ".join(_strings(node))


def _sub_logical_ids(value: Any) -> list[str]:
    """Logical ids named by ``Fn::Sub`` placeholders in ``value``.

    Needed because eleven of the fifteen bucket policies write their target as
    ``!Sub "${SomeBucket.Arn}/*"`` rather than ``!GetAtt``. Pseudo-parameters
    (``${AWS::Region}``), escaped literals (``${!Literal}``) and variables the
    two-argument form defines locally are not resource references.
    """
    local: set[str] = set()
    if isinstance(value, str):
        template = value
    elif isinstance(value, list) and value and isinstance(value[0], str):
        template = value[0]
        if len(value) > 1 and isinstance(value[1], dict):
            local = {str(k) for k in value[1]}
    else:
        return []
    found: list[str] = []
    for raw in _SUB_PLACEHOLDER.findall(template):
        name = raw.strip()
        if not name or name.startswith("!"):
            continue
        head = name.split(".")[0]
        if head.startswith("AWS::") or head in local:
            continue
        found.append(head)
    return found


def _logical_ids(node: Any) -> list[str]:
    """Logical ids referenced by ``node`` via ``Ref``, ``GetAtt`` or ``Sub``.

    Both ``Ref`` and ``Fn::Ref`` are recognised: the loader above produces
    ``Fn::Ref`` from ``!Ref``, but ``Ref`` with no prefix is CloudFormation's
    canonical long form and legal YAML, and treating it as unresolvable would
    red-line a correct policy. ``scripts/tests/test_lambda_log_groups.py``
    accepts both in three places; this matches it.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"Ref", "Fn::Ref"} and isinstance(value, str):
                found.append(value)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) and value else value
                if isinstance(target, str):
                    found.append(target.split(".")[0])
            elif key == "Fn::Sub":
                found.extend(_sub_logical_ids(value))
                if isinstance(value, list) and len(value) > 1:
                    found.extend(_logical_ids(value[1]))
            else:
                found.extend(_logical_ids(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_logical_ids(value))
    return found


@dataclass(frozen=True)
class _Policy:
    """One discovered resource policy, with enough context to report on it."""

    template: str
    name: str
    type: str
    body: dict

    @property
    def service(self) -> str:
        return POLICY_TYPES[self.type]

    @property
    def properties(self) -> dict:
        props = self.body.get("Properties")
        return props if isinstance(props, dict) else {}

    @property
    def target_ids(self) -> list[str]:
        prop = TARGET_PROPERTY[self.type]
        return _logical_ids(self.properties.get(prop))

    @property
    def statement_node(self) -> Any:
        """``Statement`` exactly as written, ``Fn::If`` and all."""
        document = self.properties.get("PolicyDocument")
        if not isinstance(document, dict):
            return None
        return document.get("Statement")

    @property
    def statements(self) -> list[dict]:
        """Every statement reachable on *some* branch. For reporting only."""
        return [s for s in _as_list(self.statement_node) if isinstance(s, dict)]

    def __str__(self) -> str:
        return f"{self.template}:{self.name}"


def _resources_of_type(
    paths: Iterable[Path], resource_type: str
) -> list[tuple[str, str]]:
    """``(template, logical id)`` for every resource of ``resource_type``."""
    found: list[tuple[str, str]] = []
    for path in paths:
        document = _load(path)
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            continue
        try:
            template = str(path.relative_to(REPO_ROOT))
        except ValueError:
            template = str(path)
        for name, body in resources.items():
            if isinstance(body, dict) and body.get("Type") == resource_type:
                found.append((template, str(name)))
    return found


def _resource_conditions(paths: Iterable[Path]) -> dict[tuple[str, str], str | None]:
    """``{(template, logical id): CloudFormation Condition or None}``."""
    conditions: dict[tuple[str, str], str | None] = {}
    for path in paths:
        document = _load(path)
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            continue
        try:
            template = str(path.relative_to(REPO_ROOT))
        except ValueError:
            template = str(path)
        for name, body in resources.items():
            if isinstance(body, dict):
                raw = body.get("Condition")
                conditions[(template, str(name))] = (
                    str(raw) if isinstance(raw, str) else None
                )
    return conditions


def _policies(paths: Iterable[Path]) -> list[_Policy]:
    """Every queue or bucket policy declared by ``paths``."""
    found: list[_Policy] = []
    for path in paths:
        document = _load(path)
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            continue
        try:
            template = str(path.relative_to(REPO_ROOT))
        except ValueError:
            template = str(path)
        for name, body in resources.items():
            if not isinstance(body, dict):
                continue
            if body.get("Type") in POLICY_TYPES:
                found.append(_Policy(template, str(name), str(body["Type"]), body))
    return found


def _all_branches_are(node: Any, expected: set[str]) -> bool:
    """True if every branch of ``node`` is a scalar in ``expected``.

    An ``Fn::If`` that resolves to the required value on only one leg does not
    satisfy a required control, so ``Effect: !If [C, Deny, Allow]`` and a
    condition value of ``!If [C, false, true]`` are rejected *by rule* rather
    than incidentally because a dict does not stringify to ``deny``/``false``.
    """
    branches = _branches(node)
    if not branches:
        return False
    for branch in branches:
        if not isinstance(branch, (str, bool, int, float)):
            return False
        if str(branch).strip().lower() not in expected:
            return False
    return True


def _principal_is_everyone(node: Any) -> bool:
    """True only if ``node`` names every possible caller.

    ``"*"`` and ``{"AWS": "*"}`` are the two spellings AWS treats as every
    principal, anonymous callers included. ``{"Service": "*"}`` is **not** one of
    them — it covers AWS service principals only, so an IAM user or an anonymous
    HTTP request would go undenied — which is why the key cannot be discarded
    and the principal flattened to its scalar leaves.
    """
    for branch in _branches(node):
        if isinstance(branch, str):
            if branch.strip() != "*":
                return False
        elif isinstance(branch, dict) and set(branch) == {"AWS"}:
            values = {v.strip() for v in _as_list(branch["AWS"]) if isinstance(v, str)}
            if values != {"*"}:
                return False
        else:
            return False
    return True


def _action_covers_service(node: Any, service: str) -> bool:
    """True if every branch of ``node`` covers the whole ``service`` namespace."""
    branches = _branches(node)
    if not branches:
        return False
    for branch in branches:
        if _contains_fn_if(branch):
            return False
        candidates = branch if isinstance(branch, list) else [branch]
        actions = {a.strip().lower() for a in candidates if isinstance(a, str)}
        if not ({"*", f"{service}:*"} & actions):
            return False
    return True


def _resource_reaches_target(statement: dict, policy: _Policy) -> bool:
    """True if ``statement``'s ``Resource`` covers the policy's own target.

    The target is identified by *resolved logical id*, not by searching the
    flattened ARN text: ``"DocumentQueue" in "DocumentQueueDLQ.Arn"`` is true,
    so a substring test passes a deny scoped to the sibling DLQ — which parses,
    deploys and enforces nothing on the queue the policy is attached to. For a
    bucket, both the bucket ARN and its objects must be covered: the bucket ARN
    alone does not match ``s3:GetObject``.
    """
    branches = _branches(statement.get("Resource"))
    if not branches:
        return False
    for branch in branches:
        if _contains_fn_if(branch):
            return False
        candidates = branch if isinstance(branch, list) else [branch]
        entries = [
            (set(_logical_ids(c)), _flat(c).strip())
            for c in candidates
            if c is not None
        ]
        if not entries:
            return False
        if any(flat == "*" for _, flat in entries):
            continue
        for target in policy.target_ids:
            matching = [flat for ids, flat in entries if target in ids]
            if not matching:
                return False
            if policy.type == "AWS::S3::BucketPolicy":
                if not any(f.endswith("/*") for f in matching):
                    return False
                if not any(not f.endswith("/*") for f in matching):
                    return False
    return True


def _condition_reason(node: Any) -> str | None:
    """Why ``node`` is not exactly a TLS-only test, or ``None`` if it is.

    The condition must hold the ``aws:SecureTransport`` test and *nothing else*.
    IAM ANDs condition keys, so any extra key narrows the deny to that key's
    scope and leaves non-TLS calls outside it undenied — a deny plus
    ``StringEquals: {aws:SourceVpc: …}`` refuses cleartext only from that VPC.
    """
    for branch in _branches(node):
        if not isinstance(branch, dict) or not branch:
            return (
                "the statement carries no Condition, so it denies every call "
                "rather than only non-TLS ones — this is not the TLS-only deny"
            )
        if _contains_fn_if(branch):
            return (
                "Condition contains an Fn::If this gate cannot prove resolves to "
                "the TLS test on every leg; teach the gate the shape rather than "
                "leaving it unchecked"
            )
        seen = False
        for operator, tests in branch.items():
            if not isinstance(tests, dict) or not tests:
                return f"condition operator {operator!r} carries no key/value map"
            for key, value in tests.items():
                if str(key).strip().lower() != CONDITION_KEY:
                    return (
                        f"condition key {key!r} under {operator!r} narrows the deny: "
                        "IAM ANDs condition keys, so non-TLS calls outside that "
                        "key's scope are left undenied"
                    )
                if str(operator).strip().lower() not in BOOL_OPERATORS:
                    return (
                        f"aws:SecureTransport tested with {operator!r}, which is not "
                        "Bool or BoolIfExists"
                    )
                # Unquoted YAML `false` parses to the bool; "false" to the
                # string. `true` must not satisfy this — an inverted condition
                # denies TLS and permits cleartext.
                if not _all_branches_are(value, {"false"}):
                    return f"aws:SecureTransport tested against {value!r}, not false"
                seen = True
        if not seen:
            return "no aws:SecureTransport test in this Condition"
    return None


def _reject_reason(statement: dict, policy: _Policy) -> str | None:
    """Why ``statement`` fails to refuse every non-TLS call, else ``None``."""
    if not _all_branches_are(statement.get("Effect"), {"deny"}):
        return f"Effect is not Deny on every branch ({statement.get('Effect')!r})"
    if not _principal_is_everyone(statement.get("Principal")):
        return (
            "Principal does not cover every caller — only '*' and {AWS: '*'} do, "
            "and {Service: '*'} covers service principals only "
            f"({statement.get('Principal')!r})"
        )
    if not _action_covers_service(statement.get("Action"), policy.service):
        return (
            f"Action does not cover the whole {policy.service} namespace on every "
            f"branch ({statement.get('Action')!r})"
        )
    if not _resource_reaches_target(statement, policy):
        return (
            f"Resource does not resolve to {policy.target_ids} "
            f"({statement.get('Resource')!r})"
        )
    return _condition_reason(statement.get("Condition"))


def _denies_non_tls(statement: dict, policy: _Policy) -> bool:
    """True if ``statement`` refuses every non-TLS call to ``policy``'s target."""
    return _reject_reason(statement, policy) is None


def _guaranteed_deny(node: Any, policy: _Policy) -> bool:
    """True if a qualifying deny is present however the conditions resolve.

    ``all`` across ``Fn::If`` legs and ``any`` across list members. A list means
    "every one of these statements is present", so one qualifying member is
    enough; an ``Fn::If`` means "one of these legs is present", so *both* legs
    must carry the deny or it disappears on one deployment. An ``Fn::If`` whose
    arity this gate does not recognise is refused, never assumed.
    """
    if isinstance(node, dict) and "Fn::If" in node:
        legs = node["Fn::If"]
        if isinstance(legs, list) and len(legs) == 3:
            return all(_guaranteed_deny(leg, policy) for leg in legs[1:])
        return False
    if isinstance(node, list):
        return any(_guaranteed_deny(item, policy) for item in node)
    if isinstance(node, dict):
        return _denies_non_tls(node, policy)
    return False


def _mentions_condition_key(node: Any) -> bool:
    """True if ``aws:SecureTransport`` appears anywhere under ``node``.

    Walks keys as well as values. ``_flat`` cannot be used for this: ``_strings``
    yields a dict's values only, and in a policy ``aws:SecureTransport`` is a
    *key*, so searching the flattened text would never find it and every finding
    would fall back to the generic message.
    """
    if isinstance(node, dict):
        return any(
            str(key).strip().lower() == CONDITION_KEY or _mentions_condition_key(value)
            for key, value in node.items()
        )
    if isinstance(node, list):
        return any(_mentions_condition_key(value) for value in node)
    return isinstance(node, str) and node.strip().lower() == CONDITION_KEY


def _explain(policy: _Policy) -> str:
    """The most specific reason ``policy`` has no guaranteed TLS-only deny."""
    near = [
        f"{statement.get('Sid') or '<no Sid>'}: {reason}"
        for statement in policy.statements
        if _mentions_condition_key(statement)
        and (reason := _reject_reason(statement, policy))
    ]
    if near:
        return (
            "a statement mentions aws:SecureTransport but does not refuse every "
            "non-TLS call — " + "; ".join(near)
        )
    if _contains_fn_if(policy.statement_node):
        return (
            "the deny is not carried by every Fn::If leg of Statement, so it "
            "disappears on at least one resolution of the condition; a required "
            "control has to hold whichever way the condition goes"
        )
    return (
        f"no statement denies {policy.service}:* for Principal '*' on "
        f"{policy.target_ids} when aws:SecureTransport is false"
    )


def _findings(paths: Iterable[Path], policy_type: str) -> dict[str, str]:
    """``{policy: reason}`` for every ``policy_type`` lacking a usable deny."""
    problems: dict[str, str] = {}
    for policy in _policies(paths):
        if policy.type != policy_type:
            continue
        if not policy.target_ids:
            problems[str(policy)] = (
                f"could not resolve the {TARGET_PROPERTY[policy_type]} property to "
                "a logical id, so the deny's scope cannot be verified — teach this "
                "gate about the shape rather than leaving it unchecked"
            )
            continue
        if not _guaranteed_deny(policy.statement_node, policy):
            problems[str(policy)] = _explain(policy)
    return problems


def _unpoliced(paths: Iterable[Path], resource_type: str) -> list[str]:
    """Resources of ``resource_type`` that no policy in their template targets."""
    policy_type = POLICY_FOR_RESOURCE[resource_type]
    targeted = {
        (policy.template, target)
        for policy in _policies(paths)
        if policy.type == policy_type
        for target in policy.target_ids
    }
    return [
        f"{template}:{name}"
        for template, name in _resources_of_type(paths, resource_type)
        if (template, name) not in targeted
    ]


REMEDY = (
    "Add the statement the sibling policies use:\n"
    "  - Sid: EnforceSSLOnly\n"
    "    Effect: Deny\n"
    '    Principal: "*"\n'
    '    Action: "<service>:*"\n'
    "    Resource: <the policy's own target ARN(s)>\n"
    "    Condition:\n"
    "      Bool:\n"
    '        "aws:SecureTransport": false\n'
    "Copy the wording from a neighbour so the statements stay diffable, and keep "
    "the Condition to that one key — IAM ANDs condition keys, so adding a VPC or "
    "source-IP test narrows the deny to that scope. For buckets created outside "
    "CloudFormation the equivalent lives in "
    "lib/idp_sdk/idp_sdk/_core/s3_security.py."
)


@pytest.mark.unit
def test_every_sqs_queue_policy_denies_non_tls() -> None:
    """Issue #964 as a permanent gate.

    ``DataMartRollupDLQPolicy`` was the one queue policy of eleven without the
    deny. This fails on that omission and on any future one, in this template or
    in a template that does not exist yet.
    """
    problems = _findings(_repo_templates(), "AWS::SQS::QueuePolicy")
    assert not problems, (
        "queue policy(ies) do not refuse non-TLS SQS calls (issue #964): "
        f"{problems}\n\n{REMEDY}"
    )


@pytest.mark.unit
def test_every_s3_bucket_policy_denies_non_tls() -> None:
    """The same control on the S3 side, enumerated by the same mechanism.

    Bucket policies were already uniform when #964 was filed; guarding them here
    is what stops the two controls diverging again — one enumerated, the other a
    convention.
    """
    problems = _findings(_repo_templates(), "AWS::S3::BucketPolicy")
    assert not problems, (
        f"bucket policy(ies) do not refuse non-TLS S3 calls: {problems}\n\n{REMEDY}"
    )


@pytest.mark.unit
def test_every_sqs_queue_has_a_policy() -> None:
    """A queue with no policy carries no deny the gate can see.

    Checking only the policies grades 11 of the 19 queues and says nothing about
    the other 8 — which is how eight queues came to have no TLS-only deny at all
    while the rule above was green. Ten of the eleven original queue policies
    grant nothing whatsoever, existing purely to carry this deny, so "no policy"
    is the same omission as #964 rather than a deliberate choice.
    """
    missing = _unpoliced(_repo_templates(), "AWS::SQS::Queue")
    assert not missing, (
        "queue(s) have no AWS::SQS::QueuePolicy, so they carry no TLS-only deny: "
        f"{sorted(missing)}\n\n{REMEDY}\n"
        "If the queue is conditional, give the policy the same Condition or the "
        "stack update fails."
    )


@pytest.mark.unit
def test_every_s3_bucket_has_a_policy() -> None:
    """The S3 side of the same coverage rule; 15 buckets, 15 policies today."""
    missing = _unpoliced(_repo_templates(), "AWS::S3::Bucket")
    assert not missing, (
        "bucket(s) have no AWS::S3::BucketPolicy, so they carry no TLS-only deny: "
        f"{sorted(missing)}\n\n{REMEDY}"
    )


def _condition_mismatches(paths: Iterable[Path]) -> dict[str, str]:
    """Policies whose CloudFormation ``Condition`` differs from their target's.

    A policy that deploys when its queue does not is a stack failure, not a
    security hole, but it is the failure mode of adding these policies in bulk —
    ``JobTrackerDLQ`` is behind ``DeployApiGateway`` — and ``make cfn-lint`` does
    not catch it. Removing the ``Condition`` from ``JobTrackerDLQPolicy`` raises
    only two advisory ``W1001`` warnings, which that gate does not fail on, so
    the mistake would surface at deploy time.
    """
    conditions = _resource_conditions(paths)
    problems: dict[str, str] = {}
    for policy in _policies(paths):
        own = conditions.get((policy.template, policy.name))
        for target in policy.target_ids:
            target_condition = conditions.get((policy.template, target))
            if target_condition is not None and target_condition != own:
                problems[str(policy)] = (
                    f"target {target} is behind Condition {target_condition!r} but "
                    f"the policy's Condition is {own!r}; the policy would deploy "
                    "without its target and fail the stack operation"
                )
    return problems


@pytest.mark.unit
def test_policy_conditions_match_their_target() -> None:
    """A conditional target needs an equally conditional policy."""
    problems = _condition_mismatches(_repo_templates())
    assert not problems, (
        f"policy/target CloudFormation Condition mismatch(es): {problems}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("resource_type,floor", sorted(MINIMUM_DISCOVERED.items()))
def test_discovery_finds_the_known_resources(resource_type: str, floor: int) -> None:
    """A gate that scans nothing passes. This is what stops that going unnoticed.

    If discovery silently returns a subset — a broken script, a filename rule
    creeping back in — the rules above go green while checking nothing.
    """
    discovered = _resources_of_type(_repo_templates(), resource_type)
    assert len(discovered) >= floor, (
        f"found only {len(discovered)} {resource_type} resources, expected at "
        f"least {floor}. Either template discovery has regressed, in which case "
        "the rules above are now passing vacuously and "
        "scripts/discover_templates.sh is the place to look, or resources really "
        "were deleted — if so, lower the floor in MINIMUM_DISCOVERED in this file "
        "and say which resource went away."
    )


@pytest.mark.unit
def test_discovery_spans_more_than_one_template() -> None:
    """The queue policies are split across templates; all must be reached."""
    templates = {p.template for p in _policies(_repo_templates())}
    expected = {
        "template.yaml",
        "patterns/unified/template.yaml",
        "feature-platform/idp-data-generator/template.yaml",
    }
    assert expected <= templates, (
        f"discovery reached only {sorted(templates)}; the parent template, the "
        "unified pattern stack and the data-generator feature stack all declare "
        "policies this gate must cover"
    )


# ---------------------------------------------------------------------------
# Meta-tests. Each mutates one property of an otherwise-correct deny and
# requires the gate to fail, so the assertion cannot decay into one that passes
# on a policy enforcing nothing.
# ---------------------------------------------------------------------------

_QUEUE_POLICY = """
Resources:
  Q:
    Type: AWS::SQS::Queue
  QPolicy:
    Type: AWS::SQS::QueuePolicy
    Properties:
      Queues:
        - !Ref Q
      PolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Sid: EnforceSSLOnly
            Effect: {effect}
            Principal: {principal}
            Action: {action}
            Resource: {resource}
            Condition:
              {operator}:
                "aws:SecureTransport": {value}
"""

_GOOD_QUEUE_POLICY = {
    "effect": "Deny",
    "principal": '"*"',
    "action": '"sqs:*"',
    "resource": "!GetAtt Q.Arn",
    "operator": "Bool",
    "value": "false",
}


def _probe(tmp_path: Path, body: str) -> list[Path]:
    path = tmp_path / "probe" / "template.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return [path]


@pytest.mark.unit
def test_gate_accepts_a_correct_queue_policy(tmp_path: Path) -> None:
    """The shape the fix for #964 added must pass, or the gate is unusable."""
    paths = _probe(tmp_path, _QUEUE_POLICY.format(**_GOOD_QUEUE_POLICY))
    assert not _findings(paths, "AWS::SQS::QueuePolicy")


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,override",
    [
        # The two mutations run by hand to prove this gate has teeth.
        ("effect-flipped-to-allow", {"effect": "Allow"}),
        ("condition-inverted", {"value": "true"}),
        # Everything else that makes a SecureTransport statement inert.
        ("condition-quoted-true", {"value": '"true"'}),
        ("principal-narrowed", {"principal": "{Service: lambda.amazonaws.com}"}),
        # Flattening the principal to its scalar leaves reduces this to "*",
        # but it denies AWS service principals only: an IAM user or an
        # anonymous HTTP caller is left undenied.
        ("principal-service-wildcard", {"principal": '{Service: "*"}'}),
        (
            "principal-account-arn",
            {"principal": '{AWS: "arn:aws:iam::123456789012:root"}'},
        ),
        ("action-too-narrow", {"action": '"sqs:SendMessage"'}),
        # A substring test on the flattened ARN passes this, because
        # "Q" is a substring of "QDLQ.Arn". The three prefix-nested pairs in
        # template.yaml make it a realistic copy-paste slip rather than a
        # theoretical one.
        ("resource-names-the-prefix-nested-sibling", {"resource": "!GetAtt QDLQ.Arn"}),
        ("resource-names-an-unrelated-queue", {"resource": "!GetAtt Other.Arn"}),
        ("wrong-condition-operator", {"operator": "StringEquals"}),
        # An Fn::If that only sometimes yields Deny/false must be refused by
        # rule, not merely because a dict fails to stringify to "deny".
        ("effect-conditional", {"effect": "!If [SomeCondition, Deny, Allow]"}),
        ("condition-value-conditional", {"value": "!If [SomeCondition, false, true]"}),
    ],
)
def test_gate_catches_an_inert_deny(case: str, override: dict, tmp_path: Path) -> None:
    """A statement that mentions SecureTransport but enforces nothing must fail.

    This is the difference between this gate and a grep for the literal string:
    each case below contains the text ``aws:SecureTransport`` and none of them
    refuses a cleartext call.
    """
    body = _QUEUE_POLICY.format(**{**_GOOD_QUEUE_POLICY, **override})
    assert "aws:SecureTransport" in body
    assert _findings(_probe(tmp_path, body), "AWS::SQS::QueuePolicy"), (
        f"inert deny {case!r} was NOT caught — the rule would pass on a queue "
        "policy that enforces nothing"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "case,override",
    [
        # {AWS: "*"} is how AWS itself writes "every principal"; it must not be
        # collateral damage from making the principal check key-aware.
        ("principal-aws-wildcard", {"principal": '{AWS: "*"}'}),
        ("principal-aws-wildcard-list", {"principal": '{AWS: ["*"]}'}),
        ("action-wildcard", {"action": '"*"'}),
        ("resource-wildcard", {"resource": '"*"'}),
        ("boolifexists", {"operator": "BoolIfExists"}),
        ("quoted-false", {"value": '"false"'}),
        (
            "effect-conditional-both-legs-deny",
            {"effect": "!If [SomeCondition, Deny, Deny]"},
        ),
    ],
)
def test_gate_accepts_an_equivalent_spelling(
    case: str, override: dict, tmp_path: Path
) -> None:
    """A deny written differently but meaning the same must still pass.

    The strict checks above are only worth having if they do not red-line the
    legitimate spellings; each of these refuses every non-TLS call.
    """
    body = _QUEUE_POLICY.format(**{**_GOOD_QUEUE_POLICY, **override})
    assert not _findings(_probe(tmp_path, body), "AWS::SQS::QueuePolicy"), (
        f"equivalent spelling {case!r} was reported as a finding — the gate would "
        "red-line a policy that does refuse every non-TLS call"
    )


@pytest.mark.unit
def test_gate_catches_a_deny_narrowed_by_another_condition_key(tmp_path: Path) -> None:
    """IAM ANDs condition keys, so a second key guts the control silently.

    Adding a VPC or source-IP restriction to an existing deny is a plausible
    edit. The result refuses non-TLS calls *only from that VPC* and leaves them
    undenied everywhere else, while still reading as an ``EnforceSSLOnly``
    statement. Both spellings are covered: a second operator alongside ``Bool``,
    and a second key inside the same ``Bool`` map.
    """
    template = (
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "    Properties:\n"
        "      Queues:\n"
        "        - !Ref Q\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 'sqs:*'\n"
        "            Resource: !GetAtt Q.Arn\n"
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n"
        "{extra}"
    )
    second_operator = _probe(
        tmp_path / "a",
        template.format(
            extra=(
                "              StringEquals:\n"
                "                'aws:SourceVpc': vpc-0123456789abcdef0\n"
            )
        ),
    )
    assert _findings(second_operator, "AWS::SQS::QueuePolicy"), (
        "a deny narrowed by aws:SourceVpc was NOT caught — it refuses cleartext "
        "only from that VPC"
    )

    second_key = _probe(
        tmp_path / "b",
        template.format(extra="                'aws:ViaAWSService': true\n"),
    )
    assert _findings(second_key, "AWS::SQS::QueuePolicy"), (
        "a second key inside the same Bool map was NOT caught"
    )


@pytest.mark.unit
def test_gate_catches_a_policy_with_no_deny_at_all(tmp_path: Path) -> None:
    """The exact shape ``DataMartRollupDLQPolicy`` shipped as: an Allow only."""
    paths = _probe(
        tmp_path,
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "    Properties:\n"
        "      Queues:\n"
        "        - !Ref Q\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Effect: Allow\n"
        "            Principal:\n"
        "              Service: !Sub lambda.${AWS::URLSuffix}\n"
        "            Action: sqs:SendMessage\n"
        "            Resource: !GetAtt Q.Arn\n",
    )
    assert _findings(paths, "AWS::SQS::QueuePolicy")


@pytest.mark.unit
def test_gate_accepts_a_deny_alongside_an_allow(tmp_path: Path) -> None:
    """The fixed shape: a service Allow plus the deny, in the house order.

    An explicit ``Deny`` beats an ``Allow`` in IAM, so the Allow does not
    re-permit what the deny refuses and this must pass.
    """
    paths = _probe(
        tmp_path,
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "    Properties:\n"
        "      Queues:\n"
        "        - !Ref Q\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Effect: Allow\n"
        "            Principal:\n"
        "              Service: !Sub lambda.${AWS::URLSuffix}\n"
        "            Action: sqs:SendMessage\n"
        "            Resource: !GetAtt Q.Arn\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 'sqs:*'\n"
        "            Resource: !GetAtt Q.Arn\n"
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n",
    )
    assert not _findings(paths, "AWS::SQS::QueuePolicy")


_DENY_STATEMENT = (
    "Sid: EnforceSSLOnly\n"
    "              Effect: Deny\n"
    "              Principal: '*'\n"
    "              Action: 'sqs:*'\n"
    "              Resource: !GetAtt Q.Arn\n"
    "              Condition:\n"
    "                Bool:\n"
    "                  'aws:SecureTransport': false\n"
)

_ALLOW_STATEMENT = (
    "Effect: Allow\n"
    "              Principal:\n"
    "                Service: lambda.amazonaws.com\n"
    "              Action: 'sqs:SendMessage'\n"
    "              Resource: !GetAtt Q.Arn\n"
)

_FN_IF_STATEMENT_LIST = (
    "Resources:\n"
    "  Q:\n"
    "    Type: AWS::SQS::Queue\n"
    "  QPolicy:\n"
    "    Type: AWS::SQS::QueuePolicy\n"
    "    Properties:\n"
    "      Queues:\n"
    "        - !Ref Q\n"
    "      PolicyDocument:\n"
    "        Statement: !If\n"
    "          - SomeCondition\n"
    "          - - {true_leg}"
    "          - - {false_leg}"
)


@pytest.mark.unit
def test_gate_accepts_a_deny_behind_an_fn_if(tmp_path: Path) -> None:
    """A statement list built by ``Fn::If`` is still a statement list.

    Both legs carry the deny, so the control holds whichever way the condition
    resolves. Contrast the asymmetric case below.
    """
    paths = _probe(
        tmp_path,
        _FN_IF_STATEMENT_LIST.format(
            true_leg=_DENY_STATEMENT,
            false_leg=_DENY_STATEMENT.replace("Bool:", "BoolIfExists:").replace(
                "': false", "': 'false'"
            ),
        ),
    )
    assert not _findings(paths, "AWS::SQS::QueuePolicy")


@pytest.mark.unit
def test_gate_catches_an_asymmetric_fn_if(tmp_path: Path) -> None:
    """A deny on only one ``Fn::If`` leg is not a deny.

    Unioning both legs and asking ``any()`` is correct for spotting a *bad*
    statement — either leg deploys — and exactly inverted for proving a
    *required* statement present. Here the false leg has no deny at all, so on
    that deployment nothing refuses a cleartext call.
    """
    paths = _probe(
        tmp_path,
        _FN_IF_STATEMENT_LIST.format(
            true_leg=_DENY_STATEMENT, false_leg=_ALLOW_STATEMENT
        ),
    )
    assert _findings(paths, "AWS::SQS::QueuePolicy"), (
        "an Fn::If carrying the deny on one leg only was NOT caught — the policy "
        "enforces nothing when the condition takes the other branch"
    )


@pytest.mark.unit
def test_gate_catches_a_conditionally_present_deny(tmp_path: Path) -> None:
    """The ``!If [C, <deny>, !Ref AWS::NoValue]`` idiom drops the deny entirely.

    This is the common CloudFormation way to include a list element
    conditionally, and it is the same leak as the asymmetric list above written
    per-element rather than per-list.
    """
    paths = _probe(
        tmp_path,
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "    Properties:\n"
        "      Queues:\n"
        "        - !Ref Q\n"
        "      PolicyDocument:\n"
        "        Statement:\n"
        "          - !If\n"
        "            - SomeCondition\n"
        "            - "
        + _DENY_STATEMENT.replace("\n              ", "\n              ")
        + "            - !Ref AWS::NoValue\n",
    )
    assert _findings(paths, "AWS::SQS::QueuePolicy"), (
        "a deny that resolves to AWS::NoValue on one leg was NOT caught"
    )


@pytest.mark.unit
def test_gate_accepts_the_long_form_ref(tmp_path: Path) -> None:
    """``Ref:`` with no ``Fn::`` prefix is legal and must not be a finding.

    The loader in this file turns ``!Ref`` into ``Fn::Ref``, but ``Ref`` is
    CloudFormation's canonical long form. Reading only ``Fn::Ref`` resolved the
    ``Queues`` property to no logical id, which took the hard-failure path and
    red-lined a wholly correct policy.
    """
    paths = _probe(
        tmp_path,
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "    Properties:\n"
        "      Queues:\n"
        "        - Ref: Q\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 'sqs:*'\n"
        "            Resource:\n"
        "              Fn::GetAtt: [Q, Arn]\n"
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n",
    )
    assert not _findings(paths, "AWS::SQS::QueuePolicy"), (
        "the long-form 'Ref:' was treated as unresolvable, so a correct policy "
        "was reported as a finding"
    )


@pytest.mark.unit
def test_gate_catches_a_queue_with_no_policy(tmp_path: Path) -> None:
    """A bare queue is invisible to the policy rules; the coverage rule sees it.

    This is what eight queues looked like when #964 was filed, and what the
    policy-only rules said nothing about.
    """
    path = tmp_path / "probe" / "template.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Resources:\n  Q:\n    Type: AWS::SQS::Queue\n")
    assert not _findings([path], "AWS::SQS::QueuePolicy"), (
        "a template with no policy at all should produce no *policy* findings — "
        "which is exactly why the coverage rule below is needed"
    )
    # Probe templates live outside the repo, so they are reported by absolute
    # path rather than relative to REPO_ROOT.
    uncovered = _unpoliced([path], "AWS::SQS::Queue")
    assert [u.rsplit("/", 2)[-2:] for u in uncovered] == [["probe", "template.yaml:Q"]]


@pytest.mark.unit
def test_coverage_rule_pairs_within_a_template(tmp_path: Path) -> None:
    """A policy only covers a resource declared in the same template.

    CloudFormation cannot express a cross-template pairing, so a policy in one
    file must not be credited for a queue in another.
    """
    a = tmp_path / "a" / "template.yaml"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("Resources:\n  Q:\n    Type: AWS::SQS::Queue\n")
    b = tmp_path / "b" / "template.yaml"
    b.parent.mkdir(parents=True, exist_ok=True)
    b.write_text(_QUEUE_POLICY.format(**_GOOD_QUEUE_POLICY))
    uncovered = _unpoliced([a, b], "AWS::SQS::Queue")
    assert [u.rsplit("/", 2)[-2:] for u in uncovered] == [["a", "template.yaml:Q"]]


@pytest.mark.unit
def test_gate_catches_a_bucket_deny_that_misses_the_objects(tmp_path: Path) -> None:
    """A bucket-only ARN does not match ``s3:GetObject``.

    All fifteen bucket policies cover the bucket and ``/*``; a deny that dropped
    the object ARN would read as present and leave every object reachable in
    cleartext. The ``!Sub`` spelling is the one eleven of them use, so it also
    exercises resolving a logical id out of an ``Fn::Sub`` placeholder.
    """
    template = (
        "Resources:\n"
        "  B:\n"
        "    Type: AWS::S3::Bucket\n"
        "  BPolicy:\n"
        "    Type: AWS::S3::BucketPolicy\n"
        "    Properties:\n"
        "      Bucket: !Ref B\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 's3:*'\n"
        "            Resource:\n"
        "{resources}"
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n"
    )
    bucket_only = _probe(
        tmp_path / "a", template.format(resources="              - !GetAtt B.Arn\n")
    )
    assert _findings(bucket_only, "AWS::S3::BucketPolicy"), (
        "a bucket-scoped deny that omits the object ARN was NOT caught"
    )

    objects_only = _probe(
        tmp_path / "b", template.format(resources='              - !Sub "${B.Arn}/*"\n')
    )
    assert _findings(objects_only, "AWS::S3::BucketPolicy"), (
        "an object-scoped deny that omits the bucket ARN was NOT caught"
    )

    both = _probe(
        tmp_path / "c",
        template.format(
            resources=(
                '              - !Sub "${B.Arn}"\n              - !Sub "${B.Arn}/*"\n'
            )
        ),
    )
    assert not _findings(both, "AWS::S3::BucketPolicy")


@pytest.mark.unit
def test_gate_catches_a_bucket_deny_scoped_to_a_prefix_nested_bucket(
    tmp_path: Path,
) -> None:
    """The S3 form of the substring slip, in the ``!Sub`` spelling.

    ``"InputBucket" in "${InputBucketLogs.Arn}"`` is true, so a substring test
    credits a deny scoped to a different bucket entirely.
    """
    paths = _probe(
        tmp_path,
        "Resources:\n"
        "  InputBucket:\n"
        "    Type: AWS::S3::Bucket\n"
        "  InputBucketLogs:\n"
        "    Type: AWS::S3::Bucket\n"
        "  InputBucketPolicy:\n"
        "    Type: AWS::S3::BucketPolicy\n"
        "    Properties:\n"
        "      Bucket: !Ref InputBucket\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 's3:*'\n"
        "            Resource:\n"
        '              - !Sub "${InputBucketLogs.Arn}"\n'
        '              - !Sub "${InputBucketLogs.Arn}/*"\n'
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n",
    )
    assert _findings(paths, "AWS::S3::BucketPolicy"), (
        "a bucket deny scoped to a prefix-nested sibling bucket was NOT caught"
    )


@pytest.mark.unit
def test_condition_rule_catches_a_policy_that_outlives_its_queue(
    tmp_path: Path,
) -> None:
    """A conditional queue with an unconditional policy fails the stack.

    ``make cfn-lint`` passes this, so nothing else in the repo would catch it
    before deploy. Matching conditions must report clean.
    """
    template = (
        "Conditions:\n"
        "  SomeCondition: !Equals ['a', 'a']\n"
        "Resources:\n"
        "  Q:\n"
        "    Type: AWS::SQS::Queue\n"
        "    Condition: SomeCondition\n"
        "  QPolicy:\n"
        "    Type: AWS::SQS::QueuePolicy\n"
        "{policy_condition}"
        "    Properties:\n"
        "      Queues:\n"
        "        - !Ref Q\n"
        "      PolicyDocument:\n"
        "        Version: '2012-10-17'\n"
        "        Statement:\n"
        "          - Sid: EnforceSSLOnly\n"
        "            Effect: Deny\n"
        "            Principal: '*'\n"
        "            Action: 'sqs:*'\n"
        "            Resource: !GetAtt Q.Arn\n"
        "            Condition:\n"
        "              Bool:\n"
        "                'aws:SecureTransport': false\n"
    )
    mismatched = _probe(tmp_path / "a", template.format(policy_condition=""))
    assert _condition_mismatches(mismatched), (
        "an unconditional policy on a conditional queue was NOT caught"
    )

    matched = _probe(
        tmp_path / "b",
        template.format(policy_condition="    Condition: SomeCondition\n"),
    )
    assert not _condition_mismatches(matched)


@pytest.mark.unit
def test_an_unparseable_template_fails_rather_than_skipping(tmp_path: Path) -> None:
    """A parse error must propagate.

    If ``_load`` swallowed ``YAMLError`` and returned ``{}``, a malformed
    template would report zero policies and the rules above would pass on it —
    the exact vacuity this gate exists to prevent.
    """
    broken = tmp_path / "probe" / "template.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("Resources:\n  Q: [unclosed\n")
    with pytest.raises(yaml.YAMLError):
        _policies([broken])


# ===========================================================================
# Closing the class: "every resource that can carry a transport control does"
# ===========================================================================
# Everything above enumerates SQS queues and S3 buckets. That closes the class
# "every queue and bucket refuses non-TLS calls" completely, and closes nothing
# else: the enumeration starts from two resource types rather than from the
# question, so a third policy-bearing type inherits no coverage at all and nothing
# says so. Measured on this tree, that is not hypothetical — nine policy-capable
# resource types are present and two are covered (GitHub #987).
#
# The gate cannot decide for itself whether a plaintext path exists for a given
# service; that is a judgement, and inventing one here would be worse than saying
# nothing. What it can do is make the judgement explicit for every type, and then
# enforce the two conditions under which a recorded judgement stops being true:
#
#   * a type exempted because it declares NO resource policy suddenly declaring
#     one -- which is exactly the hole #987 names, since the TLS deny would then
#     have to be remembered by hand; and
#   * a type exempted because its policies only ever face this account's own
#     principals granting a wildcard or foreign-account principal.
#
# Both are checkable offline, so both are checked rather than trusted.

#: Types whose resource policy is a SEPARATE CloudFormation resource.
SEPARATE_POLICY_TYPE = {
    "AWS::SQS::Queue": "AWS::SQS::QueuePolicy",
    "AWS::S3::Bucket": "AWS::S3::BucketPolicy",
    "AWS::SNS::Topic": "AWS::SNS::TopicPolicy",
}

#: Types whose resource policy is an INLINE property of the resource itself.
INLINE_POLICY_PROPERTY = {
    "AWS::KMS::Key": "KeyPolicy",
    "AWS::ECR::Repository": "RepositoryPolicyText",
    "AWS::SecretsManager::Secret": "ResourcePolicy",
    "AWS::ApiGateway::RestApi": "Policy",
}

#: Types with no per-resource policy mechanism at all — the policy, where one
#: exists, is attached to the catalog or the collection rather than to this
#: resource, so there is no document on it to carry a deny.
NO_PER_RESOURCE_POLICY = {
    "AWS::Glue::Database",
    "AWS::OpenSearchServerless::Collection",
}

#: Every policy-capable type this gate knows about. ``POLICY_FOR_RESOURCE`` is the
#: enforced subset; everything else must appear in ``TRANSPORT_CONTROL_EXEMPT``.
TRANSPORT_CONTROL_CANDIDATES = (
    set(SEPARATE_POLICY_TYPE) | set(INLINE_POLICY_PROPERTY) | NO_PER_RESOURCE_POLICY
)

#: The recorded judgement for each candidate type the gate does NOT enforce. Every
#: reason states a fact about THIS tree, because a reason that restates AWS
#: behaviour from memory cannot be checked and would rot silently.
TRANSPORT_CONTROL_EXEMPT = {
    "AWS::SNS::Topic": (
        "Three topics, and this tree declares no AWS::SNS::TopicPolicy for any of "
        "them. A topic with no policy permits only the owning account, so adding "
        "one is not free: an explicit policy REPLACES that default, and getting it "
        "wrong breaks publishing at runtime rather than at deploy time. That needs "
        "a live check against a deployed stack, which is why the deny is not added "
        "blind here (#987). The enforceable half is below: "
        "test_exempt_types_declaring_no_resource_policy_still_declare_none fails "
        "the moment a TopicPolicy appears, so whoever adds one — the alerts work in "
        "#984 is the likely occasion — has to decide about the deny then, rather "
        "than having no gate ask."
    ),
    "AWS::SecretsManager::Secret": (
        "One secret, no ResourcePolicy declared. Same shape as SNS above, and the "
        "same guard covers it."
    ),
    "AWS::Glue::Database": (
        "No per-resource policy mechanism: a Glue resource policy is attached to "
        "the Data Catalog, not to a database, so there is no document on this "
        "resource that could carry a deny."
    ),
    "AWS::OpenSearchServerless::Collection": (
        "No per-resource policy mechanism. Access is governed by the separate "
        "AWS::OpenSearchServerless::AccessPolicy and SecurityPolicy resources, "
        "which are not IAM resource policies and take no Condition block of this "
        "shape."
    ),
    "AWS::KMS::Key": (
        "Six keys, all six declaring an inline KeyPolicy — so unlike SNS there ARE "
        "documents here. Every statement in all six grants either this account's "
        "own root or an AWS service principal; none grants a wildcard or a foreign "
        "account, so the only callers are in-account principals and AWS services. "
        "test_exempt_types_with_policies_grant_no_outside_principal fails if that "
        "stops being true, at which point the deny becomes load-bearing and this "
        "exemption has to be revisited."
    ),
    "AWS::ECR::Repository": (
        "Three repositories, one declaring RepositoryPolicyText, whose single "
        "statement grants the Lambda service principal. Same guard as KMS above."
    ),
    "AWS::ApiGateway::RestApi": (
        "Two REST APIs. One declares a Policy, and only on its PRIVATE branch: it "
        "grants execute-api:Invoke to any principal, BOUNDED by a StringEquals on "
        "aws:SourceVpce naming the supplied interface endpoint, which is the "
        "documented idiom for a private API. So the grant is confined to callers "
        "arriving through that one endpoint inside the VPC rather than being open. "
        "The guard below treats an unbounded wildcard as the finding rather than a "
        "wildcard as such, so removing that Condition fails this suite. Note this "
        "is the resource policy, not the Cognito authorizer — API authorization is "
        "covered by make api-test-static, not here."
    ),
}

#: An AWS service principal: dot-separated labels, allowing ``${...}`` segments
#: because this tree writes them partition- and region-agnostically
#: (``logs.${AWS::Region}.${AWS::URLSuffix}``). Deliberately does not match an ARN,
#: which is how a foreign-account grant would be written.
_SERVICE_PRINCIPAL = re.compile(r"^(?:[a-z0-9-]+|\$\{[^}]+\})(?:\.(?:[a-z0-9-]+|\$\{[^}]+\}))+$")

#: Properties holding a policy document that is a TRUST policy rather than a
#: resource policy. Both carry a ``Principal``, which is otherwise the thing that
#: distinguishes a resource policy from an identity policy, so they have to be
#: named to be excluded.
TRUST_POLICY_PROPERTIES = {"AssumeRolePolicyDocument"}

#: Floors for the census, so a discovery break is loud rather than silently
#: turning every assertion below into a pass. Re-derive with
#: ``_resource_policy_documents(_repo_templates())``.
MINIMUM_POLICY_CAPABLE_PRESENT = 8


def _policy_documents(node: Any, _property: str | None = None) -> Iterator[tuple[str, Any]]:
    """Yield ``(property name, document)`` for every nested policy document.

    A policy document is recognised structurally, by carrying a ``Statement``, so a
    document under a property this file has never heard of is still found. That is
    the whole point: a hardcoded property list is what left SNS outside the gate.
    """
    if isinstance(node, dict):
        if "Statement" in node and _property is not None:
            yield _property, node
        for key, value in node.items():
            yield from _policy_documents(value, str(key))
    elif isinstance(node, list):
        for item in node:
            yield from _policy_documents(item, _property)


def _resource_policy_documents(
    paths: Iterable[Path],
) -> dict[str, list[tuple[str, str, Any]]]:
    """``{resource type: [(template, logical id, document)]}`` for RESOURCE policies.

    The discriminator is a ``Principal`` on at least one statement: an identity
    policy attached to a role or user never carries one, a resource policy always
    does. Trust policies also carry one and are excluded by property name.
    """
    found: dict[str, list[tuple[str, str, Any]]] = {}
    for path in paths:
        document = _load(path)
        resources = document.get("Resources")
        if not isinstance(resources, dict):
            continue
        try:
            template = str(path.relative_to(REPO_ROOT))
        except ValueError:
            template = str(path)
        for name, body in resources.items():
            if not isinstance(body, dict) or not isinstance(body.get("Type"), str):
                continue
            for prop, policy in _policy_documents(body.get("Properties")):
                if prop in TRUST_POLICY_PROPERTIES:
                    continue
                statements = _as_list(policy.get("Statement"))
                if not any(
                    isinstance(s, dict) and s.get("Principal") is not None
                    for s in statements
                ):
                    continue
                found.setdefault(body["Type"], []).append((template, str(name), policy))
    return found


@pytest.mark.unit
def test_every_resource_policy_bearing_type_is_classified() -> None:
    """No resource type may carry a resource policy without a recorded judgement.

    This is the assertion that makes the gate close a class rather than two
    resource types. It does not start from a list of types someone remembered to
    write down — it finds every policy document in every discovered template that
    carries a ``Principal``, which is what makes a policy a *resource* policy, and
    requires the type it sits on to be either enforced or exempted with a reason.

    A fourth SNS topic therefore inherits the recorded exemption, and a resource
    type nobody has considered — an EFS file system, an Events event bus, a
    CodeArtifact repository — fails this test on the day it lands, when the person
    adding it is the cheapest person to ask.
    """
    by_type = _resource_policy_documents(_repo_templates())

    unclassified = sorted(
        set(by_type)
        - set(POLICY_TYPES)
        - set(POLICY_FOR_RESOURCE)
        - set(TRANSPORT_CONTROL_EXEMPT)
    )
    assert not unclassified, (
        f"resource type(s) carry a resource policy with no recorded transport-control "
        f"judgement: {unclassified}. Locations: "
        f"{ {t: [(f, n) for f, n, _ in by_type[t]] for t in unclassified} }. "
        f"Either require the aws:SecureTransport deny for that type (add it to "
        f"POLICY_FOR_RESOURCE and give it a TARGET_PROPERTY), or record why it does "
        f"not apply in TRANSPORT_CONTROL_EXEMPT with a reason that states a fact "
        f"about this tree. Do not leave it unlisted: an unlisted type is the shape "
        f"SNS was in when #987 was filed."
    )


@pytest.mark.unit
def test_the_policy_capable_census_is_not_vacuous() -> None:
    """Guard the guard: if discovery breaks, every assertion above passes."""
    present = {
        resource_type
        for resource_type in TRANSPORT_CONTROL_CANDIDATES
        if _resources_of_type(_repo_templates(), resource_type)
    }
    assert len(present) >= MINIMUM_POLICY_CAPABLE_PRESENT, (
        f"only {len(present)} of the {len(TRANSPORT_CONTROL_CANDIDATES)} "
        f"policy-capable resource types are found in the tree ({sorted(present)}), "
        f"below the floor of {MINIMUM_POLICY_CAPABLE_PRESENT}. Template discovery "
        f"has probably broken, which would make the classification test pass "
        f"vacuously."
    )
    assert set(TRANSPORT_CONTROL_EXEMPT).isdisjoint(POLICY_FOR_RESOURCE), (
        "a resource type is both enforced and exempted, which is a contradiction: "
        f"{sorted(set(TRANSPORT_CONTROL_EXEMPT) & set(POLICY_FOR_RESOURCE))}"
    )
    for resource_type, reason in TRANSPORT_CONTROL_EXEMPT.items():
        assert len(reason) > 60, (
            f"the exemption for {resource_type} has no substantive reason. An "
            f"exemption without one is indistinguishable from an oversight."
        )


@pytest.mark.unit
def test_exempt_types_declaring_no_resource_policy_still_declare_none() -> None:
    """The hole #987 names: a policy appearing where the exemption assumed none.

    ``AWS::SNS::Topic`` and ``AWS::SecretsManager::Secret`` are exempt *because*
    this tree declares no resource policy for them, which is also why their attack
    surface is small — only the owning account can act on a resource with no policy.
    The moment a policy is added to grant a service or cross-account principal, the
    absent TLS deny stops being a boundary of the gate and becomes a gap in it, and
    with nothing checking, it would have to be remembered by hand.
    """
    paths = _repo_templates()
    offenders = []

    for resource_type in ("AWS::SNS::Topic", "AWS::SecretsManager::Secret"):
        separate = SEPARATE_POLICY_TYPE.get(resource_type)
        if separate:
            for template, name in _resources_of_type(paths, separate):
                offenders.append(f"{template}: {name} ({separate})")
        inline = INLINE_POLICY_PROPERTY.get(resource_type)
        if inline:
            for path in paths:
                for name, body in (_load(path).get("Resources") or {}).items():
                    if not isinstance(body, dict) or body.get("Type") != resource_type:
                        continue
                    if inline in (body.get("Properties") or {}):
                        offenders.append(f"{path.name}: {name} (inline {inline})")

    assert not offenders, (
        "a resource policy now exists on a type exempted in "
        "TRANSPORT_CONTROL_EXEMPT precisely because it had none:\n  "
        + "\n  ".join(offenders)
        + "\nDecide about the aws:SecureTransport deny now. If the policy should "
        "carry it, add the deny and move the type into POLICY_FOR_RESOURCE with a "
        "TARGET_PROPERTY so every future one is checked. If it should not, rewrite "
        "the exemption reason — the current one says this tree declares no such "
        "policy, and that is no longer true."
    )


@pytest.mark.unit
def test_exempt_types_with_policies_grant_no_outside_principal() -> None:
    """The other condition an exemption rests on: who the policy faces.

    ``AWS::KMS::Key``, ``AWS::ECR::Repository`` and ``AWS::ApiGateway::RestApi`` DO
    declare policy documents, and are exempt on the narrower ground that every
    statement faces this account's own root or an AWS service principal. A wildcard
    or foreign-account principal changes that: the policy would then be the boundary
    for a caller outside the account, and a transport condition on it would be
    load-bearing rather than belt-and-braces.
    """
    by_type = _resource_policy_documents(_repo_templates())
    findings = []

    for resource_type in (
        "AWS::KMS::Key",
        "AWS::ECR::Repository",
        "AWS::ApiGateway::RestApi",
    ):
        for template, name, policy in by_type.get(resource_type, []):
            for statement in _as_list(policy.get("Statement")):
                if not isinstance(statement, dict):
                    continue
                principal = statement.get("Principal")
                if principal is None:
                    continue
                if _principal_is_everyone(principal):
                    # A wildcard principal BOUNDED by a Condition is not an open
                    # grant, and refusing it outright would be wrong: it is the
                    # documented idiom for a PRIVATE REST API, where the policy
                    # grants any principal and the Condition confines the request
                    # to one interface endpoint. An UNBOUNDED wildcard is the
                    # finding, so the Condition is what is checked.
                    if not statement.get("Condition"):
                        findings.append(
                            f"{template}: {name} ({resource_type}) grants a wildcard "
                            f"Principal with no Condition bounding it"
                        )
                    continue
                for value in _strings(principal):
                    if "${AWS::AccountId}" in value:
                        continue  # this account's own root, via Fn::Sub
                    if _SERVICE_PRINCIPAL.match(value):
                        continue  # an AWS service principal
                    findings.append(
                        f"{template}: {name} ({resource_type}) grants principal "
                        f"{value!r}, which is neither this account's root nor an AWS "
                        f"service principal"
                    )

    assert not findings, (
        "an exemption in TRANSPORT_CONTROL_EXEMPT rests on these policies facing "
        "only in-account and AWS service principals, and that is no longer true:\n  "
        + "\n  ".join(findings)
        + "\nAdd the aws:SecureTransport deny to the policy and move the type into "
        "POLICY_FOR_RESOURCE, or narrow the grant back."
    )
