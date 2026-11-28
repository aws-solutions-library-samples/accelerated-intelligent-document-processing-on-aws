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

The fix is a read-modify-write, sending back exactly the described fields the
update's model accepts: a fixed denylist of read-only fields broke the weekly
ca-central-1 run when describe began returning ``ClientType``, which the update
rejects. These tests pin that shape without touching AWS, against the installed
botocore model.
"""

from __future__ import annotations

import importlib.util
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
    "ClientSecret": "should-not-be-sent-back-placeholder",
    "CreationDate": "2026-01-01T00:00:00Z",
    "LastModifiedDate": "2026-01-02T00:00:00Z",
    "ClientType": "PUBLIC",
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
    "RefreshTokenRotation": {"Feature": "ENABLED", "RetryGracePeriodSeconds": 10},
    "AuthSessionValidity": 5,
    "EnablePropagateAdditionalUserContextData": False,
}


class _Cognito:
    """A cognito-idp client whose describe answers ``described`` and whose update
    is validated against the real botocore model, then recorded.

    ``meta`` is a real client's, so the field filter reads the installed model,
    and every update goes through that real client under a ``Stubber``: a field
    the update does not accept raises the same ``ParamValidationError`` the live
    run did."""

    def __init__(self, described):
        import boto3

        self._real = boto3.client(
            "cognito-idp",
            region_name="us-west-2",
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="1234abcd" * 5,
        )
        self.meta = self._real.meta
        self.described = described
        self.calls = []

    def describe_user_pool_client(self, **kwargs):
        self.calls.append(("describe", kwargs))
        return {"UserPoolClient": dict(self.described)}

    def update_user_pool_client(self, **kwargs):
        from botocore.stub import Stubber

        self.calls.append(("update", kwargs))
        with Stubber(self._real) as stub:
            stub.add_response("update_user_pool_client", {"UserPoolClient": {}})
            return self._real.update_user_pool_client(**kwargs)


@pytest.fixture
def captured(monkeypatch):
    """Record the Cognito calls set_auth_flows makes, and answer the describe."""
    client = _Cognito(DESCRIBED)
    monkeypatch.setattr(rbac_common, "_cognito", lambda ctx: client)
    return client.calls


def _update_payload(calls):
    """The request of the update call."""
    for name, kwargs in calls:
        if name == "update":
            return kwargs
    raise AssertionError(f"no update call captured: {calls}")


def test_reads_the_client_before_updating(captured):
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    assert any(name == "describe" for name, _ in captured), (
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
        "RefreshTokenRotation",
        "AuthSessionValidity",
        "EnablePropagateAdditionalUserContextData",
    ],
)
def test_every_other_property_is_sent_back_unchanged(captured, field):
    """Each of these was silently cleared by the old one-flag update."""
    rbac_common.set_auth_flows(CTX, ["ALLOW_USER_SRP_AUTH"])
    payload = _update_payload(captured)
    assert field in payload, f"{field} would be cleared by this update"
    assert payload[field] == DESCRIBED[field]


@pytest.mark.parametrize(
    "field", ["ClientSecret", "CreationDate", "LastModifiedDate", "ClientType"]
)
def test_read_only_properties_are_not_sent_back(captured, field):
    """UpdateUserPoolClient rejects these, so echoing them would break the call.

    ``ClientType`` is the one a fixed denylist missed: Cognito started returning it
    from describe (first seen in ca-central-1) and the update rejected it."""
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
    client = _Cognito(minimal)
    monkeypatch.setattr(rbac_common, "_cognito", lambda ctx: client)
    calls = client.calls
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


def test_the_cognito_client_is_built_in_the_stacks_region(monkeypatch):
    import boto3

    made = []
    monkeypatch.setattr(
        boto3, "client", lambda service, **kw: made.append((service, kw)) or object()
    )
    rbac_common._cognito({"region": "ca-central-1"})
    assert made == [("cognito-idp", {"region_name": "ca-central-1"})]


class _Failing:
    def __init__(self, fail_on):
        self.fail_on = fail_on
        self._ok = _Cognito(DESCRIBED)
        self.meta = self._ok.meta

    def describe_user_pool_client(self, **kwargs):
        if self.fail_on == "describe":
            raise ValueError("boom")
        return self._ok.describe_user_pool_client(**kwargs)

    def update_user_pool_client(self, **kwargs):
        raise ValueError("boom")


@pytest.mark.parametrize(
    ("call", "fail_on", "message"),
    [
        (lambda: rbac_common.get_auth_flows(CTX), "describe", "describe-user-pool"),
        (lambda: rbac_common.set_auth_flows(CTX, []), "describe", "update-user-pool"),
        (lambda: rbac_common.set_auth_flows(CTX, []), "update", "update-user-pool"),
    ],
)
def test_a_cognito_error_reaches_the_caller_as_a_runtime_error(
    monkeypatch, call, fail_on, message
):
    """Callers catch RuntimeError, the contract aws() has, so a boto3 error must
    arrive as one, naming the operation."""
    monkeypatch.setattr(rbac_common, "_cognito", lambda ctx: _Failing(fail_on))
    with pytest.raises(RuntimeError, match=message):
        call()
