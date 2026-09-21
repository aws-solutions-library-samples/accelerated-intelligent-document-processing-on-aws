# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the browser may read from S3, derived from ``template.yaml``.

``CognitoIdentityPoolSetRole`` attaches a **single** ``authenticated`` role with no
``RoleMappings``, so Cognito group membership plays no part in which role a signed-in
user assumes. Every bucket that role names is therefore readable by every
authenticated user irrespective of group, with **no resolver in the path** — so no
group check, no per-document check and no per-user scope check is available to apply
to it. That makes this one IAM policy, not the API surface, the boundary for anything
it names.

Two gates read that set and they must read the *same* set:

* ``test_browser_s3_grants.py`` — the policy itself: which buckets it may name.
* ``test_iam_prose_consistency.py`` — whether the documents that enumerate the set
  agree with it.

Hence one parser here rather than one in each. Two copies of a policy reader is the
same defect class the rest of this tree worries about: they diverge, and the one that
diverges is the one nobody re-reads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template.yaml"

#: The Identity Pool's authenticated role — the one the browser assumes.
ROLE = "CognitoAuthorizedRole"


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _load_template() -> dict:
    """``template.yaml`` parsed with CloudFormation short-form tags left opaque.

    Resolving ``!Sub``/``!Ref``/``!GetAtt`` is not the point — only which logical ids a
    ``Resource`` names — so each tag becomes a single-key dict the flattener below
    understands.
    """
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def _tag(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
        return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}

    Loader.add_multi_constructor("!", _tag)
    return yaml.load(TEMPLATE.read_text(), Loader=Loader)  # nosec B506 - SafeLoader subclass


def role_s3_statements() -> list:
    """``(policy_name, statement)`` for every S3 statement on :data:`ROLE`.

    Collected across **all** of the role's inline policies rather than the one named
    ``S3``: a grant added under another policy name would be just as effective, and
    pinning only the policy that happens to hold them today is how a gate stops seeing
    its subject.
    """
    role = _load_template()["Resources"][ROLE]
    statements = []
    for policy in _as_list(role["Properties"].get("Policies")):
        for statement in _as_list(
            (policy.get("PolicyDocument") or {}).get("Statement")
        ):
            if not isinstance(statement, dict):
                continue
            actions = [str(a) for a in _as_list(statement.get("Action"))]
            if any(a.startswith("s3:") or a == "*" for a in actions):
                statements.append((policy.get("PolicyName"), statement))
    return statements


def flatten_resource(node) -> Iterator[str]:
    """One ``Resource`` entry, as zero or more matchable strings.

    CloudFormation can name the same bucket six ways, and a matcher that reads two of
    them supports a much weaker claim than "the set cannot grow back unnoticed":

    ============================================  ==========================
    Idiom                                         Flattened to
    ============================================  ==========================
    ``!Sub "arn:...:::${Bucket}/*"``              the string, as written
    ``!GetAtt Bucket.Arn`` (scalar short form)    ``Bucket.Arn``
    ``Fn::GetAtt: [Bucket, Arn]`` (list form)     ``Bucket.Arn``
    ``!Ref Bucket``                               ``${Bucket}``
    ``!Join`` over any of the above               each part, recursively
    ``"*"``                                       the string, as written
    ============================================  ==========================

    The list form of ``Fn::GetAtt`` is the one that matters most: joining its parts
    with a space rather than the ``.`` that ``GetAtt`` means left it matching neither
    the ``${...}`` alternative nor the ``X.Arn`` one, so a bucket named that way was
    invisible to every assertion downstream.

    An intrinsic this function does not model has its string parts yielded anyway: a
    dropped ``Resource`` reads as "no bucket named", which is the direction that fails
    open.
    """
    if isinstance(node, str):
        yield node
        return
    if not isinstance(node, dict):
        return
    for fn, value in node.items():
        if fn == "Fn::GetAtt":
            if isinstance(value, list):
                yield ".".join(str(v) for v in value)
            else:
                yield str(value)
        elif fn in ("Fn::Ref", "Ref"):
            # A `Ref` to a bucket yields its NAME rather than its ARN — still a
            # reference to that bucket. Rendered in the `${...}` shape so one pattern
            # covers it and `!Sub`.
            yield "${" + str(value) + "}"
        elif fn == "Fn::Sub":
            # Either a string or `[template, {vars}]`; the `${...}` references live in
            # the template half either way.
            if isinstance(value, list):
                if value and isinstance(value[0], str):
                    yield value[0]
            else:
                yield str(value)
        elif fn == "Fn::Join":
            parts = value[1] if isinstance(value, list) and len(value) > 1 else []
            for part in _as_list(parts):
                yield from flatten_resource(part)
        elif isinstance(value, (str, dict, list)):
            for part in _as_list(value):
                yield from flatten_resource(part)


def resource_strings(statement: dict) -> Iterator[str]:
    """Every ``Resource`` entry of one statement, flattened."""
    for resource in _as_list(statement.get("Resource")):
        yield from flatten_resource(resource)


_BUCKET_REF_RE = re.compile(r"\$\{([A-Za-z0-9]+)\}|\b([A-Za-z0-9]+)\.Arn\b")


def buckets_named(statements: Iterable) -> set:
    """Bucket logical ids appearing in any S3 ``Resource`` of ``statements``.

    The ``X.Arn`` alternative is deliberately not anchored to the whole string: a
    ``Fn::Join`` part, or a ``GetAtt`` inside a longer rendering, is still a reference
    to that bucket, and ``^...$`` matched only the case where the ARN was the entire
    ``Resource``.
    """
    named = set()
    for _policy, statement in statements:
        for text in resource_strings(statement):
            for match in _BUCKET_REF_RE.finditer(text):
                candidate = match.group(1) or match.group(2)
                if candidate and candidate.endswith("Bucket"):
                    named.add(candidate)
    return named


_WILDCARD_RESOURCES = frozenset({"*", "arn:aws:s3:::*", "arn:aws:s3:::*/*"})


def is_wildcard_resource(text: str) -> bool:
    """Whether a ``Resource`` reaches every bucket, naming no logical id.

    Its own predicate because a wildcard is the one shape that turns the other
    assertions into false negatives: it contributes no logical id, so the derived set
    still compares equal to the pinned one while the grant reaches the whole account.

    The partition segment is matched with ``.*`` rather than ``[^:]*`` because
    ``${AWS::Partition}`` contains colons of its own — the spelling this repo actually
    writes, and the one a ``[^:]*`` matcher does not recognise as a wildcard.
    """
    stripped = text.strip()
    if stripped in _WILDCARD_RESOURCES:
        return True
    return bool(re.fullmatch(r"arn:.*:s3:::\*(?:/\*)?", stripped))


def browser_readable_buckets() -> set:
    """The set of bucket logical ids the browser's own credentials can read."""
    return buckets_named(role_s3_statements())
