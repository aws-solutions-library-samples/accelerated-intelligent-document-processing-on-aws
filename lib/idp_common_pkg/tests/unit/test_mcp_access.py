# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Who an MCP caller is, and what they may do (``idp_common.mcp_access``).

Cognito and the Users table are moto, because the resolution under test combines
an ``AdminGetUser`` response with the configuration-scope lookup, and a stub of
either would decide the answer instead of the code. The claim sets mirror the
access tokens a live Cognito pool issued (recorded in the module docstring of
``mcp_access``): a user token carries ``username``, ``sub`` and
``cognito:groups`` and no ``email``; a client-credentials token carries
``client_id`` (equal to ``sub``) and no ``username``.
"""

import base64
import json
import time

import boto3
import pytest
from moto import mock_aws

from idp_common import mcp_access as ma
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    TENANT_GRANT_SOURCE_ENV,
    TENANT_ISOLATION_ENV,
)
from idp_common.document_scope import ABSENT

pytestmark = pytest.mark.unit

REGION = "us-east-1"
KEY = b"k" * 32
REQUEST = "0598ef60-4eb3-47c2-a3e9-7a352477cde2"


def caller(roles=("Viewer",), scope=None, write=True, kind="user"):
    return ma.Caller(
        kind=kind,
        id="id-1",
        name="someone",
        roles=list(roles),
        scope=scope,
        tenant=None,
        write_tools=write,
    )


# --- the policy table -------------------------------------------------------

#: Written out by hand from the REST operations each tool corresponds to
#: (sendAgentChatMessage, getDocument, uploadDocument, reprocessDocument), NOT
#: derived from MCP_TOOL_POLICY — a matrix read back from the table under test
#: would agree with any table.
EXPECTED_ALLOWED = {
    "Admin": {"search", "status", "get_results", "process", "reprocess"},
    "Author": {"search", "status", "get_results", "process", "reprocess"},
    "Reviewer": {"status", "get_results"},
    "Viewer": {"search", "status", "get_results"},
    "Annotator": set(),
}


@pytest.mark.parametrize("role", sorted(EXPECTED_ALLOWED))
def test_each_role_may_call_exactly_its_tools(role):
    assert set(ma.allowed_tool_names(caller(roles=[role]))) == EXPECTED_ALLOWED[role]


def test_the_policy_covers_exactly_the_five_tools():
    assert set(ma.MCP_TOOL_POLICY) == {
        "search",
        "status",
        "get_results",
        "process",
        "reprocess",
    }


def test_a_caller_with_no_role_may_call_nothing():
    assert ma.allowed_tool_names(caller(roles=[])) == []


@pytest.mark.parametrize("tool", ["process", "reprocess"])
def test_write_tools_need_write_enabled(tool):
    with pytest.raises(ma.McpAccessRefused, match="write tools"):
        ma.authorize_tool(caller(roles=["Author"], write=False, kind="service"), tool)


@pytest.mark.parametrize(
    "scope", [["acme-*"], [STRICT_UNSCOPED_DENY]], ids=["scoped", "strict-unscoped"]
)
def test_search_is_refused_to_any_scoped_caller(scope):
    with pytest.raises(ma.McpAccessRefused, match="configuration scope"):
        ma.authorize_tool(caller(roles=["Author"], scope=scope), "search")


def test_search_is_served_to_an_unscoped_author():
    assert ma.authorize_tool(caller(roles=["Author"]), "search").unscoped_only


def test_an_unknown_tool_is_refused_even_for_admin():
    with pytest.raises(ma.McpAccessRefused, match="not available"):
        ma.authorize_tool(caller(roles=["Admin"]), "delete_everything")


def test_bare_tool_name_strips_only_this_target():
    assert ma.bare_tool_name("IDPTools___get_results") == "get_results"
    assert ma.bare_tool_name("Other___get_results") == "Other___get_results"
    assert ma.bare_tool_name("get_results") == "get_results"


def test_tool_list_is_filtered_to_what_the_caller_may_call():
    listed = [
        {"name": ma.GATEWAY_SEARCH_TOOL},
        {"name": "IDPTools___search"},
        {"name": "IDPTools___status"},
        {"name": "IDPTools___get_results"},
        {"name": "IDPTools___process"},
        {"name": "IDPTools___reprocess"},
        {"name": "IDPTools___undeclared"},
    ]
    kept = {t["name"] for t in ma.filter_tool_list(caller(roles=["Reviewer"]), listed)}
    assert kept == {
        ma.GATEWAY_SEARCH_TOOL,
        "IDPTools___status",
        "IDPTools___get_results",
    }


def test_app_roles_ignore_groups_this_stack_does_not_create():
    assert ma.app_roles(["us-east-1_abc_Okta", "Viewer", "Admin"]) == [
        "Viewer",
        "Admin",
    ]


# --- the signed caller block ------------------------------------------------


def signed(**kw):
    return ma.sign_caller(
        caller(roles=["Author"], scope=["acme-*"]),
        key=KEY,
        request_id=REQUEST,
        now=kw.pop("now", None),
    )


def test_a_signed_block_round_trips():
    got = ma.verify_caller(signed(), key=KEY, request_id=REQUEST)
    assert (got.roles, got.scope, got.write_tools) == (["Author"], ["acme-*"], True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("roles", ["Admin"]),
        ("scope", None),
        ("writeTools", False),
        ("kind", "service"),
        ("id", "someone-else"),
        ("requestId", "another"),
        ("iat", 0),
    ],
)
def test_changing_any_field_invalidates_the_signature(field, value):
    block = signed()
    block[field] = value
    with pytest.raises(ma.McpAccessRefused, match="could not be verified"):
        ma.verify_caller(block, key=KEY, request_id=REQUEST)


def test_a_block_from_another_request_is_refused():
    with pytest.raises(ma.McpAccessRefused):
        ma.verify_caller(signed(), key=KEY, request_id="a-different-request")


def test_a_block_signed_with_another_key_is_refused():
    with pytest.raises(ma.McpAccessRefused):
        ma.verify_caller(signed(), key=b"x" * 32, request_id=REQUEST)


@pytest.mark.parametrize("age", [ma.CALLER_MAX_AGE_SECONDS + 1, -5])
def test_a_block_outside_its_age_is_refused(age):
    issued = time.time() - age
    block = signed(now=issued)
    with pytest.raises(ma.McpAccessRefused):
        ma.verify_caller(block, key=KEY, request_id=REQUEST)


@pytest.mark.parametrize(
    "block",
    [
        None,
        {},
        "a string",
        {"kind": "user", "roles": ["Admin"], "scope": None, "writeTools": True},
    ],
    ids=["none", "empty", "string", "unsigned-forgery"],
)
def test_an_unsigned_or_malformed_block_is_refused(block):
    with pytest.raises(ma.McpAccessRefused):
        ma.verify_caller(block, key=KEY, request_id=REQUEST)


@pytest.mark.parametrize("key,rid", [(b"", REQUEST), (KEY, "")])
def test_verification_without_key_or_request_id_is_refused(key, rid):
    with pytest.raises(ma.McpAccessRefused):
        ma.verify_caller(signed(), key=key, request_id=rid)


def test_verified_roles_drop_groups_the_stack_does_not_create():
    """A block can only ever widen to stack roles, never invent one."""
    c = caller(roles=["Viewer"])
    c.roles = ["Viewer", "Superuser"]
    block = ma.sign_caller(c, key=KEY, request_id=REQUEST)
    assert ma.verify_caller(block, key=KEY, request_id=REQUEST).roles == ["Viewer"]


# --- reading the token --------------------------------------------------------


def jwt(claims):
    def seg(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.c2ln"


ISS = ma.expected_issuer(REGION, "us-east-1_pool")


def test_token_claims_reads_a_bearer_token():
    claims = {"iss": ISS, "token_use": "access", "client_id": "c1", "sub": "c1"}
    assert ma.token_claims(f"Bearer {jwt(claims)}") == claims


@pytest.mark.parametrize(
    "header", [None, "", "Bearer", "Bearer not.a", "Bearer a.%%%.c"]
)
def test_an_unreadable_token_is_refused(header):
    with pytest.raises(ma.McpAccessRefused):
        ma.token_claims(header)


@pytest.mark.parametrize(
    "claims",
    [
        {
            "iss": "https://cognito-idp.us-east-1.amazonaws.com/other",
            "token_use": "access",
            "client_id": "c",
        },
        {"iss": ISS, "token_use": "id", "client_id": "c"},
        {"iss": ISS, "token_use": "access"},
    ],
    ids=["other-pool", "id-token", "no-client"],
)
def test_a_token_not_from_this_pool_as_access_token_is_refused(claims):
    with pytest.raises(ma.McpAccessRefused):
        ma.check_token_origin(claims, expected_issuer=ISS)


# --- resolving principals ----------------------------------------------------


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(TENANT_GRANT_SOURCE_ENV, raising=False)
    with mock_aws():
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        subs = {}
        for name, email in (
            ("scoped-user", "scoped@example.com"),
            ("open-user", "open@example.com"),
            ("admin-user", "admin@example.com"),
        ):
            user = cognito.admin_create_user(
                UserPoolId=pool,
                Username=name,
                UserAttributes=[{"Name": "email", "Value": email}],
            )["User"]
            subs[name] = next(
                a["Value"] for a in user["Attributes"] if a["Name"] == "sub"
            )
        ddb = boto3.resource("dynamodb", region_name=REGION)
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
                "PK": "USER#1",
                "SK": "USER#1",
                "email": "scoped@example.com",
                "allowedConfigVersions": ["acme-*"],
            }
        )
        yield {"cognito": cognito, "pool": pool, "ddb": ddb, "subs": subs}


def user_claims(world, name, *groups):
    return {
        "iss": ISS,
        "token_use": "access",
        "client_id": "user-client",
        "sub": world["subs"][name],
        "username": name,
        "cognito:groups": list(groups),
    }


def resolve(world, claims):
    return ma.resolve_caller(
        claims,
        user_client_ids=["user-client"],
        stack_service_client_ids=["svc-client"],
        cognito=world["cognito"],
        user_pool_id=world["pool"],
        users_table_name="users",
        dynamodb=world["ddb"],
    )


def test_a_scoped_user_carries_their_scope(world):
    got = resolve(world, user_claims(world, "scoped-user", "Author"))
    assert (got.kind, got.roles, got.scope) == ("user", ["Author"], ["acme-*"])


def test_an_unscoped_user_is_unrestricted_when_isolation_is_off(world):
    assert resolve(world, user_claims(world, "open-user", "Viewer")).scope is None


@pytest.mark.parametrize(
    "env,value",
    [(TENANT_ISOLATION_ENV, "Strict"), (TENANT_GRANT_SOURCE_ENV, "IdpClaim")],
)
def test_an_unscoped_user_matches_nothing_under_tenant_isolation(
    world, monkeypatch, env, value
):
    monkeypatch.setenv(env, value)
    got = resolve(world, user_claims(world, "open-user", "Viewer"))
    assert got.scope == [STRICT_UNSCOPED_DENY]


def test_an_admin_is_unscoped_under_strict(world, monkeypatch):
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    assert resolve(world, user_claims(world, "admin-user", "Admin")).scope is None


def test_a_user_with_no_stack_role_is_refused(world):
    with pytest.raises(ma.McpAccessRefused, match="no role"):
        resolve(world, user_claims(world, "open-user", "us-east-1_x_Okta"))


def test_a_token_whose_sub_is_not_the_named_user_is_refused(world):
    claims = user_claims(world, "open-user", "Admin")
    claims["sub"] = world["subs"]["admin-user"]
    with pytest.raises(ma.McpAccessRefused, match="does not match"):
        resolve(world, claims)


def test_a_disabled_user_is_refused_at_once(world):
    world["cognito"].admin_disable_user(UserPoolId=world["pool"], Username="open-user")
    with pytest.raises(ma.McpAccessRefused, match="disabled"):
        resolve(world, user_claims(world, "open-user", "Viewer"))


def test_a_user_the_pool_does_not_know_is_refused(world):
    claims = user_claims(world, "open-user", "Viewer")
    claims["username"] = "nobody"
    with pytest.raises(ma.McpAccessRefused, match="could not be found"):
        resolve(world, claims)


def test_a_scope_that_cannot_be_evaluated_refuses(world):
    with pytest.raises(ma.McpAccessRefused, match="could not be determined"):
        ma.resolve_user_caller(
            user_claims(world, "scoped-user", "Viewer"),
            cognito=world["cognito"],
            user_pool_id=world["pool"],
            users_table_name="",
            dynamodb=world["ddb"],
        )


def test_the_stack_service_client_is_an_unscoped_author_when_isolation_is_off(world):
    got = resolve(world, {"client_id": "svc-client", "sub": "svc-client"})
    assert (got.kind, got.roles, got.scope, got.write_tools) == (
        "service",
        ["Author"],
        None,
        True,
    )


@pytest.mark.parametrize(
    "env,value",
    [
        (TENANT_ISOLATION_ENV, "Strict"),
        (TENANT_GRANT_SOURCE_ENV, "IdpClaim"),
        (TENANT_GRANT_SOURCE_ENV, "Api"),
    ],
)
def test_the_stack_service_client_is_refused_under_tenant_isolation(
    world, monkeypatch, env, value
):
    monkeypatch.setenv(env, value)
    with pytest.raises(ma.McpAccessRefused, match="tenant"):
        resolve(world, {"client_id": "svc-client", "sub": "svc-client"})


@pytest.mark.parametrize(
    "claims",
    [
        {"client_id": "user-client", "sub": "x"},
        {"client_id": "svc-client", "sub": "x", "username": "someone"},
        {"client_id": "unknown", "sub": "unknown"},
    ],
    ids=["user-client-without-user", "service-client-with-user", "unknown-client"],
)
def test_a_token_that_fits_no_rule_is_refused(world, claims):
    with pytest.raises(ma.McpAccessRefused, match="not permitted"):
        resolve(world, claims)


# --- scope over documents -----------------------------------------------------

PROFILES = {"a.pdf": "acme-invoices", "g.pdf": "globex-w2", "n.pdf": None}


def profile_of(key):
    return PROFILES.get(key, ABSENT)


def test_a_scoped_caller_sees_only_documents_in_scope():
    got = ma.document_keys_in_scope(
        caller(scope=["acme-*"]),
        ["a.pdf", "g.pdf", "n.pdf", "missing.pdf"],
        config_version_of=profile_of,
    )
    assert got == ["a.pdf"]


def test_an_unscoped_caller_is_not_looked_up():
    def boom(_key):
        raise AssertionError("an unscoped caller must not be looked up")

    assert ma.document_keys_in_scope(caller(), ["g.pdf"], config_version_of=boom) == [
        "g.pdf"
    ]


def test_one_out_of_scope_key_refuses_the_whole_request():
    with pytest.raises(PermissionError, match="Unauthorized"):
        ma.require_document_keys_in_scope(
            caller(scope=["acme-*"]), ["a.pdf", "g.pdf"], config_version_of=profile_of
        )


@pytest.mark.parametrize("key", ["n.pdf", "missing.pdf"])
def test_a_document_with_no_profile_or_no_row_is_refused_to_a_scoped_caller(key):
    with pytest.raises(PermissionError):
        ma.require_document_keys_in_scope(
            caller(scope=["acme-*"]), [key], config_version_of=profile_of
        )


def test_the_strict_sentinel_refuses_every_document():
    with pytest.raises(PermissionError):
        ma.require_document_keys_in_scope(
            caller(scope=[STRICT_UNSCOPED_DENY]),
            ["a.pdf"],
            config_version_of=profile_of,
        )


@pytest.mark.parametrize(
    "scope,profile,allowed",
    [
        (None, "anything", True),
        (["acme-*"], "acme-invoices", True),
        (["acme-*"], "globex-w2", False),
        (["acme-*"], None, False),
    ],
)
def test_require_profile_in_scope(scope, profile, allowed):
    if allowed:
        ma.require_profile_in_scope(caller(scope=scope), profile)
    else:
        with pytest.raises(PermissionError):
            ma.require_profile_in_scope(caller(scope=scope), profile)
