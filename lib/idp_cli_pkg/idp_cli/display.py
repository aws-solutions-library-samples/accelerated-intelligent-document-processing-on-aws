# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Display Module

Rich UI components for displaying batch processing progress and status.
"""

import json
from typing import Dict

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

console = Console()


def create_progress_bar(status_data: Dict) -> Progress:
    """
    Create overall progress bar

    Args:
        status_data: Status data from progress monitor

    Returns:
        Progress object with task
    """
    total = status_data["total"]
    completed = len(status_data["completed"]) + len(status_data["failed"])

    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    progress.add_task("Overall Progress", total=total, completed=completed)

    return progress


def create_status_table(status_data: Dict) -> Table:
    """
    Create status summary table

    Args:
        status_data: Status data from progress monitor

    Returns:
        Rich Table object
    """
    table = Table(title="Status Summary", show_header=True, header_style="bold cyan")
    table.add_column("Status", style="cyan", width=15)
    table.add_column("Count", justify="right", width=10)
    table.add_column("Percentage", justify="right", width=12)

    total = status_data["total"]
    completed = len(status_data["completed"])
    running = len(status_data["running"])
    queued = len(status_data["queued"])
    failed = len(status_data["failed"])

    if total == 0:
        return table

    table.add_row(
        "✓ Completed", str(completed), f"{completed / total * 100:.1f}%", style="green"
    )
    table.add_row(
        "⟳ Running", str(running), f"{running / total * 100:.1f}%", style="yellow"
    )
    table.add_row("⏸ Queued", str(queued), f"{queued / total * 100:.1f}%")
    table.add_row("✗ Failed", str(failed), f"{failed / total * 100:.1f}%", style="red")

    return table


def create_recent_completions_table(status_data: Dict, limit: int = 5) -> Table:
    """
    Create table of recent completions

    Args:
        status_data: Status data from progress monitor
        limit: Maximum number of completions to show

    Returns:
        Rich Table object
    """
    table = Table(
        title="Recent Completions", show_header=True, header_style="bold green"
    )
    table.add_column("Document ID", style="cyan", width=60)
    table.add_column("Status", width=10)
    table.add_column("Duration", justify="right", width=12)

    completed = status_data.get("completed", [])

    # Sort by end_time (most recent first). The key is coerced to `str` so the
    # comparison has a total order whatever a producer of `status_data` put under
    # the key: a batch mixing a `datetime` with the `""` substituted for an absent
    # end time raised `'<' not supported between instances of 'datetime.datetime'
    # and 'str'` here, and since both callers catch broadly the user saw
    # `idp-cli status` exit 1 with that message instead of the table. The
    # `_batch_status_to_display_dicts` mapper now normalises to ISO 8601 strings, so
    # this coercion is a no-op on that path; it is here so that this function cannot
    # be made to raise by a caller that does not.
    sorted_completed = sorted(
        completed, key=lambda x: str(x.get("end_time", "") or ""), reverse=True
    )[:limit]

    for doc in sorted_completed:
        duration = doc.get("duration", 0)
        doc_id = doc.get("document_id", "unknown")

        # Truncate if still too long for display
        if len(doc_id) > 58:
            doc_id = doc_id[:55] + "..."

        table.add_row(doc_id, "✓ Success", f"{duration:.1f}s", style="green")

    if not sorted_completed:
        table.add_row("No completions yet", "", "", style="dim")

    return table


def create_failures_table(status_data: Dict) -> Table:
    """
    Create table of failed documents

    Args:
        status_data: Status data from progress monitor

    Returns:
        Rich Table object
    """
    table = Table(title="Failed Documents", show_header=True, header_style="bold red")
    table.add_column("Document ID", style="cyan", width=60)
    table.add_column("Error", width=60)

    failed = status_data.get("failed", [])

    for doc in failed:
        doc_id = doc.get("document_id", "unknown")
        error = doc.get("error", "Unknown error")

        # Truncate if still too long
        if len(doc_id) > 58:
            doc_id = doc_id[:55] + "..."
        if len(error) > 58:
            error = error[:55] + "..."

        table.add_row(doc_id, error, style="red")

    if not failed:
        table.add_row("No failures", "", style="dim")

    return table


def create_statistics_panel(stats: Dict) -> Panel:
    """
    Create statistics panel

    Args:
        stats: Statistics dictionary from progress monitor

    Returns:
        Rich Panel object
    """
    content = f"""[bold]Total Documents:[/bold] {stats["total"]}
[green]Completed:[/green] {stats["completed"]}
[red]Failed:[/red] {stats["failed"]}
[yellow]Running:[/yellow] {stats["running"]}
Queued: {stats["queued"]}

[bold]Completion:[/bold] {stats["completion_percentage"]:.1f}%
[bold]Success Rate:[/bold] {stats["success_rate"]:.1f}%
[bold]Avg Duration:[/bold] {stats["avg_duration_seconds"]:.1f}s"""

    return Panel(content, title="Statistics", border_style="blue")


def display_status_table(status_data: Dict):
    """
    Display status as a single table (non-live mode)

    Args:
        status_data: Status data from progress monitor
    """
    console.print()
    console.print(create_status_table(status_data))
    console.print()

    # Show failures if any
    if status_data.get("failed"):
        console.print(create_failures_table(status_data))
        console.print()


def show_final_summary(status_data: Dict, stats: Dict, elapsed_time: float):
    """
    Show final summary when all documents complete

    Args:
        status_data: Status data from progress monitor
        stats: Statistics dictionary
        elapsed_time: Total elapsed time in seconds
    """
    console.print()
    console.rule("[bold green]Batch Processing Complete", style="green")
    console.print()

    # Summary table
    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Metric", style="cyan bold")
    summary_table.add_column("Value", justify="right")

    summary_table.add_row("Total Documents", str(stats["total"]))
    summary_table.add_row(
        "Completed Successfully", str(stats["completed"]), style="green"
    )
    summary_table.add_row("Failed", str(stats["failed"]), style="red")
    summary_table.add_row("Success Rate", f"{stats['success_rate']:.1f}%")
    summary_table.add_row("Average Duration", f"{stats['avg_duration_seconds']:.1f}s")
    summary_table.add_row("Total Time", f"{elapsed_time:.1f}s")

    console.print(Panel(summary_table, title="Summary", border_style="green"))
    console.print()

    # Show failed documents if any
    if status_data.get("failed"):
        console.print("[bold red]Failed Documents:[/bold red]")
        for doc in status_data["failed"]:
            doc_id = doc.get("document_id", "unknown")
            error = doc.get("error", "Unknown error")
            console.print(f"  • {doc_id}: {error}", style="red")
        console.print()


def show_batch_submission_summary(results: Dict):
    """
    Show summary after batch submission

    Args:
        results: Results dictionary from batch processor
    """
    console.print()

    if results["uploaded"] > 0:
        console.print(
            f"✓ Uploaded {results['uploaded']} documents to InputBucket", style="green"
        )

    if results["queued"] > 0:
        console.print(
            f"✓ Sent {results['queued']} messages to processing queue", style="green"
        )

    if results["failed"] > 0:
        console.print(f"✗ Failed to process {results['failed']} documents", style="red")

    console.print()
    console.print(f"Batch ID: [bold cyan]{results['batch_id']}[/bold cyan]")
    console.print()


def show_monitoring_instructions(stack_name: str, batch_id: str):
    """
    Show instructions for monitoring progress

    Args:
        stack_name: CloudFormation stack name
        batch_id: Batch identifier
    """
    console.print("To monitor progress:")
    console.print(
        f"  [cyan]idp-cli status --stack-name {stack_name} --batch-id {batch_id}[/cyan]"
    )
    console.print()
    console.print("Or to wait for completion:")
    console.print(
        f"  [cyan]idp-cli status --stack-name {stack_name} --batch-id {batch_id} --wait[/cyan]"
    )
    console.print()


def show_monitoring_header(batch_id: str):
    """
    Show monitoring session header

    Args:
        batch_id: Batch identifier
    """
    console.print()
    console.rule(f"[bold blue]Monitoring Batch: {batch_id}", style="blue")
    console.print()
    console.print(
        "[dim]Press Ctrl+C to stop monitoring (processing will continue)[/dim]"
    )
    console.print()


def create_live_display(
    batch_id: str, status_data: Dict, stats: Dict, elapsed_time: float
) -> Table:
    """
    Create complete live display layout

    Args:
        batch_id: Batch identifier
        status_data: Status data from progress monitor
        stats: Statistics dictionary
        elapsed_time: Elapsed time in seconds

    Returns:
        Rich Table containing the full display
    """
    # Create main layout table
    layout = Table.grid(padding=1)
    layout.add_column(justify="left")

    # Header
    header = Text(f"Monitoring Batch: {batch_id}", style="bold blue")
    layout.add_row(Panel(header, border_style="blue"))

    # Progress bar
    total = stats["total"]
    completed = len(status_data["completed"]) + len(status_data["failed"])
    progress_text = f"Overall Progress: {completed}/{total} ({stats['completion_percentage']:.1f}%) • Elapsed: {elapsed_time:.0f}s"
    layout.add_row(Panel(progress_text, border_style="green"))

    # Status summary
    layout.add_row(create_status_table(status_data))

    # Recent completions (stacked vertically for more horizontal space)
    layout.add_row(create_recent_completions_table(status_data))

    # Failed documents (stacked below completions)
    layout.add_row(create_failures_table(status_data))

    # Footer
    if not stats["all_complete"]:
        footer = "[dim]Press Ctrl+C to stop monitoring (processing continues in background)[/dim]"
        layout.add_row(Panel(footer, border_style="dim"))

    return layout


def format_status_json(status_data: Dict, stats: Dict) -> str:
    """
    Format status data as JSON for programmatic use

    Args:
        status_data: Status data from progress monitor
        stats: Statistics dictionary

    Returns:
        JSON string
    """
    # For single document, return simplified format
    if stats["total"] == 1:
        doc = None
        if status_data["completed"]:
            doc = status_data["completed"][0]
        elif status_data["running"]:
            doc = status_data["running"][0]
        elif status_data["failed"]:
            doc = status_data["failed"][0]
        elif status_data["queued"]:
            doc = status_data["queued"][0]

        if doc:
            result = {
                "document_id": doc.get("document_id"),
                "status": doc.get("status"),
                "duration": doc.get("duration", 0),
                "start_time": doc.get("start_time", ""),
                "end_time": doc.get("end_time", ""),
            }

            # Add status-specific fields
            if doc.get("status") == "COMPLETED":
                result["num_sections"] = doc.get("num_sections", 0)
            elif doc.get("status") in ["FAILED", "ABORTED"]:
                result["error"] = doc.get("error", "Unknown error")
                result["failed_step"] = doc.get("failed_step", "Unknown")
            elif doc.get("status") in [
                "RUNNING",
                "CLASSIFYING",
                "EXTRACTING",
                "ASSESSING",
                "RULE_VALIDATION",
                "RULE_VALIDATION_ORCHESTRATOR",
                "SUMMARIZING",
                "HITL_IN_PROGRESS",
                "EVALUATING",
            ]:
                result["current_step"] = doc.get("current_step", doc.get("status"))

            # Determine exit code
            if doc.get("status") == "COMPLETED":
                result["exit_code"] = 0
            elif doc.get("status") in ["FAILED", "ABORTED"]:
                result["exit_code"] = 1
            else:
                result["exit_code"] = 2

            return json.dumps(result, indent=2)

        # A single-document lookup that found nothing must not fall through to the
        # batch summary. It did, and the summary derives its code from
        # `all_complete`, so `status --document-id <typo> --format json` answered
        # `exit_code: 0` — nothing was measured and the caller was told the document
        # succeeded (#1230). `show_final_status_summary` has no such fall-through and
        # answers UNKNOWN / 2 for the same input; 2 is this CLI's code for "outcome
        # not established", so the two paths now agree.
        return json.dumps(
            {
                "document_id": None,
                "status": "UNKNOWN",
                "error": "No document found matching the search criteria",
                "exit_code": 2,
            },
            indent=2,
        )

    # For batch, return full summary
    result = {
        "total": stats["total"],
        "completed": stats["completed"],
        "failed": stats["failed"],
        "running": stats["running"],
        "queued": stats["queued"],
        "completion_percentage": stats["completion_percentage"],
        "success_rate": stats["success_rate"],
        "avg_duration_seconds": stats["avg_duration_seconds"],
        "all_complete": stats["all_complete"],
    }

    # Determine exit code for batch
    if stats["all_complete"]:
        if stats["failed"] > 0:
            result["exit_code"] = 1  # Some failures
        else:
            result["exit_code"] = 0  # All succeeded
    else:
        result["exit_code"] = 2  # Still processing

    return json.dumps(result, indent=2)


def derive_exit_code(status_data: Dict, stats: Dict) -> int:
    """The exit code a status result implies. Derives; prints nothing.

    0 every document completed, 1 at least one failed, 2 the outcome is not
    established (still running, or nothing was found).

    Split out of `show_final_status_summary` so that a caller can have the code
    without the "FINAL STATUS:" line. `_monitor_progress` is that caller: it needs
    the code for `status --wait` to exit on, and it has already printed a summary
    panel of its own — and two of its three callers discard the code, so printing
    "Exit Code: 1" from there would state an exit code that contradicts `$?` for
    `process --monitor` and `rerun --monitor`.

    The rule lives here once. Two implementations of it is how the polled and waited
    forms of `status` came to disagree in the first place (#1230).

    """
    if stats["total"] == 1:
        if status_data["completed"]:
            return 0
        if status_data["failed"]:
            return 1
        return 2

    if stats["all_complete"]:
        return 1 if stats["failed"] > 0 else 0
    return 2


def show_final_status_summary(status_data: Dict, stats: Dict) -> int:
    """
    Show final status summary for programmatic use and return exit code

    The code comes from `derive_exit_code`, so this function's printing and the
    code a caller acts on cannot drift apart.

    Args:
        status_data: Status data from progress monitor
        stats: Statistics dictionary

    Returns:
        Exit code (0=success, 1=failure, 2=still processing)
    """
    # The code is derived once, above, so that the line printed here and the value a
    # caller exits on cannot drift apart.
    exit_code = derive_exit_code(status_data, stats)

    # For single document
    if stats["total"] == 1:
        doc = None
        if status_data["completed"]:
            doc = status_data["completed"][0]
            status = "COMPLETED"
        elif status_data["failed"]:
            doc = status_data["failed"][0]
            status = "FAILED"
        else:
            # Running or queued
            if status_data["running"]:
                doc = status_data["running"][0]
            elif status_data["queued"]:
                doc = status_data["queued"][0]
            status = doc.get("status", "UNKNOWN") if doc else "UNKNOWN"

        duration = doc.get("duration", 0) if doc else 0
        console.print()
        console.print(
            f"FINAL STATUS: {status} | Duration: {duration:.1f}s | Exit Code: {exit_code}"
        )
        return exit_code

    # For batch
    if stats["all_complete"]:
        if stats["failed"] > 0:
            status = f"COMPLETED WITH FAILURES ({stats['failed']} failed)"
        else:
            status = "ALL COMPLETED"
    else:
        finished = stats["completed"] + stats["failed"]
        status = f"IN PROGRESS ({finished}/{stats['total']} finished)"

    console.print()
    console.print(
        f"FINAL STATUS: {status} | Total: {stats['total']} | Success Rate: {stats['success_rate']:.1f}% | Exit Code: {exit_code}"
    )
    return exit_code
