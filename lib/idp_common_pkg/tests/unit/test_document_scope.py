# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The single-document configuration-scope check shared by every operation that
acts on one document by key.

Real (moto) UsersTable and TrackingTable, because what is under test is how the
scope row and the tracking row combine — a refusal or an allowance that depends on
a stubbed lookup proves nothing about either.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common import document_scope as ds
from idp_common.config_scope import TENANT_ISOLATION_ENV

ADMIN = {"cognito:groups": ["Admin"], "email": "admin@example.com", "sub": "s-admin"}
SCOPED = {"cognito:groups": ["Author"], "email": "acme@example.com", "sub": "s-acme"}
OPEN = {"cognito:groups": ["Viewer"], "email": "open@example.com", "sub": "s-open"}


@pytest.fixture
def tables(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        users = ddb.create_table(
            TableName="users",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "email", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "EmailIndex",
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        users.put_item(
            Item={
                "PK": "USER#acme",
                "SK": "USER#acme",
                "email": "acme@example.com",
                "allowedConfigVersions": ["acme-*"],
            }
        )
        users.put_item(
            Item={"PK": "USER#open", "SK": "USER#open", "email": "open@example.com"}
        )
        tracking = ddb.create_table(
            TableName="tracking",
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
        tracking.put_item(
            Item={"PK": "doc#a/x.pdf", "SK": "none", "ConfigVersion": "acme-invoices"}
        )
        tracking.put_item(
            Item={"PK": "doc#g/x.pdf", "SK": "none", "ConfigVersion": "globex-w2"}
        )
        tracking.put_item(
            Item={
                "PK": "doc#old/x.pdf",
                "SK": "none",
                "ConfigurationVersion": "acme-legacy",
            }
        )
        tracking.put_item(Item={"PK": "doc#bare/x.pdf", "SK": "none"})
        yield ddb, tracking


def _check(ddb, tracking, claims, *keys):
    return ds.require_document_scope(
        {"identity": {"claims": claims}},
        keys,
        users_table_name="users",
        dynamodb=ddb,
        tracking_table=tracking,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "key,allowed",
    [
        ("a/x.pdf", True),
        # The older attribute spelling is read too.
        ("old/x.pdf", True),
        ("g/x.pdf", False),
        # Unstamped and untracked are refused, with the same message.
        ("bare/x.pdf", False),
        ("missing/x.pdf", False),
    ],
)
def test_a_scoped_caller_reaches_only_documents_in_scope(tables, key, allowed):
    ddb, tracking = tables
    if allowed:
        assert _check(ddb, tracking, SCOPED, key) == ["acme-*"]
    else:
        with pytest.raises(PermissionError, match="^Unauthorized") as exc:
            _check(ddb, tracking, SCOPED, key)
        assert str(exc.value) == ds.REFUSAL


@pytest.mark.unit
def test_one_out_of_scope_key_refuses_the_whole_batch(tables):
    ddb, tracking = tables
    with pytest.raises(PermissionError):
        _check(ddb, tracking, SCOPED, "a/x.pdf", "g/x.pdf")


@pytest.mark.unit
def test_an_admin_is_never_looked_up(tables):
    _, tracking = tables

    def boom():
        raise AssertionError("an Admin must not reach the users table")

    assert (
        ds.require_document_scope(
            {"identity": {"claims": ADMIN}},
            ["g/x.pdf", "missing/x.pdf"],
            users_table_name="users",
            dynamodb=boom,
            tracking_table=tracking,
        )
        is None
    )


@pytest.mark.unit
def test_an_unscoped_caller_is_unrestricted_in_off_mode(tables):
    ddb, tracking = tables
    assert _check(ddb, tracking, OPEN, "g/x.pdf", "missing/x.pdf") is None


@pytest.mark.unit
def test_strict_mode_refuses_an_unscoped_non_admin(tables, monkeypatch):
    ddb, tracking = tables
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    with pytest.raises(PermissionError):
        _check(ddb, tracking, OPEN, "a/x.pdf")
    # ... and leaves an Admin alone.
    assert _check(ddb, tracking, ADMIN, "a/x.pdf") is None


@pytest.mark.unit
def test_an_unevaluable_scope_is_refused(tables):
    _, tracking = tables
    with pytest.raises(PermissionError):
        ds.require_document_scope(
            {"identity": {"claims": SCOPED}},
            ["a/x.pdf"],
            users_table_name="",  # unwired: ScopeLookupError
            dynamodb=boto3.resource("dynamodb", region_name="us-east-1"),
            tracking_table=tracking,
        )


@pytest.mark.unit
@pytest.mark.parametrize("event", [None, {}, {"identity": None}, {"identity": {}}])
def test_an_event_with_no_verified_identity_is_refused(event):
    with pytest.raises(PermissionError):
        ds.caller_claims(event)


@pytest.mark.unit
def test_a_custom_lookup_is_used_instead_of_the_table():
    calls = []

    def lookup(key):
        calls.append(key)
        return "acme-x" if key == "in" else ds.ABSENT

    ds.assert_document_in_scope("in", ["acme-*"], config_version_of=lookup)
    with pytest.raises(PermissionError):
        ds.assert_document_in_scope("out", ["acme-*"], config_version_of=lookup)
    assert calls == ["in", "out"]


@pytest.mark.unit
def test_an_unrestricted_check_reads_nothing():
    # No table and no lookup: a scoped call would raise, an unrestricted one must not.
    ds.assert_document_in_scope("anything", None)
    with pytest.raises(RuntimeError):
        ds.assert_document_in_scope("anything", ["acme-*"])


@pytest.mark.unit
@pytest.mark.parametrize(
    "scope,profile,allowed",
    [
        (None, "globex-x", True),
        (["acme-*"], "acme-x", True),
        (["acme-*"], "globex-x", False),
        (["acme-*"], None, False),
    ],
)
def test_assert_profile_in_scope(scope, profile, allowed):
    if allowed:
        ds.assert_profile_in_scope(scope, profile)
    else:
        with pytest.raises(PermissionError):
            ds.assert_profile_in_scope(scope, profile)


@pytest.mark.unit
def test_refuse_if_scoped():
    ds.refuse_if_scoped(None, operation="listX")
    with pytest.raises(PermissionError, match="^Unauthorized: listX"):
        ds.refuse_if_scoped(["acme-*"], operation="listX")
