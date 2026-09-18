# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for public readability of the version pointer (issue #962).

`<prefix>/idp-main-latest.json` is the one key the Web UI's
getLatestPublishedVersion resolver reads, anonymously, to drive the Build Info
"update available" indicator. It was written with no ACL and sits at `<prefix>/`
— a PARENT of the `<prefix>/<version>/` tree `set_public_acls` paginates — so it
was covered by neither mechanism.

That mattered because object ACLs are what grants anonymous read on the release
buckets. Measured 2026-09-18 with a credential-free GetObject against all three
release regions: this key answered 403 while its ACL'd sibling `idp-main.yaml`
in the same prefix answered 200. There is no prefix-wide anonymous-read bucket
policy to fall back on, so the indicator could never have worked.

These tests pin both halves of the fix — the inline ACL on the write, and the
repair pass in `set_public_acls` for pointers written by an earlier publish —
plus the readability check that would have caught the defect from the publish
output instead of leaving it to be found by code review.
"""

from __future__ import annotations

import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_sdk._core.publish import IDPPublisher

_BUCKET = "test-release-bucket"
_PREFIX = "artifacts/genai-idp"
_VERSION = "0.6.9"
_REGION = "us-east-1"


def _publisher(s3, public=True):
    pub = IDPPublisher(verbose=False)
    pub.bucket = _BUCKET
    pub.prefix = _PREFIX
    pub.version = _VERSION
    pub.prefix_and_version = f"{_PREFIX}/{_VERSION}"
    pub.main_template = "idp-main.yaml"
    pub.region = _REGION
    pub.public = public
    pub.s3_client = s3
    return pub


def _acl_is_public(s3, bucket, key):
    grants = s3.get_object_acl(Bucket=bucket, Key=key)["Grants"]
    return any(
        g.get("Grantee", {}).get("URI", "").endswith("AllUsers")
        and g.get("Permission") == "READ"
        for g in grants
    )


@mock_aws
def test_version_pointer_is_written_public_read_on_a_public_publish(
    monkeypatch, tmp_path
):
    """The pointer carries public-read from the moment it is created.

    Applying it at the write removes the dependency on a later ACL pass running
    at all, which is what #963 showed to be fragile: an artifact uploaded after
    `set_public_acls` cannot be reached by it.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3)
    pub._upload_version_pointer()

    key = f"{_PREFIX}/idp-main-latest.json"
    assert _acl_is_public(s3, _BUCKET, key)

    # The body is still the contract the resolver reads.
    body = json.loads(s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read())
    assert body["version"] == _VERSION
    assert body["templateUrl"].endswith(f"{_PREFIX}/idp-main_{_VERSION}.yaml")


@mock_aws
def test_version_pointer_stays_private_on_a_non_public_publish(monkeypatch, tmp_path):
    """A private publish must not hand the pointer out to the world.

    Most publishes target a developer's own bucket. The ACL is conditional on
    `--public` for the same reason every other artifact's is.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pub = _publisher(s3, public=False)
    pub._upload_version_pointer()

    assert not _acl_is_public(s3, _BUCKET, f"{_PREFIX}/idp-main-latest.json")


@mock_aws
def test_set_public_acls_repairs_a_pointer_written_without_an_acl(
    monkeypatch, tmp_path
):
    """The ACL pass covers the pointer, so a re-publish fixes an existing object.

    Every pointer in the release buckets today was written without an ACL. The
    inline ACL only fixes pointers written from this version onward; this pass is
    what repairs the ones already there.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    pointer_key = f"{_PREFIX}/idp-main-latest.json"
    # Everything the explicit-key loop requires, all uploaded PRIVATE.
    for key in (
        f"{_PREFIX}/{_VERSION}/layers/idp_common.zip",
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
        pointer_key,
    ):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"x")
    assert not _acl_is_public(s3, _BUCKET, pointer_key)

    _publisher(s3).set_public_acls()

    assert _acl_is_public(s3, _BUCKET, pointer_key)


@mock_aws
def test_set_public_acls_tolerates_an_absent_pointer(monkeypatch, tmp_path):
    """A missing pointer must not fail an otherwise good publish.

    `_upload_version_pointer` catches its own errors and warns, by design — the
    indicator is optional. Heading a key that was never written would turn that
    deliberate non-fatal path into a hard failure at the ACL step.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    for key in (
        f"{_PREFIX}/{_VERSION}/layers/idp_common.zip",
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
    ):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"x")

    _publisher(s3).set_public_acls()  # must not raise

    with pytest.raises(ClientError):
        s3.head_object(Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main-latest.json")


@mock_aws
def test_readability_check_fails_when_an_advertised_key_is_not_public(
    monkeypatch, tmp_path
):
    """The check must fail the publish on the pre-fix state, not just pass on the fixed one.

    This is the assertion that gives the guard its value: run against a bucket
    where the pointer is private — exactly the state the release buckets are in
    today — it has to refuse. A check only ever exercised against correct code
    has not been shown to work.
    """
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    s3.put_object(
        Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main.yaml", Body=b"x", ACL="public-read"
    )
    s3.put_object(
        Bucket=_BUCKET,
        Key=f"{_PREFIX}/idp-main_{_VERSION}.yaml",
        Body=b"x",
        ACL="public-read",
    )
    # The pointer: present, but private — the #962 state.
    s3.put_object(Bucket=_BUCKET, Key=f"{_PREFIX}/idp-main-latest.json", Body=b"{}")

    with pytest.raises(Exception, match="idp-main-latest.json"):
        _publisher(s3).verify_public_readability()


@mock_aws
def test_readability_check_passes_once_every_advertised_key_is_public(
    monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    for key in (
        f"{_PREFIX}/idp-main.yaml",
        f"{_PREFIX}/idp-main_{_VERSION}.yaml",
        f"{_PREFIX}/idp-main-latest.json",
    ):
        s3.put_object(Bucket=_BUCKET, Key=key, Body=b"x", ACL="public-read")

    _publisher(s3).verify_public_readability()  # must not raise


@mock_aws
def test_readability_check_is_skipped_for_a_private_publish(monkeypatch, tmp_path):
    """A private publish advertises nothing publicly, so nothing is checked."""
    monkeypatch.chdir(tmp_path)
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)

    # No objects at all: the check must not even look.
    _publisher(s3, public=False).verify_public_readability()
