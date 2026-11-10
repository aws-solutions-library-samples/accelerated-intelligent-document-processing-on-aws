# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
A tenant grant is bound to the Cognito account (its ``sub``) it was made for.

Cognito does not make ``email`` unique: the pool has no ``UsernameAttributes`` or
``AliasAttributes``, and a federated account and a native one, or two federated
accounts, can carry the same address and both be live. The UsersTable is reached by
the ``SUB#`` pointer first and by ``EmailIndex`` second, so a first-time account
with no pointer lands on whatever row already has its address. These tests drive the
real writer (``apply_tenant_grant``) and the real reader
(``resolve_allowed_config_versions``) against moto tables shaped as
``template.yaml`` declares ``UsersTable`` and its ``EmailIndex``.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common import user_scope as us
from idp_common.config import tenants as t
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    TENANT_GRANT_SOURCE_ENV,
    resolve_allowed_config_versions,
    sub_pointer_key,
    user_row_key,
)

ACME = {
    "tenantId": "acme",
    "prefixes": ["clients/acme/"],
    "profilePattern": "acme-*",
    "grantedBy": ["tenant.acme"],
}
GLOBEX = {
    "tenantId": "globex",
    "prefixes": ["clients/globex/"],
    "profilePattern": "globex_*",
    "grantedBy": ["tenant.globex"],
}
TENANTS = [ACME, GLOBEX]

EMAIL = "shared@example.com"
FIRST = ("sub-first", "tenant.acme", ["acme-*"])
SECOND = ("sub-second", "tenant.globex", ["globex_*"])


@pytest.fixture
def tables(monkeypatch):
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "IdpClaim")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
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
        yield ddb, table


def _sign_in(table, sub, claim):
    """What the pre-token trigger does at a fresh federated sign-in."""
    return us.apply_tenant_grant(
        table,
        email=EMAIL,
        cognito_sub=sub,
        resolution=t.resolve_grants([claim], TENANTS),
        source=us.SCOPE_SOURCE_IDP,
        persona_for_new_row="Viewer",
    )


def _resolve(ddb, sub):
    return resolve_allowed_config_versions(
        EMAIL, users_table_name="users", dynamodb=ddb, caller_sub=sub
    )


def _user_rows(table):
    return [i for i in table.scan()["Items"] if i["PK"].startswith("USER#")]


@pytest.mark.unit
@pytest.mark.parametrize(
    "first,second",
    [(FIRST, SECOND), (SECOND, FIRST)],
    ids=["first-then-second", "second-then-first"],
)
def test_two_accounts_sharing_an_address_keep_their_own_tenant(tables, first, second):
    ddb, table = tables
    _sign_in(table, first[0], first[1])
    _sign_in(table, second[0], second[1])

    # Each account resolves to the tenant its own provider claim granted, never
    # to the other account's.
    assert _resolve(ddb, first[0]) == first[2]
    assert _resolve(ddb, second[0]) == second[2]

    # One row per account, each recording its own sub, each pointer naming it.
    rows = _user_rows(table)
    assert sorted(r["cognitoSub"] for r in rows) == sorted([first[0], second[0]])
    for sub, _, scope in (first, second):
        pointer = table.get_item(Key=sub_pointer_key(sub))["Item"]
        row = table.get_item(Key=user_row_key(pointer["userId"]))["Item"]
        assert row["cognitoSub"] == sub
        assert row["allowedConfigVersions"] == scope


@pytest.mark.unit
def test_a_row_recording_no_sub_is_still_used(tables):
    """A row an Admin pre-created by address records no sub yet; the first fresh
    sign-in with that address must still claim it, as it always has."""
    ddb, table = tables
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Author",
        }
    )
    result = _sign_in(table, "sub-1", "tenant.acme")
    assert result["action"] == "updated" and result["userId"] == "u1"
    row = table.get_item(Key=user_row_key("u1"))["Item"]
    assert row["cognitoSub"] == "sub-1"
    assert len(_user_rows(table)) == 1
    assert _resolve(ddb, "sub-1") == ["acme-*"]


@pytest.mark.unit
def test_a_returning_account_reaches_its_row_through_its_pointer(tables):
    ddb, table = tables
    first = _sign_in(table, "sub-1", "tenant.acme")
    again = _sign_in(table, "sub-1", "tenant.globex")
    assert again["action"] == "updated" and again["userId"] == first["userId"]
    assert len(_user_rows(table)) == 1
    assert _resolve(ddb, "sub-1") == ["globex_*"]


@pytest.mark.unit
@pytest.mark.parametrize("source", ["IdpClaim", "Api"])
def test_the_reader_denies_a_row_recording_another_accounts_sub(
    tables, monkeypatch, source
):
    """A row found by address that records a different sub is not the caller's.
    Under an external grant source its scope is another account's tenant, so the
    caller is denied rather than handed it."""
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, source)
    ddb, table = tables
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Viewer",
            "cognitoSub": "sub-owner",
            "allowedConfigVersions": ["acme-*"],
        }
    )
    assert _resolve(ddb, "sub-owner") == ["acme-*"]
    assert _resolve(ddb, "sub-other") == [STRICT_UNSCOPED_DENY]
    # With no sub presented, the email join is all there is; unchanged.
    assert _resolve(ddb, "") == ["acme-*"]


@pytest.mark.unit
def test_the_reader_denies_a_pointer_naming_another_accounts_row(tables):
    """A pointer that names a row recording a different sub (the state the shared-
    row defect left behind) is not believed under an external grant source."""
    ddb, table = tables
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Viewer",
            "cognitoSub": "sub-second",
            "allowedConfigVersions": ["globex_*"],
        }
    )
    table.put_item(Item={**sub_pointer_key("sub-first"), "userId": "u1"})
    assert _resolve(ddb, "sub-first") == [STRICT_UNSCOPED_DENY]


@pytest.mark.unit
def test_under_manual_the_reader_still_applies_the_rows_scope(tables, monkeypatch):
    """Under Manual an Admin set the scope by hand on a row matched by address, and
    a scope is a restriction there (an unscoped caller is unrestricted), so applying
    it is the tighter answer than dropping it."""
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Manual")
    ddb, table = tables
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Viewer",
            "cognitoSub": "sub-owner",
            "allowedConfigVersions": ["acme-*"],
        }
    )
    assert _resolve(ddb, "sub-other") == ["acme-*"]


@pytest.mark.unit
def test_under_manual_with_strict_isolation_the_reader_denies(tables, monkeypatch):
    """Strict isolation denies an unscoped caller, so another account's scope would
    be a grant to this one, not a restriction; it denies as the external sources
    do."""
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Manual")
    monkeypatch.setenv("TENANT_ISOLATION_MODE", "Strict")
    ddb, table = tables
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Viewer",
            "cognitoSub": "sub-owner",
            "allowedConfigVersions": ["acme-*"],
        }
    )
    assert _resolve(ddb, "sub-owner") == ["acme-*"]
    assert _resolve(ddb, "sub-other") == [STRICT_UNSCOPED_DENY]


def _shared_row(table):
    """One row shared by two accounts: it records the second account's sub and
    tenant, and both accounts' pointers name it."""
    table.put_item(
        Item={
            **user_row_key("u1"),
            "userId": "u1",
            "email": EMAIL,
            "persona": "Viewer",
            "status": "active",
            "cognitoSub": "sub-second",
            "allowedConfigVersions": ["globex_*"],
            "scopeTenant": "globex",
            "scopeSource": "idp",
        }
    )
    for sub in ("sub-first", "sub-second"):
        table.put_item(Item={**sub_pointer_key(sub), "userId": "u1", "cognitoSub": sub})


@pytest.mark.unit
def test_an_existing_shared_row_separates_at_the_next_fresh_sign_ins(tables):
    ddb, table = tables
    _shared_row(table)
    # Before anyone signs in again, the account the row does not record is denied
    # rather than given the other account's tenant.
    assert _resolve(ddb, "sub-first") == [STRICT_UNSCOPED_DENY]
    assert _resolve(ddb, "sub-second") == ["globex_*"]

    first = _sign_in(table, "sub-first", "tenant.acme")
    assert first["action"] == "created" and first["userId"] != "u1"
    # The second account's row is untouched by the first account's sign-in.
    row = table.get_item(Key=user_row_key("u1"))["Item"]
    assert row["cognitoSub"] == "sub-second"
    assert row["allowedConfigVersions"] == ["globex_*"]
    assert _resolve(ddb, "sub-first") == ["acme-*"]
    assert _resolve(ddb, "sub-second") == ["globex_*"]

    second = _sign_in(table, "sub-second", "tenant.globex")
    assert second["userId"] == "u1"
    assert _resolve(ddb, "sub-first") == ["acme-*"]


@pytest.mark.unit
def test_an_admin_sign_in_does_not_clear_another_accounts_scope(tables):
    """The Admin path removes a scope the claim granted; it must remove only this
    account's, never one on a row recording another sub with the same address."""
    ddb, table = tables
    _sign_in(table, "sub-first", "tenant.acme")
    cleared = us.clear_granted_scope(
        table, email=EMAIL, cognito_sub="sub-admin", source=us.SCOPE_SOURCE_IDP
    )
    assert cleared is None
    assert _resolve(ddb, "sub-first") == ["acme-*"]
    (row,) = _user_rows(table)
    assert row["cognitoSub"] == "sub-first"


@pytest.mark.unit
def test_a_grant_that_creates_no_row_leaves_another_accounts_row_alone(tables):
    """A claim mapping to no role creates no row; with only another account's row
    for the address there is nothing of this account's to rewrite."""
    _, table = tables
    _sign_in(table, "sub-first", "tenant.acme")
    result = us.apply_tenant_grant(
        table,
        email=EMAIL,
        cognito_sub="sub-second",
        resolution=t.resolve_grants(["tenant.globex"], TENANTS),
        source=us.SCOPE_SOURCE_IDP,
        create_missing=False,
    )
    assert result["action"] == "missing"
    (row,) = _user_rows(table)
    assert row["allowedConfigVersions"] == ["acme-*"]
    assert "Item" not in table.get_item(Key=sub_pointer_key("sub-second"))
