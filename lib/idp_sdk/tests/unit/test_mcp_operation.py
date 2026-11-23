# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``IDPClient.mcp``: settings come from the stack's credential manager, and the
operations are the idp_common ones, with their refusals as SDK exceptions."""

from types import SimpleNamespace

import pytest

from idp_sdk import IDPClient
from idp_sdk.exceptions import (
    IDPResourceNotFoundError,
    IDPStackError,
    IDPValidationError,
)

pytestmark = pytest.mark.unit

ENV = {
    "MCP_CREDENTIALS_TABLE": "creds",
    "GATEWAY_ID": "gw-1",
    "USER_POOL_ID": "us-east-1_pool",
    "KMS_KEY_ID": "arn:aws:kms:us-east-1:111122223333:key/k",
    "STACK_NAME": "stk",
    "OAUTH_SCOPES": "idp-mcp-connector/access",
    "TOKEN_URL": "https://auth.example/oauth2/token",
    "GATEWAY_URL": "https://gw.example/mcp",
    "TENANT_ISOLATION_MODE": "Strict",
    "TENANT_GRANT_SOURCE": "Manual",
}


class Paginator:
    def __init__(self, resources):
        self.resources = resources

    def paginate(self, **_kw):
        return [{"StackResourceSummaries": self.resources}]


def fake_boto(resources, env):
    def boto(service):
        if service == "cloudformation":
            return SimpleNamespace(get_paginator=lambda _n: Paginator(resources))
        if service == "lambda":
            return SimpleNamespace(
                get_function_configuration=lambda FunctionName: {
                    "Environment": {"Variables": env}
                }
            )
        if service == "sts":
            return SimpleNamespace(
                get_caller_identity=lambda: {"Arn": "arn:aws:iam::1:role/ci"}
            )
        return SimpleNamespace(name=service)

    return boto


@pytest.fixture
def client(monkeypatch):
    c = IDPClient(stack_name="stk", region="us-east-1")
    resources = [
        {
            "LogicalResourceId": "McpCredentialManagerFunction",
            "PhysicalResourceId": "stk-mcp-credential-manager",
        }
    ]
    monkeypatch.setattr(c.mcp, "_boto", fake_boto(resources, dict(ENV)))
    return c


def test_settings_come_from_the_manager_functions_environment(client):
    svc = client.mcp._services(None)
    assert (svc.gateway_id, svc.user_pool_id, svc.stack_name) == (
        "gw-1",
        "us-east-1_pool",
        "stk",
    )
    assert svc.oauth_scopes == ["idp-mcp-connector/access"]
    # The stack's mode, not this machine's: validation refuses what the page would.
    assert svc.environ == {
        "TENANT_ISOLATION_MODE": "Strict",
        "TENANT_GRANT_SOURCE": "Manual",
    }


def test_a_stack_without_mcp_is_reported(client, monkeypatch):
    monkeypatch.setattr(client.mcp, "_boto", fake_boto([], dict(ENV)))
    with pytest.raises(IDPResourceNotFoundError, match="EnableMCP"):
        client.mcp._services(None)


def test_a_manager_missing_settings_is_reported(client, monkeypatch):
    resources = [
        {"LogicalResourceId": "McpCredentialManagerFunction", "PhysicalResourceId": "f"}
    ]
    monkeypatch.setattr(client.mcp, "_boto", fake_boto(resources, {}))
    with pytest.raises(IDPStackError, match="GATEWAY_ID"):
        client.mcp._services(None)


def test_create_passes_the_caller_identity_as_actor(client, monkeypatch):
    from idp_common import mcp_credentials as mc

    seen = {}

    def create(services, **kw):
        seen.update(kw)
        return {
            "credential": {"clientId": "c1", "name": "n", "role": "Viewer"},
            "clientId": "c1",
            "clientSecret": "s3cr3t-value",
            "tokenUrl": "t",
            "gatewayUrl": "g",
            "secretArn": "arn",
            "oauthScopes": ["x"],
        }

    monkeypatch.setattr(mc, "create_credential", create)
    issued = client.mcp.create_credential(
        "n", "Viewer", allowed_config_versions=["acme-*"]
    )
    assert issued.client_secret == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(issued)
    assert seen["actor"] == "arn:aws:iam::1:role/ci"
    assert seen["allowed_config_versions"] == ["acme-*"]


def test_refusals_become_sdk_exceptions(client, monkeypatch):
    from idp_common import mcp_credentials as mc

    def refuse(*_a, **_k):
        raise mc.CredentialError("Role must be one of Viewer")

    def missing(*_a, **_k):
        raise mc.CredentialNotFound("No MCP credential 'x'.")

    monkeypatch.setattr(mc, "create_credential", refuse)
    with pytest.raises(IDPValidationError, match="Role must"):
        client.mcp.create_credential("n", "Nope")
    monkeypatch.setattr(mc, "revoke_credential", missing)
    with pytest.raises(IDPResourceNotFoundError):
        client.mcp.revoke_credential("x")
