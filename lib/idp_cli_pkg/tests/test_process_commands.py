# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the four document-submitting commands and the two bodies behind them.

`process` and its deprecated alias `run-inference` are thin wrappers over
`_process_impl`, which accepts four mutually exclusive input sources — `--manifest`,
`--dir`, `--s3-uri`, `--test-set` — and hands three of them to `IDPClient.batch.process`
with a *different* keyword set each. Which source was given therefore decides which
arguments the SDK receives, and the wrong keyword set does not fail: `file_pattern` sent
down the manifest path would be accepted and ignored, and a dropped `config_version`
would process the batch under whatever configuration the profile currently holds while
the caller believes they pinned one. So most of what follows asserts the exact keyword
dictionary for each branch rather than spot-checking one argument.

`reprocess` and its deprecated alias `rerun-inference` both route to
`_rerun_inference_impl`, which re-runs documents that already exist and deletes
intermediate results as it goes. Two things are the contract there: which documents get
selected (an explicit `--document-ids` list, or every document in a `--batch-id`, and
never both), and whether the user was asked first. The confirmation tests read the
negative case off the client — answering "no" must leave `batch.reprocess` uncalled —
because "printed a cancellation message" and "started nothing" are different claims.

Refusals are asserted with the `api_calls` fixture as well as the patched client, since
input validation happens before `idp_sdk` is even imported: a refusal must make no AWS
call at all, and `api_calls` is the place that is visible.

Both spellings of the reprocess command are exercised through the `cli` group by name,
not by calling the implementation: the wiring between the command and the body is
itself something that has been wrong here, and a test that calls the body cannot see
it.

One command is pinned here as defective rather than working: `--config` is accepted by
`process` and then never used, written up on the test that pins it.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli
from idp_sdk.models import BatchProcessResult, BatchReprocessResult

PROCESS_COMMANDS = ("process", "run-inference")
REPROCESS_COMMANDS = ("reprocess", "rerun-inference")


@pytest.fixture
def runner():
    return CliRunner()


def process_result(
    batch_id: str = "cli-batch-20250102-030405",
    queued: int = 3,
    uploaded: int = 0,
    failed: int = 0,
) -> BatchProcessResult:
    """The real SDK model, so a field the command reads by the wrong name raises."""
    return BatchProcessResult(
        batch_id=batch_id,
        document_ids=[f"{batch_id}/doc{i}.pdf" for i in range(queued)],
        queued=queued,
        uploaded=uploaded,
        failed=failed,
        source="./docs",
        output_prefix="cli-batch",
        timestamp=datetime(2025, 1, 2, 3, 4, 5),
    )


def reprocess_result(
    queued: int = 2, failed: int = 0, failed_documents=None
) -> BatchReprocessResult:
    return BatchReprocessResult(
        documents_queued=queued,
        documents_failed=failed,
        failed_documents=failed_documents or [],
        step="extraction",
    )


def patched_client(result=None):
    """Patch `idp_sdk.IDPClient` and pre-load `batch.process` with `result`.

    `_process_impl` does `from idp_sdk import IDPClient` inside the function body, so
    the module attribute is the seam. Returns the patcher; use it as a context manager
    and read `mock_cls.return_value` for the client.
    """
    patcher = patch("idp_sdk.IDPClient")
    mock_cls = patcher.start()
    client = MagicMock()
    client.batch.process.return_value = result or process_result()
    mock_cls.return_value = client
    return patcher, mock_cls, client


class TestInputSourceValidation:
    @pytest.mark.parametrize("command", PROCESS_COMMANDS)
    def test_no_input_source_is_refused_before_anything_is_submitted(
        self, runner, command, api_calls
    ):
        """Exit 1, a message naming all four options, and no AWS call whatsoever.

        The validation runs ahead of `from idp_sdk import IDPClient`, so this also
        pins that a mistyped invocation costs nothing: no client is constructed, no
        stack is described, nothing is uploaded.
        """
        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(cli, [command, "--stack-name", "my-stack"])

        assert result.exit_code == 1
        assert (
            "Error: Must specify one of: --manifest, --dir, --s3-uri, or --test-set"
            in result.output
        )
        assert mock_cls.called is False
        assert api_calls.operations() == []

    @pytest.mark.parametrize("command", PROCESS_COMMANDS)
    @pytest.mark.parametrize(
        "extra",
        [
            ["--manifest", "MANIFEST", "--dir", "DIR"],
            ["--manifest", "MANIFEST", "--s3-uri", "s3://bucket/prefix/"],
            ["--manifest", "MANIFEST", "--test-set", "fcc-example"],
            ["--dir", "DIR", "--s3-uri", "s3://bucket/prefix/"],
            ["--dir", "DIR", "--test-set", "fcc-example"],
            ["--s3-uri", "s3://bucket/prefix/", "--test-set", "fcc-example"],
        ],
        ids=[
            "manifest+dir",
            "manifest+s3",
            "manifest+testset",
            "dir+s3",
            "dir+testset",
            "s3+testset",
        ],
    )
    def test_two_input_sources_are_refused(
        self, runner, tmp_path, command, extra, api_calls
    ):
        """Every pair, because the check counts sources rather than testing pairs.

        `--manifest` and `--dir` are `click.Path(exists=True)`, so the placeholders
        are substituted for a real file and a real directory — otherwise click would
        reject the path first and this would pass without the command's own check
        running.
        """
        manifest = tmp_path / "docs.csv"
        manifest.write_text("path\n", encoding="utf-8")
        directory = tmp_path / "documents"
        directory.mkdir()
        args = [
            str(manifest) if a == "MANIFEST" else str(directory) if a == "DIR" else a
            for a in extra
        ]

        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(cli, [command, "--stack-name", "my-stack", *args])

        assert result.exit_code == 1
        assert "Error: Cannot specify multiple input sources" in result.output
        assert mock_cls.called is False
        assert api_calls.operations() == []

    @pytest.mark.parametrize("command", PROCESS_COMMANDS)
    def test_all_four_sources_at_once_is_refused(self, runner, tmp_path, command):
        manifest = tmp_path / "docs.csv"
        manifest.write_text("path\n", encoding="utf-8")
        directory = tmp_path / "documents"
        directory.mkdir()

        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(
                cli,
                [
                    command,
                    "--stack-name",
                    "my-stack",
                    "--manifest",
                    str(manifest),
                    "--dir",
                    str(directory),
                    "--s3-uri",
                    "s3://bucket/prefix/",
                    "--test-set",
                    "fcc-example",
                ],
            )

        assert result.exit_code == 1
        assert "Cannot specify multiple input sources" in result.output
        assert mock_cls.called is False

    def test_a_missing_manifest_path_is_refused_by_click_itself(self, runner):
        """`--manifest` is `click.Path(exists=True)`, so this is exit 2, not exit 1.

        Worth distinguishing: exit 2 is click's usage error and comes with the usage
        block, whereas the command's own refusals above are exit 1. A script that
        keys on the exit code sees two different failures here.
        """
        result = runner.invoke(
            cli, ["process", "--stack-name", "my-stack", "--manifest", "no-such.csv"]
        )

        assert result.exit_code == 2
        assert "no-such.csv" in result.output


class TestInputSourceDispatch:
    def test_the_manifest_path_sends_the_manifest_and_no_scan_options(
        self, runner, tmp_path
    ):
        """Exact keyword set: `file_pattern` and `recursive` are not part of it.

        A manifest lists its documents, so passing scan options down this branch
        would be meaningless — and silently accepted. Asserting the whole dictionary
        is what makes that visible.
        """
        manifest = tmp_path / "docs.csv"
        manifest.write_text("path\n", encoding="utf-8")
        patcher, mock_cls, client = patched_client()
        try:
            result = runner.invoke(
                cli,
                [
                    "process",
                    "--stack-name",
                    "my-stack",
                    "--manifest",
                    str(manifest),
                    "--batch-id",
                    "my-experiment-v1",
                    "--number-of-files",
                    "5",
                    "--config-profile",
                    "lending",
                    "--config-revision",
                    "7",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        call = client.batch.process.call_args
        assert call.args == ()
        assert call.kwargs == {
            "manifest": str(manifest),
            "batch_prefix": "cli-batch",
            "batch_id": "my-experiment-v1",
            "number_of_files": 5,
            "config_version": "lending",
            "config_revision": 7,
        }

    def test_the_directory_path_sends_the_scan_options(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            result = runner.invoke(
                cli,
                [
                    "process",
                    "--stack-name",
                    "my-stack",
                    "--dir",
                    str(directory),
                    "--file-pattern",
                    "invoice*.pdf",
                    "--no-recursive",
                    "--batch-prefix",
                    "nightly",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert client.batch.process.call_args.kwargs == {
            "directory": str(directory),
            "file_pattern": "invoice*.pdf",
            "recursive": False,
            "batch_prefix": "nightly",
            "batch_id": None,
            "number_of_files": None,
            "config_version": None,
            "config_revision": None,
        }

    def test_the_directory_path_defaults_to_recursive_pdf_scanning(
        self, runner, tmp_path
    ):
        """The two scan defaults are documented in `--help` and are easy to invert."""
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        kwargs = client.batch.process.call_args.kwargs
        assert kwargs["file_pattern"] == "*.pdf"
        assert kwargs["recursive"] is True

    def test_the_s3_path_sends_the_uri_and_the_scan_options(self, runner):
        """No `exists=True` on `--s3-uri`, so the URI is passed through verbatim."""
        patcher, mock_cls, client = patched_client()
        try:
            result = runner.invoke(
                cli,
                [
                    "process",
                    "--stack-name",
                    "my-stack",
                    "--s3-uri",
                    "s3://data-lake/archive/2024/",
                    "--file-pattern",
                    "*.tif",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert client.batch.process.call_args.kwargs == {
            "s3_uri": "s3://data-lake/archive/2024/",
            "file_pattern": "*.tif",
            "recursive": True,
            "batch_prefix": "cli-batch",
            "batch_id": None,
            "number_of_files": None,
            "config_version": None,
            "config_revision": None,
        }

    def test_the_stack_name_and_region_reach_the_client_constructor(
        self, runner, tmp_path
    ):
        """`--region` must reach `IDPClient`, not be left to ambient resolution.

        The conftest removes `AWS_PROFILE` and points the config file at
        `os.devnull`, so nothing else could supply a region here — an assertion that
        passes for the wrong reason is the thing being avoided.
        """
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            runner.invoke(
                cli,
                [
                    "process",
                    "--stack-name",
                    "my-stack",
                    "--dir",
                    str(directory),
                    "--region",
                    "eu-west-2",
                ],
            )
        finally:
            patcher.stop()

        mock_cls.assert_called_once_with(stack_name="my-stack", region="eu-west-2")

    def test_no_region_option_leaves_the_region_unset(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        mock_cls.assert_called_once_with(stack_name="my-stack", region=None)

    def test_run_inference_is_the_same_body_as_process(self, runner, tmp_path):
        """The deprecated alias must not drift: same keywords, same client call."""
        directory = tmp_path / "documents"
        directory.mkdir()
        seen = []
        for command in PROCESS_COMMANDS:
            patcher, mock_cls, client = patched_client()
            try:
                result = runner.invoke(
                    cli,
                    [
                        command,
                        "--stack-name",
                        "my-stack",
                        "--dir",
                        str(directory),
                        "--number-of-files",
                        "2",
                    ],
                )
            finally:
                patcher.stop()
            assert result.exit_code == 0, result.output
            seen.append(client.batch.process.call_args.kwargs)

        assert seen[0] == seen[1]

    def test_run_inference_help_says_it_is_deprecated(self, runner):
        result = runner.invoke(cli, ["run-inference", "--help"])

        assert result.exit_code == 0
        assert "DEPRECATED" in result.output
        assert "idp-cli process" in result.output

    def test_the_config_option_is_accepted_and_then_ignored(self, runner, tmp_path):
        """DEFECT, pinned as it behaves today (`cli.py:1659`, `1707-1738`).

        `--config` is declared on both `process` and `run-inference`, typed as an
        existing path, and documented as "Path to configuration YAML file". The
        parameter arrives in `_process_impl` and is never read again: it is not
        passed to `client.batch.process`, which does accept a `config_path`, and
        `BatchProcessor` does act on it. So a caller who submits a batch with
        `--config ./bank-statement.yaml` gets a silent, full-price run under the
        stack's existing configuration, with nothing in the output to say the file
        was disregarded. Compare `--config-profile`, which does reach the SDK.
        """
        directory = tmp_path / "documents"
        directory.mkdir()
        config = tmp_path / "config.yaml"
        # Deliberately not valid YAML: the command succeeds anyway, which is the
        # sharpest available evidence that nothing ever opens the file.
        config.write_text("this: is: not: yaml: [", encoding="utf-8")
        patcher, mock_cls, client = patched_client()
        try:
            result = runner.invoke(
                cli,
                [
                    "process",
                    "--stack-name",
                    "my-stack",
                    "--dir",
                    str(directory),
                    "--config",
                    str(config),
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        kwargs = client.batch.process.call_args.kwargs
        assert "config" not in kwargs
        assert "config_path" not in kwargs
        assert str(config) not in str(kwargs)
        # No warning either: the output is the ordinary submission summary.
        assert "Batch ID:" in result.output


class TestSubmissionOutput:
    def test_the_batch_id_and_queued_count_are_always_reported(self, runner, tmp_path):
        """The batch id is the only handle a caller has for every later command."""
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client(
            process_result(batch_id="cli-batch-xyz", queued=3)
        )
        try:
            result = runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert "Batch ID: cli-batch-xyz" in result.output
        assert "Documents queued: 3" in result.output

    def test_upload_and_failure_lines_appear_only_when_non_zero(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()

        patcher, mock_cls, client = patched_client(
            process_result(queued=4, uploaded=4, failed=2)
        )
        try:
            noisy = runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        patcher, mock_cls, client = patched_client(
            process_result(queued=4, uploaded=0, failed=0)
        )
        try:
            quiet = runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        assert "Files uploaded: 4" in noisy.output
        assert "Files failed: 2" in noisy.output
        assert "Files uploaded" not in quiet.output
        assert "Files failed" not in quiet.output
        # A partial failure is still exit 0: the batch was submitted.
        assert noisy.exit_code == 0

    def test_an_sdk_failure_is_reported_and_exits_one(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.process.side_effect = RuntimeError("InputBucket does not exist")
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli, ["process", "--stack-name", "my-stack", "--dir", str(directory)]
            )
        finally:
            patcher.stop()

        assert result.exit_code == 1
        assert "Error: InputBucket does not exist" in result.output


class TestMonitorFlag:
    def test_monitor_hands_the_client_and_batch_id_to_the_monitor(
        self, runner, tmp_path
    ):
        """The monitor is given the already-built client, not a stack name.

        `_monitor_progress` treats a non-client first argument as a legacy stack
        name and builds its own client from it, so passing the wrong thing here does
        not fail — it silently doubles the stack lookups and watches the right batch
        anyway. Hence an assertion on the keywords.
        """
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client(
            process_result(batch_id="cli-batch-xyz", queued=2)
        )
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                result = runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--dir",
                        str(directory),
                        "--monitor",
                        "--refresh-interval",
                        "11",
                    ],
                )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        monitor.assert_called_once_with(
            client=client, batch_id="cli-batch-xyz", refresh_interval=11
        )

    def test_without_monitor_nothing_is_watched(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                runner.invoke(
                    cli,
                    ["process", "--stack-name", "my-stack", "--dir", str(directory)],
                )
        finally:
            patcher.stop()

        assert monitor.called is False

    def test_monitor_is_skipped_when_nothing_was_queued(self, runner, tmp_path):
        """Nothing queued means nothing to watch, and the loop would wait 60s for it.

        `_monitor_progress` treats an empty batch as `all_complete` immediately but
        holds for its 60-second grace period before believing it, so monitoring a
        zero-document submission would hang for a minute and then print an empty
        summary.
        """
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client(process_result(queued=0))
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                result = runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--dir",
                        str(directory),
                        "--monitor",
                    ],
                )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert "Documents queued: 0" in result.output
        assert monitor.called is False

    def test_the_default_refresh_interval_is_five_seconds(self, runner, tmp_path):
        directory = tmp_path / "documents"
        directory.mkdir()
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--dir",
                        str(directory),
                        "--monitor",
                    ],
                )
        finally:
            patcher.stop()

        assert monitor.call_args.kwargs["refresh_interval"] == 5


class TestTestSetPath:
    """`--test-set` is a different code path: a Lambda-driven test run, not an upload."""

    def test_the_test_set_path_bypasses_batch_process_entirely(self, runner):
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._process_test_set") as process_test_set:
                process_test_set.return_value = {
                    "batch_id": "test-run-42",
                    "documents_queued": 6,
                    "queued": 6,
                    "uploaded": 0,
                    "failed": 0,
                }
                result = runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--test-set",
                        "fcc-example-test",
                        "--context",
                        "Experiment v2.1",
                        "--number-of-files",
                        "6",
                        "--config-profile",
                        "v2",
                    ],
                )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert client.batch.process.called is False
        process_test_set.assert_called_once_with(
            stack_name="my-stack",
            test_set_name="fcc-example-test",
            context="Experiment v2.1",
            region=None,
            client=client,
            number_of_files=6,
            config_version="v2",
        )
        assert "Batch ID: test-run-42" in result.output
        assert "Documents queued: 6" in result.output

    def test_a_pinned_config_revision_is_dropped_on_the_test_set_path(self, runner):
        """DEFECT, pinned as it behaves today (`cli.py:1690-1698`).

        `_process_test_set` takes a `config_revision` parameter and forwards it to the
        test-runner Lambda payload, and `_process_impl` does not pass it. So
        `process --test-set ts --config-profile v2 --config-revision 7` runs under
        whatever v2 currently holds, while the same flags on `--dir`, `--manifest`
        and `--s3-uri` do pin the revision. The run is then recorded and compared as
        if it were r7. That is the exact failure mode `test_config_revision.py`'s
        module docstring describes — "a silently dropped revision is worse than a
        rejected one" — surviving on the one path it was not checked on.
        """
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._process_test_set") as process_test_set:
                process_test_set.return_value = {
                    "batch_id": "test-run-42",
                    "queued": 1,
                    "uploaded": 0,
                    "failed": 0,
                }
                result = runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--test-set",
                        "fcc-example-test",
                        "--config-profile",
                        "v2",
                        "--config-revision",
                        "7",
                    ],
                )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        kwargs = process_test_set.call_args.kwargs
        assert kwargs["config_version"] == "v2"
        assert "config_revision" not in kwargs
        assert "7" not in result.output

    def test_the_test_set_counts_come_from_the_legacy_dict(self, runner):
        """The test-set path returns a dict, so the fields are read by key, not attribute.

        `queued` is preferred and `documents_queued` is the fallback; the two are set
        to different values here so a swap of the precedence shows up.
        """
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._process_test_set") as process_test_set:
                process_test_set.return_value = {
                    "batch_id": "test-run-42",
                    "queued": 6,
                    "documents_queued": 99,
                    "uploaded": 2,
                    "failed": 1,
                }
                result = runner.invoke(
                    cli,
                    [
                        "process",
                        "--stack-name",
                        "my-stack",
                        "--test-set",
                        "fcc-example-test",
                    ],
                )
        finally:
            patcher.stop()

        assert "Documents queued: 6" in result.output
        assert "Documents queued: 99" not in result.output
        assert "Files uploaded: 2" in result.output
        assert "Files failed: 1" in result.output

    def test_a_test_set_failure_is_reported_and_exits_one(self, runner):
        patcher, mock_cls, client = patched_client()
        try:
            with patch("idp_cli.cli._process_test_set") as process_test_set:
                process_test_set.side_effect = RuntimeError("test set not found")
                result = runner.invoke(
                    cli, ["process", "--stack-name", "my-stack", "--test-set", "nope"]
                )
        finally:
            patcher.stop()

        assert result.exit_code == 1
        assert "Error: test set not found" in result.output


class TestReprocessReachesTheSharedImplementation:
    """`reprocess` runs, and runs the same body its deprecated alias runs.

    The defect these pin is that `reprocess` used to end with
    `return rerun_inference(...)`, and `rerun_inference` is not a function — it is
    the `click.Command` the `@cli.command` decorator left behind. Calling a
    `Command` invokes `Command.main()`, which takes at most five arguments, so the
    eight forwarded ones raised `TypeError` before any of the command's own code
    ran and `idp-cli reprocess` had never worked at all. The fix routes it to
    `_rerun_inference_impl`, the plain function `rerun-inference` already called.

    Every test here goes through the `cli` group with the command *named*, rather
    than calling the implementation, because the defect was entirely in that
    wiring: a test that called `_rerun_inference_impl` directly passed throughout.
    """

    def test_reprocess_runs_and_submits_the_documents(self, runner):
        """Exit 0 and a real reprocess call — the shape that was unreachable."""
        patcher, mock_cls, client = patched_client()
        client.batch.reprocess.return_value = reprocess_result(queued=1)
        try:
            result = runner.invoke(
                cli,
                [
                    "reprocess",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "batch-1/doc.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert result.exception is None, result.exception
        client.batch.reprocess.assert_called_once_with(
            step="extraction",
            document_ids=["batch-1/doc.pdf"],
            batch_id=None,
        )
        assert "Queued 1 documents for extraction reprocessing" in result.output

    @pytest.mark.parametrize("command", REPROCESS_COMMANDS)
    def test_both_spellings_make_the_same_sdk_call(self, runner, command):
        """The documented name and the deprecated one are interchangeable.

        This is the property the fix is for: `reprocess` is what `--help` and
        `docs/idp-cli.md` point users at, and it must do what `rerun-inference`
        does rather than being a second, differently-behaving path. Parametrising
        one test over both spellings is what makes a future divergence fail.
        """
        patcher, mock_cls, client = patched_client()
        client.batch.reprocess.return_value = reprocess_result(queued=2)
        try:
            result = runner.invoke(
                cli,
                [
                    command,
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                    "--document-ids",
                    "batch-1/a.pdf, batch-1/b.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert mock_cls.call_args.kwargs == {"stack_name": "my-stack", "region": None}
        client.batch.reprocess.assert_called_once_with(
            step="classification",
            document_ids=["batch-1/a.pdf", "batch-1/b.pdf"],
            batch_id=None,
        )

    def test_reprocess_reaches_its_own_input_validation(self, runner, api_calls):
        """The refusal `_rerun_inference_impl` prints is now reachable.

        Through the broken wiring the `TypeError` was raised before the body, so
        neither `--document-ids` nor `--batch-id` produced no message at all. The
        refusal must also cost nothing: no client, no AWS call.
        """
        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(
                cli,
                ["reprocess", "--stack-name", "my-stack", "--step", "classification"],
            )

        assert result.exit_code == 1
        assert not isinstance(result.exception, TypeError), result.exception
        assert (
            "Error: Must specify either --document-ids or --batch-id" in result.output
        )
        assert mock_cls.called is False
        assert api_calls.operations() == []

    def test_its_help_still_works(self, runner):
        """The option surface, unchanged by the rewiring of the body."""
        result = runner.invoke(cli, ["reprocess", "--help"])

        assert result.exit_code == 0
        for option in (
            "--step",
            "--document-ids",
            "--batch-id",
            "--force",
            "--monitor",
        ):
            assert option in result.output


class TestRerunSelection:
    """`rerun-inference` is the spelling that reaches `_rerun_inference_impl` today."""

    def test_neither_document_ids_nor_batch_id_is_refused(self, runner, api_calls):
        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                ],
            )

        assert result.exit_code == 1
        assert (
            "Error: Must specify either --document-ids or --batch-id" in result.output
        )
        assert mock_cls.called is False
        assert api_calls.operations() == []

    def test_both_document_ids_and_batch_id_is_refused(self, runner, api_calls):
        with patch("idp_sdk.IDPClient") as mock_cls:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                    "--document-ids",
                    "batch-1/doc.pdf",
                    "--batch-id",
                    "batch-1",
                ],
            )

        assert result.exit_code == 1
        assert (
            "Error: Cannot specify both --document-ids and --batch-id" in result.output
        )
        assert mock_cls.called is False
        assert api_calls.operations() == []

    def test_an_unknown_step_is_refused_by_the_choice_type(self, runner):
        """Only `classification` and `extraction` clear different amounts of state."""
        result = runner.invoke(
            cli,
            [
                "rerun-inference",
                "--stack-name",
                "my-stack",
                "--step",
                "ocr",
                "--document-ids",
                "batch-1/doc.pdf",
            ],
        )

        assert result.exit_code == 2
        assert "ocr" in result.output

    def test_a_document_id_list_is_split_and_stripped(self, runner):
        """Whitespace around a comma is ordinary when the list is pasted from a report.

        The list is passed to the SDK explicitly with `batch_id=None`, which is what
        tells the SDK not to re-resolve the selection itself.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=3)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    " batch-1/a.pdf , batch-1/b.pdf,batch-1/c.pdf ",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        client.batch.reprocess.assert_called_once_with(
            step="extraction",
            document_ids=["batch-1/a.pdf", "batch-1/b.pdf", "batch-1/c.pdf"],
            batch_id=None,
        )
        assert client.batch.get_document_ids.called is False
        assert "Processing 3 specified documents" in result.output

    def test_a_batch_id_is_expanded_into_an_explicit_document_list(self, runner):
        """The batch is resolved here and the resolved list is what the SDK is given.

        `batch_id=None` in the reprocess call is deliberate — passing the batch id
        through would make the SDK fetch the list a second time, and the count the
        user was shown in the confirmation prompt would then not be the count that
        was acted on.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.get_document_ids.return_value = [
            "batch-1/a.pdf",
            "batch-1/b.pdf",
        ]
        client.batch.reprocess.return_value = reprocess_result(queued=2)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                    "--batch-id",
                    "cli-batch-20251015-143000",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        client.batch.get_document_ids.assert_called_once_with(
            "cli-batch-20251015-143000"
        )
        client.batch.reprocess.assert_called_once_with(
            step="classification",
            document_ids=["batch-1/a.pdf", "batch-1/b.pdf"],
            batch_id=None,
        )
        assert "Found 2 documents in batch" in result.output

    def test_an_empty_batch_is_still_submitted_with_an_empty_list(self, runner):
        """Pinned as it behaves today: a selection of nothing is not short-circuited.

        A `--batch-id` that resolves to no documents prints "Found 0 documents in
        batch", asks "Reprocess 0 documents from extraction step?", and on
        confirmation calls the SDK with an empty list. Nothing is harmed — the SDK
        queues nothing and the command reports nothing queued — but the user is
        prompted to confirm an operation with no subject, and the exit code is 0,
        which a script cannot distinguish from a reprocess that did work.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.get_document_ids.return_value = []
        client.batch.reprocess.return_value = reprocess_result(queued=0)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--batch-id",
                    "empty-batch",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert "Found 0 documents in batch" in result.output
        client.batch.reprocess.assert_called_once_with(
            step="extraction", document_ids=[], batch_id=None
        )
        # Nothing queued, so no "Queued N documents" line and no monitoring.
        assert "Queued" not in result.output

    def test_a_batch_lookup_failure_is_reported_and_exits_one(self, runner):
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.get_document_ids.side_effect = RuntimeError("no such batch")
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--batch-id",
                    "typo",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 1
        assert "Error: no such batch" in result.output
        assert client.batch.reprocess.called is False


class TestRerunConfirmation:
    def test_answering_no_starts_nothing(self, runner, api_calls):
        """The negative case read off the client, not off the printed message.

        This is the destructive command in the pair — classification reprocessing
        deletes every extraction result the documents have — so "declined" has to
        mean the SDK was never called, and no AWS call was made either.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                    "--document-ids",
                    "batch-1/a.pdf,batch-1/b.pdf",
                ],
                input="n\n",
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0
        assert "Rerun cancelled" in result.output
        assert client.batch.reprocess.called is False
        assert api_calls.operations() == []

    def test_the_prompt_names_the_count_and_the_step(self, runner):
        """A confirmation that does not say what it will do is not a confirmation."""
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "a.pdf,b.pdf,c.pdf",
                ],
                input="n\n",
            )
        finally:
            patcher.stop()

        assert "Reprocess 3 documents from extraction step?" in result.output

    def test_pressing_enter_accepts_because_the_default_is_yes(self, runner):
        """Pinned as it behaves today: `click.confirm(..., default=True)`.

        Enter on a prompt that clears extraction results is a destructive default.
        It is the current behaviour and the prompt shows `[Y/n]`, so it is at least
        visible; a test is here so that flipping it is a deliberate change.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=2)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "a.pdf,b.pdf",
                ],
                input="\n",
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert "[Y/n]" in result.output
        assert client.batch.reprocess.called is True

    def test_force_skips_the_prompt_entirely(self, runner):
        """With no stdin at all: a prompt would fail the invocation rather than pass."""
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=1)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "a.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0, result.output
        assert "Reprocess 1 documents" not in result.output
        assert client.batch.reprocess.called is True

    @pytest.mark.parametrize(
        ("step", "cleared", "kept"),
        [
            (
                "classification",
                [
                    "All page classifications",
                    "All document sections",
                    "All extraction results",
                ],
                ["OCR data (pages, images, text)"],
            ),
            (
                "extraction",
                ["Section extraction results", "Section attributes"],
                [
                    "OCR data (pages, images, text)",
                    "Page classifications",
                    "Document sections structure",
                ],
            ),
        ],
    )
    def test_the_prompt_says_what_each_step_destroys_and_keeps(
        self, runner, step, cleared, kept
    ):
        """This inventory is the only thing a user has to judge the prompt by.

        The two steps clear different amounts of state — classification throws away
        the extraction results as well — and the lists are printed before the
        confirmation, so getting them the wrong way round would invite the user to
        approve far more deletion than they intended.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        mock_cls.return_value = MagicMock()
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    step,
                    "--document-ids",
                    "a.pdf",
                ],
                input="n\n",
            )
        finally:
            patcher.stop()

        assert f"Rerun Step: {step}" in result.output
        for line in cleared + kept:
            assert line in result.output


class TestRerunResults:
    def test_a_queued_count_is_reported(self, runner):
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=4)
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "a.pdf,b.pdf,c.pdf,d.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert "Queued 4 documents for extraction reprocessing" in result.output

    def test_each_failed_document_is_named_with_its_error(self, runner):
        """A partial failure must say *which* documents, or the user cannot retry them.

        Note the exit code: a reprocess where every document failed to queue still
        exits 0. That is the current contract and it is what a script sees.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(
            queued=1,
            failed=2,
            failed_documents=[
                {"object_key": "batch-1/b.pdf", "error": "NoSuchKey"},
                {"object_key": "batch-1/c.pdf", "error": "AccessDenied"},
            ],
        )
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "batch-1/a.pdf,batch-1/b.pdf,batch-1/c.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 0
        assert "Failed to queue 2 documents" in result.output
        assert "batch-1/b.pdf: NoSuchKey" in result.output
        assert "batch-1/c.pdf: AccessDenied" in result.output

    def test_a_failure_part_way_through_exits_one(self, runner):
        """The documents already cleared stay cleared; only the exit code is asserted."""
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.side_effect = RuntimeError(
            "throttled after 3 of 10 documents"
        )
        mock_cls.return_value = client
        try:
            result = runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "classification",
                    "--document-ids",
                    "a.pdf,b.pdf",
                    "--force",
                ],
            )
        finally:
            patcher.stop()

        assert result.exit_code == 1
        assert "Error: throttled after 3 of 10 documents" in result.output

    def test_monitoring_a_document_id_rerun_watches_a_batch_called_rerun(self, runner):
        """Pinned as it behaves today (`cli.py:2286-2291`).

        With `--document-ids` there is no batch to watch, and the literal string
        `"rerun"` is passed as the batch id. `get_status("rerun")` then reports on
        whatever documents happen to match that substring — normally none — so
        `--monitor` on a document-id rerun watches an empty batch and sits through
        the monitor's 60-second grace period before printing a summary of nothing.
        With `--batch-id` the real batch id is used and monitoring works.
        """
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=2)
        mock_cls.return_value = client
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                runner.invoke(
                    cli,
                    [
                        "rerun-inference",
                        "--stack-name",
                        "my-stack",
                        "--step",
                        "extraction",
                        "--document-ids",
                        "a.pdf,b.pdf",
                        "--force",
                        "--monitor",
                    ],
                )
        finally:
            patcher.stop()

        monitor.assert_called_once_with(
            client=client, batch_id="rerun", refresh_interval=5
        )

    def test_monitoring_a_batch_rerun_watches_that_batch(self, runner):
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.get_document_ids.return_value = ["batch-7/a.pdf"]
        client.batch.reprocess.return_value = reprocess_result(queued=1)
        mock_cls.return_value = client
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                runner.invoke(
                    cli,
                    [
                        "rerun-inference",
                        "--stack-name",
                        "my-stack",
                        "--step",
                        "extraction",
                        "--batch-id",
                        "batch-7",
                        "--force",
                        "--monitor",
                        "--refresh-interval",
                        "9",
                    ],
                )
        finally:
            patcher.stop()

        monitor.assert_called_once_with(
            client=client, batch_id="batch-7", refresh_interval=9
        )

    def test_nothing_queued_means_nothing_monitored(self, runner):
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=0, failed=1)
        mock_cls.return_value = client
        try:
            with patch("idp_cli.cli._monitor_progress") as monitor:
                runner.invoke(
                    cli,
                    [
                        "rerun-inference",
                        "--stack-name",
                        "my-stack",
                        "--step",
                        "extraction",
                        "--batch-id",
                        "batch-7",
                        "--force",
                        "--monitor",
                    ],
                )
        finally:
            patcher.stop()

        assert monitor.called is False

    def test_the_region_reaches_the_client(self, runner):
        patcher = patch("idp_sdk.IDPClient")
        mock_cls = patcher.start()
        client = MagicMock()
        client.batch.reprocess.return_value = reprocess_result(queued=1)
        mock_cls.return_value = client
        try:
            runner.invoke(
                cli,
                [
                    "rerun-inference",
                    "--stack-name",
                    "my-stack",
                    "--step",
                    "extraction",
                    "--document-ids",
                    "a.pdf",
                    "--force",
                    "--region",
                    "us-west-2",
                ],
            )
        finally:
            patcher.stop()

        mock_cls.assert_called_once_with(stack_name="my-stack", region="us-west-2")
