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

import pytest

# The policy parser is shared with `test_iam_prose_consistency.py`, which cross-checks
# the documents against the same derived set. Two copies of a policy reader diverge,
# and the copy that diverges is the one nobody re-reads.
from browser_s3_policy import ROLE, role_s3_statements
from browser_s3_policy import buckets_named as _buckets_named
from browser_s3_policy import is_wildcard_resource as _is_wildcard_resource
from browser_s3_policy import resource_strings as _resource_strings

pytestmark = pytest.mark.unit


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# Buckets the browser's own credentials may read, as CloudFormation logical ids. The
# residual, named -- these two are exempt from the rule the rest of this file enforces,
# which is why the constant is named to be found: `PINNED_` is in
# `exemption_discovery.NAME_VOCABULARY`, so it is discovered and carries a registry
# entry in `gate_exemptions.json` rather than sitting here as an unregistered
# exclusion. Adding to this set is a decision about the security boundary above, not a
# detail -- see #1033 before you do.
PINNED_BROWSER_READABLE_BUCKETS = {"InputBucket", "OutputBucket"}

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


@pytest.fixture(scope="module")
def s3_statements():
    """Every S3 statement in the authenticated role's inline policies.

    Collected across **all** its policies rather than the one named ``S3``: a grant
    added under another policy name would be just as effective, and pinning only the
    policy that happens to hold them today is how a gate stops seeing its subject.
    """
    return role_s3_statements()


def test_the_gate_sees_its_subject(s3_statements):
    """Otherwise every assertion below passes vacuously."""
    assert s3_statements, (
        f"{ROLE} declares no S3 statement at all. Either the browser no longer reads "
        "S3 directly — in which case delete this gate and #1033 with it — or this "
        "test has stopped finding the policy it is meant to read."
    )


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
    assert _buckets_named(s3_statements) == PINNED_BROWSER_READABLE_BUCKETS, (
        "the buckets the browser can read directly have changed. See #1033: this set "
        "is the unclosed half of UI.T06 and every entry in it is outside every API "
        "authorization control this deployment has."
    )


def test_no_s3_grant_on_this_role_uses_a_wildcard_resource(s3_statements):
    """A wildcard names no bucket, so it evades every assertion above.

    `Resource: "*"` (or `arn:*:s3:::*`) contributes no logical id, so the derived set
    still compares equal to the pinned one, no forbidden bucket appears, and the
    `ListBucket` check finds no `${Bucket}` to object to — while the grant reaches every
    bucket in the account. It is the one shape that turns all three green assertions
    into a false negative, so it gets its own.
    """
    for policy, statement in s3_statements:
        for text in _resource_strings(statement):
            assert not _is_wildcard_resource(text), (
                f"{ROLE}.{policy} grants S3 on {text!r}, a wildcard. Every "
                "authenticated user assumes this role with no resolver in the path, so "
                "this reaches every bucket in the account — including the two whose "
                "contents are partitioned per user. Name the buckets explicitly; the "
                "other assertions in this file cannot see a wildcard."
            )


def test_the_resource_matcher_reads_every_cloudformation_idiom():
    """The matcher's own coverage, asserted rather than assumed.

    Each idiom below is a way `template.yaml` could name a bucket tomorrow. An idiom
    the flattener drops reads as "no bucket named", which is the direction that fails
    open — and two of these (the `Fn::GetAtt` list form and `Ref`) were dropped by the
    first version of this gate, so this is not a hypothetical class.
    """
    cases = {
        "sub-scalar": {
            "Fn::Sub": "arn:${AWS::Partition}:s3:::${ConfigurationBucket}/*"
        },
        "sub-list": {"Fn::Sub": ["arn:aws:s3:::${ConfigurationBucket}/*", {"X": "y"}]},
        "getatt-scalar": {"Fn::GetAtt": "ConfigurationBucket.Arn"},
        "getatt-list": {"Fn::GetAtt": ["ConfigurationBucket", "Arn"]},
        "ref": {"Ref": "ConfigurationBucket"},
        "fn-ref": {"Fn::Ref": "ConfigurationBucket"},
        "join-over-ref": {
            "Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "ConfigurationBucket"}, "/*"]]
        },
        "join-over-getatt-list": {
            "Fn::Join": ["", [{"Fn::GetAtt": ["ConfigurationBucket", "Arn"]}, "/*"]]
        },
    }
    for name, resource in cases.items():
        found = _buckets_named([("Probe", {"Resource": resource})])
        assert found == {"ConfigurationBucket"}, (
            f"the {name} idiom is not read by browser_s3_policy.flatten_resource / "
            f"buckets_named "
            f"(got {found or 'nothing'}), so a bucket named that way would be "
            "invisible to every assertion in this file"
        )

    for wildcard in ("*", "arn:aws:s3:::*", "arn:${AWS::Partition}:s3:::*/*"):
        assert _is_wildcard_resource(wildcard), wildcard
    for specific in (
        "arn:${AWS::Partition}:s3:::${InputBucket}/*",
        "arn:aws:s3:::my-bucket/*",
    ):
        assert not _is_wildcard_resource(specific), specific


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
        # Compared on the DERIVED reference set, not on a `${Bucket}` substring: the
        # substring test saw only the `!Sub` spelling, so a `Ref` or a `Fn::GetAtt`
        # list form granting ListBucket on a partitioned bucket read as clean.
        enumerable = _buckets_named([(policy, statement)])
        for bucket in sorted(BROWSER_FORBIDDEN_BUCKETS):
            assert bucket not in enumerable, (
                f"{ROLE}.{policy} lets the browser enumerate {bucket}: "
                f"{BROWSER_FORBIDDEN_BUCKETS[bucket]}"
            )
