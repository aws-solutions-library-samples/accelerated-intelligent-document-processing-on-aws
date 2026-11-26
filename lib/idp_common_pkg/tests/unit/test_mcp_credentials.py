# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""MCP service credentials (``idp_common.mcp_credentials``) and the gateway's client
list (``idp_common.mcp_gateway``).

Cognito, Secrets Manager and DynamoDB are moto, so a created credential is a real
(moto) app client with a real stored secret and a real row. moto has no AgentCore
Gateway, so the gateway is a small fake that keeps one configuration and validates
every ``UpdateGateway`` request against the real botocore service model, and that
replaces the configuration on update exactly as the live API was measured to.
"""

import json
import threading
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import ParamValidationError
from botocore.validate import ParamValidator
from moto import mock_aws

from idp_common import mcp_access, mcp_gateway
from idp_common import mcp_credentials as mc
from idp_common.config_scope import (
    STRICT_UNSCOPED_DENY,
    TENANT_GRANT_SOURCE_ENV,
    TENANT_ISOLATION_ENV,
)

pytestmark = pytest.mark.unit

REGION = "us-east-1"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
TENANTS = [
    {"tenantId": "acme", "prefixes": ["acme/"], "profilePattern": "acme-*"},
    {"tenantId": "globex", "prefixes": ["globex/"], "profilePattern": "globex-*"},
]


class FakeGateway:
    """One gateway. ``update_gateway`` replaces its configuration with the request
    (measured behaviour), after validating the request against the service model."""

    def __init__(self, allowed):
        real = boto3.client("bedrock-agentcore-control", region_name=REGION)
        self.meta = real.meta
        self._validator = ParamValidator()
        self._model = real.meta.service_model.operation_model("UpdateGateway")
        self.state = {
            "gatewayId": "gw-1",
            "gatewayArn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/gw-1",
            "gatewayUrl": "https://gw.example/mcp",
            "status": "READY",
            "name": "gw",
            "roleArn": "arn:aws:iam::111122223333:role/r",
            "protocolType": "MCP",
            "protocolConfiguration": {"mcp": {"searchType": "SEMANTIC"}},
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {
                    "discoveryUrl": "https://example/.well-known/openid-configuration",
                    "allowedClients": list(allowed),
                }
            },
            "interceptorConfigurations": mcp_gateway.desired_interceptors(
                "arn:aws:lambda:us-east-1:111122223333:function:i"
            ),
        }
        self.updates = 0
        self.fail_next_update = False
        self.concurrent = None

    def get_gateway(self, gatewayIdentifier):
        return json.loads(json.dumps(self.state))

    def update_gateway(self, **request):
        report = self._validator.validate(request, self._model.input_shape)
        if report.has_errors():
            raise ParamValidationError(report=report.generate_report())
        if self.fail_next_update:
            self.fail_next_update = False
            raise RuntimeError("UpdateGateway failed")
        keep = {
            k: self.state[k]
            for k in ("gatewayId", "gatewayArn", "gatewayUrl", "status")
        }
        self.state = {
            **keep,
            **{k: v for k, v in request.items() if k != "gatewayIdentifier"},
        }
        if self.concurrent:
            self.concurrent(self.state)
            self.concurrent = None
        self.updates += 1
        return {}

    def allowed(self):
        return self.state["authorizerConfiguration"]["customJWTAuthorizer"][
            "allowedClients"
        ]


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv(TENANT_ISOLATION_ENV, raising=False)
    monkeypatch.delenv(TENANT_GRANT_SOURCE_ENV, raising=False)
    with mock_aws():
        cognito = boto3.client("cognito-idp", region_name=REGION)
        pool = cognito.create_user_pool(PoolName="p")["UserPool"]["Id"]
        cognito.create_resource_server(
            UserPoolId=pool,
            Identifier="idp-mcp-connector",
            Name="idp-mcp-connector",
            Scopes=[{"ScopeName": "access", "ScopeDescription": "a"}],
        )
        kms = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["Arn"]
        ddb = boto3.resource("dynamodb", region_name=REGION)
        table = ddb.create_table(
            TableName="creds",
            KeySchema=[{"AttributeName": "clientId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "clientId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        gateway = FakeGateway(["user-client", "stack-client"])
        notes = []
        services = mc.CredentialServices(
            cognito=cognito,
            secrets=boto3.client("secretsmanager", region_name=REGION),
            table=table,
            control=gateway,
            gateway_id="gw-1",
            user_pool_id=pool,
            stack_name="stk",
            kms_key_id=kms,
            oauth_scopes=["idp-mcp-connector/access"],
            token_url="https://auth.example/oauth2/token",
            gateway_url="https://gw.example/mcp",
            tenants=lambda: list(TENANTS),
            notify=lambda **kw: notes.append(kw),
        )
        monkeypatch.setattr(mcp_gateway.time, "sleep", lambda _s: None)
        yield {"svc": services, "gw": gateway, "notes": notes, "pool": pool}


def create(world, **kw):
    args = {
        "name": "nightly",
        "role": "Viewer",
        "allowed_config_versions": ["acme-*"],
        "actor": "admin@example.com",
        "now": NOW,
    }
    args.update(kw)
    return mc.create_credential(world["svc"], **args)


# --- create ---------------------------------------------------------------------


def test_create_issues_a_client_credentials_app_with_a_stored_secret(world):
    out = create(world)
    svc = world["svc"]
    client = svc.cognito.describe_user_pool_client(
        UserPoolId=world["pool"], ClientId=out["clientId"]
    )["UserPoolClient"]
    assert client["AllowedOAuthFlows"] == ["client_credentials"]
    assert client["AccessTokenValidity"] == 60
    # No user can sign in through it, and no user token can write an attribute
    # through it.
    assert client["ExplicitAuthFlows"] == ["ALLOW_REFRESH_TOKEN_AUTH"]
    assert client["WriteAttributes"] == ["email"]
    assert client["ReadAttributes"] == ["email"]
    assert client["EnableTokenRevocation"] is True
    assert client["PreventUserExistenceErrors"] == "ENABLED"
    stored = json.loads(
        svc.secrets.get_secret_value(SecretId=f"stk/mcp/credentials/{out['clientId']}")[
            "SecretString"
        ]
    )
    assert stored["clientSecret"] == out["clientSecret"] == client["ClientSecret"]
    assert out["clientId"] in world["gw"].allowed()
    assert world["gw"].allowed()[:2] == ["user-client", "stack-client"]


def test_the_secret_is_returned_once_and_never_listed_or_stored_on_the_row(
    world, caplog
):
    caplog.set_level("DEBUG")
    out = create(world)
    secret = out["clientSecret"]
    row = mc.get_row(world["svc"].table, out["clientId"])
    assert secret not in json.dumps(row, default=str)
    assert secret not in json.dumps(mc.list_credentials(world["svc"]), default=str)
    assert secret not in json.dumps(out["credential"], default=str)
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("idp_common")
    )
    assert ours and secret not in ours


def test_updating_the_gateway_keeps_its_other_configuration(world):
    before = world["gw"].get_gateway("gw-1")
    create(world)
    after = world["gw"].get_gateway("gw-1")
    for field in ("protocolConfiguration", "interceptorConfigurations", "roleArn"):
        assert after[field] == before[field]


def test_a_failed_gateway_update_leaves_nothing_behind(world):
    world["gw"].fail_next_update = True
    with pytest.raises(RuntimeError):
        create(world)
    svc = world["svc"]
    assert svc.table.scan()["Items"] == []
    clients = svc.cognito.list_user_pool_clients(
        UserPoolId=world["pool"], MaxResults=60
    )
    assert clients["UserPoolClients"] == []
    secrets = svc.secrets.list_secrets()["SecretList"]
    assert not [s for s in secrets if "mcp/credentials" in s["Name"]]


@pytest.mark.parametrize(
    "kw,message",
    [
        ({"role": "Superuser"}, "Role must be"),
        ({"write_tools": True}, "Write tools"),
        ({"allowed_config_versions": ["acme-*", "globex-*"]}, "spans tenants"),
        ({"allowed_config_versions": ["acme-x", "shared"]}, "mixes tenant"),
        ({"expires_in_days": 5000}, "Expiry"),
        ({"name": "bad/name"}, "Name must"),
        ({"role": "Admin"}, "no configuration scope"),
    ],
)
def test_create_refuses_what_a_user_scope_would_refuse(world, kw, message):
    with pytest.raises(mc.CredentialError, match=message):
        create(world, **kw)
    assert world["svc"].table.scan()["Items"] == []


@pytest.mark.parametrize(
    "env,value",
    [(TENANT_ISOLATION_ENV, "Strict"), (TENANT_GRANT_SOURCE_ENV, "IdpClaim")],
)
def test_tenant_isolation_requires_a_scope_and_refuses_admin(
    world, monkeypatch, env, value
):
    monkeypatch.setenv(env, value)
    with pytest.raises(mc.CredentialError, match="needs a configuration scope"):
        create(world, allowed_config_versions=None)
    with pytest.raises(mc.CredentialError, match="Admin credential"):
        create(world, role="Admin", allowed_config_versions=None)


def test_never_expiring_is_allowed_and_default_is_ninety_days(world):
    never = create(world, expires_in_days=0)["credential"]
    default = create(world, name="other")["credential"]
    assert "expiresAt" not in never
    assert default["expiresAt"] == "2027-02-09T12:00:00Z"


# --- resolving a caller -----------------------------------------------------------


def test_a_credential_resolves_to_its_role_and_scope(world):
    out = create(world, role="Author", write_tools=True)
    caller = mc.caller_from_row(
        mc.get_row(world["svc"].table, out["clientId"]), now=NOW
    )
    assert (caller.kind, caller.roles, caller.scope, caller.write_tools) == (
        "service",
        ["Author"],
        ["acme-*"],
        True,
    )


def test_a_viewer_credential_reads_and_cannot_write(world):
    out = create(world)
    caller = mc.caller_from_row(
        mc.get_row(world["svc"].table, out["clientId"]), now=NOW
    )
    mcp_access.authorize_tool(caller, "get_results")
    with pytest.raises(mcp_access.McpAccessRefused):
        mcp_access.authorize_tool(caller, "process")


@pytest.mark.parametrize(
    "change,word",
    [
        (
            lambda svc, cid: mc.revoke_credential(svc, cid, actor="a", now=NOW),
            "revoked",
        ),
        (
            lambda svc, cid: mc.update_credential(
                svc, cid, enabled=False, actor="a", now=NOW
            ),
            "disabled",
        ),
    ],
    ids=["revoked", "disabled"],
)
def test_a_revoked_or_disabled_credential_is_refused_on_the_next_read(
    world, change, word
):
    out = create(world)
    change(world["svc"], out["clientId"])
    with pytest.raises(mcp_access.McpAccessRefused, match=word):
        mc.caller_from_row(mc.get_row(world["svc"].table, out["clientId"]), now=NOW)


def test_an_expired_credential_is_refused(world):
    out = create(world, expires_in_days=1)
    row = mc.get_row(world["svc"].table, out["clientId"])
    with pytest.raises(mcp_access.McpAccessRefused, match="expired"):
        mc.caller_from_row(row, now=NOW + timedelta(days=2))


def test_an_unknown_client_is_refused():
    with pytest.raises(mcp_access.McpAccessRefused, match="not permitted"):
        mc.caller_from_row(None, now=NOW)


def test_strict_switched_on_later_narrows_an_unscoped_credential(world, monkeypatch):
    out = create(world, allowed_config_versions=None)
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    caller = mc.caller_from_row(
        mc.get_row(world["svc"].table, out["clientId"]), now=NOW
    )
    assert caller.scope == [STRICT_UNSCOPED_DENY]


def test_strict_switched_on_later_refuses_an_admin_credential(world, monkeypatch):
    out = create(world, role="Admin", allowed_config_versions=None)
    monkeypatch.setenv(TENANT_ISOLATION_ENV, "Strict")
    with pytest.raises(mcp_access.McpAccessRefused, match="Admin"):
        mc.caller_from_row(mc.get_row(world["svc"].table, out["clientId"]), now=NOW)


def test_resolve_caller_routes_other_clients_to_the_registry(world):
    out = create(world)
    caller = mcp_access.resolve_caller(
        {"client_id": out["clientId"], "sub": out["clientId"]},
        user_client_ids=["user-client"],
        cognito=None,
        user_pool_id="",
        users_table_name="",
        dynamodb=None,
        credential_row=lambda cid: mc.get_row(world["svc"].table, cid),
    )
    assert caller.name == "nightly" and caller.scope == ["acme-*"]


# --- revoke, rotate, update, tenant -------------------------------------------------


def test_revoke_removes_the_client_secret_and_gateway_entry(world):
    out = create(world)
    cid = out["clientId"]
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW)
    svc = world["svc"]
    assert cid not in world["gw"].allowed()
    with pytest.raises(svc.cognito.exceptions.ResourceNotFoundException):
        svc.cognito.describe_user_pool_client(UserPoolId=world["pool"], ClientId=cid)
    described = svc.secrets.describe_secret(SecretId=f"stk/mcp/credentials/{cid}")
    assert "DeletedDate" in described
    assert mc.get_row(svc.table, cid)["status"] == "revoked"


def test_revoke_refuses_the_credential_even_when_cleanup_fails(world):
    out = create(world)
    world["gw"].fail_next_update = True
    mc.revoke_credential(world["svc"], out["clientId"], actor="a", now=NOW)
    with pytest.raises(mcp_access.McpAccessRefused):
        mc.caller_from_row(mc.get_row(world["svc"].table, out["clientId"]), now=NOW)


def test_rotate_issues_a_replacement_and_shortens_the_old_one(world):
    old = create(world, role="Author", write_tools=True)
    new = mc.rotate_credential(
        world["svc"], old["clientId"], overlap_days=3, actor="a", now=NOW
    )
    svc = world["svc"]
    old_row = mc.get_row(svc.table, old["clientId"])
    new_row = mc.get_row(svc.table, new["clientId"])
    assert new["clientSecret"] and new["clientId"] != old["clientId"]
    assert (
        new_row["role"],
        new_row["allowedConfigVersions"],
        new_row["writeTools"],
    ) == (
        "Author",
        ["acme-*"],
        True,
    )
    assert old_row["expiresAt"] == "2026-11-14T12:00:00Z"
    assert old_row["replacedBy"] == new["clientId"]
    assert new_row["rotatedFrom"] == old["clientId"]
    assert {old["clientId"], new["clientId"]} <= set(world["gw"].allowed())


def test_rotate_overlap_is_bounded(world):
    old = create(world)
    with pytest.raises(mc.CredentialError, match="overlap"):
        mc.rotate_credential(
            world["svc"], old["clientId"], overlap_days=8, actor="a", now=NOW
        )


def test_update_validates_the_merged_access(world):
    out = create(world)
    with pytest.raises(mc.CredentialError, match="Write tools"):
        mc.update_credential(world["svc"], out["clientId"], write_tools=True, actor="a")
    updated = mc.update_credential(
        world["svc"],
        out["clientId"],
        role="Author",
        write_tools=True,
        actor="a",
        now=NOW,
    )
    assert updated["role"] == "Author" and updated["writeTools"] is True


def test_a_revoked_credential_cannot_be_changed(world):
    out = create(world)
    mc.revoke_credential(world["svc"], out["clientId"], actor="a", now=NOW)
    with pytest.raises(mc.CredentialError, match="revoked"):
        mc.update_credential(world["svc"], out["clientId"], enabled=True, actor="a")


def test_set_tenant_only_under_the_api_grant_source(world, monkeypatch):
    out = create(world)
    with pytest.raises(mc.CredentialError, match="TenantGrantSource is Api"):
        mc.set_credential_tenant(world["svc"], out["clientId"], "globex", actor="a")
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Api")
    got = mc.set_credential_tenant(world["svc"], out["clientId"], "globex", actor="a")
    assert got["allowedConfigVersions"] == ["globex-*"] and got["tenant"] == "globex"


def test_under_api_a_credential_is_created_unscoped_and_resolves_to_nothing(
    world, monkeypatch
):
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Api")
    with pytest.raises(mc.CredentialError, match="setMcpCredentialTenant"):
        create(world)
    out = create(world, allowed_config_versions=None)
    caller = mc.caller_from_row(
        mc.get_row(world["svc"].table, out["clientId"]), now=NOW
    )
    assert caller.scope == [STRICT_UNSCOPED_DENY]


# --- the sweep ------------------------------------------------------------------------


def test_the_sweep_revokes_expired_and_warns_once_before_expiry(world):
    gone = create(world, name="gone", expires_in_days=1)["clientId"]
    soon = create(world, name="soon", expires_in_days=10)["clientId"]
    later = create(world, name="later", expires_in_days=60)["clientId"]
    first = mc.sweep(world["svc"], now=NOW + timedelta(days=2))
    assert (first["expired"], first["warned"]) == ([gone], [soon])
    assert mc.get_row(world["svc"].table, gone)["status"] == "revoked"
    assert gone not in world["gw"].allowed()
    second = mc.sweep(world["svc"], now=NOW + timedelta(days=3))
    assert (second["expired"], second["warned"]) == ([], [])
    assert [n["users"] for n in world["notes"]] == [["admin@example.com"]]
    assert later in world["gw"].allowed()


# --- last used, and concurrent gateway writers ------------------------------------------


def test_last_used_is_recorded_at_most_hourly(world):
    out = create(world)
    table = world["svc"].table
    mc.touch_last_used(table, out["clientId"], NOW)
    mc.touch_last_used(table, out["clientId"], NOW + timedelta(minutes=30))
    assert mc.get_row(table, out["clientId"])["lastUsedAt"] == "2026-11-11T12:00:00Z"
    mc.touch_last_used(table, out["clientId"], NOW + timedelta(minutes=61))
    assert mc.get_row(table, out["clientId"])["lastUsedAt"] == "2026-11-11T13:01:00Z"


def test_a_concurrent_client_list_change_is_retried(world):
    gw = world["gw"]

    def other_writer(state):
        # Another writer's update lands right after ours, from a stale read.
        state["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] = [
            "user-client",
            "stack-client",
        ]

    gw.concurrent = other_writer
    mcp_gateway.update_allowed_clients(gw, "gw-1", add=["new-client"])
    assert "new-client" in gw.allowed() and gw.updates == 2


def test_the_gateway_is_never_left_with_no_clients(world):
    with pytest.raises(ValueError):
        mcp_gateway.update_allowed_clients(
            world["gw"], "gw-1", remove=["user-client", "stack-client"]
        )


def test_write_tools_on_a_row_never_reach_a_role_that_may_not_write():
    """Validation never stores this row; a row written another way still grants no
    write tools to a Viewer."""
    caller = mc.caller_from_row(
        {"clientId": "c", "role": "Viewer", "writeTools": True, "enabled": True},
        now=NOW,
    )
    assert caller.write_tools is False


def test_an_unchanged_client_list_is_not_written(world):
    gw = world["gw"]
    assert mcp_gateway.update_allowed_clients(gw, "gw-1", add=["user-client"]) == [
        "user-client",
        "stack-client",
    ]
    assert gw.updates == 0


def test_a_gateway_without_a_jwt_authorizer_is_refused(world):
    world["gw"].state["authorizerConfiguration"] = {}
    with pytest.raises(RuntimeError, match="no custom JWT authorizer"):
        mcp_gateway.update_allowed_clients(world["gw"], "gw-1", add=["new-client"])


def test_a_client_list_that_keeps_changing_is_given_up_on(world):
    gw = world["gw"]
    real = gw.update_gateway

    def update_then_lose(**request):
        real(**request)
        gw.state["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] = [
            "user-client"
        ]
        return {}

    gw.update_gateway = update_then_lose
    with pytest.raises(RuntimeError, match="could not update the allowed clients"):
        mcp_gateway.update_allowed_clients(gw, "gw-1", add=["new-client"], attempts=2)


class _Secrets:
    def __init__(self, value):
        self.value = value

    def get_secret_value(self, SecretId):
        return {"SecretString": self.value}


def test_the_signing_key_is_the_secret_bytes():
    assert mcp_access.load_signing_key(_Secrets("abc"), "arn:secret") == b"abc"


@pytest.mark.parametrize(
    "secret_id,value,message",
    [("", "abc", "not configured"), ("arn:secret", "", "empty")],
)
def test_a_missing_or_empty_signing_key_raises(secret_id, value, message):
    with pytest.raises(RuntimeError, match=message):
        mcp_access.load_signing_key(_Secrets(value), secret_id)


@pytest.mark.parametrize(
    "key,request_id,message",
    [(b"", "req", "signing key"), (b"k", "", "request id")],
)
def test_signing_needs_a_key_and_a_request_id(key, request_id, message):
    caller = mcp_access.Caller(
        kind="user",
        id="u",
        name="u",
        roles=["Viewer"],
        scope=None,
        tenant=None,
        write_tools=False,
    )
    with pytest.raises(ValueError, match=message):
        mcp_access.sign_caller(caller, key=key, request_id=request_id)


# --- a revoke racing another change, rotation, reconciliation, registry removal ------


def _stale_require(monkeypatch, row):
    """A change that read the row before a concurrent revoke landed."""
    monkeypatch.setattr(mc, "_require", lambda _svc, _cid: dict(row))


def test_an_update_that_read_the_row_before_a_revoke_cannot_restore_it(
    world, monkeypatch
):
    out = create(world)
    cid = out["clientId"]
    before = mc.get_row(world["svc"].table, cid)
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW)
    _stale_require(monkeypatch, before)
    with pytest.raises(mc.CredentialError, match="revoked"):
        mc.update_credential(world["svc"], cid, enabled=True, actor="a", now=NOW)
    row = mc.get_row(world["svc"].table, cid)
    assert (row["status"], row["enabled"]) == ("revoked", False)


def test_a_tenant_grant_that_read_the_row_before_a_revoke_cannot_restore_it(
    world, monkeypatch
):
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Api")
    out = create(world, allowed_config_versions=None)
    cid = out["clientId"]
    before = mc.get_row(world["svc"].table, cid)
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW)
    _stale_require(monkeypatch, before)
    with pytest.raises(mc.CredentialError, match="revoked"):
        mc.set_credential_tenant(world["svc"], cid, "acme", actor="a")
    assert mc.get_row(world["svc"].table, cid)["status"] == "revoked"


def test_a_replacement_is_withdrawn_when_the_old_credential_cannot_be_shortened(
    world, monkeypatch
):
    old = create(world)
    table = world["svc"].table
    real = table.update_item

    def fail_on_shorten(**kw):
        if "replacedBy" in kw.get("UpdateExpression", ""):
            raise RuntimeError("write failed")
        return real(**kw)

    monkeypatch.setattr(table, "update_item", fail_on_shorten)
    with pytest.raises(RuntimeError):
        mc.rotate_credential(world["svc"], old["clientId"], actor="a", now=NOW)
    monkeypatch.setattr(table, "update_item", real)
    rows = {r["clientId"]: r for r in table.scan()["Items"]}
    (new_id,) = [c for c in rows if c != old["clientId"]]
    assert rows[new_id]["status"] == "revoked"
    assert new_id not in world["gw"].allowed()
    assert rows[old["clientId"]]["status"] == "active"


def test_the_sweep_does_not_warn_about_a_credential_already_rotated(world):
    old = create(world, expires_in_days=30)
    mc.rotate_credential(
        world["svc"], old["clientId"], overlap_days=7, actor="a", now=NOW
    )
    result = mc.sweep(world["svc"], now=NOW + timedelta(days=1))
    assert result["warned"] == []


def test_the_sweep_puts_the_gateway_client_list_back_in_line(world):
    live = create(world, name="live")["clientId"]
    gone = create(world, name="gone")["clientId"]
    mc.revoke_credential(world["svc"], gone, actor="a", now=NOW)
    gw = world["gw"]
    # A concurrent stale writer dropped a live client and restored a revoked one.
    gw.state["authorizerConfiguration"]["customJWTAuthorizer"]["allowedClients"] = [
        "user-client",
        "stack-client",
        gone,
    ]
    result = mc.sweep(world["svc"], now=NOW)
    assert result["reconciled"] == {"ok": True}
    assert live in gw.allowed() and gone not in gw.allowed()
    assert gw.allowed()[:2] == ["user-client", "stack-client"]


def test_removing_the_registry_revokes_every_credential(world):
    a = create(world, name="a")["clientId"]
    b = create(world, name="b")["clientId"]
    svc = world["svc"]
    result = mc.revoke_all(svc, actor="cloudformation", now=NOW)
    assert sorted(result["revoked"]) == sorted([a, b]) and result["errors"] == []
    assert world["gw"].allowed() == ["user-client", "stack-client"]
    for cid in (a, b):
        assert mc.get_row(svc.table, cid)["status"] == "revoked"
        with pytest.raises(svc.cognito.exceptions.ResourceNotFoundException):
            svc.cognito.describe_user_pool_client(
                UserPoolId=world["pool"], ClientId=cid
            )
        assert "DeletedDate" in svc.secrets.describe_secret(
            SecretId=f"stk/mcp/credentials/{cid}"
        )


def test_removing_the_registry_reports_rather_than_raises(world):
    a = create(world)["clientId"]
    world["gw"].fail_next_update = True
    result = mc.revoke_all(world["svc"], actor="cloudformation", now=NOW)
    assert result["revoked"] == [a]
    assert [e for e in result["errors"] if e.startswith("gateway")]
    assert mc.get_row(world["svc"].table, a)["status"] == "revoked"


# --- the remaining branches of each operation ---------------------------------------


def test_an_update_can_narrow_or_clear_the_scope(world):
    out = create(world, allowed_config_versions=["acme-*"])
    cid = out["clientId"]
    narrowed = mc.update_credential(
        world["svc"], cid, allowed_config_versions=["acme-invoices"], actor="a"
    )
    assert narrowed["allowedConfigVersions"] == ["acme-invoices"]
    cleared = mc.update_credential(world["svc"], cid, clear_scope=True, actor="a")
    assert "allowedConfigVersions" not in cleared


def test_under_api_an_update_cannot_set_the_scope(world, monkeypatch):
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Api")
    out = create(world, allowed_config_versions=None)
    with pytest.raises(mc.CredentialError, match="entitlement system"):
        mc.update_credential(
            world["svc"],
            out["clientId"],
            allowed_config_versions=["acme-*"],
            actor="a",
        )


@pytest.mark.parametrize(
    "setup,tenant,message",
    [
        ("admin", "acme", "cross-tenant"),
        ("revoked", "acme", "revoked"),
        ("viewer", "initech", "not declared"),
    ],
)
def test_a_tenant_grant_is_refused(world, monkeypatch, setup, tenant, message):
    out = create(
        world,
        role="Admin" if setup == "admin" else "Viewer",
        allowed_config_versions=None,
    )
    if setup == "revoked":
        mc.revoke_credential(world["svc"], out["clientId"], actor="a", now=NOW)
    monkeypatch.setenv(TENANT_GRANT_SOURCE_ENV, "Api")
    with pytest.raises(mc.CredentialError, match=message):
        mc.set_credential_tenant(world["svc"], out["clientId"], tenant, actor="a")


def test_only_an_active_credential_can_be_rotated(world):
    out = create(world)
    mc.update_credential(world["svc"], out["clientId"], enabled=False, actor="a")
    with pytest.raises(mc.CredentialError, match="Only an active"):
        mc.rotate_credential(world["svc"], out["clientId"], actor="a", now=NOW)


def test_rotating_with_no_overlap_revokes_the_old_credential_at_once(world):
    old = create(world)
    mc.rotate_credential(
        world["svc"], old["clientId"], overlap_days=0, actor="a", now=NOW
    )
    assert mc.get_row(world["svc"].table, old["clientId"])["status"] == "revoked"


def test_rotation_never_extends_an_old_credential_that_expires_sooner(world):
    old = create(world, expires_in_days=2)
    mc.rotate_credential(
        world["svc"], old["clientId"], overlap_days=7, actor="a", now=NOW
    )
    row = mc.get_row(world["svc"].table, old["clientId"])
    assert row["expiresAt"] == "2026-11-13T12:00:00Z"


def test_revoking_twice_tolerates_what_is_already_gone(world):
    out = create(world)
    mc.revoke_credential(world["svc"], out["clientId"], actor="a", now=NOW)
    again = mc.revoke_credential(world["svc"], out["clientId"], actor="a", now=NOW)
    assert again["status"] == "revoked"


def test_the_sweep_leaves_a_never_expiring_credential_alone(world):
    out = create(world, expires_in_days=0)
    result = mc.sweep(world["svc"], now=NOW + timedelta(days=5000))
    assert result["expired"] == [] and result["warned"] == []
    assert mc.get_row(world["svc"].table, out["clientId"])["status"] == "active"


def test_removing_the_registry_keeps_a_stack_owned_client(world):
    out = create(world)
    world["svc"].table.update_item(
        Key={"clientId": out["clientId"]},
        UpdateExpression="SET kind = :k",
        ExpressionAttributeValues={":k": "stack-default"},
    )
    mc.revoke_all(world["svc"], actor="cloudformation", now=NOW)
    client = world["svc"].cognito.describe_user_pool_client(
        UserPoolId=world["pool"], ClientId=out["clientId"]
    )
    assert client["UserPoolClient"]["ClientId"] == out["clientId"]
    # The release being returned to signs in with it, so the gateway keeps it too.
    assert out["clientId"] in world["gw"].allowed()


def test_removing_the_registry_stops_deleting_before_time_runs_out(world):
    ids = [create(world, name=f"c{i}")["clientId"] for i in range(3)]
    result = mc.revoke_all(
        world["svc"], actor="cloudformation", now=NOW, seconds_left=lambda: 5.0
    )
    # The gateway and the rows are done first, so nothing is accepted any more...
    assert world["gw"].allowed() == ["user-client", "stack-client"]
    assert all(mc.get_row(world["svc"].table, c)["status"] == "revoked" for c in ids)
    # ...and the deletes left for want of time are reported, not attempted.
    assert sorted(result["unfinished"]) == sorted(ids)
    for cid in ids:
        world["svc"].cognito.describe_user_pool_client(
            UserPoolId=world["pool"], ClientId=cid
        )


def test_a_row_with_no_valid_role_is_refused():
    with pytest.raises(mcp_access.McpAccessRefused, match="no valid role"):
        mc.caller_from_row(
            {"clientId": "c", "role": "Superuser", "enabled": True}, now=NOW
        )


# --- the stack-created client -----------------------------------------------------------


def test_the_stack_client_keeps_its_access_when_isolation_is_off(world):
    row = mc.register_stack_default(
        world["svc"].table, "stack-client", secret_arn="arn"
    )
    caller = mc.caller_from_row(row, now=NOW)
    assert (row["name"], row["kind"], row["enabled"]) == (
        "Default (stack-created)",
        "stack-default",
        True,
    )
    assert (caller.roles, caller.scope, caller.write_tools) == (["Author"], None, True)


@pytest.mark.parametrize(
    "environ",
    [
        {TENANT_ISOLATION_ENV: "Strict"},
        {TENANT_GRANT_SOURCE_ENV: "IdpClaim"},
        {TENANT_GRANT_SOURCE_ENV: "Api"},
    ],
)
def test_the_stack_client_is_registered_disabled_under_tenant_isolation(world, environ):
    row = mc.register_stack_default(
        world["svc"].table, "stack-client", secret_arn="arn", environ=environ
    )
    assert row["enabled"] is False and row["status"] == "disabled"
    with pytest.raises(mcp_access.McpAccessRefused, match="disabled"):
        mc.caller_from_row(row, now=NOW)


def test_a_stack_update_never_undoes_an_admins_change(world):
    table = world["svc"].table
    mc.register_stack_default(table, "stack-client", secret_arn="arn")
    table.update_item(
        Key={"clientId": "stack-client"},
        UpdateExpression="SET enabled = :f, allowedConfigVersions = :s",
        ExpressionAttributeValues={":f": False, ":s": ["acme-*"]},
    )
    again = mc.register_stack_default(table, "stack-client", secret_arn="arn2")
    assert again["enabled"] is False and again["allowedConfigVersions"] == ["acme-*"]


def test_removing_the_stack_row_never_touches_an_admin_credential(world):
    out = create(world)
    assert mc.remove_stack_default(world["svc"].table, out["clientId"]) is False
    assert mc.get_row(world["svc"].table, out["clientId"])
    mc.register_stack_default(world["svc"].table, "stack-client", secret_arn="arn")
    assert mc.remove_stack_default(world["svc"].table, "stack-client") is True


def test_revoking_the_stack_client_keeps_its_app_client(world):
    """The stack owns that app client (MCPConnectorClient); revoking refuses it and
    takes it off the gateway, and leaves the client and its stack secret alone."""
    svc = world["svc"]
    client = svc.cognito.create_user_pool_client(
        UserPoolId=world["pool"], ClientName="mcp-connector-client", GenerateSecret=True
    )["UserPoolClient"]["ClientId"]
    world["gw"].state["authorizerConfiguration"]["customJWTAuthorizer"][
        "allowedClients"
    ].append(client)
    mc.register_stack_default(svc.table, client, secret_arn="arn")
    mc.revoke_credential(svc, client, actor="a", now=NOW)
    assert svc.cognito.describe_user_pool_client(
        UserPoolId=world["pool"], ClientId=client
    )
    assert client not in world["gw"].allowed()
    with pytest.raises(mcp_access.McpAccessRefused, match="revoked"):
        mc.caller_from_row(mc.get_row(svc.table, client), now=NOW)


# --- work finished after the request: deferred revocation, deadline-bound create ----


def _deferring(world):
    """Record what a request asks ``services.defer`` to run, without running it."""
    asked = []
    world["svc"].defer = lambda task, cid: asked.append((task, cid))
    return asked


def _row(world, cid):
    row = mc.get_row(world["svc"].table, cid)
    assert row is not None
    return row


def _client_exists(world, cid):
    try:
        world["svc"].cognito.describe_user_pool_client(
            UserPoolId=world["pool"], ClientId=cid
        )
    except world["svc"].cognito.exceptions.ResourceNotFoundException:
        return False
    return True


def _past():
    import time

    return time.monotonic() - 1


def test_a_deferred_revoke_refuses_at_once_and_touches_nothing_slow(world):
    cid = create(world)["clientId"]
    asked = _deferring(world)
    updates = world["gw"].updates
    out = mc.revoke_credential(
        world["svc"], cid, actor="a", now=NOW, defer_cleanup=True
    )
    assert out["status"] == "revoked"
    row = mc.get_row(world["svc"].table, cid)
    assert row[mc.CLEANUP_PENDING] is True
    with pytest.raises(mcp_access.McpAccessRefused, match="revoked"):
        mc.caller_from_row(row, now=NOW)
    # No gateway write, and the client and secret are still there for the task.
    assert world["gw"].updates == updates and cid in world["gw"].allowed()
    assert _client_exists(world, cid)
    assert asked == [(mc.TASK_FINISH_REVOCATION, cid)]
    # The marker is bookkeeping and never reaches a response.
    assert mc.CLEANUP_PENDING not in out


def test_a_deferred_revoke_succeeds_when_the_finishing_task_cannot_be_scheduled(
    world, caplog
):
    cid = create(world)["clientId"]

    def broken(_task, _cid):
        raise RuntimeError("Lambda invoke refused")

    world["svc"].defer = broken
    out = mc.revoke_credential(
        world["svc"], cid, actor="a", now=NOW, defer_cleanup=True
    )
    assert out["status"] == "revoked"
    assert mc.get_row(world["svc"].table, cid)[mc.CLEANUP_PENDING] is True
    assert "daily sweep will finish it" in caplog.text


def test_finishing_a_revocation_is_complete_and_idempotent(world):
    cid = create(world)["clientId"]
    _deferring(world)
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW, defer_cleanup=True)
    first = mc.finish_revocation(world["svc"], cid)
    assert first == {"clientId": cid, "complete": True, "errors": []}
    assert cid not in world["gw"].allowed() and not _client_exists(world, cid)
    assert "DeletedDate" in world["svc"].secrets.describe_secret(
        SecretId=f"stk/mcp/credentials/{cid}"
    )
    assert mc.CLEANUP_PENDING not in _row(world, cid)
    # Everything is already gone, the secret already scheduled for deletion.
    assert mc.finish_revocation(world["svc"], cid)["complete"] is True


def test_a_partly_failed_revocation_cleanup_stays_marked_until_it_finishes(world):
    cid = create(world)["clientId"]
    _deferring(world)
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW, defer_cleanup=True)
    world["gw"].fail_next_update = True
    partial = mc.finish_revocation(world["svc"], cid)
    assert partial["complete"] is False
    assert [e for e in partial["errors"] if e.startswith("gateway")]
    row = mc.get_row(world["svc"].table, cid)
    assert row[mc.CLEANUP_PENDING] is True and "UpdateGateway" in row["cleanupError"]
    # The steps that could finish did.
    assert not _client_exists(world, cid)
    again = mc.finish_revocation(world["svc"], cid)
    assert again["complete"] is True and cid not in world["gw"].allowed()
    row = _row(world, cid)
    assert mc.CLEANUP_PENDING not in row and "cleanupError" not in row


def test_finishing_a_revocation_never_touches_a_live_credential(world):
    cid = create(world)["clientId"]
    result = mc.finish_revocation(world["svc"], cid)
    assert result["skipped"] == "not revoked" and result["complete"] is False
    assert cid in world["gw"].allowed() and _client_exists(world, cid)


def test_an_inline_revoke_clears_its_own_marker(world):
    cid = create(world)["clientId"]
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW)
    assert mc.CLEANUP_PENDING not in _row(world, cid)


def test_a_create_past_the_gateway_deadline_keeps_the_credential_and_its_secret(
    world,
):
    asked = _deferring(world)
    out = create(world, gateway_deadline=_past())
    cid = out["clientId"]
    assert out["clientSecret"] and out["credential"]["status"] == "active"
    assert mc.GATEWAY_PENDING not in out["credential"]
    row = mc.get_row(world["svc"].table, cid)
    assert row[mc.GATEWAY_PENDING] is True and row["status"] == "active"
    assert _client_exists(world, cid)
    assert cid not in world["gw"].allowed()
    assert asked == [(mc.TASK_FINISH_GATEWAY_ADD, cid)]
    done = mc.finish_gateway_add(world["svc"], cid)
    assert done["complete"] is True and cid in world["gw"].allowed()
    assert mc.GATEWAY_PENDING not in _row(world, cid)


def test_a_create_whose_gateway_update_was_sent_is_finished_without_a_second_one(
    world, monkeypatch
):
    import time

    gw = world["gw"]
    real_update = gw.update_gateway
    reads = {"updating": 0}

    def slow_update(**request):
        reads["updating"] = 10**6
        return real_update(**request)

    real_get = gw.get_gateway

    def slow_get(gatewayIdentifier):
        state = real_get(gatewayIdentifier)
        if reads["updating"]:
            reads["updating"] -= 1
            state["status"] = "UPDATING"
        return state

    monkeypatch.setattr(gw, "update_gateway", slow_update)
    monkeypatch.setattr(gw, "get_gateway", slow_get)
    # time.sleep is already patched out by the fixture; wait for real, briefly.
    pause = threading.Event()
    monkeypatch.setattr(mcp_gateway.time, "sleep", lambda s: pause.wait(min(s, 0.01)))
    _deferring(world)
    out = create(world, gateway_deadline=time.monotonic() + 0.2)
    cid = out["clientId"]
    assert mc.get_row(world["svc"].table, cid)[mc.GATEWAY_PENDING] is True
    reads["updating"] = 0
    updates = gw.updates
    assert mc.finish_gateway_add(world["svc"], cid)["complete"] is True
    assert gw.updates == updates and cid in gw.allowed()


def test_a_create_that_fails_other_than_by_the_deadline_is_withdrawn(world):
    import time

    world["gw"].fail_next_update = True
    asked = _deferring(world)
    with pytest.raises(RuntimeError, match="UpdateGateway failed"):
        create(world, gateway_deadline=time.monotonic() + 600)
    assert world["svc"].table.scan()["Items"] == []
    clients = world["svc"].cognito.list_user_pool_clients(
        UserPoolId=world["pool"], MaxResults=60
    )
    assert clients["UserPoolClients"] == [] and asked == []


def test_finishing_a_gateway_add_never_admits_a_revoked_client(world):
    _deferring(world)
    cid = create(world, gateway_deadline=_past())["clientId"]
    mc.revoke_credential(world["svc"], cid, actor="a", now=NOW, defer_cleanup=True)
    assert mc.GATEWAY_PENDING not in _row(world, cid)
    result = mc.finish_gateway_add(world["svc"], cid)
    assert result["skipped"] == "revoked"
    assert cid not in world["gw"].allowed()


def test_a_revoke_landing_while_a_gateway_add_is_in_flight_wins(world):
    """The revoke's row write lands after the task read the row, and its own
    gateway removal ran before the addition landed: the task sees the revoked row
    after its addition and takes the client off again."""
    _deferring(world)
    svc = world["svc"]
    cid = create(world, gateway_deadline=_past())["clientId"]

    def revoke_meanwhile(_state):
        mc.revoke_credential(svc, cid, actor="a", now=NOW, defer_cleanup=True)

    world["gw"].concurrent = revoke_meanwhile
    result = mc.finish_gateway_add(svc, cid)
    assert result["complete"] is False
    assert result["skipped"] == "revoked while being admitted"
    assert cid not in world["gw"].allowed()


def test_the_sweep_finishes_work_requests_left_marked(world):
    asked = _deferring(world)
    svc = world["svc"]
    gone = create(world, name="gone")["clientId"]
    mc.revoke_credential(svc, gone, actor="a", now=NOW, defer_cleanup=True)
    new = create(world, name="new", gateway_deadline=_past())["clientId"]
    assert len(asked) == 2
    # The reconciliation's own gateway update fails, so whatever reaches the
    # gateway below was done by the finishing tasks.
    world["gw"].fail_next_update = True
    result = mc.sweep(svc, now=NOW, seconds_left=lambda: 250.0)
    assert result["reconciled"]["ok"] is False
    assert sorted(result["finished"]) == sorted([gone, new])
    assert result["unfinished"] == []
    assert gone not in world["gw"].allowed() and new in world["gw"].allowed()
    assert not _client_exists(world, gone)
    assert mc.CLEANUP_PENDING not in _row(world, gone)
    assert mc.GATEWAY_PENDING not in _row(world, new)
    # Nothing is left for the next day's sweep.
    assert mc.sweep(svc, now=NOW)["finished"] == []


def test_the_sweep_leaves_marked_work_when_time_runs_out(world):
    _deferring(world)
    svc = world["svc"]
    gone = create(world, name="gone")["clientId"]
    mc.revoke_credential(svc, gone, actor="a", now=NOW, defer_cleanup=True)
    asked = _deferring(world)
    result = mc.sweep(svc, now=NOW, seconds_left=lambda: 5.0)
    assert result["finished"] == []
    assert result["unfinished"] == [
        {
            "clientId": gone,
            "task": mc.TASK_FINISH_REVOCATION,
            "error": "no time; handed to a new invocation",
        }
    ]
    # Handed to a fresh invocation, and still marked in case that never runs.
    assert asked == [(mc.TASK_FINISH_REVOCATION, gone)]
    assert _client_exists(world, gone)
    assert _row(world, gone)[mc.CLEANUP_PENDING] is True
    world["svc"].defer = None
    left = mc.sweep(svc, now=NOW, seconds_left=lambda: 5.0)["unfinished"]
    assert left[0]["error"] == "no time"


def test_removing_the_registry_finishes_a_deferred_revocation(world):
    _deferring(world)
    svc = world["svc"]
    gone = create(world)["clientId"]
    mc.revoke_credential(svc, gone, actor="a", now=NOW, defer_cleanup=True)
    result = mc.revoke_all(svc, actor="cloudformation", now=NOW)
    assert result["revoked"] == [] and result["errors"] == []
    assert not _client_exists(world, gone)
    assert gone not in world["gw"].allowed()


def test_a_deferred_rotation_with_no_overlap_leaves_the_old_cleanup_to_its_task(
    world,
):
    asked = _deferring(world)
    old = create(world)["clientId"]
    new = mc.rotate_credential(
        world["svc"], old, overlap_days=0, actor="a", now=NOW, defer_cleanup=True
    )
    assert new["clientId"] in world["gw"].allowed()
    assert mc.get_row(world["svc"].table, old)["status"] == "revoked"
    assert _client_exists(world, old)
    assert asked == [(mc.TASK_FINISH_REVOCATION, old)]


def test_an_unknown_task_is_refused(world):
    with pytest.raises(ValueError, match="Unknown MCP credential task"):
        mc.run_task(world["svc"], "delete-everything", "c")


def test_a_gateway_wait_past_its_deadline_reads_nothing(world):
    gw = world["gw"]
    called = []
    gw.get_gateway = lambda gatewayIdentifier: called.append(1)
    with pytest.raises(mcp_gateway.GatewayDeadlineExceeded):
        mcp_gateway.wait_until_ready(gw, "gw-1", deadline=_past())
    assert called == []


def _slow_gateway(world, monkeypatch):
    """A gateway that stays UPDATING after every update until ``state['slow']`` is
    cleared. Sleeps really pass at a fiftieth of their length, so an unbounded
    wait (60 polls of 5 seconds) still takes seconds while a deadline is reached
    in a fraction of one."""
    gw = world["gw"]
    state = {"slow": True, "updating": False}
    real_update, real_get = gw.update_gateway, gw.get_gateway

    def update(**request):
        if state["slow"]:
            state["updating"] = True
        return real_update(**request)

    def get(gatewayIdentifier):
        out = real_get(gatewayIdentifier)
        if state["updating"] and state["slow"]:
            out["status"] = "UPDATING"
        return out

    monkeypatch.setattr(gw, "update_gateway", update)
    monkeypatch.setattr(gw, "get_gateway", get)
    pause = threading.Event()
    monkeypatch.setattr(mcp_gateway.time, "sleep", lambda s: pause.wait(s / 50))
    return state


def test_a_no_overlap_rotation_keeps_the_old_credential_until_the_new_one_works(
    world,
):
    asked = _deferring(world)
    svc = world["svc"]
    old = create(world, expires_in_days=30)["clientId"]
    new = mc.rotate_credential(
        svc,
        old,
        overlap_days=0,
        actor="a",
        now=NOW,
        gateway_deadline=_past(),
        defer_cleanup=True,
    )["clientId"]
    # The replacement is not admitted yet, so the old one still works, for at
    # most a day.
    old_row = _row(world, old)
    assert mc.status_of(old_row, NOW) == "active"
    assert old_row["expiresAt"] == "2026-11-12T12:00:00Z"
    assert old_row["replacedBy"] == new
    assert _row(world, new)[mc.REVOKE_ON_ACTIVATION] == old
    assert asked == [(mc.TASK_FINISH_GATEWAY_ADD, new)]
    assert mc.finish_gateway_add(svc, new)["complete"] is True
    assert new in world["gw"].allowed()
    assert _row(world, old)["status"] == "revoked"
    assert mc.REVOKE_ON_ACTIVATION not in _row(world, new)
    assert asked[-1] == (mc.TASK_FINISH_REVOCATION, old)


def test_the_sweep_finishes_a_held_rotation(world):
    _deferring(world)
    svc = world["svc"]
    old = create(world)["clientId"]
    new = mc.rotate_credential(
        svc, old, overlap_days=0, actor="a", now=NOW, gateway_deadline=_past()
    )["clientId"]
    result = mc.sweep(svc, now=NOW, seconds_left=lambda: 250.0)
    assert new in result["finished"]
    assert _row(world, old)["status"] == "revoked"


def test_activation_revokes_only_the_credential_it_replaced(world):
    _deferring(world)
    svc = world["svc"]
    bystander = create(world, name="bystander")["clientId"]
    new = create(world, name="new", gateway_deadline=_past())["clientId"]
    svc.table.update_item(
        Key={"clientId": new},
        UpdateExpression="SET revokeOnActivation = :o",
        ExpressionAttributeValues={":o": bystander},
    )
    assert mc.finish_gateway_add(svc, new)["complete"] is True
    assert _row(world, bystander)["status"] == "active"


def test_the_sweep_is_bounded_when_the_gateway_never_settles(world, monkeypatch):
    import time

    asked = _deferring(world)
    svc = world["svc"]
    live = create(world, name="live")["clientId"]
    gone = create(world, name="gone", expires_in_days=1)["clientId"]
    _slow_gateway(world, monkeypatch)
    started = time.monotonic()
    result = mc.sweep(
        svc,
        now=NOW + timedelta(days=2),
        seconds_left=lambda: mc.CLEANUP_RESERVE_SECONDS + 0.3,
    )
    # Without deadlines each gateway wait runs 60 polls of 5 seconds.
    assert time.monotonic() - started < 5
    assert result["expired"] == [gone]
    assert result["reconciled"]["ok"] is False
    # The expired credential is refused at once and its cleanup is left marked.
    row = _row(world, gone)
    assert row["status"] == "revoked" and row[mc.CLEANUP_PENDING] is True
    assert [u["clientId"] for u in result["unfinished"]] == [gone]
    assert _row(world, live)["status"] == "active"
    assert asked == []
