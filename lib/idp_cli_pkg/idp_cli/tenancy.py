# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp-cli tenancy`` — tenant declarations, access policies, audit and live check.

Every command is a thin call into ``IDPClient.tenancy``; the rules themselves are in
``idp_common.config.tenants``. Machine-readable output (``--json``, and a template
written to ``-``) goes to stdout verbatim; everything else goes to stderr.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn, Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

err_console = Console(stderr=True)

# Exit codes for `verify`, one per outcome, so a script can tell "isolation is
# broken" from "the check could not run" from "nothing was checked".
VERIFY_EXIT = {"PASS": 0, "FAIL": 1, "ERROR": 2, "SKIP": 3}  # nosec B105 - outcome labels, not a credential


def _client(stack_name: str, region: Optional[str]):
    from idp_sdk import IDPClient

    return IDPClient(stack_name=stack_name, region=region)


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]{escape(message)}[/red]")
    sys.exit(code)


def _emit_json(payload: Any) -> None:
    click.echo(
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )


def _load_yaml(path: str) -> Any:
    # Imported here: PyYAML arrives through idp-sdk, and this module is imported
    # when `cli` is, so a module-level import would replace the CLI's own
    # missing-dependency message with a bare ImportError.
    import yaml

    try:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _fail(f"Could not read {path}: {e}")


stack_option = click.option(
    "--stack-name", required=True, help="CloudFormation stack name"
)
region_option = click.option("--region", help="AWS region (optional)")


@click.group(name="tenancy")
def tenancy():
    """Tenant isolation: declarations, S3 access policies, audit, live check.

    A tenant is a set of Input-bucket prefixes plus a Configuration Profile
    pattern. Declaring one makes the stack refuse prefix mappings and user scopes
    that cross it; TenantIsolationMode=Strict also refuses such documents at ingest
    and denies unscoped non-admin users. See docs/multi-tenancy.md.
    """


@tenancy.command(name="list")
@stack_option
@region_option
@click.option("--json", "as_json", is_flag=True, help="Print JSON to stdout")
def list_tenants(stack_name: str, region: Optional[str], as_json: bool):
    """List declared tenants and the stack's isolation mode."""
    from idp_sdk import IDPError

    try:
        result = _client(stack_name, region).tenancy.list()
    except IDPError as e:
        _fail(str(e))
    if as_json:
        _emit_json(result.model_dump_json(indent=2))
        return
    err_console.print(
        f"TenantIsolationMode: [bold]{escape(result.isolation_mode)}[/bold]"
    )
    if not result.tenants:
        err_console.print("[yellow]No tenants declared[/yellow]")
        return
    table = Table(show_header=True, header_style="bold blue")
    table.add_column("Tenant", style="cyan")
    table.add_column("Prefixes")
    table.add_column("Profiles")
    table.add_column("Description", style="green")
    for t in result.tenants:
        table.add_row(
            escape(t.tenant_id),
            escape("\n".join(t.prefixes)),
            escape(t.profile_pattern),
            escape(t.description or ""),
        )
    err_console.print(table)


@tenancy.command(name="put")
@stack_option
@region_option
@click.option("--tenant-id", required=True, help="Lowercase tenant identifier")
@click.option(
    "--prefix",
    "prefixes",
    multiple=True,
    required=True,
    help="Input-bucket prefix the tenant owns, ending in '/'. Repeatable.",
)
@click.option("--profile-pattern", required=True, help="Profile pattern, e.g. 'acme-*'")
@click.option("--description", help="Optional description")
def put_tenant(
    stack_name: str,
    region: Optional[str],
    tenant_id: str,
    prefixes: tuple,
    profile_pattern: str,
    description: Optional[str],
):
    """Create or replace one tenant declaration."""
    from idp_sdk import IDPError

    try:
        info = _client(stack_name, region).tenancy.put(
            tenant_id, list(prefixes), profile_pattern, description=description
        )
    except IDPError as e:
        _fail(str(e))
    err_console.print(
        f"[green]Saved tenant {escape(info.tenant_id)}: "
        f"{escape(', '.join(info.prefixes))} -> {escape(info.profile_pattern)}[/green]"
    )


@tenancy.command(name="delete")
@stack_option
@region_option
@click.option("--tenant-id", required=True)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def delete_tenant(stack_name: str, region: Optional[str], tenant_id: str, yes: bool):
    """Remove one tenant declaration (documents and profiles are untouched)."""
    from idp_sdk import IDPError

    if not yes:
        click.confirm(
            f"Delete tenant {tenant_id!r}? Its prefixes stop being a boundary.",
            abort=True,
            err=True,
        )
    try:
        removed = _client(stack_name, region).tenancy.delete(tenant_id)
    except IDPError as e:
        _fail(str(e))
    if not removed:
        _fail(f"No tenant {tenant_id!r} is declared.")
    err_console.print(f"[green]Deleted tenant {escape(tenant_id)}[/green]")


@tenancy.command(name="apply")
@stack_option
@region_option
@click.option(
    "--file",
    "path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML: tenants: [{tenantId, prefixes, profilePattern, description}]",
)
@click.option("--prune", is_flag=True, help="Delete tenants the file does not declare")
@click.option("--yes", is_flag=True, help="Write the changes (default is a dry run)")
@click.option(
    "--json", "as_json", is_flag=True, help="Print the plan as JSON to stdout"
)
def apply_tenants(
    stack_name: str,
    region: Optional[str],
    path: str,
    prune: bool,
    yes: bool,
    as_json: bool,
):
    """Make the stack's tenants match a declarations file. Dry run unless --yes."""
    from idp_sdk import IDPError

    raw = _load_yaml(path)
    declarations = raw.get("tenants") if isinstance(raw, dict) else raw
    if not isinstance(declarations, list):
        _fail(f"{path}: expected a 'tenants:' list.")
    try:
        result = _client(stack_name, region).tenancy.apply(
            declarations, prune=prune, dry_run=not yes
        )
    except IDPError as e:
        _fail(str(e))
    if as_json:
        _emit_json(result.model_dump_json(indent=2))
    else:
        for item in result.plan:
            line = f"{item.action.value:>9}  {item.tenant_id}"
            if item.error:
                err_console.print(
                    f"[red]{escape(line)}  REFUSED: {escape(item.error)}[/red]"
                )
            else:
                err_console.print(escape(line))
    errors = [i for i in result.plan if i.error]
    if errors:
        _fail(f"{len(errors)} refusal(s); nothing was written.")
    if result.applied:
        err_console.print("[green]Applied.[/green]")
    else:
        err_console.print(
            "[yellow]Dry run — re-run with --yes to write these changes.[/yellow]"
        )


@tenancy.command(name="generate-policies")
@stack_option
@region_option
@click.option(
    "--principals",
    "principals_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="YAML: tenantId -> {uploaders: [...], consumers: [...], kmsKeyArn: ...}",
)
@click.option("--output", default="-", help="Template file to write, or '-' for stdout")
def generate_policies(
    stack_name: str, region: Optional[str], principals_path: str, output: str
):
    """Write a CloudFormation template of per-tenant S3 access points."""
    from idp_sdk import IDPError

    principals = _load_yaml(principals_path) or {}
    try:
        result = _client(stack_name, region).tenancy.generate_policies(principals)
    except IDPError as e:
        _fail(str(e))
    if output == "-":
        click.echo(result.template, nl=False)
    else:
        Path(output).write_text(result.template, encoding="utf-8")
        err_console.print(
            f"[green]Wrote {escape(output)} ({len(result.access_points)} access points)[/green]"
        )
    for warning in result.warnings:
        err_console.print(f"[yellow]{escape(warning)}[/yellow]")


@tenancy.command(name="audit")
@stack_option
@region_option
@click.option(
    "--principals",
    "principals_path",
    type=click.Path(exists=True, dir_okay=False),
    help="Also compare deployed access point policies with this principals file",
)
@click.option(
    "--sample", default=500, show_default=True, help="Tracked documents to examine"
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the report as JSON to stdout"
)
def audit(
    stack_name: str,
    region: Optional[str],
    principals_path: Optional[str],
    sample: int,
    as_json: bool,
):
    """Report what weakens tenant isolation. Exit 1 when there are findings."""
    from idp_sdk import IDPError

    principals = _load_yaml(principals_path) if principals_path else None
    try:
        result = _client(stack_name, region).tenancy.audit(
            principals=principals, document_sample=sample
        )
    except IDPError as e:
        _fail(str(e), code=2)
    if as_json:
        _emit_json(result.model_dump_json(indent=2))
    else:
        err_console.print(
            f"TenantIsolationMode: [bold]{escape(result.isolation_mode)}[/bold]   "
            f"tenants: {result.tenant_count}"
        )
        for f in result.findings:
            err_console.print(
                f"[red]{escape(f.kind)}[/red]  {escape(f.subject)}\n    {escape(f.detail)}"
            )
        for note in result.notes:
            err_console.print(f"[dim]{escape(note)}[/dim]")
        if not result.findings:
            err_console.print("[green]No findings.[/green]")
    sys.exit(1 if result.findings else 0)


@tenancy.command(name="verify")
@stack_option
@region_option
@click.option(
    "--document",
    "document_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="A document to upload under each tenant's prefix (e.g. samples/lending_package.pdf)",
)
@click.option(
    "--tenant",
    "tenant_ids",
    multiple=True,
    help="Tenant to check (give two). Defaults to the first two declared.",
)
@click.option(
    "--timeout", default=240, show_default=True, help="Seconds to wait for ingest"
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the results as JSON to stdout"
)
def verify(
    stack_name: str,
    region: Optional[str],
    document_path: str,
    tenant_ids: tuple,
    timeout: int,
    as_json: bool,
):
    """Live cross-tenant check against a deployed stack.

    Creates a throwaway Admin and one scoped Viewer per tenant, uploads a document
    under each tenant's prefix, checks each Viewer reads only its own tenant's
    document, and (in Strict mode) that cross-tenant uploads are refused. Removes
    everything it created. Exit 0 PASS, 1 FAIL, 2 ERROR, 3 SKIP.
    """
    from idp_sdk import IDPError

    document = Path(document_path).read_bytes()
    try:
        result = _client(stack_name, region).tenancy.verify(
            document,
            Path(document_path).name,
            tenant_ids=list(tenant_ids) or None,
            timeout_seconds=timeout,
        )
    except IDPError as e:
        _fail(str(e), code=2)
    outcome = result.outcome.value
    if as_json:
        payload = json.loads(result.model_dump_json())
        payload["outcome"] = outcome
        _emit_json(payload)
    else:
        colour = {"PASS": "green", "FAIL": "red", "ERROR": "red", "SKIP": "yellow"}  # nosec B105 - outcome labels
        for c in result.checks:
            err_console.print(
                f"[{colour[c.outcome.value]}]{c.outcome.value:>5}[/{colour[c.outcome.value]}]  "
                f"{escape(c.name)}  [dim]{escape(c.detail)}[/dim]"
            )
        err_console.print(
            f"\nOverall: [{colour[outcome]}]{outcome}[/{colour[outcome]}]"
        )
    sys.exit(VERIFY_EXIT[outcome])
