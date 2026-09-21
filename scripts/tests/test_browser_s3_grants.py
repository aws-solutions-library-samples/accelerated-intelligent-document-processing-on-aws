# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the BROWSER may read from S3 with the signed-in user's own credentials.

``CognitoIdentityPoolSetRole`` attaches a **single** ``authenticated`` role with no
``RoleMappings``, so Cognito group membership plays no part in which role a signed-in
user assumes. Every bucket that role names is therefore readable by every
authenticated user irrespective of group, with **no resolver in the path** — so no
group check, no per-document check and no per-user scope check is available to apply
to it. That makes this one IAM policy, not the API surface, the boundary for anything
it names, and it is why the list belongs under a gate rather than under review.

Two things are pinned, in opposite directions.

**The Configuration bucket must not be there.** It holds
``config_revisions/<profile>/<nnnnnn>.json.gz`` — each configuration profile's full
recorded history, prompts and few-shot examples included — and which profiles a user
may see is their per-user ``allowedConfigVersions``, which no IAM grant on a shared
role can express. Granted to the browser it was also ``s3:ListBucket``, so the profile
names did not have to be guessed. The reads that remain go through
``getFileContents`` / ``getFilePresignedUrl``, where ``key_scope`` applies the same
axis to the key. Re-adding the bucket here would reopen the axis while every one of
those resolver-side checks still passed, which is exactly the shape that makes a
regression invisible.

**The Input and Output buckets are there, and that is the open residual.** The web UI
signs those GETs in the browser today (``use-page-thumbnails`` and ``PageImageViewer``
fetch ``page.ImageUri`` from the Output bucket; ``FileViewer`` defaults to
``presignVia: 'client'`` against the Input bucket; ``presignForExport`` signs
client-side for exactly those two and routes everything else to the resolver), so
removing them means redesigning the document read path rather than trimming a policy —
issue #1033. Pinning the set is what stops it growing quietly: this test fails if a
**third** bucket is added, which is the only way the residual can widen without anyone
choosing to widen it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template.yaml"

ROLE = "CognitoAuthorizedRole"

# Buckets the browser's own credentials may read, as CloudFormation logical ids. The
# residual, named. Adding to this set is a decision about the security boundary above,
# not a detail — see #1033 before you do.
BROWSER_READABLE_BUCKETS = {"InputBucket", "OutputBucket"}

# Buckets that must never be reachable with the browser's own credentials, and why.
# One entry per bucket, each carrying the per-user axis that an IAM grant on a single
# shared role cannot express.
BROWSER_FORBIDDEN_BUCKETS = {
    "ConfigurationBucket": (
        "holds every configuration profile's revision history under "
        "config_revisions/<profile>/; visibility is the caller's per-user "
        "allowedConfigVersions, enforced on the key by "
        "get_file_contents_resolver/key_scope.py"
    ),
    "TestSetBucket": (
        "holds each test set's source documents and ground truth under "
        "<test_set_id>/; visibility is an Annotator's per-user allowedTestSets, "
        "enforced on the key by get_file_contents_resolver/key_scope.py"
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


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _resource_strings(statement: dict):
    """Every ``Resource`` entry of a statement, as template text.

    ``!Sub``, ``!GetAtt`` and plain strings all reduce to something a logical id can be
    matched in, because the point is which bucket is named, not how.
    """
    for resource in _as_list(statement.get("Resource")):
        if isinstance(resource, str):
            yield resource
        elif isinstance(resource, dict):
            for value in resource.values():
                if isinstance(value, str):
                    yield value
                elif isinstance(value, list):
                    yield " ".join(str(v) for v in value)


@pytest.fixture(scope="module")
def s3_statements():
    """Every S3 statement in the authenticated role's inline policies.

    Collected across **all** its policies rather than the one named ``S3``: a grant
    added under another policy name would be just as effective, and pinning only the
    policy that happens to hold them today is how a gate stops seeing its subject.
    """
    doc = yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)
    role = doc["Resources"][ROLE]
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


def test_the_gate_sees_its_subject(s3_statements):
    """Otherwise every assertion below passes vacuously."""
    assert s3_statements, (
        f"{ROLE} declares no S3 statement at all. Either the browser no longer reads "
        "S3 directly — in which case delete this gate and #1033 with it — or this "
        "test has stopped finding the policy it is meant to read."
    )


def _buckets_named(s3_statements) -> set:
    """Bucket logical ids appearing in any S3 resource of the role."""
    named = set()
    pattern = re.compile(r"\$\{([A-Za-z0-9]+)\}|^([A-Za-z0-9]+)\.Arn$")
    for _policy, statement in s3_statements:
        for text in _resource_strings(statement):
            for match in pattern.finditer(text):
                candidate = match.group(1) or match.group(2)
                if candidate and candidate.endswith("Bucket"):
                    named.add(candidate)
    return named


@pytest.mark.parametrize(
    "bucket,why",
    sorted(BROWSER_FORBIDDEN_BUCKETS.items()),
    ids=sorted(BROWSER_FORBIDDEN_BUCKETS),
)
def test_a_per_user_partitioned_bucket_is_not_browser_readable(
    s3_statements, bucket, why
):
    assert bucket not in _buckets_named(s3_statements), (
        f"{ROLE} grants S3 on {bucket}, which every authenticated user assumes "
        f"regardless of group and reaches with no resolver in the path. {bucket} {why} "
        "— so a grant here defeats that axis for every scoped user while the "
        "resolver-side check still passes. Route the read through "
        "getFileContents/getFilePresignedUrl instead."
    )


def test_the_set_of_browser_readable_buckets_has_not_grown(s3_statements):
    """The residual, pinned so it cannot widen without being chosen.

    A failure here is not automatically a defect — it means the browser's direct-read
    reach changed. Decide whether that bucket is partitioned per user (if so it belongs
    in BROWSER_FORBIDDEN_BUCKETS and the read belongs behind a resolver), then update
    this set and the note in `docs/rbac.md` together.
    """
    assert _buckets_named(s3_statements) == BROWSER_READABLE_BUCKETS, (
        "the buckets the browser can read directly have changed. See #1033: this set "
        "is the unclosed half of UI.T06 and every entry in it is outside every API "
        "authorization control this deployment has."
    )


def test_list_bucket_is_confined_to_the_browser_readable_buckets(s3_statements):
    """`s3:ListBucket` is the enumeration primitive, so it gets its own assertion.

    Object read needs a key; listing produces the keys. A `ListBucket` on a bucket
    whose objects are per-user partitioned removes the need to guess names at all,
    which is what made the configuration axis reachable without a single API call.
    """
    for policy, statement in s3_statements:
        actions = {str(a) for a in _as_list(statement.get("Action"))}
        if not ({"s3:ListBucket", "s3:*", "*"} & actions):
            continue
        for text in _resource_strings(statement):
            for bucket in BROWSER_FORBIDDEN_BUCKETS:
                assert f"${{{bucket}}}" not in text, (
                    f"{ROLE}.{policy} lets the browser enumerate {bucket}: "
                    f"{BROWSER_FORBIDDEN_BUCKETS[bucket]}"
                )
