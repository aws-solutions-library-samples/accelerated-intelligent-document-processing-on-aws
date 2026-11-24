# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``idp-cli review-queues`` — the production review queues of a stack.

A configuration profile's routing rules send documents to these queues by id.
Every command is a thin call into ``IDPClient.review_queues``, which asks the
stack's review queues resolver, so a queue created here passes the same checks as
one created in the web UI's Review Management. Machine-readable output (``--json``)
goes to stdout verbatim; everything else goes to stderr.
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

stack_option = click.option(
    "--stack-name", required=True, help="CloudFormation stack name"
)
region_option = click.option("--region", help="AWS region (optional)")


def _queues(stack_name: str, region: Optional[str]):
    from idp_sdk import IDPClient

    return IDPClient(stack_name=stack_name, region=region).review_queues


def _fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(f"[red]✗ {escape(message)}[/red]")
    sys.exit(code)


def _emit_json(payload: Any) -> None:
    click.echo(
        payload
        if isinstance(payload, str)
        else json.dumps(payload, indent=2, default=str)
    )


def _load_definition(path: str) -> dict:
    """A queue definition from a YAML or JSON file."""
    # Imported here: PyYAML arrives through idp-sdk, and this module is imported
    # when `cli` is, so a module-level import would replace the CLI's own
    # missing-dependency message with a bare ImportError.
    import yaml

    try:
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        _fail(f"Could not read {path}: {e}")
    if not isinstance(loaded, dict):
        _fail(f"{path} does not hold a queue definition (a mapping with an id)")
    return loaded


def _definition(
    file: Optional[str],
    queue_id: Optional[str],
    name: Optional[str],
    description: Optional[str],
    priority: Optional[int],
    time_limit_minutes: Optional[int],
    annotators: tuple,
    checklist: tuple,
    reject_reasons: tuple,
    skip_reasons: tuple,
    enabled: Optional[bool],
) -> dict:
    """The definition a command was given: a file, with the options laid over it.

    An option that was not given changes nothing, so a file can be reused with
    one setting overridden on the command line.
    """
    definition = _load_definition(file) if file else {}
    given = {
        "id": queue_id,
        "name": name,
        "description": description,
        "priority": priority,
        "time_limit_minutes": time_limit_minutes,
        "eligible_annotators": list(annotators) or None,
        "checklist": list(checklist) or None,
        "reject_reasons": list(reject_reasons) or None,
        "skip_reasons": list(skip_reasons) or None,
        "enabled": enabled,
    }
    for field, value in given.items():
        if value is not None:
            # A file may spell a field the API's way; the option replaces either.
            definition.pop(_api_name(field), None)
            definition[field] = value
    if not definition.get("id"):
        _fail("A queue id is required: give --id, or a --file that has one")
    return definition


def _api_name(field: str) -> str:
    head, *rest = field.split("_")
    return head + "".join(part.title() for part in rest)


def _definition_options(command):
    """The options `create` and `update` share."""
    options = [
        click.option(
            "--file",
            "file",
            type=click.Path(exists=True),
            help="Queue definition as YAML or JSON; options given with it override it",
        ),
        click.option(
            "--id", "queue_id", help="Queue id (lower-case letters, digits, hyphens)"
        ),
        click.option("--name", help="Name shown to people"),
        click.option("--description", help="What the queue is for"),
        click.option("--priority", type=int, help="Higher is worked first"),
        click.option(
            "--time-limit-minutes",
            type=int,
            help="Minutes a claimed document is held for its reviewer",
        ),
        click.option(
            "--annotator",
            "annotators",
            multiple=True,
            help="Email of an Annotator who works this queue (repeat for several)",
        ),
        click.option(
            "--checklist-item",
            "checklist",
            multiple=True,
            help="Something a reviewer confirms before completing (repeat)",
        ),
        click.option(
            "--reject-reason",
            "reject_reasons",
            multiple=True,
            help="A reason a reviewer can reject a document for (repeat)",
        ),
        click.option(
            "--skip-reason",
            "skip_reasons",
            multiple=True,
            help="A reason a reviewer can skip a document for (repeat)",
        ),
        click.option(
            "--enabled/--disabled",
            "enabled",
            default=None,
            help="A disabled queue takes no new documents; they go to Default",
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _show(queue) -> None:
    err_console.print(
        f"  id: [cyan]{escape(queue.id)}[/cyan]  name: {escape(queue.name or '')}  "
        f"priority: {queue.priority}  enabled: {queue.enabled}  "
        f"version: {queue.version}"
    )


@click.group(name="review-queues")
def review_queues():
    """Production review queues: list, create, update, delete.

    A profile's routing rules name a queue by its id, and a rule that names a
    queue the stack does not have is refused when the profile is saved, so create
    the queue first. See docs/review-routing.md.
    """


@review_queues.command(name="list")
@stack_option
@region_option
@click.option(
    "--limit",
    type=int,
    default=25,
    show_default=True,
    help="Queues per page (the server allows up to 100)",
)
@click.option("--next-token", help="The next token a previous page printed")
@click.option(
    "--json", "as_json", is_flag=True, help="Print the page as JSON to stdout"
)
def list_queues(
    stack_name: str,
    region: Optional[str],
    limit: int,
    next_token: Optional[str],
    as_json: bool,
):
    """List the review queues, a page at a time."""
    from idp_sdk import IDPError

    try:
        page = _queues(stack_name, region).list(limit=limit, next_token=next_token)
    except IDPError as e:
        _fail(str(e))
    if as_json:
        _emit_json(page.model_dump_json(indent=2))
        return
    if not page.queues:
        err_console.print("[yellow]No review queues on this page[/yellow]")
        return
    table = Table(show_header=True, header_style="bold blue")
    table.add_column("Id", style="cyan")
    table.add_column("Name")
    table.add_column("Priority", justify="right")
    table.add_column("Enabled")
    table.add_column("Annotators", justify="right")
    table.add_column("Time limit (min)", justify="right")
    table.add_column("Version", justify="right")
    for queue in page.queues:
        table.add_row(
            escape(queue.id) + (" (default)" if queue.is_default_queue else ""),
            escape(queue.name or ""),
            str(queue.priority),
            "yes" if queue.enabled else "no",
            str(len(queue.eligible_annotators or [])),
            str(queue.time_limit_minutes),
            str(queue.version),
        )
    err_console.print(table)
    if page.next_token:
        err_console.print(f"More queues: --next-token {escape(page.next_token)}")


@review_queues.command(name="create")
@stack_option
@region_option
@_definition_options
@click.option(
    "--json", "as_json", is_flag=True, help="Print the queue as JSON to stdout"
)
def create_queue(stack_name: str, region: Optional[str], as_json: bool, **options):
    """Create a review queue.

    Only the id is required. A queue with no Annotator listed is worked by
    Admins only.
    """
    from idp_sdk import IDPError

    definition = _definition(**options)
    try:
        queue = _queues(stack_name, region).create(definition)
    except IDPError as e:
        _fail(str(e))
    if as_json:
        _emit_json(queue.model_dump_json(indent=2))
        return
    err_console.print("[green]✓ Created the review queue[/green]")
    _show(queue)


@review_queues.command(name="update")
@stack_option
@region_option
@_definition_options
@click.option(
    "--expected-version",
    type=int,
    help="Refuse the update if the queue's version is no longer this one",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the queue as JSON to stdout"
)
def update_queue(
    stack_name: str,
    region: Optional[str],
    expected_version: Optional[int],
    as_json: bool,
    **options,
):
    """Replace a review queue's definition.

    The definition replaces the stored one: a setting left out goes back to its
    default, except whether the queue is enabled and its skip reasons, which are
    kept. To change one setting, write the whole definition to a file
    (review-queues list --json shows it) and give the file with the change.
    """
    from idp_sdk import IDPError

    definition = _definition(**options)
    try:
        queue = _queues(stack_name, region).update(
            definition, expected_version=expected_version
        )
    except IDPError as e:
        _fail(str(e))
    if as_json:
        _emit_json(queue.model_dump_json(indent=2))
        return
    err_console.print("[green]✓ Updated the review queue[/green]")
    _show(queue)


@review_queues.command(name="delete")
@stack_option
@region_option
@click.option("--id", "queue_id", required=True, help="The queue's id")
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
def delete_queue(stack_name: str, region: Optional[str], queue_id: str, yes: bool):
    """Delete a review queue; the documents in it move to the Default queue.

    Routing rules that name the queue are not changed: documents they send there
    go to the Default queue from then on.
    """
    from idp_sdk import IDPError

    if not yes and not click.confirm(
        f"Delete the review queue '{queue_id}' and move its documents to the "
        "Default queue?",
        default=False,
        err=True,
    ):
        err_console.print("[yellow]Nothing deleted[/yellow]")
        return
    try:
        moved = _queues(stack_name, region).delete(queue_id)
    except IDPError as e:
        _fail(str(e))
    err_console.print(
        f"[green]✓ Deleted the review queue '{escape(queue_id)}'[/green]; "
        f"{moved} document(s) moved to the Default queue"
    )
