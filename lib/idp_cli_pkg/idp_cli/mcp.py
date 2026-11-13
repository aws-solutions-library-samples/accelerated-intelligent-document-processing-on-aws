# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp-cli mcp credentials`` — MCP service credentials.

Every command is a thin call into ``IDPClient.mcp``; the rules are in
``idp_common.mcp_credentials``. ``create`` and ``rotate`` print the new client's
secret once, as JSON on stdout, and say on stderr that it will not be shown again;
it is also stored in Secrets Manager under the ARN the output names.
"""

from __future__ import annotations

import json
import sys
from typing import Any, NoReturn, Optional, Tuple

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

err_console = Console(stderr=True)


def _client(stack_name: str, region: Optional[str]):
    from idp_sdk import IDPClient

    return IDPClient(stack_name=stack_name, region=region)


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]{escape(message)}[/red]")
    sys.exit(code)


def _emit(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


def _run(fn):
    from idp_sdk.exceptions import IDPError

    try:
        return fn()
    except IDPError as e:
        _fail(str(e))


stack_option = click.option(
    "--stack-name", required=True, help="CloudFormation stack name"
)
region_option = click.option("--region", help="AWS region (optional)")


@click.group()
def mcp():
    """MCP server administration."""


@mcp.group()
def credentials():
    """MCP service credentials (client-credentials apps with a role and scope)."""


@credentials.command(name="list")
@stack_option
@region_option
@click.option("--json", "as_json", is_flag=True, help="Print JSON to stdout")
def list_credentials(stack_name: str, region: Optional[str], as_json: bool):
    """List credentials. Secrets are never listed."""
    items = _run(lambda: _client(stack_name, region).mcp.list_credentials())
    if as_json:
        _emit([i.model_dump() for i in items])
        return
    table = Table(title="MCP credentials")
    for col in ("Client ID", "Name", "Role", "Scope", "Write", "Status", "Expires"):
        table.add_column(col)
    for i in items:
        table.add_row(
            i.client_id,
            i.name or "",
            i.role or "",
            ", ".join(i.allowed_config_versions) or "(all)",
            "yes" if i.write_tools else "no",
            i.status or "",
            i.expires_at or "never",
        )
    err_console.print(table)


def _issued_payload(issued) -> dict:
    return {
        "clientId": issued.client_id,
        "clientSecret": issued.client_secret,
        "tokenUrl": issued.token_url,
        "gatewayUrl": issued.gateway_url,
        "secretArn": issued.secret_arn,
        "oauthScopes": issued.oauth_scopes,
        "credential": issued.credential.model_dump(),
    }


@credentials.command(name="create")
@stack_option
@region_option
@click.option("--name", required=True, help="A name for the credential")
@click.option(
    "--role",
    required=True,
    type=click.Choice(["Viewer", "Reviewer", "Author", "Admin"]),
    help="The role its callers act with",
)
@click.option(
    "--scope",
    "scope",
    multiple=True,
    help="A Configuration Profile name or pattern it may see; repeat for several",
)
@click.option("--write-tools", is_flag=True, help="Allow process and reprocess")
@click.option(
    "--expires-in-days",
    type=int,
    default=None,
    help="Days until it expires (default 90; 0 for never)",
)
def create_credential(
    stack_name: str,
    region: Optional[str],
    name: str,
    role: str,
    scope: Tuple[str, ...],
    write_tools: bool,
    expires_in_days: Optional[int],
):
    """Create a credential and print its secret once."""
    issued = _run(
        lambda: _client(stack_name, region).mcp.create_credential(
            name,
            role,
            allowed_config_versions=list(scope) or None,
            write_tools=write_tools,
            expires_in_days=expires_in_days,
        )
    )
    err_console.print(
        "[yellow]The client secret below is shown once. It is also stored in "
        "Secrets Manager under secretArn.[/yellow]"
    )
    _emit(_issued_payload(issued))


@credentials.command(name="update")
@stack_option
@region_option
@click.option("--client-id", required=True)
@click.option("--role", type=click.Choice(["Viewer", "Reviewer", "Author", "Admin"]))
@click.option("--scope", "scope", multiple=True, help="Replace the scope")
@click.option("--clear-scope", is_flag=True, help="Remove the scope")
@click.option("--write-tools/--no-write-tools", default=None)
@click.option("--enable/--disable", "enabled", default=None)
def update_credential(
    stack_name: str,
    region: Optional[str],
    client_id: str,
    role: Optional[str],
    scope: Tuple[str, ...],
    clear_scope: bool,
    write_tools: Optional[bool],
    enabled: Optional[bool],
):
    """Change a credential's role, scope, write tools or enabled flag."""
    item = _run(
        lambda: _client(stack_name, region).mcp.update_credential(
            client_id,
            role=role,
            allowed_config_versions=list(scope) or None,
            clear_scope=clear_scope,
            write_tools=write_tools,
            enabled=enabled,
        )
    )
    _emit(item.model_dump())


@credentials.command(name="rotate")
@stack_option
@region_option
@click.option("--client-id", required=True)
@click.option(
    "--overlap-days",
    type=click.IntRange(0, 7),
    default=7,
    show_default=True,
    help="Days the old credential keeps working",
)
def rotate_credential(
    stack_name: str, region: Optional[str], client_id: str, overlap_days: int
):
    """Issue a replacement and print its secret once."""
    issued = _run(
        lambda: _client(stack_name, region).mcp.rotate_credential(
            client_id, overlap_days=overlap_days
        )
    )
    err_console.print(
        "[yellow]The new client secret below is shown once. The old credential "
        f"stops working in {overlap_days} day(s).[/yellow]"
    )
    _emit(_issued_payload(issued))


@credentials.command(name="revoke")
@stack_option
@region_option
@click.option("--client-id", required=True)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def revoke_credential(
    stack_name: str, region: Optional[str], client_id: str, yes: bool
):
    """Revoke a credential. Its callers are refused on their next request."""
    if not yes and not click.confirm(f"Revoke {client_id}?", err=True):
        _fail("Not revoked.", code=1)
    item = _run(lambda: _client(stack_name, region).mcp.revoke_credential(client_id))
    _emit(item.model_dump())


@credentials.command(name="set-tenant")
@stack_option
@region_option
@click.option("--client-id", required=True)
@click.option("--tenant-id", help="The tenant to grant; omit to grant none")
def set_credential_tenant(
    stack_name: str, region: Optional[str], client_id: str, tenant_id: Optional[str]
):
    """Grant a credential one tenant (TenantGrantSource=Api only)."""
    item = _run(
        lambda: _client(stack_name, region).mcp.set_credential_tenant(
            client_id, tenant_id
        )
    )
    _emit(item.model_dump())
