# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ties ``S3VectorManagerRole``'s s3vectors grants to what the handler does.

The role in ``nested/bedrockkb/template.yaml`` cannot name the vector bucket
exactly: the handler runs the ``BucketName`` it is given (by default
``${AWS::StackName}-s3-vectors``) through ``sanitize_bucket_name()``, which
lowercases it, and a nested stack's name always carries uppercase
(``IDP1-DOCUMENTKB-1LSEGJJKD4T8Y``). CloudFormation has no lowercase
intrinsic, so an ARN built from the raw stack name never matches the bucket
the Lambda creates and every call is an implicit deny. These tests resolve the
template's granted ARN patterns the way CloudFormation would and assert they
match the ARNs the handler actually constructs — and that the exact-name
pattern would not have.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from handler import sanitize_bucket_name

TEMPLATE = Path(__file__).resolve().parents[3] / "template.yaml"
HANDLER = Path(__file__).resolve().parents[1] / "handler.py"

PARTITION = "aws"
REGION = "us-east-1"
ACCOUNT = "123456789012"

# Realistic nested-stack names: CloudFormation appends an uppercase random
# suffix, and parent names may push the total past S3's 63-char limit.
NESTED_STACK_NAMES = [
    "IDP1-DOCUMENTKB-1LSEGJJKD4T8Y",
    "idp-lower-DOCUMENTKB-ABC123DEF456G",
    "my-very-long-parent-stack-name-for-testing-limits-DOCUMENTKB-1LSEGJJKD4T8Y",
    "Stack_With_Underscores-DOCUMENTKB-XYZ987",
]


class _CfnLoader(yaml.SafeLoader):
    """SafeLoader that tolerates CloudFormation short-form tags."""


def _tag_to_python(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"Fn::{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"Fn::{tag_suffix}": loader.construct_sequence(node, deep=True)}
    return {f"Fn::{tag_suffix}": loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _tag_to_python)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    with TEMPLATE.open() as fh:
        return yaml.load(fh, Loader=_CfnLoader)


@pytest.fixture(scope="module")
def index_name(template) -> str:
    """The parent stack never passes ``pS3VectorIndexName``, so the default is
    what every deployment uses."""
    return template["Parameters"]["pS3VectorIndexName"]["Default"]


@pytest.fixture(scope="module")
def s3vectors_statements(template) -> dict[str, dict[str, Any]]:
    role = template["Resources"]["S3VectorManagerRole"]
    policies = role["Properties"]["Policies"]
    (policy,) = [p for p in policies if p.get("PolicyName") == "S3VectorsAccess"]
    by_sid = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
    assert set(by_sid) == {"S3VectorsBucketManage", "S3VectorsIndexManage"}
    return by_sid


def _resolve_sub(node: Any, stack_name: str, index_name: str) -> str:
    """Resolve a ``!Sub`` string the way CloudFormation would for this stack."""
    assert isinstance(node, dict) and set(node) == {"Fn::Sub"}, (
        f"Resource must be a single !Sub string, got {node!r}"
    )
    text = node["Fn::Sub"]
    assert isinstance(text, str), "list-form !Sub is not expected here"
    values = {
        "AWS::Partition": PARTITION,
        "AWS::Region": REGION,
        "AWS::AccountId": ACCOUNT,
        "AWS::StackName": stack_name,
        "pS3VectorIndexName": index_name,
    }
    resolved = re.sub(r"\$\{([^}]+)\}", lambda m: values[m.group(1)], text)
    assert "${" not in resolved
    return resolved


def _iam_matches(pattern: str, arn: str) -> bool:
    """IAM Resource matching: ``*`` spans any run of characters (including
    ``/``), ``?`` matches exactly one; everything else is literal and
    case-sensitive."""
    regex = "".join(
        ".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern
    )
    return re.fullmatch(regex, arn) is not None


def _handler_arns(stack_name: str, index_name: str) -> tuple[str, str]:
    """Build the ARNs exactly as ``create_s3_vector_resources`` does, from the
    ``BucketName`` the template passes to ``S3VectorBucketAndIndex``."""
    bucket_name = sanitize_bucket_name(f"{stack_name}-s3-vectors")
    bucket_arn = f"arn:{PARTITION}:s3vectors:{REGION}:{ACCOUNT}:bucket/{bucket_name}"
    return bucket_arn, f"{bucket_arn}/index/{index_name}"


@pytest.mark.unit
@pytest.mark.parametrize("stack_name", NESTED_STACK_NAMES)
def test_sanitizer_changes_every_realistic_nested_stack_name(stack_name):
    """The premise of the wildcard: the name the handler creates is never the
    name the template can spell."""
    raw = f"{stack_name}-s3-vectors"
    assert sanitize_bucket_name(raw) != raw


@pytest.mark.unit
@pytest.mark.parametrize("stack_name", NESTED_STACK_NAMES)
def test_bucket_grant_matches_the_bucket_the_handler_creates(
    s3vectors_statements, index_name, stack_name
):
    pattern = _resolve_sub(
        s3vectors_statements["S3VectorsBucketManage"]["Resource"],
        stack_name,
        index_name,
    )
    bucket_arn, _ = _handler_arns(stack_name, index_name)
    assert _iam_matches(pattern, bucket_arn), (pattern, bucket_arn)


@pytest.mark.unit
@pytest.mark.parametrize("stack_name", NESTED_STACK_NAMES)
def test_index_grant_matches_the_index_the_handler_creates(
    s3vectors_statements, index_name, stack_name
):
    pattern = _resolve_sub(
        s3vectors_statements["S3VectorsIndexManage"]["Resource"],
        stack_name,
        index_name,
    )
    _, index_arn = _handler_arns(stack_name, index_name)
    assert _iam_matches(pattern, index_arn), (pattern, index_arn)


@pytest.mark.unit
def test_index_grant_is_exact_on_index_name(s3vectors_statements, index_name):
    """Only the bucket segment is a wildcard; a different index name must be
    denied, since the handler passes IndexName through unsanitized."""
    stack_name = NESTED_STACK_NAMES[0]
    pattern = _resolve_sub(
        s3vectors_statements["S3VectorsIndexManage"]["Resource"],
        stack_name,
        index_name,
    )
    bucket_arn, _ = _handler_arns(stack_name, index_name)
    assert not _iam_matches(pattern, f"{bucket_arn}/index/some-other-index")
    assert pattern.endswith(f"/index/{index_name}")


@pytest.mark.unit
def test_grants_stay_inside_this_account_and_region(s3vectors_statements, index_name):
    for sid, stmt in s3vectors_statements.items():
        pattern = _resolve_sub(stmt["Resource"], NESTED_STACK_NAMES[0], index_name)
        assert pattern.startswith(
            f"arn:{PARTITION}:s3vectors:{REGION}:{ACCOUNT}:bucket/"
        ), (sid, pattern)
        assert pattern != "*"


@pytest.mark.unit
@pytest.mark.parametrize("stack_name", NESTED_STACK_NAMES)
def test_exact_stack_name_arn_would_have_been_denied(stack_name, index_name):
    """Regression evidence for the deploy-breaking shape this replaced: an ARN
    spelled from ``${AWS::StackName}-s3-vectors`` never matches the bucket."""
    exact = (
        f"arn:{PARTITION}:s3vectors:{REGION}:{ACCOUNT}:bucket/{stack_name}-s3-vectors"
    )
    bucket_arn, index_arn = _handler_arns(stack_name, index_name)
    assert not _iam_matches(exact, bucket_arn)
    assert not _iam_matches(f"{exact}/index/{index_name}", index_arn)


@pytest.mark.unit
def test_granted_actions_are_exactly_what_the_handler_calls(s3vectors_statements):
    """Every ``s3vectors_client.<method>(`` in handler.py maps to a granted
    action, and nothing is granted that the handler never calls."""
    source = HANDLER.read_text()
    called = set(re.findall(r"s3vectors_client\.([a-z_]+)\(", source))
    assert called, "no s3vectors client calls found in handler.py"
    called_actions = {
        "s3vectors:" + "".join(part.capitalize() for part in m.split("_"))
        for m in called
    }
    granted = set()
    for stmt in s3vectors_statements.values():
        granted.update(stmt["Action"])
    assert granted == called_actions
