# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deleting a document removes its upload from the versioned Input bucket (#2166).

The doubles in ``test_delete_documents_coverage.py`` pin the calls; these tests run the
same path against a **versioned bucket in moto** and assert what is left in it, which is
the property the issue is about. moto answers ``ListObjectVersions`` and
``DeleteObjects`` itself, so the exact-key filter, the delete markers and the listing's
page boundary are exercised against moto's implementation of S3 versioning, not against
a fixture written to agree with the code. moto is not S3: what this establishes is the
behaviour against moto's model of versioning, and the IAM side (which moto does not
enforce) is covered by the template tests instead.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common.delete_documents import delete_single_document

REGION = "us-east-1"
IN, OUT, TABLE = "idp-input", "idp-output", "idp-tracking"
KEY = "batch-1/invoice.pdf"


def _versions(s3, bucket: str) -> list[tuple[str, str]]:
    """Every (key, kind) left in ``bucket``, versions and delete markers both."""
    left: list[tuple[str, str]] = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        left += [(v["Key"], "version") for v in page.get("Versions", [])]
        left += [(m["Key"], "marker") for m in page.get("DeleteMarkers", [])]
    return left


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (IN, OUT):
            s3.create_bucket(Bucket=bucket)
            s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield s3, table


@pytest.mark.unit
def test_every_version_and_marker_of_the_upload_is_gone(aws):
    s3, table = aws
    # Two uploads of the same key (a re-upload) and a prior versionless delete, which
    # is exactly the state a pre-#2166 delete left behind: a marker over noncurrent
    # versions holding the bytes.
    s3.put_object(Bucket=IN, Key=KEY, Body=b"first")
    s3.put_object(Bucket=IN, Key=KEY, Body=b"second")
    s3.delete_object(Bucket=IN, Key=KEY)
    s3.put_object(Bucket=IN, Key=KEY, Body=b"third")
    assert len(_versions(s3, IN)) == 4

    result = delete_single_document(KEY, table, s3, IN, OUT)

    assert result["errors"] == []
    assert result["deleted"]["input_file"] is True
    assert result["deleted"]["input_versions"] == 4
    assert _versions(s3, IN) == [], "the upload is still recoverable from the bucket"


@pytest.mark.unit
def test_neighbouring_keys_sharing_the_prefix_are_untouched(aws):
    s3, table = aws
    neighbours = [f"{KEY}.bak", f"{KEY}2", f"{KEY}/attachment.pdf"]
    for key in [KEY, *neighbours]:
        s3.put_object(Bucket=IN, Key=key, Body=b"x")
        s3.put_object(Bucket=IN, Key=key, Body=b"y")
    s3.delete_object(Bucket=IN, Key=neighbours[0])  # a neighbour's own marker

    delete_single_document(KEY, table, s3, IN, OUT)

    left = _versions(s3, IN)
    assert all(key != KEY for key, _ in left)
    assert sorted(left) == sorted(
        [(k, "version") for k in neighbours for _ in range(2)]
        + [(neighbours[0], "marker")]
    )


@pytest.mark.unit
def test_a_listing_beyond_one_page_is_purged_whole(aws):
    # ListObjectVersions returns at most 1000 entries a page and DeleteObjects takes at
    # most 1000 keys, so 1003 versions cross both boundaries. moto paginates at 1000.
    s3, table = aws
    for i in range(1003):
        s3.put_object(Bucket=IN, Key=KEY, Body=str(i).encode())
    first = s3.list_object_versions(Bucket=IN, Prefix=KEY)
    assert first["IsTruncated"] is True, "the fixture never reached a second page"

    result = delete_single_document(KEY, table, s3, IN, OUT)

    assert result["deleted"]["input_versions"] == 1003
    assert _versions(s3, IN) == []


@pytest.mark.unit
def test_an_output_listing_beyond_one_page_is_purged_whole(aws):
    # The same page boundary on the output side, where a document's outputs are many
    # keys beneath ``{key}/``. Deleting each page as it was listed lost the remainder
    # here as well, because the next page's marker named a deleted version.
    s3, table = aws
    for i in range(1003):
        s3.put_object(Bucket=OUT, Key=f"{KEY}/sections/{i:04d}.json", Body=b"{}")
    s3.put_object(Bucket=OUT, Key=f"{KEY}.bak/sections/0.json", Body=b"{}")

    result = delete_single_document(KEY, table, s3, IN, OUT)

    assert result["deleted"]["output_files"] == 1003
    assert _versions(s3, OUT) == [(f"{KEY}.bak/sections/0.json", "version")]


@pytest.mark.unit
def test_a_caller_denied_the_version_listing_still_hides_the_upload(aws):
    """A caller holding ``s3:DeleteObject`` but not ``s3:ListBucketVersions``.

    That is a CLI user whose policy was written for the versionless delete. Refusing the
    purge outright left the upload as the **current**, readable object while the rest of
    the delete delisted the document, which is worse than the delete marker that caller
    used to get. The versionless delete is the floor; the failure is still reported.
    moto enforces no IAM, so the denial is injected on the client.
    """
    from botocore.exceptions import ClientError

    s3, table = aws

    def deny(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
            "ListObjectVersions",
        )

    s3.put_object(Bucket=IN, Key=KEY, Body=b"original")
    s3.meta.events.register("before-call.s3.ListObjectVersions", deny)

    result = delete_single_document(KEY, table, s3, IN, OUT)

    s3.meta.events.unregister("before-call.s3.ListObjectVersions", deny)
    assert result["success"] is False
    assert any(
        "from input bucket" in e and "AccessDenied" in e for e in result["errors"]
    )
    with pytest.raises(ClientError) as raised:
        s3.head_object(Bucket=IN, Key=KEY)
    assert raised.value.response["Error"]["Code"] in ("404", "NoSuchKey")
