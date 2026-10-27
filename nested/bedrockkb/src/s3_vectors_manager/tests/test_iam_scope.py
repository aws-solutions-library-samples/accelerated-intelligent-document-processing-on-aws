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

The one grant that names the bucket exactly, ``S3VectorsOwnBucketIndexes``
(ListIndexes, and DeleteIndex on any index, so the stack Delete can empty the
bucket), takes the name from the ``S3VectorBucketNameLowercase`` custom
resource. These tests run that resource's inline code from the template, and
check its output against ``sanitize_bucket_name`` for every shape of nested
stack name the parent stack can produce.
"""

from __future__ import annotations

import random
import re
import string
from pathlib import Path
from types import SimpleNamespace
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
def s3vectors_policy_statements(template) -> list[Any]:
    role = template["Resources"]["S3VectorManagerRole"]
    policies = role["Properties"]["Policies"]
    (policy,) = [p for p in policies if p.get("PolicyName") == "S3VectorsAccess"]
    return policy["PolicyDocument"]["Statement"]


@pytest.fixture(scope="module")
def s3vectors_statements(s3vectors_policy_statements) -> dict[str, dict[str, Any]]:
    """Every statement by Sid, with the conditional one taken from its !If."""
    by_sid = {}
    for stmt in s3vectors_policy_statements:
        if set(stmt) == {"Fn::If"}:
            _condition, stmt, _otherwise = stmt["Fn::If"]
        by_sid[stmt["Sid"]] = stmt
    assert set(by_sid) == {
        "S3VectorsBucketManage",
        "S3VectorsIndexManage",
        "S3VectorsOwnBucketIndexes",
    }
    return by_sid


def _run_lowercase_function(template, properties: dict[str, Any]) -> str:
    """Run ``GetAdjustedStackNameFunction``'s inline code from the template, as
    CloudFormation would, and return the ``OutputString`` it sends."""
    code = template["Resources"]["GetAdjustedStackNameFunction"]["Properties"][
        "InlineCode"
    ]
    sent: list[dict[str, Any]] = []
    stub = SimpleNamespace(
        SUCCESS="SUCCESS",
        send=lambda _event, _context, _status, data, *a, **k: sent.append(data),
    )
    namespace: dict[str, Any] = {}
    import sys

    previous = sys.modules.get("cfnresponse")
    sys.modules["cfnresponse"] = stub  # type: ignore[assignment]
    try:
        exec(compile(code, "GetAdjustedStackNameFunction", "exec"), namespace)  # noqa: S102
    finally:
        if previous is not None:
            sys.modules["cfnresponse"] = previous
    namespace["handler"]({"ResourceProperties": properties}, None)
    ((data,),) = [(d,) for d in sent]
    return data["OutputString"]


def _own_bucket_name(template, stack_name: str) -> str:
    """``S3VectorBucketNameLowercase.OutputString`` for a stack of this name."""
    props = template["Resources"]["S3VectorBucketNameLowercase"]["Properties"]
    assert props == {
        "ServiceToken": {"Fn::GetAtt": "GetAdjustedStackNameFunction.Arn"},
        "InputString": {"Fn::Sub": "${AWS::StackName}-s3-vectors"},
        "ExactS3VectorBucketName": "true",
    }
    resolved = {
        "InputString": f"{stack_name}-s3-vectors",
        "ExactS3VectorBucketName": props["ExactS3VectorBucketName"],
    }
    return _run_lowercase_function(template, resolved)


def _resolve_sub(
    node: Any, stack_name: str, index_name: str, own_bucket: str | None = None
) -> str:
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
    if own_bucket is not None:
        values["S3VectorBucketNameLowercase.OutputString"] = own_bucket
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
        raw = (
            stmt["Resource"]
            if isinstance(stmt["Resource"], list)
            else [stmt["Resource"]]
        )
        for node in raw:
            pattern = _resolve_sub(
                node, NESTED_STACK_NAMES[0], index_name, "own-bucket"
            )
            assert pattern.startswith(
                f"arn:{PARTITION}:s3vectors:{REGION}:{ACCOUNT}:bucket/"
            ), (sid, pattern)
            assert pattern != "*"
            # Partition, Region and account come from pseudo parameters, so the
            # grant is the same in every partition.
            assert node["Fn::Sub"].startswith(
                "arn:${AWS::Partition}:s3vectors:${AWS::Region}:${AWS::AccountId}:bucket/"
            ), sid


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


def _parent_nested_stack_names(count: int = 200) -> list[str]:
    """Nested stack names the parent stack can produce for DOCUMENTKB.

    The parent's ``IsStacknameLengthOK`` caps its own name at 25 characters.
    A stack name is a letter followed by letters, digits and hyphens, and
    CloudFormation names the nested stack
    ``<parent>-DOCUMENTKB-<12 or 13 uppercase letters and digits>``. Names with
    ``--`` are covered separately: the sanitizer collapses them.
    """
    rng = random.Random(1590)
    names = []
    alphabet = string.ascii_letters + string.digits + "-"
    for _ in range(count):
        length = rng.randint(1, 25)
        parent = rng.choice(string.ascii_letters) + "".join(
            rng.choice(alphabet) for _ in range(length - 1)
        )
        if "--" in parent or parent.endswith("-"):
            continue  # "--" once joined to "-DOCUMENTKB"; see the mismatch test
        suffix = "".join(
            rng.choice(string.ascii_uppercase + string.digits)
            for _ in range(rng.choice((12, 13)))
        )
        names.append(f"{parent}-DOCUMENTKB-{suffix}")
    names.append("A" * 25 + "-DOCUMENTKB-" + "Z" * 13)  # the longest
    return names


PARENT_NESTED_STACK_NAMES = _parent_nested_stack_names()


@pytest.mark.unit
def test_the_parent_cap_is_the_premise_of_the_exact_bucket_grant():
    """The 25-character cap is what keeps the bucket name under 63 characters,
    so that sanitizing it only lowercases it."""
    parent = (Path(__file__).resolve().parents[5] / "template.yaml").read_text()
    match = re.search(
        r"IsStacknameLengthOK:\n(?:.*\n){1,6}?\s+MaxLength: (\d+)", parent
    )
    assert match, "IsStacknameLengthOK and its MaxLength not found in template.yaml"
    assert int(match.group(1)) == 25
    longest = max(PARENT_NESTED_STACK_NAMES, key=len)
    assert len(f"{longest}-s3-vectors") <= 63


@pytest.mark.unit
def test_the_lowercase_function_still_truncates_the_collection_name_to_32(template):
    """``AdjustedStackName`` names the OpenSearch collection and its policies, so
    a change to its output would replace them on existing stacks. It passes
    exactly the properties it always did, and gets the same output."""
    assert template["Resources"]["AdjustedStackName"]["Properties"] == {
        "ServiceToken": {"Fn::GetAtt": "GetAdjustedStackNameFunction.Arn"},
        "InputString": {"Fn::Ref": "AWS::StackName"},
    }
    for name in ("IDP1-DOCUMENTKB-1LSEGJJKD4T8Y-AND-SOME-MORE", "a--b", "x" * 80):
        output = _run_lowercase_function(template, {"InputString": name})
        assert output == name.lower()[:32]


@pytest.mark.unit
@pytest.mark.parametrize(
    "stack_name", PARENT_NESTED_STACK_NAMES[:40] + NESTED_STACK_NAMES[:2]
)
def test_own_bucket_grant_names_exactly_the_bucket_the_handler_creates(
    template, s3vectors_statements, index_name, stack_name
):
    own = _own_bucket_name(template, stack_name)
    bucket_arn, index_arn = _handler_arns(stack_name, index_name)
    resources = [
        _resolve_sub(r, stack_name, index_name, own)
        for r in s3vectors_statements["S3VectorsOwnBucketIndexes"]["Resource"]
    ]
    assert resources == [bucket_arn, f"{bucket_arn}/index/*"]
    for other in (index_name, "renamed-index", "created-outside-the-stack"):
        assert _iam_matches(resources[1], f"{bucket_arn}/index/{other}")
    # No other bucket: not one that merely shares the suffix or a prefix.
    for other_bucket in (bucket_arn.replace(":bucket/", ":bucket/x"), bucket_arn + "x"):
        assert other_bucket != bucket_arn
        assert not any(_iam_matches(r, other_bucket) for r in resources)
        assert not _iam_matches(resources[1], f"{other_bucket}/index/{index_name}")
    assert index_arn.startswith(bucket_arn)


@pytest.mark.unit
def test_every_parent_produced_name_matches_the_sanitizer(template):
    """The whole sample, compared directly (the parametrized test above runs a
    subset of it through the IAM matcher)."""
    assert len(PARENT_NESTED_STACK_NAMES) > 100
    mismatched = [
        n
        for n in PARENT_NESTED_STACK_NAMES
        if _own_bucket_name(template, n) != sanitize_bucket_name(f"{n}-s3-vectors")
    ]
    assert mismatched == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "stack_name",
    [
        "idp--x-DOCUMENTKB-ABC123DEF456G",  # the sanitizer collapses "--"
        "idp--DOCUMENTKB-ABC123DEF456G",  # a parent name ending in "-"
        "standalone-" + "a" * 60,  # too long: the sanitizer truncates
        # Too long, and its truncation would be another stack's bucket name.
        "b" * 52 + "-s3-vectors-more",
    ],
)
def test_where_the_names_differ_the_named_index_can_still_be_deleted(
    template, s3vectors_statements, index_name, stack_name
):
    """Outside the parent's names the own-bucket grant can miss the bucket. The
    role then still deletes the index named by pS3VectorIndexName, which is
    the behaviour before the own-bucket grant existed."""
    own = _own_bucket_name(template, stack_name)
    bucket_arn, index_arn = _handler_arns(stack_name, index_name)
    resources = [
        _resolve_sub(r, stack_name, index_name, own)
        for r in s3vectors_statements["S3VectorsOwnBucketIndexes"]["Resource"]
    ]
    assert not any(_iam_matches(r, bucket_arn) for r in resources)
    # Nor any other bucket: the derived name is one no vector bucket can have.
    assert not VALID_BUCKET_NAME.fullmatch(own), own
    named = _resolve_sub(
        s3vectors_statements["S3VectorsIndexManage"]["Resource"], stack_name, index_name
    )
    assert _iam_matches(named, index_arn)
    assert (
        "s3vectors:DeleteIndex"
        in s3vectors_statements["S3VectorsIndexManage"]["Action"]
    )


@pytest.mark.unit
def test_own_bucket_grant_exists_only_for_the_stack_derived_bucket_name(
    template, s3vectors_policy_statements
):
    """A pS3VectorBucketName may name a bucket that existed before the stack;
    the handler adopts it on Create, so its other indexes must not become
    deletable. The statement and the name resource share one condition, and
    that condition excludes a supplied bucket name."""
    (conditional,) = [s for s in s3vectors_policy_statements if set(s) == {"Fn::If"}]
    condition, stmt, otherwise = conditional["Fn::If"]
    assert stmt["Sid"] == "S3VectorsOwnBucketIndexes"
    assert condition == "UseDefaultS3VectorBucketName"
    assert otherwise == {"Fn::Ref": "AWS::NoValue"}
    assert template["Conditions"][condition] == {
        "Fn::And": [
            {"Fn::Condition": "UseS3Vectors"},
            {"Fn::Not": [{"Fn::Condition": "HasS3VectorBucketName"}]},
        ]
    }
    resources = template["Resources"]
    assert resources["S3VectorBucketNameLowercase"]["Condition"] == condition
    bucket_name = resources["S3VectorBucketAndIndex"]["Properties"]["BucketName"]
    assert bucket_name == {
        "Fn::If": [
            "HasS3VectorBucketName",
            {"Fn::Ref": "pS3VectorBucketName"},
            resources["S3VectorBucketNameLowercase"]["Properties"]["InputString"],
        ]
    }


@pytest.mark.unit
def test_each_action_is_granted_on_exactly_its_pinned_resource(
    s3vectors_statements, index_name
):
    """Every s3vectors action, the statements it appears in, and exactly the
    Resource each grants, as CloudFormation resolves them for one stack. Any
    change, wider or narrower, is a deliberate IAM change and must update this
    table."""
    stack_name = NESTED_STACK_NAMES[0]
    own = "own-bucket"
    bucket = f"arn:{PARTITION}:s3vectors:{REGION}:{ACCOUNT}:bucket/"
    pinned = {
        "s3vectors:CreateVectorBucket": [[bucket + "*"]],
        "s3vectors:GetVectorBucket": [[bucket + "*"]],
        "s3vectors:DeleteVectorBucket": [[bucket + "*"]],
        "s3vectors:CreateIndex": [[bucket + f"*/index/{index_name}"]],
        "s3vectors:DeleteIndex": [
            [bucket + f"*/index/{index_name}"],
            [bucket + own, bucket + own + "/index/*"],
        ],
        "s3vectors:ListIndexes": [[bucket + own, bucket + own + "/index/*"]],
    }
    granted: dict[str, list[list[str]]] = {}
    for stmt in s3vectors_statements.values():
        assert stmt["Effect"] == "Allow"
        assert set(stmt) == {"Sid", "Effect", "Action", "Resource"}, stmt
        raw = (
            stmt["Resource"]
            if isinstance(stmt["Resource"], list)
            else [stmt["Resource"]]
        )
        resources = [_resolve_sub(r, stack_name, index_name, own) for r in raw]
        for action in stmt["Action"]:
            granted.setdefault(action, []).append(resources)
    assert granted == pinned


# The vector bucket name the s3vectors service model accepts: the bucket part
# of the vectorBucketArn pattern.


def _valid_bucket_name_pattern() -> re.Pattern[str]:
    import botocore.session

    model = botocore.session.get_session().get_service_model("s3vectors")
    arn = model.operation_model("ListIndexes").input_shape.members["vectorBucketArn"]
    pattern = arn.metadata["pattern"]
    bucket = pattern.split(":bucket/", 1)[1]
    assert bucket == "[a-z0-9][a-z0-9-.]{1,61}[a-z0-9]", bucket
    return re.compile(bucket)


VALID_BUCKET_NAME = _valid_bucket_name_pattern()


def _any_stack_names(count: int = 400) -> list[str]:
    """Stack names of any shape CloudFormation accepts (a letter, then letters,
    digits and hyphens, up to 128 characters), including the ones the parent
    stack cannot produce."""
    rng = random.Random(1627)
    alphabet = string.ascii_letters + string.digits + "-"
    names = []
    for _ in range(count):
        length = rng.choice((rng.randint(1, 60), rng.randint(40, 128)))
        names.append(
            rng.choice(string.ascii_letters)
            + "".join(rng.choice(alphabet) for _ in range(length - 1))
        )
    names += ["a-", "a--b", "b" * 52 + "-s3-vectors-more", "c" * 128]
    return names


@pytest.mark.unit
def test_the_derived_name_is_the_handlers_bucket_or_no_bucket(template):
    """For any stack name, S3VectorBucketNameLowercase gives either exactly the
    bucket the handler creates, or a name no vector bucket can have. Never a
    different bucket's name."""
    names = _any_stack_names()
    outcomes = {"exact": 0, "no bucket": 0}
    for name in names:
        own = _own_bucket_name(template, name)
        if own == sanitize_bucket_name(f"{name}-s3-vectors"):
            outcomes["exact"] += 1
        else:
            assert not VALID_BUCKET_NAME.fullmatch(own), (name, own)
            outcomes["no bucket"] += 1
    # Both branches are exercised, so neither half of the property is vacuous.
    assert outcomes["exact"] > 50 and outcomes["no bucket"] > 50, outcomes
