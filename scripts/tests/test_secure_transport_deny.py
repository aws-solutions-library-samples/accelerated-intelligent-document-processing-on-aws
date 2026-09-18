# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every resource policy on a queue or bucket must refuse non-TLS requests.

**The gap this closes (issue #964).** Ten of the eleven ``AWS::SQS::QueuePolicy``
resources in this repo carried an explicit ``Deny`` conditioned on
``aws:SecureTransport: false``; ``DataMartRollupDLQPolicy`` in ``template.yaml``
did not. All fifteen ``AWS::S3::BucketPolicy`` resources carried the equivalent
deny. So the control was applied by convention ten times and omitted once, and
nothing could tell the difference — because no gate enumerated the class.

**Honest bound on the impact.** SQS and S3 endpoints are HTTPS and every AWS SDK
uses TLS unless deliberately reconfigured, so the deny is defence in depth
rather than the only thing standing between a caller and cleartext. Its value is
that it makes the guarantee explicit and auditable: without it a caller pointed
at an HTTP endpoint would succeed and nothing in the template would refuse.

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
``Effect: Deny``, a principal covering everyone, an action covering the whole
service namespace, a ``Bool``/``BoolIfExists`` test of
``aws:SecureTransport`` against false, and a ``Resource`` that actually reaches
the policy's own target. The meta-tests at the bottom mutate each of those
properties in turn and require the gate to fail, so the assertion cannot rot
into a tautology.

**A template that will not parse is a failure, not a skip.** Swallowing a
``YAMLError`` would turn "this template declares nothing" into a pass, which is
precisely the vacuity the gate exists to prevent.

Known seam, shared with the sibling gates: ``Fn::If`` is expanded only in its
3-arity form, and ``Fn::ForEach`` / ``Transform: AWS::LanguageExtensions`` is
invisible. The repo does not use ``Fn::ForEach``; if that changes, teach this
gate about it rather than trusting it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

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

# Condition operators that can express "the request did not arrive over TLS".
# BoolIfExists is included because aws:SecureTransport is always present on an
# SQS or S3 request, so the two are equivalent here.
BOOL_OPERATORS = {"bool", "boolifexists"}

CONDITION_KEY = "aws:securetransport"

# Floors, not exact counts, so adding a queue or bucket does not edit this file.
# Measured on the tree that fixed #964: 11 queue policies (10 in template.yaml,
# 1 in patterns/unified/template.yaml) and 15 bucket policies (13 in
# template.yaml, 2 under scripts/sdlc/cfn/). A drop below these means discovery
# broke, which would make every assertion below pass vacuously.
MINIMUM_DISCOVERED = {"AWS::SQS::QueuePolicy": 11, "AWS::S3::BucketPolicy": 15}


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags.

    Subclasses ``SafeLoader``, so the ``python/object`` constructors that make
    ``yaml.load`` dangerous are never registered, and the multi-constructor
    below only ever returns plain scalars, lists and dicts.
    """


def _tag_to_python(loader: Any, tag_suffix: str, node: yaml.Node) -> dict:
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
    """Every value ``node`` can evaluate to, expanding ``Fn::If`` branches.

    A statement reached through only one leg still deploys on that leg, so both
    legs are inspected.
    """
    if isinstance(node, dict) and "Fn::If" in node:
        value = node["Fn::If"]
        if isinstance(value, list) and len(value) == 3:
            return [b for branch in value[1:] for b in _branches(branch)]
    return [node]


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

    Lets a ``Resource`` be compared to a logical id whether it was written as
    ``!GetAtt Q.Arn`` or ``!Sub "${Q.Arn}/*"``.
    """
    return " ".join(_strings(node))


def _logical_ids(node: Any) -> list[str]:
    """Logical ids referenced by ``node`` via ``Ref`` or ``GetAtt``."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Fn::Ref" and isinstance(value, str):
                found.append(value)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) and value else value
                if isinstance(target, str):
                    found.append(target.split(".")[0])
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
    def statements(self) -> list[dict]:
        document = self.properties.get("PolicyDocument")
        if not isinstance(document, dict):
            return []
        return [s for s in _as_list(document.get("Statement")) if isinstance(s, dict)]

    def __str__(self) -> str:
        return f"{self.template}:{self.name}"


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


def _denies_non_tls(statement: dict, policy: _Policy) -> bool:
    """True if ``statement`` refuses every non-TLS call to ``policy``'s target."""
    if str(statement.get("Effect", "")).strip().lower() != "deny":
        return False

    # A deny that names one principal leaves every other caller free to use the
    # cleartext endpoint.
    principals = {p.strip() for p in _strings(statement.get("Principal"))}
    if principals != {"*"}:
        return False

    actions = {
        a.strip().lower()
        for a in _as_list(statement.get("Action"))
        if isinstance(a, str)
    }
    if not ({"*", f"{policy.service}:*"} & actions):
        return False

    if not _resource_reaches_target(statement, policy):
        return False

    for branch in _branches(statement.get("Condition")):
        if not isinstance(branch, dict):
            continue
        for operator, tests in branch.items():
            if str(operator).strip().lower() not in BOOL_OPERATORS:
                continue
            for test in _branches(tests):
                if not isinstance(test, dict):
                    continue
                for key, value in test.items():
                    if str(key).strip().lower() != CONDITION_KEY:
                        continue
                    # Unquoted YAML `false` parses to the bool; "false" to the
                    # string. `true` must not satisfy this — an inverted
                    # condition denies TLS and permits cleartext.
                    if str(value).strip().lower() == "false":
                        return True
    return False


def _resource_reaches_target(statement: dict, policy: _Policy) -> bool:
    """True if ``statement``'s ``Resource`` covers the policy's own target.

    A deny scoped to some other queue's ARN parses, deploys and enforces
    nothing. For a bucket, both the bucket ARN and its objects must be covered:
    the bucket ARN alone does not match ``s3:GetObject``.
    """
    resources = [_flat(r) for r in _as_list(statement.get("Resource"))]
    if not resources:
        return False
    if "*" in resources:
        return True
    for target in policy.target_ids:
        matching = [r for r in resources if target in r]
        if not matching:
            return False
        if policy.type == "AWS::S3::BucketPolicy":
            if not any(r.endswith("/*") for r in matching):
                return False
            if not any(not r.endswith("/*") for r in matching):
                return False
    return True


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
        if not any(_denies_non_tls(s, policy) for s in policy.statements):
            problems[str(policy)] = (
                f"no statement denies {policy.service}:* for Principal '*' on "
                f"{policy.target_ids} when aws:SecureTransport is false"
            )
    return problems


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
    "Copy the wording from a neighbour so the statements stay diffable. For "
    "buckets created outside CloudFormation the equivalent lives in "
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
@pytest.mark.parametrize("policy_type,floor", sorted(MINIMUM_DISCOVERED.items()))
def test_discovery_finds_the_known_policies(policy_type: str, floor: int) -> None:
    """A gate that scans nothing passes. This is what stops that going unnoticed.

    If discovery silently returns a subset — a broken script, a filename rule
    creeping back in — the two rules above go green while checking nothing.
    """
    discovered = [p for p in _policies(_repo_templates()) if p.type == policy_type]
    assert len(discovered) >= floor, (
        f"found only {len(discovered)} {policy_type} resources, expected at least "
        f"{floor}. Template discovery has regressed, so the TLS-only rules above "
        "are passing vacuously. Check scripts/discover_templates.sh."
    )


@pytest.mark.unit
def test_discovery_spans_more_than_one_template() -> None:
    """The queue policies are split across two templates; both must be reached."""
    templates = {p.template for p in _policies(_repo_templates())}
    assert {"template.yaml", "patterns/unified/template.yaml"} <= templates, (
        f"discovery reached only {sorted(templates)}; both the parent template "
        "and the unified pattern stack declare policies this gate must cover"
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
        ("action-too-narrow", {"action": '"sqs:SendMessage"'}),
        ("resource-names-another-queue", {"resource": "!GetAtt Other.Arn"}),
        ("wrong-condition-operator", {"operator": "StringEquals"}),
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
    """The fixed shape: a service Allow plus the deny, in the house order."""
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


@pytest.mark.unit
def test_gate_accepts_a_deny_behind_an_fn_if(tmp_path: Path) -> None:
    """A statement list built by ``Fn::If`` is still a statement list."""
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
        "        Statement: !If\n"
        "          - SomeCondition\n"
        "          - - Sid: EnforceSSLOnly\n"
        "              Effect: Deny\n"
        "              Principal: '*'\n"
        "              Action: 'sqs:*'\n"
        "              Resource: !GetAtt Q.Arn\n"
        "              Condition:\n"
        "                Bool:\n"
        "                  'aws:SecureTransport': false\n"
        "          - - Sid: EnforceSSLOnly\n"
        "              Effect: Deny\n"
        "              Principal: '*'\n"
        "              Action: 'sqs:*'\n"
        "              Resource: !GetAtt Q.Arn\n"
        "              Condition:\n"
        "                BoolIfExists:\n"
        "                  'aws:SecureTransport': 'false'\n",
    )
    assert not _findings(paths, "AWS::SQS::QueuePolicy")


@pytest.mark.unit
def test_gate_catches_a_bucket_deny_that_misses_the_objects(tmp_path: Path) -> None:
    """A bucket-only ARN does not match ``s3:GetObject``.

    All fifteen bucket policies cover the bucket and ``/*``; a deny that dropped
    the object ARN would read as present and leave every object reachable in
    cleartext.
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

    both = _probe(
        tmp_path / "b",
        template.format(
            resources=(
                '              - !GetAtt B.Arn\n              - !Sub "${B.Arn}/*"\n'
            )
        ),
    )
    assert not _findings(both, "AWS::S3::BucketPolicy")


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
