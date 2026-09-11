# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every Lambda that reads configuration-revision bodies can also *list* them.

Revision bodies live at ``config_revisions/<profile>/<nnnnnn>.json.gz`` in the
Configuration bucket, and a pinned document or test run reads exactly one of
them. The roles were granted ``s3:GetObject`` on that prefix and nothing else —
and S3 answers a GetObject for a **missing** key with ``403 AccessDenied``
rather than ``404 NoSuchKey`` when the caller has no ``s3:ListBucket`` on the
bucket. So a pruned or never-cut pinned revision surfaced in OCR as

    is not authorized to perform: s3:ListBucket on resource: "arn:aws:s3:::…"

which points at IAM instead of at the real cause, and the store's clear
"revision is not available" path never fired (#878).

Rule: a function whose policies grant ``s3:GetObject`` on
``…/config_revisions/*`` must, in the same policy list, also hold
``s3:ListBucket`` on the bucket itself — either a statement scoped to the
revisions prefix (``Condition.StringLike.s3:prefix == config_revisions/*``), or
a SAM ``S3ReadPolicy``/``S3CrudPolicy`` on that bucket, which include it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES = [
    "template.yaml",
    "patterns/unified/template.yaml",
    "nested/api-resolvers/template.yaml",
]

REVISIONS_PREFIX = "config_revisions/*"


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


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _sub_text(node: Any) -> str | None:
    """The template string of an ``!Sub``, or the string itself."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict) and "Fn::Sub" in node:
        sub = node["Fn::Sub"]
        return sub if isinstance(sub, str) else (sub[0] if sub else None)
    return None


def _statements(policies: Any):
    """Every IAM statement dict in a SAM ``Policies`` list."""
    for policy in _as_list(policies):
        if not isinstance(policy, dict):
            continue
        for stmt in _as_list(policy.get("Statement")):
            if isinstance(stmt, dict):
                yield stmt


def _grants(stmt: dict, action: str) -> bool:
    return stmt.get("Effect") == "Allow" and any(
        a in (action, "s3:*") for a in _as_list(stmt.get("Action"))
    )


def _bucket_of_revisions_grant(stmt: dict) -> str | None:
    """``${Bucket}`` reference of a GetObject grant on the revisions prefix."""
    if not _grants(stmt, "s3:GetObject"):
        return None
    for res in _as_list(stmt.get("Resource")):
        text = _sub_text(res)
        if text and text.endswith(f"/{REVISIONS_PREFIX}"):
            return text[: -len(f"/{REVISIONS_PREFIX}")]
    return None


def _has_scoped_list(policies: Any, bucket_arn: str) -> bool:
    for stmt in _statements(policies):
        if not _grants(stmt, "s3:ListBucket"):
            continue
        if not any(_sub_text(r) == bucket_arn for r in _as_list(stmt.get("Resource"))):
            continue
        prefixes = _as_list(
            ((stmt.get("Condition") or {}).get("StringLike") or {}).get("s3:prefix")
        )
        if REVISIONS_PREFIX in prefixes:
            return True
    return False


def _has_sam_read_policy(policies: Any, bucket_arn: str) -> bool:
    """A SAM S3ReadPolicy / S3CrudPolicy on the same bucket includes ListBucket."""
    for policy in _as_list(policies):
        if not isinstance(policy, dict):
            continue
        for name in ("S3ReadPolicy", "S3CrudPolicy"):
            ref = (policy.get(name) or {}).get("BucketName")
            if isinstance(ref, dict):
                logical = ref.get("Ref") or ref.get("Fn::Ref")
                if logical and bucket_arn.endswith("${" + logical + "}"):
                    return True
    return False


def _functions_reading_revisions():
    for rel in TEMPLATES:
        resources = _load(rel).get("Resources", {})
        for name, body in resources.items():
            if body.get("Type") != "AWS::Serverless::Function":
                continue
            policies = (body.get("Properties") or {}).get("Policies")
            for stmt in _statements(policies):
                bucket_arn = _bucket_of_revisions_grant(stmt)
                if bucket_arn:
                    yield rel, name, policies, bucket_arn
                    break


CASES = list(_functions_reading_revisions())


def test_the_gate_sees_the_readers():
    """If the grants move, the gate must move with them — not pass vacuously."""
    assert len(CASES) >= 10, [c[:2] for c in CASES]


@pytest.mark.parametrize(
    "rel,name,policies,bucket_arn", CASES, ids=[f"{c[0]}::{c[1]}" for c in CASES]
)
def test_a_revision_body_reader_can_also_list_the_prefix(
    rel, name, policies, bucket_arn
):
    assert _has_scoped_list(policies, bucket_arn) or _has_sam_read_policy(
        policies, bucket_arn
    ), (
        f"{rel}: {name} may GetObject {bucket_arn}/{REVISIONS_PREFIX} but has no "
        f"s3:ListBucket on {bucket_arn} scoped to s3:prefix={REVISIONS_PREFIX}. "
        f"Without it a missing revision body is a 403, not a 404 (#878)."
    )


def test_scoped_list_is_recognised_and_unscoped_grants_are_not():
    bucket = "arn:${AWS::Partition}:s3:::${ConfigurationBucket}"
    scoped = [
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": {"Fn::Sub": bucket},
                    "Condition": {"StringLike": {"s3:prefix": REVISIONS_PREFIX}},
                }
            ]
        }
    ]
    assert _has_scoped_list(scoped, bucket)
    # Right action, right bucket, but no prefix condition: not the grant we ask for.
    unscoped = [
        {
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": {"Fn::Sub": bucket},
                }
            ]
        }
    ]
    assert not _has_scoped_list(unscoped, bucket)
    # Right shape, wrong bucket.
    other = [
        {
            "Statement": [
                {**scoped[0]["Statement"][0], "Resource": {"Fn::Sub": bucket + "X"}}
            ]
        }
    ]
    assert not _has_scoped_list(other, bucket)
    assert _has_sam_read_policy(
        [{"S3ReadPolicy": {"BucketName": {"Ref": "ConfigurationBucket"}}}], bucket
    )
    assert not _has_sam_read_policy(
        [{"S3ReadPolicy": {"BucketName": {"Ref": "InputBucket"}}}], bucket
    )
