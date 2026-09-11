"""Guards `rbac_common.set_auth_flows` against clearing the UI app client.

The bug this suite exists to prevent
------------------------------------
`UpdateUserPoolClient` is a **full replace**, not a patch: any property omitted
from the request is cleared. `set_auth_flows` used to send only
`--explicit-auth-flows`, so every time the RBAC harness minted a token it silently
stripped the UI app client of:

* `CallbackURLs` / `LogoutURLs` / `AllowedOAuthFlows` — breaking the Web UI's
  hosted-UI login,
* `SupportedIdentityProviders` — breaking external IdP federation,
* `ReadAttributes` / `WriteAttributes` — including the write restriction that
  keeps end users from writing attributes the application never writes,
* the token validities and `PreventUserExistenceErrors`.

CloudFormation does not repair it: the client resource is unchanged in the
template, so the next stack update skips it and the out-of-band edit survives.
Observed on a live stack — the client came back with every one of those `null`
after a `make api-test` run.

The fix is a read-modify-write. These tests pin that shape without touching AWS.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _load_rbac_common():
    # scripts/sdlc/tests/ -> scripts/rbac_common.py
    path = Path(__file__).resolve().parents[2] / "rbac_common.py"
    spec = importlib.util.spec_from_file_location("rbac_common", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rbac_common = _load_rbac_common()

CTX = {
    "user_pool": "us-west-2_abc123",
    "client": "clientid123",
    "region": "us-west-2",
}

# What a deployed client looks like, including the properties that were lost.
DESCRIBED = {
    "UserPoolId": "us-west-2_abc123",
    "ClientName": "IDP-Client",
    "ClientId": "clientid123",
    "ClientSecret": "should-not-be-sent-back",
    "CreationDate": "2026-01-01T00:00:00Z",
    "LastModifiedDate": "2026-01-02T00:00:00Z",
    "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    "ReadAttributes": ["email", "custom:idp_groups"],
    "WriteAttributes": ["email", "given_name", "family_name", "custom:idp_groups"],
    "SupportedIdentityProviders": ["COGNITO", "MyOkta"],
    "CallbackURLs": ["https://example.cloudfront.net/"],
    "LogoutURLs": ["https://example.cloudfront.net/"],
    "AllowedOAuthFlows": ["code"],
    "AllowedOAuthScopes": ["openid", "email"],
    "AllowedOAuthFlowsUserPoolClient": True,
    "PreventUserExistenceErrors": "ENABLED",
    "EnableTokenRevocation": True,
    "AccessTokenValidity": 1,
    "IdTokenValidity": 1,
    "RefreshTokenValidity": 30,
    "TokenValidityUnits": {"AccessToken": "hours"},
}


@pytest.fixture
def captured(monkeypatch):
    """Record the aws(...) calls set_auth_flows makes, and answer the describe."""
    calls = []

    def fake_aws(*args, region=None):
        calls.append({"args": args, "region": region})
        if "describe-user-pool-client" in args:
            # Honour --query the way the CLI does: get_auth_flows asks only for
            # the flows list, set_auth_flows asks for the whole client.
            query = args[args.index("--query") + 1] if "--query" in args else ""
            if query.endswith("ExplicitAuthFlows"):
                return list(DESCRIBED["ExplicitAuthFlows"])
            return dict(DESCRIBED)
        return {}

    monkeypatch.setattr(rbac_common, "aws", fake_aws)
    return calls


def _update_payload(calls):
    """The JSON body of the update call."""
    for call in calls:
        if "update-user-pool-client" in call["args"]:
            args = call["args"]
            idx = args.index("--cli-input-json")
            return json.loads(args[idx + 1])
    raise AssertionError(f"no update call captured: {calls}")


def test_reads_the_client_before_updating(captured):
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    assert any("describe-user-pool-client" in c["args"] for c in captured), (
        "set_auth_flows must read the current client before replacing it — "
        "UpdateUserPoolClient clears anything omitted"
    )


def test_only_the_auth_flows_change(captured):
    new_flows = ["ALLOW_USER_SRP_AUTH", "ALLOW_ADMIN_USER_PASSWORD_AUTH"]
    rbac_common.set_auth_flows(CTX, new_flows)
    payload = _update_payload(captured)
    assert payload["ExplicitAuthFlows"] == new_flows


@pytest.mark.parametrize(
    "field",
    [
        "ReadAttributes",
        "WriteAttributes",
        "SupportedIdentityProviders",
        "CallbackURLs",
        "LogoutURLs",
        "AllowedOAuthFlows",
        "AllowedOAuthScopes",
        "PreventUserExistenceErrors",
        "AccessTokenValidity",
        "IdTokenValidity",
        "RefreshTokenValidity",
        "TokenValidityUnits",
        "EnableTokenRevocation",
        "AllowedOAuthFlowsUserPoolClient",
        "ClientName",
    ],
)
def test_every_other_property_is_sent_back_unchanged(captured, field):
    """Each of these was silently cleared by the old one-flag update."""
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    payload = _update_payload(captured)
    assert field in payload, f"{field} would be cleared by this update"
    assert payload[field] == DESCRIBED[field]


@pytest.mark.parametrize("field", ["ClientSecret", "CreationDate", "LastModifiedDate"])
def test_read_only_properties_are_not_sent_back(captured, field):
    """UpdateUserPoolClient rejects these, so echoing them would break the call."""
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    payload = _update_payload(captured)
    assert field not in payload


def test_identifiers_are_present(captured):
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    payload = _update_payload(captured)
    assert payload["UserPoolId"] == CTX["user_pool"]
    assert payload["ClientId"] == CTX["client"]


def test_absent_properties_are_not_sent_as_null(monkeypatch):
    """A client that genuinely has no WriteAttributes must not get an empty list.

    Sending `null`/`[]` back for something the client does not define would be a
    change, not a preservation.
    """
    minimal = {
        "UserPoolId": CTX["user_pool"],
        "ClientId": CTX["client"],
        "ClientName": "IDP-Client",
        "ExplicitAuthFlows": ["ALLOW_USER_SRP_AUTH"],
        "WriteAttributes": None,
    }
    calls = []

    def fake_aws(*args, region=None):
        calls.append({"args": args, "region": region})
        if "describe-user-pool-client" in args:
            query = args[args.index("--query") + 1] if "--query" in args else ""
            if query.endswith("ExplicitAuthFlows"):
                return list(minimal["ExplicitAuthFlows"])
            return dict(minimal)
        return {}

    monkeypatch.setattr(rbac_common, "aws", fake_aws)
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    payload = _update_payload(calls)
    assert "WriteAttributes" not in payload


def test_restore_puts_back_the_captured_flows(captured):
    """enable_admin_auth -> restore_auth_flows round-trips the original flows."""
    ctx = dict(CTX)
    rbac_common.enable_admin_auth(ctx)
    assert ctx["orig_auth_flows"] == DESCRIBED["ExplicitAuthFlows"]
    captured.clear()
    rbac_common.restore_auth_flows(ctx)
    payload = _update_payload(captured)
    assert payload["ExplicitAuthFlows"] == DESCRIBED["ExplicitAuthFlows"]
