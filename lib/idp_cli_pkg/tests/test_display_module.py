# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for `idp_cli.display`, the module that renders batch/document status.

Most of this module is Rich rendering, but two functions carry decisions a caller
acts on rather than reads. `format_status_json` chooses the process exit code that
`idp-cli status --format json` exits with, and `show_final_status_summary` chooses
the exit code the default (table) output exits with. A wrong code there is the
difference between a CI pipeline passing and failing on a document that failed, so
the exit code of every single-document and batch shape is asserted here, and the
two functions are additionally compared against each other on the same input --
they compute the same answer twice, by different rules, and they do not always
agree. Where they disagree the current behaviour is pinned and the test says so.

Two rendering conventions are used deliberately.

For anything whose value is the text an operator reads, the returned Rich object
(or the module console's output) is rendered through a 200-column non-terminal
`Console` and the assertion is on the resulting text -- that is what a reader
actually sees, including the style markup being resolved away.

For truncation, ordering and row counts the assertion is on the table's cell
values instead, because rendering cannot answer those questions: a 60-column
table column pads short cells and wraps long ones, so a rendered row cannot
distinguish an id `display.py` truncated to 55 characters plus an ellipsis from
one Rich wrapped for the column. The cells are where `display.py`'s own decision
is visible.

`display.py` has its own module-level `console`, which the autouse fixture in
`conftest.py` does not pin (that one covers `idp_cli.cli`), so it is pinned here.
"""

import json

import pytest
from rich.console import Console, RenderableType

from idp_cli import display

# The nine statuses `format_status_json` treats as in-progress, i.e. the ones for
# which it emits `current_step`. `idp_cli.cli._batch_status_to_display_dicts`
# carries the same list as the definition of its "running" bucket.
IN_PROGRESS_STATUSES = (
    "RUNNING",
    "CLASSIFYING",
    "EXTRACTING",
    "ASSESSING",
    "RULE_VALIDATION",
    "RULE_VALIDATION_ORCHESTRATOR",
    "SUMMARIZING",
    "HITL_IN_PROGRESS",
    "EVALUATING",
)


@pytest.fixture(autouse=True)
def pinned_display_console(monkeypatch):
    """
    Pin `display.console` to a 200-column non-terminal console.

    Without this the module console detects the environment: no tty means Rich
    falls back to 80 columns and ellipsizes the wider table columns, and
    `FORCE_COLOR=1` (or a pty, as some CI runners provide) puts ANSI escapes
    between a label and its value. Either one makes a content assertion fail for
    a reason that has nothing to do with the code under test.
    """
    console = Console(width=200, force_terminal=False)
    monkeypatch.setattr(display, "console", console)
    return console


def _render(renderable: RenderableType) -> str:
    """Render a Rich object to plain text the way an operator would see it."""
    out = Console(width=200, force_terminal=False)
    with out.capture() as capture:
        out.print(renderable)
    return capture.get()


def _cells(table, column: int) -> list[str]:
    """The raw cell values of one table column, before Rich lays them out."""
    return [str(cell) for cell in table.columns[column]._cells]


def _doc(**overrides) -> dict:
    """A document status dict shaped like the ones the status commands build."""
    doc = {
        "document_id": "batch-1/invoice.pdf",
        "status": "COMPLETED",
        "duration": 12.5,
        "start_time": "2025-01-10T10:00:00Z",
        "end_time": "2025-01-10T10:05:00Z",
    }
    doc.update(overrides)
    return doc


def _status_data(total=0, completed=(), running=(), queued=(), failed=()) -> dict:
    return {
        "total": total,
        "completed": list(completed),
        "running": list(running),
        "queued": list(queued),
        "failed": list(failed),
    }


def _stats(
    total=0,
    completed=0,
    failed=0,
    running=0,
    queued=0,
    completion_percentage=0.0,
    success_rate=0.0,
    avg_duration_seconds=0.0,
    all_complete=False,
) -> dict:
    return {
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "queued": queued,
        "completion_percentage": completion_percentage,
        "success_rate": success_rate,
        "avg_duration_seconds": avg_duration_seconds,
        "all_complete": all_complete,
    }


# ---------------------------------------------------------------------------
# create_progress_bar
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_progress_bar_counts_both_terminal_buckets_as_finished():
    """
    "Finished" is completed plus failed, not completed alone.

    A bar that only counts successes never reaches 100% on a batch with a failure,
    so the caller's live display sits at less than full forever while nothing is
    still running.
    """
    status_data = _status_data(
        total=10, completed=[_doc()] * 3, failed=[_doc()] * 2, running=[_doc()] * 5
    )

    progress = display.create_progress_bar(status_data)

    task = progress.tasks[0]
    assert task.description == "Overall Progress"
    assert task.total == 10
    assert task.completed == 5


@pytest.mark.unit
def test_progress_bar_on_an_empty_batch_has_no_work_and_does_not_divide():
    """A zero-document batch must produce a bar, not a ZeroDivisionError."""
    progress = display.create_progress_bar(_status_data(total=0))

    task = progress.tasks[0]
    assert task.total == 0
    assert task.completed == 0


@pytest.mark.unit
def test_progress_bar_renders_through_the_module_console(pinned_display_console):
    """The bar is wired to the module console, which is what the caller prints to."""
    progress = display.create_progress_bar(_status_data(total=4, completed=[_doc()]))

    assert progress.console is pinned_display_console


# ---------------------------------------------------------------------------
# create_status_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_status_table_percentages_are_shares_of_the_total():
    status_data = _status_data(
        total=8,
        completed=[_doc()] * 4,
        running=[_doc()] * 2,
        queued=[_doc()],
        failed=[_doc()],
    )

    table = display.create_status_table(status_data)

    assert _cells(table, 0) == [
        "✓ Completed",
        "⟳ Running",
        "⏸ Queued",
        "✗ Failed",
    ]
    assert _cells(table, 1) == ["4", "2", "1", "1"]
    assert _cells(table, 2) == ["50.0%", "25.0%", "12.5%", "12.5%"]


@pytest.mark.unit
def test_status_table_is_empty_when_the_total_is_zero():
    """
    The early return on `total == 0` is what keeps the percentage division safe.

    An empty batch is a real state -- `status --batch-id` on a batch whose
    documents have not been recorded yet -- so this must render an empty table
    rather than raise ZeroDivisionError out of the command.
    """
    table = display.create_status_table(_status_data(total=0))

    assert table.row_count == 0
    assert len(table.columns) == 3
    assert "Status Summary" in _render(table)


@pytest.mark.unit
def test_status_table_renders_the_counts_an_operator_reads():
    status_data = _status_data(total=2, completed=[_doc()], failed=[_doc()])

    rendered = _render(display.create_status_table(status_data))

    assert "Status Summary" in rendered
    assert "Completed" in rendered
    assert "50.0%" in rendered


# ---------------------------------------------------------------------------
# create_recent_completions_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recent_completions_are_newest_first_and_capped_at_the_limit():
    status_data = _status_data(
        total=3,
        completed=[
            _doc(document_id="doc1", end_time="2025-01-10T10:00:00Z"),
            _doc(document_id="doc2", end_time="2025-01-10T10:05:00Z"),
            _doc(document_id="doc3", end_time="2025-01-10T10:03:00Z"),
        ],
    )

    table = display.create_recent_completions_table(status_data, limit=2)

    assert _cells(table, 0) == ["doc2", "doc3"]


@pytest.mark.unit
def test_recent_completions_defaults_to_five_rows():
    status_data = _status_data(
        total=7,
        completed=[
            _doc(document_id=f"doc{i}", end_time=f"2025-01-10T10:0{i}:00Z")
            for i in range(7)
        ],
    )

    table = display.create_recent_completions_table(status_data)

    assert table.row_count == 5
    assert _cells(table, 0) == ["doc6", "doc5", "doc4", "doc3", "doc2"]


@pytest.mark.unit
def test_recent_completions_truncates_an_over_long_document_id():
    """
    Document ids are S3 keys and can be far wider than the 60-column column.

    The cut is asserted on the cell rather than the rendered row because Rich
    would wrap an untruncated id across two lines, which reads as a pass.
    """
    long_id = "batch-1/" + "x" * 90
    status_data = _status_data(total=1, completed=[_doc(document_id=long_id)])

    table = display.create_recent_completions_table(status_data)

    (cell,) = _cells(table, 0)
    assert cell == long_id[:55] + "..."
    assert len(cell) == 58


@pytest.mark.unit
@pytest.mark.parametrize("length", [57, 58])
def test_recent_completions_keeps_an_id_at_or_below_the_threshold_intact(length):
    """58 characters is the boundary: `> 58` truncates, so 58 itself survives."""
    doc_id = "d" * length
    table = display.create_recent_completions_table(
        _status_data(total=1, completed=[_doc(document_id=doc_id)])
    )

    assert _cells(table, 0) == [doc_id]


@pytest.mark.unit
def test_recent_completions_shows_a_placeholder_row_when_there_are_none():
    """
    An empty table with only a header reads as a broken display, so a row says so.

    The `completed` key is absent here as well as empty, which is the `.get`
    default path -- a caller that built the dict from a partial lookup.
    """
    table = display.create_recent_completions_table({"total": 0})

    assert _cells(table, 0) == ["No completions yet"]
    assert "No completions yet" in _render(table)


@pytest.mark.unit
def test_recent_completions_falls_back_when_a_document_has_no_id_or_duration():
    table = display.create_recent_completions_table(
        _status_data(total=1, completed=[{"end_time": "2025-01-10T10:00:00Z"}])
    )

    assert _cells(table, 0) == ["unknown"]
    assert _cells(table, 1) == ["✓ Success"]
    assert _cells(table, 2) == ["0.0s"]


@pytest.mark.unit
def test_recent_completions_renders_the_duration_to_one_decimal():
    table = display.create_recent_completions_table(
        _status_data(total=1, completed=[_doc(duration=123.456)])
    )

    assert _cells(table, 2) == ["123.5s"]


# ---------------------------------------------------------------------------
# create_failures_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failures_table_lists_every_failure_with_its_error():
    status_data = _status_data(
        total=2,
        failed=[
            _doc(document_id="doc1", status="FAILED", error="Classification timeout"),
            _doc(document_id="doc2", status="FAILED", error="Invalid format"),
        ],
    )

    table = display.create_failures_table(status_data)

    assert _cells(table, 0) == ["doc1", "doc2"]
    assert _cells(table, 1) == ["Classification timeout", "Invalid format"]
    rendered = _render(table)
    assert "Failed Documents" in rendered
    assert "Classification timeout" in rendered


@pytest.mark.unit
def test_failures_table_truncates_both_the_id_and_the_error():
    """A Bedrock or Textract error message routinely runs past 58 characters."""
    long_id = "y" * 70
    long_error = "z" * 70
    table = display.create_failures_table(
        _status_data(total=1, failed=[_doc(document_id=long_id, error=long_error)])
    )

    assert _cells(table, 0) == [long_id[:55] + "..."]
    assert _cells(table, 1) == [long_error[:55] + "..."]


@pytest.mark.unit
def test_failures_table_shows_a_placeholder_row_when_nothing_failed():
    table = display.create_failures_table({"total": 3})

    assert _cells(table, 0) == ["No failures"]
    assert "No failures" in _render(table)


@pytest.mark.unit
def test_failures_table_falls_back_when_a_failure_carries_no_detail():
    """
    A failure with no error text must still say something.

    `_batch_status_to_display_dicts` sets `error` to `""` when the tracking record
    has none, and an empty string is falsy but is not missing, so this covers the
    absent-key path where the default text is used.
    """
    table = display.create_failures_table(_status_data(total=1, failed=[{}]))

    assert _cells(table, 0) == ["unknown"]
    assert _cells(table, 1) == ["Unknown error"]


# ---------------------------------------------------------------------------
# create_statistics_panel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_statistics_panel_reports_every_metric_it_is_given():
    stats = _stats(
        total=10,
        completed=6,
        failed=2,
        running=1,
        queued=1,
        completion_percentage=80.0,
        success_rate=75.0,
        avg_duration_seconds=42.55,
    )

    rendered = _render(display.create_statistics_panel(stats))

    assert "Statistics" in rendered
    assert "Total Documents: 10" in rendered
    assert "Completed: 6" in rendered
    assert "Failed: 2" in rendered
    assert "Running: 1" in rendered
    assert "Queued: 1" in rendered
    assert "Completion: 80.0%" in rendered
    assert "Success Rate: 75.0%" in rendered
    assert "Avg Duration: 42.5s" in rendered


# ---------------------------------------------------------------------------
# display_status_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_display_status_table_adds_the_failures_table_only_when_something_failed(
    pinned_display_console,
):
    with pinned_display_console.capture() as capture:
        display.display_status_table(
            _status_data(total=2, completed=[_doc()], failed=[_doc(error="boom")])
        )
    with_failures = capture.get()

    with pinned_display_console.capture() as capture:
        display.display_status_table(_status_data(total=1, completed=[_doc()]))
    without_failures = capture.get()

    assert "Failed Documents" in with_failures
    assert "boom" in with_failures
    assert "Status Summary" in without_failures
    assert "Failed Documents" not in without_failures


# ---------------------------------------------------------------------------
# show_final_summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_final_summary_reports_the_totals_and_the_wall_clock(pinned_display_console):
    stats = _stats(
        total=10, completed=8, failed=2, success_rate=80.0, avg_duration_seconds=30.25
    )

    with pinned_display_console.capture() as capture:
        display.show_final_summary(_status_data(total=10), stats, elapsed_time=95.4)
    rendered = capture.get()

    assert "Batch Processing Complete" in rendered
    assert "Total Documents" in rendered
    assert "Completed Successfully" in rendered
    assert "Success Rate" in rendered
    assert "80.0%" in rendered
    assert "30.2s" in rendered
    assert "95.4s" in rendered


@pytest.mark.unit
def test_final_summary_names_each_failed_document_and_its_error(pinned_display_console):
    status_data = _status_data(
        total=2,
        completed=[_doc()],
        failed=[_doc(document_id="doc-bad", status="FAILED", error="OCR timeout")],
    )

    with pinned_display_console.capture() as capture:
        display.show_final_summary(status_data, _stats(total=2), elapsed_time=1.0)
    rendered = capture.get()

    assert "Failed Documents:" in rendered
    assert "• doc-bad: OCR timeout" in rendered


@pytest.mark.unit
def test_final_summary_omits_the_failure_list_when_nothing_failed(
    pinned_display_console,
):
    with pinned_display_console.capture() as capture:
        display.show_final_summary(
            _status_data(total=1, completed=[_doc()]), _stats(total=1), elapsed_time=1.0
        )

    assert "Failed Documents:" not in capture.get()


@pytest.mark.unit
def test_final_summary_falls_back_for_a_failure_with_no_detail(pinned_display_console):
    with pinned_display_console.capture() as capture:
        display.show_final_summary(
            _status_data(total=1, failed=[{}]), _stats(total=1), elapsed_time=1.0
        )

    assert "• unknown: Unknown error" in capture.get()


# ---------------------------------------------------------------------------
# show_batch_submission_summary / monitoring headers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_batch_submission_summary_reports_each_non_zero_count(pinned_display_console):
    results = {"uploaded": 3, "queued": 3, "failed": 1, "batch_id": "batch-abc"}

    with pinned_display_console.capture() as capture:
        display.show_batch_submission_summary(results)
    rendered = capture.get()

    assert "✓ Uploaded 3 documents to InputBucket" in rendered
    assert "✓ Sent 3 messages to processing queue" in rendered
    assert "✗ Failed to process 1 documents" in rendered
    assert "Batch ID: batch-abc" in rendered


@pytest.mark.unit
def test_batch_submission_summary_says_only_the_batch_id_when_nothing_happened(
    pinned_display_console,
):
    """
    Every count is conditional, so a no-op submission prints no claims at all.

    A summary that said "Uploaded 0 documents" and "Failed to process 0
    documents" would report a failure that did not happen.
    """
    results = {"uploaded": 0, "queued": 0, "failed": 0, "batch_id": "batch-empty"}

    with pinned_display_console.capture() as capture:
        display.show_batch_submission_summary(results)
    rendered = capture.get()

    assert "Uploaded" not in rendered
    assert "Sent" not in rendered
    assert "Failed" not in rendered
    assert "Batch ID: batch-empty" in rendered


@pytest.mark.unit
def test_monitoring_instructions_print_commands_that_can_be_pasted(
    pinned_display_console,
):
    with pinned_display_console.capture() as capture:
        display.show_monitoring_instructions("my-stack", "batch-9")
    rendered = capture.get()

    assert "idp-cli status --stack-name my-stack --batch-id batch-9" in rendered
    assert "idp-cli status --stack-name my-stack --batch-id batch-9 --wait" in rendered


@pytest.mark.unit
def test_monitoring_header_names_the_batch_and_how_to_stop(pinned_display_console):
    with pinned_display_console.capture() as capture:
        display.show_monitoring_header("batch-9")
    rendered = capture.get()

    assert "Monitoring Batch: batch-9" in rendered
    assert "Press Ctrl+C to stop monitoring" in rendered


# ---------------------------------------------------------------------------
# create_live_display
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_live_display_stacks_every_panel_of_the_monitoring_view():
    status_data = _status_data(
        total=10,
        completed=[_doc(document_id="doc-ok")] * 3,
        failed=[_doc(document_id="doc-bad", status="FAILED", error="OCR timeout")] * 2,
        running=[_doc()] * 5,
    )
    stats = _stats(
        total=10, completed=3, failed=2, running=5, completion_percentage=50.0
    )

    rendered = _render(display.create_live_display("batch-9", status_data, stats, 12.4))

    assert "Monitoring Batch: batch-9" in rendered
    assert "Overall Progress: 5/10 (50.0%) • Elapsed: 12s" in rendered
    assert "Status Summary" in rendered
    assert "Recent Completions" in rendered
    assert "doc-ok" in rendered
    assert "Failed Documents" in rendered
    assert "doc-bad" in rendered
    assert "Press Ctrl+C to stop monitoring" in rendered


@pytest.mark.unit
def test_live_display_drops_the_ctrl_c_footer_once_everything_is_complete():
    """
    The footer is advice to a user who can still wait, so it goes when nothing can
    change. `all_complete` is the only thing that decides it.
    """
    status_data = _status_data(total=2, completed=[_doc()] * 2)
    stats = _stats(total=2, completed=2, completion_percentage=100.0, all_complete=True)

    rendered = _render(display.create_live_display("batch-9", status_data, stats, 5.0))

    assert "Overall Progress: 2/2 (100.0%)" in rendered
    assert "Press Ctrl+C" not in rendered


@pytest.mark.unit
def test_live_display_counts_against_stats_total_while_the_table_divides_by_status_total():
    """
    The two totals in the live view come from two different dictionaries.

    The progress line uses `stats["total"]`; the status table's percentages divide
    by `status_data["total"]`. `_batch_status_to_display_dicts` sets both from the
    same source so they agree in production, and this pins that the rendering does
    not reconcile them -- a future caller that computes the two separately would
    get a view that contradicts itself rather than an error.
    """
    status_data = _status_data(total=4, completed=[_doc()] * 2)
    stats = _stats(total=10, completed=2, completion_percentage=20.0)

    rendered = _render(display.create_live_display("batch-9", status_data, stats, 1.0))

    assert "Overall Progress: 2/10 (20.0%)" in rendered
    assert "50.0%" in rendered  # 2 of status_data's 4, not of stats' 10


# ---------------------------------------------------------------------------
# format_status_json -- single document
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_single_completed_document_exits_zero_and_carries_its_section_count():
    doc = _doc(status="COMPLETED", num_sections=3)
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, completed=[doc]), _stats(total=1, completed=1)
        )
    )

    assert payload == {
        "document_id": "batch-1/invoice.pdf",
        "status": "COMPLETED",
        "duration": 12.5,
        "start_time": "2025-01-10T10:00:00Z",
        "end_time": "2025-01-10T10:05:00Z",
        "num_sections": 3,
        "exit_code": 0,
    }


@pytest.mark.unit
def test_json_single_completed_document_defaults_its_section_count_to_zero():
    doc = _doc(status="COMPLETED")
    doc.pop("duration")
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, completed=[doc]), _stats(total=1, completed=1)
        )
    )

    assert payload["num_sections"] == 0
    assert payload["duration"] == 0


@pytest.mark.unit
@pytest.mark.parametrize("status", ["FAILED", "ABORTED"])
def test_json_a_terminal_failure_exits_one_and_carries_the_error(status):
    """
    Exit 1 is the whole point of `--format json` for a CI caller.

    ABORTED is included because it is terminal: a caller that retried on anything
    other than 1 would loop forever on a document a human already stopped.
    """
    doc = _doc(status=status, error="Textract throttled", failed_step="OCR")
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, failed=[doc]), _stats(total=1, failed=1)
        )
    )

    assert payload["status"] == status
    assert payload["error"] == "Textract throttled"
    assert payload["failed_step"] == "OCR"
    assert payload["exit_code"] == 1
    assert "num_sections" not in payload
    assert "current_step" not in payload


@pytest.mark.unit
def test_json_a_failure_with_no_recorded_detail_still_names_a_step():
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, failed=[_doc(status="FAILED")]),
            _stats(total=1, failed=1),
        )
    )

    assert payload["error"] == "Unknown error"
    assert payload["failed_step"] == "Unknown"
    assert payload["exit_code"] == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", IN_PROGRESS_STATUSES)
def test_json_an_in_progress_status_exits_two_and_reports_the_current_step(status):
    doc = _doc(status=status, current_step="Extraction")
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, running=[doc]), _stats(total=1, running=1)
        )
    )

    assert payload["current_step"] == "Extraction"
    assert payload["exit_code"] == 2
    assert "error" not in payload
    assert "num_sections" not in payload


@pytest.mark.unit
def test_json_an_in_progress_document_with_no_step_repeats_its_status():
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, running=[_doc(status="SUMMARIZING")]),
            _stats(total=1, running=1),
        )
    )

    assert payload["current_step"] == "SUMMARIZING"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["QUEUED", "NOT_FOUND", "REDACTED_SUPERSEDED"])
def test_json_a_status_outside_all_three_lists_exits_two_with_no_extra_field(status):
    """
    The three status lists are not exhaustive, and the fall-through answer is 2.

    QUEUED is the ordinary case. NOT_FOUND and REDACTED_SUPERSEDED are the two
    terminal statuses `idp_sdk`'s progress monitor produces that none of the three
    lists names, so a caller polling for a non-2 exit code never gets one for
    them; that is pinned rather than endorsed.
    """
    doc = _doc(status=status)
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1, queued=[doc]), _stats(total=1, queued=1)
        )
    )

    assert payload["status"] == status
    assert payload["exit_code"] == 2
    assert "num_sections" not in payload
    assert "error" not in payload
    assert "current_step" not in payload


@pytest.mark.unit
def test_json_bucket_precedence_is_completed_then_running_then_failed_then_queued():
    """
    Only one bucket is read, so which one is checked first decides the answer.

    A document appears in exactly one bucket in production; this asserts the order
    directly rather than inferring it, because the order here differs from the one
    `show_final_status_summary` uses and that difference is what the agreement
    tests below are about.
    """
    everywhere = _status_data(
        total=1,
        completed=[_doc(document_id="from-completed", status="COMPLETED")],
        running=[_doc(document_id="from-running", status="RUNNING")],
        failed=[_doc(document_id="from-failed", status="FAILED")],
        queued=[_doc(document_id="from-queued", status="QUEUED")],
    )
    stats = _stats(total=1)

    def picked(status_data):
        return json.loads(display.format_status_json(status_data, stats))["document_id"]

    assert picked(everywhere) == "from-completed"
    everywhere["completed"] = []
    assert picked(everywhere) == "from-running"
    everywhere["running"] = []
    assert picked(everywhere) == "from-failed"
    everywhere["failed"] = []
    assert picked(everywhere) == "from-queued"


@pytest.mark.unit
def test_json_a_single_document_with_no_bucket_entry_reports_an_unknown_outcome():
    """
    The guarantee (#1230): `total == 1` with four empty buckets no longer falls
    through to the batch summary and answers 0.

    `format_status_json` takes its single-document branch only if one of the four
    buckets holds something. With `stats["total"] == 1`, `all_complete` true and no
    failures -- the shape a batch record whose document lookup returned nothing
    produces -- the caller used to be handed `exit_code: 0` and a payload with no
    `document_id` in it, so a CI job reading the exit code concluded the document
    succeeded. It now answers 2, which is this module's code for an outcome that was
    not established, and names the document as `None` rather than omitting it.

    The batch keys are asserted *absent*: the point is that the batch summary is not
    what comes back, and a payload carrying `total` would mean the fall-through is
    still there with a patched code.
    """
    payload = json.loads(
        display.format_status_json(
            _status_data(total=1), _stats(total=1, all_complete=True)
        )
    )

    assert payload["exit_code"] == 2
    assert payload["document_id"] is None
    assert payload["status"] == "UNKNOWN"
    assert "total" not in payload
    assert "all_complete" not in payload


# ---------------------------------------------------------------------------
# format_status_json -- batch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_json_batch_that_finished_cleanly_exits_zero():
    stats = _stats(
        total=4,
        completed=4,
        completion_percentage=100.0,
        success_rate=100.0,
        avg_duration_seconds=11.0,
        all_complete=True,
    )
    payload = json.loads(
        display.format_status_json(_status_data(total=4, completed=[_doc()] * 4), stats)
    )

    assert payload == {
        "total": 4,
        "completed": 4,
        "failed": 0,
        "running": 0,
        "queued": 0,
        "completion_percentage": 100.0,
        "success_rate": 100.0,
        "avg_duration_seconds": 11.0,
        "all_complete": True,
        "exit_code": 0,
    }


@pytest.mark.unit
def test_json_batch_that_finished_with_any_failure_exits_one():
    stats = _stats(total=4, completed=3, failed=1, success_rate=75.0, all_complete=True)
    payload = json.loads(display.format_status_json(_status_data(total=4), stats))

    assert payload["exit_code"] == 1


@pytest.mark.unit
def test_json_batch_still_running_exits_two_even_with_a_failure_already_recorded():
    """
    2 must win over 1 while work remains, or a caller stops polling too early and
    reports a partial result as the final one.
    """
    stats = _stats(total=4, completed=1, failed=1, running=2, all_complete=False)
    payload = json.loads(display.format_status_json(_status_data(total=4), stats))

    assert payload["exit_code"] == 2


@pytest.mark.unit
def test_json_is_pretty_printed_so_a_human_can_read_the_piped_output():
    out = display.format_status_json(
        _status_data(total=2), _stats(total=2, all_complete=True)
    )

    assert out.startswith("{\n  ")
    assert json.loads(out)["exit_code"] == 0


# ---------------------------------------------------------------------------
# show_final_status_summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_summary_single_completed_document_returns_zero(pinned_display_console):
    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(
            _status_data(total=1, completed=[_doc(duration=7.25)]),
            _stats(total=1, completed=1, all_complete=True),
        )

    assert code == 0
    assert "FINAL STATUS: COMPLETED | Duration: 7.2s | Exit Code: 0" in capture.get()


@pytest.mark.unit
def test_summary_single_failed_document_returns_one(pinned_display_console):
    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(
            _status_data(total=1, failed=[_doc(status="FAILED", duration=3.0)]),
            _stats(total=1, failed=1, all_complete=True),
        )

    assert code == 1
    assert "FINAL STATUS: FAILED | Duration: 3.0s | Exit Code: 1" in capture.get()


@pytest.mark.unit
@pytest.mark.parametrize("bucket", ["running", "queued"])
def test_summary_an_unfinished_document_returns_two_and_prints_its_own_status(
    bucket, pinned_display_console
):
    status_data = _status_data(total=1)
    status_data[bucket] = [_doc(status="EXTRACTING", duration=0)]

    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(status_data, _stats(total=1))

    assert code == 2
    assert "FINAL STATUS: EXTRACTING" in capture.get()


@pytest.mark.unit
def test_summary_labels_by_bucket_and_not_by_the_documents_own_status(
    pinned_display_console,
):
    """
    The completed and failed branches hardcode their label, ignoring `status`.

    A document in the `completed` bucket is reported as COMPLETED whatever its
    `status` field says -- here REDACTED_SUPERSEDED, which `idp_sdk`'s progress
    monitor does place in that bucket. `format_status_json` reads the field
    instead, which is why the two functions can differ; see the agreement tests
    below.
    """
    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(
            _status_data(total=1, completed=[_doc(status="REDACTED_SUPERSEDED")]),
            _stats(total=1, completed=1, all_complete=True),
        )

    assert code == 0
    assert "FINAL STATUS: COMPLETED" in capture.get()


@pytest.mark.unit
def test_summary_with_no_bucket_entry_reports_unknown_and_returns_two(
    pinned_display_console,
):
    """
    `total == 1` and four empty buckets is the one place `doc` can be None, and
    each of the three uses of it is guarded -- so this prints rather than raising
    AttributeError, which is the failure mode the guards exist for.
    """
    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(
            _status_data(total=1), _stats(total=1, all_complete=True)
        )

    assert code == 2
    assert "FINAL STATUS: UNKNOWN | Duration: 0.0s | Exit Code: 2" in capture.get()


@pytest.mark.unit
def test_summary_batch_that_finished_cleanly_returns_zero(pinned_display_console):
    stats = _stats(total=5, completed=5, success_rate=100.0, all_complete=True)

    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(_status_data(total=5), stats)
    rendered = capture.get()

    assert code == 0
    assert "FINAL STATUS: ALL COMPLETED | Total: 5" in rendered
    assert "Success Rate: 100.0% | Exit Code: 0" in rendered


@pytest.mark.unit
def test_summary_batch_with_failures_returns_one_and_counts_them(
    pinned_display_console,
):
    stats = _stats(total=5, completed=3, failed=2, success_rate=60.0, all_complete=True)

    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(_status_data(total=5), stats)

    assert code == 1
    assert "COMPLETED WITH FAILURES (2 failed)" in capture.get()


@pytest.mark.unit
def test_summary_batch_still_running_returns_two_and_counts_what_finished(
    pinned_display_console,
):
    stats = _stats(total=5, completed=2, failed=1, running=2, success_rate=66.7)

    with pinned_display_console.capture() as capture:
        code = display.show_final_status_summary(_status_data(total=5), stats)

    assert code == 2
    assert "IN PROGRESS (3/5 finished)" in capture.get()


# ---------------------------------------------------------------------------
# Do the two exit-code paths agree?
# ---------------------------------------------------------------------------
#
# `idp-cli status` calls `format_status_json` for `--format json` and
# `show_final_status_summary` for the default table output, and exits with
# whichever code it got. The same document must therefore produce the same code
# from both, and for three of the four buckets it does. The exceptions are pinned
# below.


def _both_codes(status_data, stats) -> tuple[int, int]:
    """The exit code each of the two paths returns for one input."""
    from_json = json.loads(display.format_status_json(status_data, stats))["exit_code"]
    from_summary = display.show_final_status_summary(status_data, stats)
    return from_json, from_summary


@pytest.mark.unit
@pytest.mark.parametrize(
    ("bucket", "status", "expected"),
    [
        ("completed", "COMPLETED", 0),
        ("failed", "FAILED", 1),
        ("running", "RUNNING", 2),
        ("running", "HITL_IN_PROGRESS", 2),
        ("queued", "QUEUED", 2),
    ],
)
def test_both_exit_code_paths_agree_on_the_ordinary_single_document_states(
    bucket, status, expected
):
    """
    These five are the bucket/status pairs `_batch_status_to_display_dicts`
    produces for a document in an ordinary state, so `--format json` and the
    default output must exit alike on all of them.
    """
    status_data = _status_data(total=1)
    status_data[bucket] = [_doc(status=status)]
    stats = _stats(total=1, all_complete=bucket in ("completed", "failed"))

    from_json, from_summary = _both_codes(status_data, stats)

    assert from_json == from_summary == expected


@pytest.mark.unit
def test_an_aborted_document_gets_a_different_exit_code_from_each_output_format():
    """
    DEFECT (pinned, not fixed): `idp-cli status` on a single ABORTED document
    exits 1 with `--format json` and 2 with the default table output.

    `_batch_status_to_display_dicts` in `cli.py` sorts a document into `completed`
    on COMPLETED, into `failed` on exactly FAILED, into `running` on the nine
    in-progress statuses, and everything else -- including ABORTED -- into
    `queued`. `format_status_json` then reads the document's `status` field, sees
    ABORTED in its failure list and answers 1. `show_final_status_summary` reads
    the bucket instead, finds `completed` and `failed` empty, and answers 2.

    ABORTED is terminal, so the consequence is not a one-poll discrepancy: a
    script looping on the default output until the exit code stops being 2 never
    stops, and one that treats 2 as "keep waiting" reports a document a human
    deliberately stopped as still processing.
    """
    status_data = _status_data(
        total=1,
        queued=[_doc(status="ABORTED", error="Aborted by user", failed_step="N/A")],
    )
    stats = _stats(total=1, queued=1, all_complete=False)

    from_json, from_summary = _both_codes(status_data, stats)

    assert from_json == 1
    assert from_summary == 2


@pytest.mark.unit
def test_a_single_document_lookup_that_found_nothing_agrees_across_both_paths():
    """
    The guarantee (#1230): with `total == 1` and no bucket entry, both output paths
    answer 2.

    The JSON path took its single-document branch only when a bucket was non-empty,
    so it fell through to the batch summary and derived the code from `all_complete`,
    answering 0 — nothing was measured and the caller was told the document
    succeeded. The table path has no such fall-through and has always answered
    UNKNOWN / 2. Asserted as equality *and* as the value, since two paths that agree
    on the wrong answer would satisfy equality alone.
    """
    from_json, from_summary = _both_codes(
        _status_data(total=1), _stats(total=1, all_complete=True)
    )

    assert from_json == from_summary == 2


@pytest.mark.unit
def test_the_two_paths_read_different_buckets_first_when_a_document_is_in_two():
    """
    Latent, not reachable through `cli.py` today: the precedence orders differ.

    `format_status_json` checks completed, running, failed, queued;
    `show_final_status_summary` checks completed, failed, then running and queued.
    A document listed in both `running` and `failed` therefore exits 2 from the
    JSON path and 1 from the table path. `_batch_status_to_display_dicts` puts
    each document in exactly one bucket, so this cannot happen there -- it is
    pinned so that a second caller building `status_data` by hand finds the
    divergence in a test rather than in a pipeline.
    """
    status_data = _status_data(
        total=1,
        running=[_doc(status="RUNNING")],
        failed=[_doc(status="FAILED", error="boom")],
    )

    from_json, from_summary = _both_codes(status_data, _stats(total=1))

    assert from_json == 2
    assert from_summary == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("completed", "failed", "all_complete", "expected"),
    [
        (4, 0, True, 0),
        (3, 1, True, 1),
        (1, 0, False, 2),
    ],
)
def test_both_exit_code_paths_agree_on_every_batch_shape(
    completed, failed, all_complete, expected
):
    """Multi-document batches are decided by `stats` alone in both functions."""
    stats = _stats(
        total=4,
        completed=completed,
        failed=failed,
        running=4 - completed - failed,
        all_complete=all_complete,
    )

    from_json, from_summary = _both_codes(_status_data(total=4), stats)

    assert from_json == from_summary == expected


@pytest.mark.unit
def test_derive_exit_code_equals_show_final_status_summary_over_the_whole_input_space():
    """The two must never drift, and a sampled comparison would not say that.

    `derive_exit_code` was extracted from `show_final_status_summary` so that
    `_monitor_progress` can have the code without the printed "FINAL STATUS" line —
    two of its three callers discard the value, and printing "Exit Code: 1" there
    would state a code contradicting `$?` for `process --monitor`. The extraction is
    only safe while the two agree, and the whole reason the extraction was worth doing
    rather than re-deriving the rule from `stats` is that two implementations of one
    rule is how the polled and waited forms of `status` came to disagree (#1230).

    So this is exhaustive over the space the rule reads rather than a sample: every
    combination of the four buckets being empty or not, crossed with `total` in
    {0, 1, 2}, `all_complete` in {True, False} and `failed` in {0, 1}. A table of
    hand-picked cases is what let the original divergence sit unnoticed.
    """
    import itertools

    checked = 0
    for pattern in itertools.product([0, 1], repeat=4):
        for total in (0, 1, 2):
            for all_complete in (True, False):
                for failed_count in (0, 1):
                    status_data = {
                        "total": total,
                        "completed": [_doc(status="COMPLETED")] * pattern[0],
                        "running": [_doc(status="RUNNING")] * pattern[1],
                        "failed": [_doc(status="FAILED")] * pattern[2],
                        "queued": [_doc(status="QUEUED")] * pattern[3],
                    }
                    stats = _stats(
                        total=total,
                        completed=pattern[0],
                        failed=failed_count,
                        running=pattern[1],
                        queued=pattern[3],
                        all_complete=all_complete,
                    )
                    checked += 1
                    assert display.derive_exit_code(
                        status_data, stats
                    ) == display.show_final_status_summary(status_data, stats), (
                        f"drift at buckets={pattern} total={total} "
                        f"all_complete={all_complete} failed={failed_count}"
                    )

    # The loop has to have run; a generator that produced nothing would pass.
    assert checked == 192
