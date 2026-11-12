# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``idp-cli mcp credentials``: thin calls into the SDK, payload on stdout only."""

import json

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk.models.mcp import McpCredential, McpCredentialIssued

pytestmark = pytest.mark.unit


class FakeMcp:
    def __init__(self):
        self.calls = []

    def create_credential(self, name, role, **kw):
        self.calls.append(("create", name, role, kw))
        return McpCredentialIssued(
            credential=McpCredential(client_id="c1", name=name, role=role),
            client_id="c1",
            client_secret="s3cr3t-value",
            token_url="https://auth.example/oauth2/token",
            secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:x",
        )

    def list_credentials(self):
        return [McpCredential(client_id="c1", name="n", role="Viewer")]

    def revoke_credential(self, client_id):
        self.calls.append(("revoke", client_id))
        return McpCredential(client_id=client_id, status="revoked")


@pytest.fixture
def fake(monkeypatch):
    import idp_cli.mcp as mod

    holder = FakeMcp()
    monkeypatch.setattr(
        mod, "_client", lambda stack, region: type("C", (), {"mcp": holder})()
    )
    return holder


def test_create_prints_the_secret_once_as_json_on_stdout(fake):
    r = CliRunner().invoke(
        cli,
        [
            "mcp",
            "credentials",
            "create",
            "--stack-name",
            "s",
            "--name",
            "nightly",
            "--role",
            "Viewer",
            "--scope",
            "acme-*",
            "--scope",
            "acme-eu",
        ],
    )
    assert r.exit_code == 0, r.output
    payload = json.loads(r.stdout)
    assert payload["clientSecret"] == "s3cr3t-value"
    assert "shown once" in r.stderr
    assert fake.calls[0][3]["allowed_config_versions"] == ["acme-*", "acme-eu"]


def test_list_json_carries_no_secret(fake):
    r = CliRunner().invoke(
        cli, ["mcp", "credentials", "list", "--stack-name", "s", "--json"]
    )
    assert r.exit_code == 0
    assert json.loads(r.stdout)[0]["client_id"] == "c1"
    assert "secret" not in r.stdout.lower().replace("secret_arn", "")


def test_revoke_asks_unless_yes(fake):
    r = CliRunner().invoke(
        cli,
        ["mcp", "credentials", "revoke", "--stack-name", "s", "--client-id", "c1"],
        input="n\n",
    )
    assert r.exit_code == 1 and fake.calls == []
    r = CliRunner().invoke(
        cli,
        [
            "mcp",
            "credentials",
            "revoke",
            "--stack-name",
            "s",
            "--client-id",
            "c1",
            "--yes",
        ],
    )
    assert r.exit_code == 0 and fake.calls == [("revoke", "c1")]
