# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the commands that read results back: `list-batches`, `list-versions`,
`download-results`, and the parts of `status` that `tests/test_status_command.py`
leaves uncovered.

`download-results` is the one command in this group that writes to the local disk, so
it is tested against a real `moto` S3 bucket holding real objects, and the assertions
read the downloaded files back off the filesystem and compare their bytes and their
paths. Asserting that `download_file` was called would pass just as happily if the
files landed in the wrong directory, if the key prefix were mishandled, or if the
`--file-types` filter selected nothing; reading the tree back is what distinguishes
those. Only `IDPClient`'s stack lookup is substituted — `StackInfo`, which would need a
real CloudFormation stack — so the S3 listing, the pagination, the type filter, the
directory creation and the downloads are all the production code paths.

`list-batches` and `list-versions` are read-only renderers, and what matters about them
is the empty case (both return quietly rather than failing), the numbers they compute
from the SDK model, and the truncation they apply to timestamps.

The `status` tests here are deliberately narrow: `tests/test_status_command.py` already
covers the search, the two refusals, `--get-time`, `--show-details`, `--document-id` and
the JSON format. What was left was the interaction between `--wait` and the other
options, and the one thing that combination used to expose: `status --wait` reported a
batch that finished with failures as a success, because `_monitor_progress` had no
return value for the command to exit with, while the polled form of the same command
exited 1. `_monitor_progress` now returns the code `display.derive_exit_code` answers
and `--wait` exits on it, so the two forms agree — asserted in both directions below,
since agreement on one batch could be a property of the fixture.

`derive_exit_code` rather than `show_final_status_summary`, which answers the same code
but also **prints** it. Two of `_monitor_progress`'s three callers discard the value, so
printing "Exit Code: N" from there would have `process --monitor` and `rerun --monitor`
state a code contradicting `$?`. So `--wait` prints no FINAL STATUS line, and that is
asserted rather than left to drift.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws
from rich.console import Console

from idp_cli import cli as cli_module
from idp_cli import display as display_module
from idp_cli.cli import cli
from idp_sdk.models import BatchDownloadResult, BatchInfo, BatchListResult, BatchStatus
from idp_sdk.models.document import DocumentStatus

OUTPUT_BUCKET = "my-stack-outputbucket"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def unstyled_display_console(monkeypatch):
    """Pin `display.console`, which the package-wide fixture does not reach.

    `status` renders its table and its "FINAL STATUS" line through `display.py`'s own
    module-level console, so without this the assertions below depend on the terminal
    width of whatever runs them.
    """
    monkeypatch.setattr(
        display_module, "console", Console(width=200, force_terminal=False)
    )


class Clock:
    """A `time` stand-in whose `sleep` advances a counter instead of waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def doc_status(document_id: str, status: str, **kwargs) -> DocumentStatus:
    return DocumentStatus(document_id=document_id, status=status, **kwargs)  # type: ignore[arg-type]


def batch_status(documents: list[DocumentStatus], **kwargs) -> BatchStatus:
    defaults = {
        "batch_id": "batch-1",
        "total": len(documents),
        "completed": sum(1 for d in documents if d.status == "COMPLETED"),
        "failed": sum(1 for d in documents if d.status == "FAILED"),
        "in_progress": 0,
        "queued": 0,
        "success_rate": 1.0,
        "all_complete": True,
    }
    defaults.update(kwargs)
    return BatchStatus(documents=documents, **defaults)  # type: ignore[arg-type]


def search_hit(*object_keys: str) -> dict:
    """What `TrackingTableSearcher.search_by_pk_and_status` returns, in DynamoDB shape."""
    return {
        "success": True,
        "count": len(object_keys),
        "items": [{"ObjectKey": {"S": key}} for key in object_keys],
    }


# ---------------------------------------------------------------------------
# status: the --wait combinations
# ---------------------------------------------------------------------------


class TestStatusWait:
    def test_json_format_is_refused_for_live_monitoring_with_a_warning(self, runner):
        """`--format json --wait` warns and falls back to the table, rather than failing.

        A live Rich display cannot also be a JSON document, so one of the two has to
        give. The warning goes to stderr, which is the point: a caller piping stdout
        into `jq` still gets no JSON, but the explanation does not corrupt the stream
        it is explaining.
        """
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch("idp_cli.cli._monitor_progress", return_value=0) as monitor,
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf"
            )
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--wait",
                    "--format",
                    "json",
                ],
            )

        assert result.exit_code == 0, result.output
        assert (
            "Warning: --format json ignored with --wait (using table display for live "
            "monitoring)" in result.output
        )
        monitor.assert_called_once()
        assert monitor.call_args.kwargs["batch_id"] == "batch-1"
        assert monitor.call_args.kwargs["refresh_interval"] == 5

    def test_wait_monitors_the_batch_id_it_searched_for(self, runner):
        """The identifier handed to the monitor is the batch id, not a document list.

        The search above it resolves individual `ObjectKey`s, and those are *not*
        what the monitor is given — it takes the batch id and re-resolves. Passing a
        document key here instead would monitor one document and report the batch as
        a single-document batch.
        """
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch("idp_cli.cli._monitor_progress", return_value=0) as monitor,
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf", "batch-1/b.pdf"
            )
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--wait",
                    "--refresh-interval",
                    "13",
                ],
            )

        assert result.exit_code == 0, result.output
        monitor.assert_called_once()
        assert monitor.call_args.kwargs["batch_id"] == "batch-1"
        assert monitor.call_args.kwargs["refresh_interval"] == 13

    def test_wait_monitors_a_single_document_by_its_own_id(self, runner):
        """With `--document-id` there is no search, and the document id is the identifier."""
        with patch("idp_cli.cli._monitor_progress", return_value=0) as monitor:
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "batch-1/invoice.pdf",
                    "--wait",
                ],
            )

        assert result.exit_code == 0, result.output
        assert monitor.call_args.kwargs["batch_id"] == "batch-1/invoice.pdf"

    def test_get_time_with_wait_shows_the_timing_statistics_and_then_monitors(
        self, runner
    ):
        """Without `--wait`, `--get-time` prints the statistics and exits 0 immediately.

        With `--wait` it must print them and then carry on into the monitor — the
        early `sys.exit(0)` is guarded by `if not wait`. A regression that exited
        unconditionally would make `--get-time --wait` silently stop watching, and
        because the exit code is 0 either way nothing else would show it.
        """
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch("idp_cli.cli._monitor_progress", return_value=0) as monitor,
        ):
            searcher = searcher_cls.return_value
            searcher.search_by_pk_and_status.return_value = search_hit("batch-1/a.pdf")
            searcher.calculate_timing_statistics.return_value = {
                "success": True,
                "valid_count": 1,
            }
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--get-time",
                    "--wait",
                ],
            )

        assert result.exit_code == 0, result.output
        searcher.display_timing_statistics.assert_called_once()
        monitor.assert_called_once()

    def test_get_time_without_wait_stops_before_the_status_table(self, runner):
        """The other side of that branch: `sys.exit(0)` with no table and no monitoring."""
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch("idp_cli.cli._monitor_progress", return_value=0) as monitor,
            patch("idp_sdk.operations.batch.BatchOperation.get_status") as get_status,
        ):
            searcher = searcher_cls.return_value
            searcher.search_by_pk_and_status.return_value = search_hit("batch-1/a.pdf")
            searcher.calculate_timing_statistics.return_value = {"success": True}
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--get-time",
                ],
            )

        assert result.exit_code == 0
        assert monitor.called is False
        assert get_status.called is False
        assert "FINAL STATUS" not in result.output

    def test_wait_exits_non_zero_when_documents_failed(self, runner, monkeypatch):
        """The guarantee: the two forms of `status` agree on the same batch (#1230).

        Without `--wait`, `status` ends at `display.show_final_status_summary`, which
        returns 1 for a batch that finished with failures, and the command exits with
        that code. With `--wait` it used to end at `_monitor_progress`, which returned
        nothing, so the command fell off the end of the `try` block and exited 0 —
        and `--wait` is the natural form to use in a script, which is exactly where
        the exit code is the only thing read. `_monitor_progress` now derives the code
        through that same function and `status --wait` exits on it.

        Both halves are asserted in one place so that agreement cannot be read as a
        property of the fixture, and so that a regression in either form shows here.
        """
        mixed = batch_status(
            [
                doc_status("batch-1/a.pdf", "COMPLETED", duration_seconds=3.0),
                doc_status("batch-1/b.pdf", "FAILED", error="Textract threw"),
            ],
            success_rate=0.5,
        )
        args = [
            "status",
            "--stack-name",
            "my-stack",
            "--batch-id",
            "batch-1",
            "--object-status",
            "COMPLETED",
        ]

        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch(
                "idp_sdk.operations.batch.BatchOperation.get_status", return_value=mixed
            ),
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf", "batch-1/b.pdf"
            )
            polled = runner.invoke(cli, args)
            monkeypatch.setattr(cli_module, "time", Clock())
            waited = runner.invoke(cli, [*args, "--wait"])

        assert polled.exit_code == 1
        assert "COMPLETED WITH FAILURES (1 failed)" in polled.output

        assert waited.exit_code == 1
        assert "Batch Processing Complete" in waited.output
        assert "Textract threw" in waited.output
        # `--wait` deliberately does NOT print the polled form's "FINAL STATUS"
        # line: `_monitor_progress` derives the code without printing it, because
        # `process --monitor` and `rerun --monitor` discard the value and would
        # otherwise state an exit code contradicting `$?`. The failure is visible in
        # the summary panel instead.
        assert "FINAL STATUS" not in waited.output
        assert "Exit Code" not in waited.output

    def test_wait_still_exits_zero_on_a_clean_batch(self, runner, monkeypatch):
        """Non-vacuity for the test above: the code tracks the batch, not the flag."""
        # Two documents, so the batch branch of `show_final_status_summary` is the one
        # exercised -- the same branch the failing case above goes through. A
        # single-document fixture would take the per-document branch instead and
        # compare two different rules.
        clean = batch_status(
            [
                doc_status("batch-1/a.pdf", "COMPLETED", duration_seconds=3.0),
                doc_status("batch-1/b.pdf", "COMPLETED", duration_seconds=4.0),
            ],
        )
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch(
                "idp_sdk.operations.batch.BatchOperation.get_status", return_value=clean
            ),
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf", "batch-1/b.pdf"
            )
            monkeypatch.setattr(cli_module, "time", Clock())
            waited = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--wait",
                ],
            )

        assert waited.exit_code == 0, waited.output
        assert "Batch Processing Complete" in waited.output

    def test_wait_exits_two_when_the_watch_reached_no_verdict(self, runner):
        """The end-to-end half of the exit-2 contract, which nothing pinned.

        `_monitor_progress` returning 2 on a monitoring error or a Ctrl-C is tested
        directly in `test_monitor_and_display_mapping.py`, but every other test here
        patches it with `return_value=0` — so collapsing the 2 into a 1 at this call
        site left the whole suite green while three documents claimed the behaviour.
        This asserts the command propagates whatever the monitor answered, unaltered.
        """
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch("idp_cli.cli._monitor_progress", return_value=2),
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf"
            )
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                    "--wait",
                ],
            )

        assert result.exit_code == 2, result.output

    def test_show_details_is_suppressed_when_json_was_asked_for(self, runner):
        """`--show-details` renders a Rich table on stdout, which would precede the payload.

        The searcher's table goes through its own console, so it cannot be redirected
        to stderr the way the command's other progress lines are; with `--format
        json` it is skipped instead. A caller who asks for JSON wants the
        machine-readable document list, and a rendered table in front of it makes the
        stream unparseable from character 0 — the symptom issue #905 reported.
        """
        with patch(
            "idp_cli.search_tracking_table.TrackingTableSearcher"
        ) as searcher_cls:
            searcher = searcher_cls.return_value
            searcher.search_by_pk_and_status.return_value = search_hit("batch-1/a.pdf")
            with patch(
                "idp_sdk.operations.batch.BatchOperation.get_status",
                return_value=batch_status(
                    [doc_status("batch-1/a.pdf", "COMPLETED", duration_seconds=1.0)]
                ),
            ):
                json_run = runner.invoke(
                    cli,
                    [
                        "status",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "batch-1",
                        "--object-status",
                        "COMPLETED",
                        "--show-details",
                        "--format",
                        "json",
                    ],
                )

        assert json_run.exit_code == 0, json_run.output
        assert searcher.display_results.called is False

    def test_an_unexpected_failure_is_reported_on_stderr_and_exits_one(self, runner):
        """The command's outer handler: any exception becomes one line and exit 1.

        The error goes to `err_console` rather than stdout, so `status --format json`
        keeps its stdout clean even when it fails — a caller's `jq` sees an empty
        stream instead of a mixture of prose and JSON.
        """
        with patch(
            "idp_cli.search_tracking_table.TrackingTableSearcher"
        ) as searcher_cls:
            searcher_cls.side_effect = RuntimeError("tracking table does not exist")
            result = runner.invoke(
                cli, ["status", "--stack-name", "my-stack", "--batch-id", "batch-1"]
            )

        assert result.exit_code == 1
        assert "Error: tracking table does not exist" in result.output

    def test_a_status_lookup_failure_during_display_exits_one(self, runner):
        """The same handler, reached from the second half of the command."""
        with (
            patch(
                "idp_cli.search_tracking_table.TrackingTableSearcher"
            ) as searcher_cls,
            patch(
                "idp_sdk.operations.batch.BatchOperation.get_status",
                side_effect=RuntimeError("DynamoDB is unavailable"),
            ),
        ):
            searcher_cls.return_value.search_by_pk_and_status.return_value = search_hit(
                "batch-1/a.pdf"
            )
            result = runner.invoke(
                cli,
                [
                    "status",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--object-status",
                    "COMPLETED",
                ],
            )

        assert result.exit_code == 1
        assert "Error: DynamoDB is unavailable" in result.output


# ---------------------------------------------------------------------------
# list-batches
# ---------------------------------------------------------------------------


def batch_list(*batches: BatchInfo) -> BatchListResult:
    return BatchListResult(batches=list(batches), count=len(batches))


class TestListBatches:
    def test_an_empty_result_says_so_and_exits_zero(self, runner):
        """Nothing found is not an error: the command returns rather than exiting 1.

        A fresh stack has no batches, and `list-batches` is the command a user runs
        to find that out.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.return_value = batch_list()
            client_cls.return_value = client
            result = runner.invoke(cli, ["list-batches", "--stack-name", "my-stack"])

        assert result.exit_code == 0
        assert "No batches found" in result.output
        assert "Recent Batches" not in result.output

    def test_each_batch_becomes_a_row_with_its_document_count(self, runner):
        """The Documents column is `len(document_ids)`, not a field the SDK reports.

        That derivation is the one piece of arithmetic in this command, and the two
        batches below have deliberately different document counts, queued counts and
        failure counts so a column swap cannot pass.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.return_value = batch_list(
                BatchInfo(
                    batch_id="cli-batch-20251015-143000",
                    document_ids=["a.pdf", "b.pdf", "c.pdf"],
                    queued=3,
                    failed=0,
                    timestamp="2025-10-15T14:30:00.123456+00:00",
                ),
                BatchInfo(
                    batch_id="cli-batch-20251016-090000",
                    document_ids=["d.pdf"],
                    queued=0,
                    failed=1,
                    timestamp="2025-10-16T09:00:00.000000+00:00",
                ),
            )
            client_cls.return_value = client
            result = runner.invoke(cli, ["list-batches", "--stack-name", "my-stack"])

        assert result.exit_code == 0, result.output
        assert "cli-batch-20251015-143000" in result.output
        assert "cli-batch-20251016-090000" in result.output
        # Timestamps are trimmed to 19 characters — seconds precision, no offset.
        assert "2025-10-15T14:30:00" in result.output
        assert ".123456" not in result.output
        assert "+00:00" not in result.output
        row = [
            line
            for line in result.output.splitlines()
            if "cli-batch-20251015-143000" in line
        ]
        assert len(row) == 1
        cells = [cell.strip() for cell in row[0].strip("│ ").split("│")]
        assert cells[:4] == ["cli-batch-20251015-143000", "3", "3", "0"]

    def test_the_limit_is_forwarded_and_named_in_the_title(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.return_value = batch_list(
                BatchInfo(
                    batch_id="b1",
                    document_ids=["a.pdf"],
                    queued=1,
                    failed=0,
                    timestamp="2025-10-15T14:30:00",
                )
            )
            client_cls.return_value = client
            result = runner.invoke(
                cli, ["list-batches", "--stack-name", "my-stack", "--limit", "5"]
            )

        client.batch.list.assert_called_once_with(limit=5)
        assert "Recent Batches (Last 5)" in result.output

    def test_the_default_limit_is_ten(self, runner):
        """An unbounded list would scan the whole tracking table."""
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.return_value = batch_list()
            client_cls.return_value = client
            runner.invoke(cli, ["list-batches", "--stack-name", "my-stack"])

        client.batch.list.assert_called_once_with(limit=10)

    def test_a_non_numeric_limit_is_refused_by_click(self, runner):
        result = runner.invoke(
            cli, ["list-batches", "--stack-name", "my-stack", "--limit", "lots"]
        )

        assert result.exit_code == 2
        assert "lots" in result.output

    def test_the_region_reaches_the_client(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.return_value = batch_list()
            client_cls.return_value = client
            runner.invoke(
                cli,
                ["list-batches", "--stack-name", "my-stack", "--region", "ap-south-1"],
            )

        client_cls.assert_called_once_with(stack_name="my-stack", region="ap-south-1")

    def test_a_failure_is_reported_and_exits_one(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list.side_effect = RuntimeError("stack not found")
            client_cls.return_value = client
            result = runner.invoke(cli, ["list-batches", "--stack-name", "my-stack"])

        assert result.exit_code == 1
        assert "Error: stack not found" in result.output


# ---------------------------------------------------------------------------
# list-versions
# ---------------------------------------------------------------------------


class TestListVersions:
    def test_a_document_with_no_versions_says_so_and_exits_zero(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list_versions.return_value = []
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "list-versions",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                ],
            )

        assert result.exit_code == 0
        assert "No versions found for loan-123/package.pdf" in result.output
        assert "Run ID" not in result.output

    def test_every_run_record_field_is_rendered(self, runner):
        """Five columns, read out of a plain dict the SDK builds from the run manifest.

        The `RunId` is the value a caller then passes to
        `download-results --run-id`, so a truncated or reordered column here breaks
        the next command in the sequence.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list_versions.return_value = [
                {
                    "RunId": "20250707T141530Z-exec-abc",
                    "CompletionTime": "2025-07-07T14:15:30.987654+00:00",
                    "ConfigVersion": "lending-v2",
                    "PageCount": 12,
                    "FileCount": 7,
                    "ManifestUri": "s3://out/manifests/run.json",
                }
            ]
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "list-versions",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Versions for loan-123/package.pdf" in result.output
        for column in ("Run ID", "Completed", "Config Profile", "Pages", "Files"):
            assert column in result.output
        assert "20250707T141530Z-exec-abc" in result.output
        assert "2025-07-07T14:15:30" in result.output
        assert ".987654" not in result.output
        assert "lending-v2" in result.output
        row = [
            line
            for line in result.output.splitlines()
            if "20250707T141530Z-exec-abc" in line
        ]
        assert len(row) == 1
        cells = [cell.strip() for cell in row[0].strip("│ ").split("│")]
        assert cells[3:5] == ["12", "7"]
        # ManifestUri is not a column; it must not leak into the table.
        assert "s3://out/manifests/run.json" not in result.output

    def test_missing_fields_get_placeholders_rather_than_blowing_up(self, runner):
        """An older run record may not carry every key.

        The substitutions differ per column and are worth pinning individually: the
        run id and completion time become empty, the profile becomes the literal
        "N/A", and the two counts become "-". Reading a `KeyError` instead would take
        the whole listing down over one incomplete record.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list_versions.return_value = [{"RunId": "run-1"}]
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "list-versions",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                ],
            )

        assert result.exit_code == 0, result.output
        row = [line for line in result.output.splitlines() if "run-1" in line]
        assert len(row) == 1
        cells = [cell.strip() for cell in row[0].strip("│ ").split("│")]
        assert cells == ["run-1", "", "N/A", "-", "-"]

    def test_the_document_id_is_required(self, runner):
        result = runner.invoke(cli, ["list-versions", "--stack-name", "my-stack"])

        assert result.exit_code == 2
        assert "--document-id" in result.output

    def test_the_document_id_is_passed_as_a_keyword(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list_versions.return_value = []
            client_cls.return_value = client
            runner.invoke(
                cli,
                [
                    "list-versions",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                    "--region",
                    "us-west-2",
                ],
            )

        client.batch.list_versions.assert_called_once_with(
            document_id="loan-123/package.pdf"
        )
        client_cls.assert_called_once_with(stack_name="my-stack", region="us-west-2")

    def test_a_failure_is_reported_and_exits_one(self, runner):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.list_versions.side_effect = RuntimeError("no such document")
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "list-versions",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "nope.pdf",
                ],
            )

        assert result.exit_code == 1
        assert "Error: no such document" in result.output


# ---------------------------------------------------------------------------
# download-results
# ---------------------------------------------------------------------------


class StubStackInfo:
    """Stands in for `StackInfo` so no CloudFormation stack is needed.

    Everything below this — the S3 listing, the `--file-types` filter, the local
    directory creation and the downloads themselves — is the real `BatchProcessor`
    running against `moto`.
    """

    def __init__(self, stack_name, region=None):
        self.stack_name = stack_name
        self.region = region

    def validate_stack(self) -> bool:
        return True

    def get_resources(self) -> dict:
        return {
            "OutputBucket": OUTPUT_BUCKET,
            "InputBucket": "my-stack-inputbucket",
            "DocumentsTable": "my-stack-documents",
        }


#: One batch's worth of output, in the layout the pipeline writes: a directory per
#: document, then a directory per artifact kind. Two documents so that
#: `documents_downloaded` is a distinct count rather than a copy of the file count.
BATCH_OBJECTS = {
    "batch-1/invoice.pdf/pages/1/text.json": '{"page": 1}',
    "batch-1/invoice.pdf/pages/2/text.json": '{"page": 2}',
    "batch-1/invoice.pdf/sections/0/result.json": '{"total": "42.00"}',
    "batch-1/invoice.pdf/summary/summary.json": '{"summary": "an invoice"}',
    "batch-1/statement.pdf/sections/0/result.json": '{"balance": "17.50"}',
    "batch-1/statement.pdf/evaluation/report.json": '{"accuracy": 1.0}',
}


def seed_output_bucket(objects: dict[str, str] | None = None) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=OUTPUT_BUCKET)
    for key, body in (BATCH_OBJECTS if objects is None else objects).items():
        s3.put_object(Bucket=OUTPUT_BUCKET, Key=key, Body=body.encode("utf-8"))


class TestDownloadResults:
    def test_every_object_under_the_batch_prefix_lands_on_disk(self, runner, tmp_path):
        """Read back off the filesystem: the paths and the bytes, for all six objects.

        The local layout mirrors the S3 key exactly under the output directory, which
        is what makes a second `download-results` into the same directory idempotent
        and what lets a caller find an artifact by the key they saw in the console.
        Asserting the exact set of relative paths is what catches a prefix being
        stripped or doubled; asserting the bytes is what catches the wrong object
        being fetched for a key.
        """
        output_dir = tmp_path / "results"
        with mock_aws():
            seed_output_bucket()
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "batch-1",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 0, result.output
        written = {
            str(path.relative_to(output_dir)): path.read_text(encoding="utf-8")
            for path in output_dir.rglob("*")
            if path.is_file()
        }
        assert written == BATCH_OBJECTS
        assert "Downloaded 6 files to" in result.output
        # Two distinct documents under the batch prefix.
        assert "Documents: 2" in result.output
        assert f"Output: {output_dir}/batch-1/" in result.output

    def test_an_output_directory_that_does_not_exist_is_created(self, runner, tmp_path):
        """Several levels deep, and not created beforehand.

        `--output-dir` is `click.Path()` with no `exists=True`, so the command is
        responsible for making it. A missing intermediate directory would otherwise
        surface as a `FileNotFoundError` from `download_file` after the listing has
        already succeeded.
        """
        output_dir = tmp_path / "does" / "not" / "exist" / "yet"
        assert not output_dir.exists()
        with mock_aws():
            seed_output_bucket(
                {"batch-1/invoice.pdf/sections/0/result.json": '{"total": "1.00"}'}
            )
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "batch-1",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 0, result.output
        written = output_dir / "batch-1/invoice.pdf/sections/0/result.json"
        assert written.read_text(encoding="utf-8") == '{"total": "1.00"}'

    def test_file_types_selects_only_the_named_artifact_directories(
        self, runner, tmp_path
    ):
        """A comma-separated list, matched as a `/<type>/` path segment.

        Two types are asked for out of the four present, so this pins both what is
        taken and what is left behind — a filter that matched substrings rather than
        segments, or that fell back to "all" when it matched nothing, would fail
        here.
        """
        output_dir = tmp_path / "results"
        with mock_aws():
            seed_output_bucket()
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "batch-1",
                        "--output-dir",
                        str(output_dir),
                        "--file-types",
                        "sections, summary",
                    ],
                )

        assert result.exit_code == 0, result.output
        written = {
            str(path.relative_to(output_dir))
            for path in output_dir.rglob("*")
            if path.is_file()
        }
        assert written == {
            "batch-1/invoice.pdf/sections/0/result.json",
            "batch-1/invoice.pdf/summary/summary.json",
            "batch-1/statement.pdf/sections/0/result.json",
        }
        assert "Downloaded 3 files to" in result.output

    def test_the_default_file_type_is_everything(self, runner, tmp_path):
        """`--file-types all` and the default must agree.

        The default value is the literal string "all", which the command turns into
        `["all"]` rather than splitting, and the SDK expands to the four kinds.
        """
        results = []
        for args in ([], ["--file-types", "all"]):
            output_dir = tmp_path / f"results{len(results)}"
            with mock_aws():
                seed_output_bucket()
                with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                    result = runner.invoke(
                        cli,
                        [
                            "download-results",
                            "--stack-name",
                            "my-stack",
                            "--batch-id",
                            "batch-1",
                            "--output-dir",
                            str(output_dir),
                            *args,
                        ],
                    )
            assert result.exit_code == 0, result.output
            results.append(
                {
                    str(path.relative_to(output_dir))
                    for path in output_dir.rglob("*")
                    if path.is_file()
                }
            )

        assert results[0] == results[1] == set(BATCH_OBJECTS)

    def test_an_existing_local_file_is_overwritten(self, runner, tmp_path):
        """There is no `--overwrite` option, and no prompt: the newer bytes win.

        Re-downloading a batch after a reprocess is the ordinary reason to run this
        twice, and it has to replace the stale result rather than skip it or append
        to it. Worth knowing in the other direction too: a file that the current
        batch no longer produces is *not* removed, so a directory reused across runs
        can hold a mixture.
        """
        output_dir = tmp_path / "results"
        stale = output_dir / "batch-1/invoice.pdf/sections/0/result.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"total": "STALE"}', encoding="utf-8")
        orphan = output_dir / "batch-1/invoice.pdf/sections/9/result.json"
        orphan.parent.mkdir(parents=True)
        orphan.write_text('{"from": "an older run"}', encoding="utf-8")

        with mock_aws():
            seed_output_bucket(
                {"batch-1/invoice.pdf/sections/0/result.json": '{"total": "42.00"}'}
            )
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "batch-1",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 0, result.output
        assert stale.read_text(encoding="utf-8") == '{"total": "42.00"}'
        assert orphan.exists()

    def test_a_batch_with_no_results_downloads_nothing_and_exits_zero(
        self, runner, tmp_path
    ):
        """An empty listing is reported, not treated as an error.

        A batch that is still processing, or whose id was mistyped, has no objects
        under its prefix. The command says "Found 0 files to download", reports zero
        documents, creates the output directory anyway, and exits 0 — so a script
        cannot tell a mistyped batch id from a batch that produced nothing.
        """
        output_dir = tmp_path / "results"
        with mock_aws():
            seed_output_bucket()
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--batch-id",
                        "no-such-batch",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "Found 0 files to download" in result.output
        assert "Downloaded 0 files to" in result.output
        assert "Documents: 0" in result.output
        assert output_dir.is_dir()
        assert list(output_dir.rglob("*")) == []

    def test_neither_a_batch_id_nor_a_run_id_is_refused(
        self, runner, tmp_path, api_calls
    ):
        """Exit 1 with a message naming both routes, and nothing read from S3."""
        output_dir = tmp_path / "results"
        with mock_aws():
            seed_output_bucket()
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 1
        assert "Provide either --batch-id or --document-id/--run-id" in result.output
        assert api_calls.of("ListObjectsV2") == []
        assert api_calls.of("GetObject") == []
        assert not output_dir.exists()

    def test_a_run_id_without_a_document_id_is_refused(
        self, runner, tmp_path, api_calls
    ):
        """A run id identifies a run *of a document*, so it cannot stand alone.

        The refusal has to come before any download, because there is nothing to
        resolve the run against — and it must not fall through to the batch path,
        which would download something plausible and wrong.
        """
        output_dir = tmp_path / "results"
        with mock_aws():
            seed_output_bucket()
            with patch("idp_sdk._core.batch_processor.StackInfo", StubStackInfo):
                result = runner.invoke(
                    cli,
                    [
                        "download-results",
                        "--stack-name",
                        "my-stack",
                        "--run-id",
                        "20250707T141530Z-exec-abc",
                        "--output-dir",
                        str(output_dir),
                    ],
                )

        assert result.exit_code == 1
        assert "--run-id requires --document-id" in result.output
        assert api_calls.of("ListObjectsV2") == []
        assert api_calls.of("GetObject") == []

    def test_the_output_directory_is_required(self, runner):
        result = runner.invoke(
            cli, ["download-results", "--stack-name", "my-stack", "--batch-id", "b1"]
        )

        assert result.exit_code == 2
        assert "--output-dir" in result.output

    def test_a_version_download_takes_the_pinned_run_and_stops_there(
        self, runner, tmp_path
    ):
        """The version path returns before the batch path, and reports the SDK's own directory.

        `download_version` fetches each output object by its recorded S3 `VersionId`,
        so the exact bytes of that run are reproduced even after a later run
        overwrote them. It is mocked here rather than driven through `moto`: it needs
        a committed run manifest, which is the SDK's contract to test and not this
        command's. What is asserted is this command's part — the keywords it passes,
        that the batch download is *not* also attempted, and that the count it prints
        comes from the result rather than from `--output-dir`.
        """
        output_dir = tmp_path / "results"
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.download_version.return_value = BatchDownloadResult(
                files_downloaded=4,
                documents_downloaded=1,
                output_dir=str(output_dir / "20250707T141530Z-exec-abc"),
            )
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "download-results",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                    "--run-id",
                    "20250707T141530Z-exec-abc",
                    "--output-dir",
                    str(output_dir),
                ],
            )

        assert result.exit_code == 0, result.output
        client.batch.download_version.assert_called_once_with(
            document_id="loan-123/package.pdf",
            run_id="20250707T141530Z-exec-abc",
            output_dir=str(output_dir),
        )
        assert client.batch.download_results.called is False
        assert (
            "Downloading version 20250707T141530Z-exec-abc of loan-123/package.pdf"
            in result.output
        )
        assert "Downloaded 4 files to" in result.output
        assert "20250707T141530Z-exec-abc" in result.output

    def test_a_document_id_with_no_run_id_falls_through_to_the_batch_refusal(
        self, runner, tmp_path
    ):
        """Pinned as it behaves today: `--document-id` alone is not a download route.

        Only `--run-id` triggers the version path, and the batch path needs
        `--batch-id`, so `--document-id` on its own is refused — by the message about
        `--batch-id`, which does not mention that a `--run-id` is what is missing.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client_cls.return_value = MagicMock()
            result = runner.invoke(
                cli,
                [
                    "download-results",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                    "--output-dir",
                    str(tmp_path / "results"),
                ],
            )

        assert result.exit_code == 1
        assert "Provide either --batch-id or --document-id/--run-id" in result.output

    def test_the_region_reaches_the_client(self, runner, tmp_path):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.download_results.return_value = BatchDownloadResult(
                files_downloaded=0, documents_downloaded=0, output_dir=str(tmp_path)
            )
            client_cls.return_value = client
            runner.invoke(
                cli,
                [
                    "download-results",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--output-dir",
                    str(tmp_path / "results"),
                    "--region",
                    "eu-central-1",
                ],
            )

        client_cls.assert_called_once_with(stack_name="my-stack", region="eu-central-1")
        assert client.batch.download_results.call_args.kwargs["file_types"] == ["all"]

    def test_a_download_failure_is_reported_and_exits_one(self, runner, tmp_path):
        """A denied bucket is the realistic case, and it must not exit 0.

        The message is what a user has to act on, so it carries the underlying error
        rather than a generic one.
        """
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.download_results.side_effect = RuntimeError(
                "AccessDenied on OutputBucket"
            )
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "download-results",
                    "--stack-name",
                    "my-stack",
                    "--batch-id",
                    "batch-1",
                    "--output-dir",
                    str(tmp_path / "results"),
                ],
            )

        assert result.exit_code == 1
        assert "Error: AccessDenied on OutputBucket" in result.output

    def test_a_version_download_failure_is_reported_and_exits_one(
        self, runner, tmp_path
    ):
        with patch("idp_sdk.IDPClient") as client_cls:
            client = MagicMock()
            client.batch.download_version.side_effect = RuntimeError("run not found")
            client_cls.return_value = client
            result = runner.invoke(
                cli,
                [
                    "download-results",
                    "--stack-name",
                    "my-stack",
                    "--document-id",
                    "loan-123/package.pdf",
                    "--run-id",
                    "nope",
                    "--output-dir",
                    str(tmp_path / "results"),
                ],
            )

        assert result.exit_code == 1
        assert "Error: run not found" in result.output


def test_the_client_seam_is_the_sdk_attribute_not_the_one_bound_in_cli(runner):
    """A guard on the patch target every test in this file relies on.

    `idp_cli.cli` binds `IDPClient` at module level as well, and some *other*
    commands use that binding — but the commands here re-import it inside their own
    bodies (`from idp_sdk import IDPClient`), so `idp_sdk.IDPClient` is the seam and
    `idp_cli.cli.IDPClient` is not. Patching the wrong one of those does not fail
    loudly: it leaves the real client in place and the test reaches for real AWS,
    which the `no_outbound_http` fixture then reports as a network error with no hint
    about the cause. This test pins the distinction directly — the module-level name
    is replaced with a mock that must go untouched, while the SDK-level patch is the
    one that takes effect.
    """
    with (
        patch.object(cli_module, "IDPClient") as module_level,
        patch("idp_sdk.IDPClient") as sdk_level,
    ):
        sdk_level.return_value.batch.list.return_value = batch_list()
        result = runner.invoke(cli, ["list-batches", "--stack-name", "my-stack"])

    assert result.exit_code == 0, result.output
    assert sdk_level.called is True
    assert module_level.called is False
