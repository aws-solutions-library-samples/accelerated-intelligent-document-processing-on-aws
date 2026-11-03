# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
The UsersTable scope writer: what a tenant grant from an identity provider's claim
or an entitlement push leaves on a user's row, and the readers' answer afterwards.

Each assertion is read back through ``resolve_allowed_config_versions`` as well as
off the row, because the row being right is only the means: what matters is the
scope every consumer computes from it.
"""

import boto3
import pytest
from moto import mock_aws

from idp_common import user_scope as us
from idp_common.config import tenants as t
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    TENANT_GRANT_SOURCE_ENV,
    TENANT_ISOLATION_ENV,
    grants_are_external,
    resolve_allowed_config_versions,
    scope_allows,
    sub_pointer_key,
    unscoped_is_denied,
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


def _users_table():
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
    return ddb, table


def _row(table, user_id, **attrs):
    item = {**user_row_key(user_id), "userId": user_id, **attrs}
    table.put_item(Item=item)
    return item


def _resolve(ddb, email, sub=""):
    return resolve_allowed_config_versions(
        email, users_table_name="users", dynamodb=ddb, caller_sub=sub
    )


def _grant(table, values, **kwargs):
    return us.apply_tenant_grant(
        table,
        resolution=t.resolve_grants(values, TENANTS),
        source=kwargs.pop("source", us.SCOPE_SOURCE_IDP),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _modes(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(TENANT_GRANT_SOURCE_ENV, raising=False)


# ---------------------------------------------------------------------------
# Grant source and the unscoped rule
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "Manual"),
        ("Manual", "Manual"),
        ("IdpClaim", "IdpClaim"),
        ("Api", "Api"),
        # Not the enum: read as today's behaviour, never as an external source.
        ("idpclaim", "Manual"),
        ("", "Manual"),
    ],
)
def test_tenant_grant_source(value, expected):
    env = {} if value is None else {TENANT_GRANT_SOURCE_ENV: value}
    assert us.tenant_grant_source(env) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "isolation,source,denied",
    [
        (None, None, False),
        ("Off", "Manual", False),
        ("Strict", "Manual", True),
        ("Off", "IdpClaim", True),
        ("Off", "Api", True),
        ("Off", "api", False),
    ],
)
def test_unscoped_is_denied(isolation, source, denied):
    env = {}
    if isolation is not None:
        env[TENANT_ISOLATION_ENV] = isolation
    if source is not None:
        env[TENANT_GRANT_SOURCE_ENV] = source
    assert unscoped_is_denied(env) is denied
    assert grants_are_external(env) is (source in ("IdpClaim", "Api"))


@pytest.mark.unit
@mock_aws
@pytest.mark.parametrize("source", ["IdpClaim", "Api"])
def test_an_unscoped_user_is_denied_under_an_external_grant_source(monkeypatch, source):
    # A federated user who signed in before any push, or a row an Admin created
    # with no scope: outside strict isolation an empty scope means unrestricted,
    # which under an external grant source would be every tenant's data.
    ddb, table = _users_table()
    _row(table, "u1", email="open@example.com")
    assert _resolve(ddb, "open@example.com") is None
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, source)
    assert _resolve(ddb, "open@example.com") == [STRICT_UNSCOPED_DENY]
    assert _resolve(ddb, "nobody@example.com") == [STRICT_UNSCOPED_DENY]


@pytest.mark.unit
@pytest.mark.parametrize("profile", ["acme-x", "default", "", None, "!no-tenant-*"])
def test_the_no_tenant_marker_matches_no_profile(profile):
    assert not scope_allows([us.NO_TENANT_GRANTED], profile)


@pytest.mark.unit
def test_the_no_tenant_marker_cannot_be_a_profile_name():
    # Profile names are [A-Za-z0-9._-]; the marker leads with '!'.
    assert not us.NO_TENANT_GRANTED[0].isalnum()
    assert us.NO_TENANT_GRANTED[0] not in "._-*?["


# ---------------------------------------------------------------------------
# apply_tenant_grant
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_a_grant_scopes_an_existing_row_to_exactly_one_tenant():
    ddb, table = _users_table()
    _row(
        table,
        "u1",
        email="ann@example.com",
        persona="Viewer",
        cognitoSub="sub-1",
        allowedConfigVersions=["globex_*", "default"],
    )
    result = _grant(
        table,
        ["IdP-Viewers", "tenant.acme"],
        email="ann@example.com",
        cognito_sub="sub-1",
    )
    assert result == {
        "action": "updated",
        "userId": "u1",
        "allowedConfigVersions": ["acme-*"],
        "tenant": "acme",
    }
    row = table.get_item(Key=user_row_key("u1"))["Item"]
    # Replaced, not merged: the grant is the whole scope.
    assert row["allowedConfigVersions"] == ["acme-*"]
    assert row["scopeSource"] == "idp"
    assert row["scopeTenant"] == "acme"
    assert table.get_item(Key=sub_pointer_key("sub-1"))["Item"]["userId"] == "u1"
    assert _resolve(ddb, "ann@example.com", "sub-1") == ["acme-*"]


@pytest.mark.unit
@mock_aws
@pytest.mark.parametrize(
    "values", [[], ["IdP-Viewers"], ["tenant.acme", "tenant.globex"]]
)
def test_no_tenant_or_several_writes_the_deny_marker(values):
    ddb, table = _users_table()
    _row(
        table,
        "u1",
        email="ann@example.com",
        persona="Viewer",
        allowedConfigVersions=["acme-*"],
        scopeTenant="acme",
    )
    result = _grant(table, values, email="ann@example.com", cognito_sub="sub-1")
    assert result["allowedConfigVersions"] == [us.NO_TENANT_GRANTED]
    assert result["tenant"] is None
    row = table.get_item(Key=user_row_key("u1"))["Item"]
    assert row["allowedConfigVersions"] == [us.NO_TENANT_GRANTED]
    # The tenant the user no longer holds is not left recorded on the row.
    assert "scopeTenant" not in row
    # Never empty: an empty scope reads as unrestricted with the modes off.
    scope = _resolve(ddb, "ann@example.com", "sub-1")
    assert scope == [us.NO_TENANT_GRANTED]
    assert not scope_allows(scope, "acme-x")
    assert not scope_allows(scope, "globex_x")


@pytest.mark.unit
@mock_aws
def test_an_admin_row_is_left_unchanged():
    ddb, table = _users_table()
    _row(table, "a1", email="boss@example.com", persona="Admin")
    result = _grant(table, ["tenant.acme"], email="boss@example.com", cognito_sub="s")
    assert result["action"] == "admin-unchanged"
    row = table.get_item(Key=user_row_key("a1"))["Item"]
    assert "allowedConfigVersions" not in row
    assert "Item" not in table.get_item(Key=sub_pointer_key("s"))


@pytest.mark.unit
@mock_aws
def test_no_row_is_created_for_a_new_admin():
    _, table = _users_table()
    result = _grant(
        table,
        ["tenant.acme"],
        email="boss@example.com",
        cognito_sub="s",
        persona_for_new_row="Admin",
    )
    assert result["action"] == "admin-unchanged"
    assert table.scan()["Items"] == []


@pytest.mark.unit
@mock_aws
def test_a_missing_row_is_created_marked_as_already_in_cognito():
    ddb, table = _users_table()
    result = _grant(
        table,
        ["tenant.globex"],
        email="new@example.com",
        cognito_sub="sub-9",
        persona_for_new_row="Author",
    )
    assert result["action"] == "created"
    row = table.get_item(Key=user_row_key(result["userId"]))["Item"]
    assert row["email"] == "new@example.com"
    assert row["persona"] == "Author"
    # src/lambda/user_sync skips a row with this marker; without it the stream
    # would create a second Cognito account and email the user an invitation.
    assert row["source"] == "cognito"
    assert row["cognitoSub"] == "sub-9"
    assert row["allowedConfigVersions"] == ["globex_*"]
    assert row["scopeTenant"] == "globex"
    assert _resolve(ddb, "other@example.com", "sub-9") == ["globex_*"]


@pytest.mark.unit
@mock_aws
def test_a_missing_row_is_reported_when_creation_is_off():
    _, table = _users_table()
    result = _grant(
        table, ["tenant.acme"], email="ghost@example.com", create_missing=False
    )
    assert result["action"] == "missing"
    assert table.scan()["Items"] == []


@pytest.mark.unit
@mock_aws
def test_an_api_push_uses_the_sub_already_recorded_on_the_row():
    # An entitlement push knows only the email; the pointer must still be written,
    # or the scope detaches from the account when its address diverges.
    ddb, table = _users_table()
    _row(table, "u1", email="ann@example.com", persona="Viewer", cognitoSub="sub-1")
    _grant(table, ["tenant.acme"], email="ann@example.com", source=us.SCOPE_SOURCE_API)
    assert table.get_item(Key=sub_pointer_key("sub-1"))["Item"]["userId"] == "u1"
    assert table.get_item(Key=user_row_key("u1"))["Item"]["scopeSource"] == "api"
    assert _resolve(ddb, "renamed@example.com", "sub-1") == ["acme-*"]


@pytest.mark.unit
@mock_aws
def test_the_row_is_found_through_the_pointer_before_the_email():
    ddb, table = _users_table()
    _row(table, "u1", email="old@example.com", persona="Viewer", cognitoSub="sub-1")
    table.put_item(Item={**sub_pointer_key("sub-1"), "userId": "u1"})
    result = _grant(
        table, ["tenant.acme"], email="new@example.com", cognito_sub="sub-1"
    )
    assert result["action"] == "updated"
    assert result["userId"] == "u1"
    assert len(table.scan()["Items"]) == 2  # the row and its pointer, no new row


@pytest.mark.unit
@mock_aws
def test_a_write_failure_raises():
    _, table = _users_table()
    _row(table, "u1", email="ann@example.com", persona="Viewer")

    class Broken:
        def __getattr__(self, name):
            return getattr(table, name)

        def update_item(self, **kwargs):
            raise RuntimeError("throttled")

    with pytest.raises(RuntimeError, match="throttled"):
        _grant(Broken(), ["tenant.acme"], email="ann@example.com")
