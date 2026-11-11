# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp-cli terraform ...`` — commands that apply only to Terraform deployments.

``idp-cli terraform migrate-from-genai-idp-terraform <phase>`` moves a deployment
of the separate ``awslabs/genai-idp-terraform`` implementation onto this
repository's published Terraform module in place, keeping its buckets, tables,
KMS key and Cognito user pool. Each phase is a thin call into
``IDPClient.terraform.migrate_from_genai_idp_terraform``; see
``docs/terraform-migration.md``. Machine-readable output (``--json``) goes to
stdout verbatim; everything else goes to stderr.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, NoReturn, Optional, Tuple

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

err_console = Console(stderr=True)

# The per-region default templates and the region resolver live in `idp_cli.cli`,
# which imports this module to register the group; `cli` hands them over with
# `configure` rather than this module importing `cli` back.
_TEMPLATE_URLS: Dict[str, str] = {}
_REGION_RESOLVER: Optional[Callable[[Optional[str]], str]] = None


def configure(
    template_urls: Dict[str, str], resolve_region: Callable[[Optional[str]], str]
) -> None:
    global _REGION_RESOLVER
    _TEMPLATE_URLS.clear()
    _TEMPLATE_URLS.update(template_urls)
    _REGION_RESOLVER = resolve_region


def _resolve_region(region: Optional[str]) -> str:
    if _REGION_RESOLVER is None:
        raise RuntimeError("idp_cli.terraform used before idp_cli.cli configured it")
    return _REGION_RESOLVER(region)


GROUP_HELP = "Commands for Terraform deployments of the accelerator."
MIGRATE_HELP = """Move a genai-idp-terraform deployment onto this module, in place.

For deployments made with the separate awslabs/genai-idp-terraform repository.
The new deployment adopts the old one's buckets, tables, KMS key and Cognito user
pool (users keep their passwords) and recreates everything else. Run the phases
in order: plan, check-retention, freeze, fix-config, adopt-user-pool, apply,
then (after verifying the new deployment) detach and check-destroy. Every phase
that changes something is a dry run unless given --apply. See
docs/terraform-migration.md.
"""


def _client(stack_name: str, region: Optional[str]):
    from idp_sdk import IDPClient

    return IDPClient(stack_name=stack_name, region=_resolve_region(region))


def _migration(stack_name: str, region: Optional[str]):
    return _client(stack_name, region).terraform.migrate_from_genai_idp_terraform


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]✗ {escape(message)}[/red]")
    sys.exit(code)


def _emit_json(model: Any) -> None:
    click.echo(model.model_dump_json(indent=2))


def _print_step(result, apply_flag: bool) -> None:
    """Human output for a phase result, on stderr."""
    if result.changes:
        head = (
            "Changed"
            if result.applied
            else ("Would change" if not apply_flag else "Planned")
        )
        err_console.print(f"[bold]{head}:[/bold]")
        for line in result.changes:
            err_console.print(f"  {escape(line)}")
    elif not result.findings:
        err_console.print("Nothing to change.")
    for line in result.notes:
        err_console.print(f"[dim]{escape(line)}[/dim]")
    if result.backup_path:
        err_console.print(f"Backup: {escape(result.backup_path)}")
    if result.back_out:
        err_console.print("[bold]To undo:[/bold]")
        for line in result.back_out:
            err_console.print(f"  {escape(line)}")
    for line in result.findings:
        err_console.print(f"[red]✗ {escape(line)}[/red]")
    if result.ok:
        if result.changes and not result.applied and not apply_flag:
            err_console.print(
                "[yellow]Dry run: pass --apply to make these changes.[/yellow]"
            )
        else:
            err_console.print(f"[green]✓ {escape(result.phase)}: OK[/green]")


def _finish(result, as_json: bool, apply_flag: bool = False) -> None:
    if as_json:
        _emit_json(result)
    else:
        _print_step(result, apply_flag)
    if not result.ok:
        sys.exit(1)


stack_option = click.option(
    "--stack-name",
    required=True,
    help="Name of the NEW deployment (its label stack; at most 25 characters)",
)
region_option = click.option("--region", help="AWS region")
dir_option = click.option(
    "--terraform-dir",
    help="The new deployment's root configuration directory "
    "(default: ./idp-terraform/<stack-name>)",
)
bin_option = click.option(
    "--terraform-bin",
    default="terraform",
    show_default=True,
    help="Terraform binary: terraform, tofu, or a path",
)
apply_option = click.option(
    "--apply", "apply_", is_flag=True, help="Make the changes (default is a dry run)"
)
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Print JSON to stdout"
)
accept_option = click.option(
    "--accept-expiry",
    multiple=True,
    metavar="KIND",
    help="Adopt a bucket even though the module's expiry rule would delete objects "
    "already in it (KIND is the bucket kind, e.g. logging_bucket). Repeatable.",
)


@click.group(name="terraform", help=GROUP_HELP)
def terraform():
    pass


@terraform.group(name="migrate-from-genai-idp-terraform", help=MIGRATE_HELP)
def migrate():
    pass


@migrate.command(name="plan")
@stack_option
@region_option
@click.option(
    "--old-state",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="The old deployment's terraform.tfstate, or `terraform state pull` output",
)
@click.option(
    "--admin-email",
    required=True,
    help="The new deployment's admin user. An existing user of exactly this name "
    "(case-sensitive) in the old pool is adopted.",
)
@click.option("--parameters", help="Stack parameters, as for `idp-cli deploy`")
@click.option(
    "--terraform-module-url",
    help="Terraform module zip URL (default: the latest module for the region)",
)
@dir_option
@bin_option
@click.option(
    "--terraform-assume-role-arn", help="An IAM role the aws and awscc providers assume"
)
@json_option
def plan_cmd(
    stack_name: str,
    region: Optional[str],
    old_state: str,
    admin_email: str,
    parameters: Optional[str],
    terraform_module_url: Optional[str],
    terraform_dir: Optional[str],
    terraform_bin: str,
    terraform_assume_role_arn: Optional[str],
    as_json: bool,
):
    """Map the old state to the module and write adopt.tf (changes nothing in AWS).

    Writes the new deployment's main.tf and terraform.tfvars, runs `terraform
    init`, and writes adopt.tf plus genai-idp-terraform-migration/ (the plan the
    later phases read, and the addresses to detach from the old state). Refuses an
    old resource that holds data and has no verdict.
    """
    from idp_sdk import IDPError, default_terraform_pointer_url

    from .parameters import parse_parameters

    try:
        region = _resolve_region(region)
        pointer_url = None
        if not terraform_module_url:
            if region not in _TEMPLATE_URLS:
                _fail(
                    f"Region '{region}' has no published Terraform module; pass "
                    "--terraform-module-url"
                )
            pointer_url = default_terraform_pointer_url(_TEMPLATE_URLS[region], None)
        params = parse_parameters(
            parameters,
            on_warning=lambda m: err_console.print(f"[yellow]⚠ {escape(m)}[/yellow]"),
        )
        result = _migration(stack_name, region).plan(
            old_state,
            admin_email,
            params,
            module_url=terraform_module_url,
            pointer_url=pointer_url,
            terraform_dir=terraform_dir,
            terraform_bin=terraform_bin,
            assume_role_arn=terraform_assume_role_arn,
        )
    except (IDPError, ValueError) as e:
        _fail(str(e))
    if as_json:
        _emit_json(result)
        return
    table = Table(show_header=True, header_style="bold blue")
    table.add_column("Verdict")
    table.add_column("Old address", style="cyan")
    table.add_column("New address")
    table.add_column("Evidence")
    shown = [r for r in result.resources if r.verdict != "bucket-or-table-setting"]
    for r in shown:
        if r.verdict == "recreate" and r.kind == r.old_type:
            continue
        table.add_row(
            escape(r.verdict),
            escape(r.old_address),
            escape(r.new_address or "-"),
            escape(r.evidence),
        )
    err_console.print(table)
    generic = sum(
        1 for r in result.resources if r.verdict == "recreate" and r.kind == r.old_type
    )
    settings = sum(
        1 for r in result.resources if r.verdict == "bucket-or-table-setting"
    )
    err_console.print(
        f"{result.import_count} import blocks in adopt.tf; the user pool is imported "
        f"by adopt-user-pool. {settings} settings of kept buckets and tables are "
        f"detached with them; {generic} other resources are destroyed with the old "
        "deployment and recreated by the module."
    )
    if result.parameters:
        err_console.print(
            "Parameters written to terraform.tfvars: "
            + escape(
                ", ".join(f"{k}={v}" for k, v in sorted(result.parameters.items()))
            )
        )
    for w in result.warnings:
        err_console.print(f"[yellow]⚠ {escape(w)}[/yellow]")
    for role, path in result.files.items():
        err_console.print(f"  {role}: {escape(path)}")
    err_console.print(
        "[bold]After the new deployment is verified, detach these from the old state[/bold] "
        "(the `detach` phase runs this for you):"
    )
    for line in result.detach_commands:
        err_console.print(f"  {escape(line)}")


@migrate.command(name="check-retention")
@stack_option
@region_option
@dir_option
@accept_option
@json_option
def check_retention_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    accept_expiry: Tuple[str, ...],
    as_json: bool,
):
    """Compare each adopted bucket's oldest object with the module's expiry rule."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).check_retention(
            terraform_dir, accept_expiry
        )
    except IDPError as e:
        _fail(str(e))
    if not as_json:
        table = Table(show_header=True, header_style="bold blue")
        for col in (
            "Bucket kind",
            "Bucket",
            "Expires after (days)",
            "Oldest object (days)",
            "",
        ):
            table.add_column(col)
        for row in result.details.get("buckets", []):
            table.add_row(
                escape(row["kind"]),
                escape(row["bucket"]),
                str(row["expiryDays"] if row["expiryDays"] is not None else "never"),
                str(
                    row["oldestObjectAgeDays"]
                    if row["oldestObjectAgeDays"] is not None
                    else "empty"
                ),
                "[green]ok[/green]" if row["ok"] else "[red]would expire[/red]",
            )
        err_console.print(table)
    _finish(result, as_json)


@migrate.command(name="freeze")
@stack_option
@region_option
@dir_option
@apply_option
@click.option("--wait", is_flag=True, help="Wait until no document is in flight")
@click.option("--timeout", default=1800, show_default=True, help="Seconds to wait")
@json_option
def freeze_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    apply_: bool,
    wait: bool,
    timeout: int,
    as_json: bool,
):
    """Disable the old deployment's upload rule; report documents in flight."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).freeze(
            terraform_dir, apply=apply_, wait=wait, timeout_seconds=timeout
        )
    except IDPError as e:
        _fail(str(e))
    _finish(result, as_json, apply_)


@migrate.command(name="fix-config")
@stack_option
@region_option
@dir_option
@apply_option
@click.option(
    "--restore",
    type=click.Path(exists=True, dir_okay=False),
    help="Write the rows of a backup this phase made back to the table",
)
@json_option
def fix_config_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    apply_: bool,
    restore: Optional[str],
    as_json: bool,
):
    """Back up, then remove the revision counters from the old Config# rows."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).fix_config(
            terraform_dir, apply=apply_, restore=restore
        )
    except IDPError as e:
        _fail(str(e))
    _finish(result, as_json, apply_)


@migrate.command(name="adopt-user-pool")
@stack_option
@region_option
@dir_option
@bin_option
@apply_option
@json_option
def adopt_user_pool_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    terraform_bin: str,
    apply_: bool,
    as_json: bool,
):
    """Import the user pool into the new state and normalise its schema there."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).adopt_user_pool(
            terraform_dir, terraform_bin, apply=apply_
        )
    except IDPError as e:
        _fail(str(e))
    _finish(result, as_json, apply_)


@migrate.command(name="apply")
@stack_option
@region_option
@dir_option
@bin_option
@apply_option
@accept_option
@json_option
def apply_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    terraform_bin: str,
    apply_: bool,
    accept_expiry: Tuple[str, ...],
    as_json: bool,
):
    """Plan the adoption, check it imports in place and destroys nothing, then apply."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).apply(
            terraform_dir, terraform_bin, apply=apply_, accept_expiry=accept_expiry
        )
    except IDPError as e:
        _fail(str(e))
    _finish(result, as_json, apply_)


@migrate.command(name="detach")
@stack_option
@region_option
@dir_option
@bin_option
@click.option(
    "--old-terraform-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="The old deployment's directory, where `terraform state` commands run",
)
@apply_option
@json_option
def detach_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    terraform_bin: str,
    old_terraform_dir: str,
    apply_: bool,
    as_json: bool,
):
    """Remove the adopted and kept resources from the old state (backup first)."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).detach(
            old_terraform_dir, terraform_dir, terraform_bin, apply=apply_
        )
    except IDPError as e:
        _fail(str(e))
    _finish(result, as_json, apply_)


@migrate.command(name="check-destroy")
@stack_option
@region_option
@dir_option
@click.option(
    "--destroy-plan",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="`terraform show -json` of `terraform plan -destroy -out=<file>` in the old "
    "deployment's directory",
)
@json_option
def check_destroy_cmd(
    stack_name: str,
    region: Optional[str],
    terraform_dir: Optional[str],
    destroy_plan: str,
    as_json: bool,
):
    """Check the old deployment's destroy plan deletes nothing that was kept."""
    from idp_sdk import IDPError

    try:
        result = _migration(stack_name, region).check_destroy(
            destroy_plan, terraform_dir
        )
    except IDPError as e:
        _fail(str(e))
    if not as_json:
        err_console.print(
            f"The destroy plan deletes {result.details.get('deletions', 0)} resources."
        )
    _finish(result, as_json)
