# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the Test Studio reading commands: `test-result` and `test-compare`.

These two commands are how a Test Studio run is read from a terminal or a
pipeline: `test-result` reports one run's accuracy, cost and file counts, and
`test-compare` puts two or more runs side by side and writes a JSON and a CSV
export. Neither performs the evaluation — the SDK does — so what these tests
check is the part the CLI owns: which run ids it parses out of one
comma-separated option, what it refuses, how it formats a metric that is present
versus one that is absent, what it writes to disk, and what exit code it returns.

What shaped them:

- **The exported files are the contract, so they are read back off disk.** The CSV
  `test-compare` writes is consumed by spreadsheets and scripts, and a column in
  the wrong order or an `N/A` where a number belongs is invisible to a test that
  only checks the command exited 0. Every export test parses the file it wrote.
- **The metrics are deliberately asymmetric.** Each run in the fixtures gets a
  different value for every metric, and the two runs' values are never equal, so a
  defect that reads the wrong run's column, or that formats a percentage as a
  ratio, changes the assertion. Symmetric fixture data is how a swapped-argument
  bug survives a green suite.
- **A metric can be present with the value `None`.** `accuracy_breakdown` is an
  `Optional[float]` per key on any zero-denominator path, so `.get(k, 0)` does not
  help and `f"{None:.2%}"` raises. There is a dedicated formatter for that and
  both of its branches are covered.
- **Three guards in this area do not guard.** They are pinned as current behaviour
  with the consequence written out, not fixed; see the tests whose names say
  `defect`.

`abort-test-run` is covered by `tests/test_abort_test_runs.py`; only the one
branch that file leaves unreached is added here.
"""

import csv
import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli import cli as cli_module


@pytest.fixture
def runner():
    return CliRunner()


def _test_run_result(**overrides):
    """A `TestRunResult` as `client.testing.get_test_result` really returns one.

    Built from the real pydantic model, so a field the command reads that the
    model does not declare fails here rather than rendering as a `MagicMock`
    repr in the output.
    """
    from idp_sdk.models.testing import TestRunResult

    fields = {
        "test_run_id": "fake-w2-20260409-123456",
        "test_set_name": "fake-w2",
        "status": "COMPLETE",
        "files_count": 10,
        "completed_files": 10,
        "failed_files": 0,
        "overall_accuracy": 0.9375,
        "accuracy_breakdown": {"precision": 0.875, "recall": 0.75, "f1_score": 0.8125},
        "total_cost": 1.2345,
        "created_at": "2026-04-09T12:34:56Z",
        "completed_at": "2026-04-09T12:40:00Z",
        "raw_data": {"testRunId": "fake-w2-20260409-123456", "status": "COMPLETE"},
    }
    fields.update(overrides)
    return TestRunResult(**fields)


def _patched_client(**attrs):
    """Patch `idp_sdk.IDPClient` — these commands re-import it inside their bodies.

    Patching `idp_cli.cli.IDPClient` would have no effect here: `test-result`,
    `test-compare` and `abort-test-run` each do `from idp_sdk import IDPClient`
    inside the function, so the name is resolved from the source module at call
    time rather than from the module-level binding the other commands use.
    """
    client = MagicMock()
    for key, value in attrs.items():
        setattr(client.testing, key, value)
    return patch("idp_sdk.IDPClient", return_value=client), client


class TestTestResultRegionResolution:
    def test_an_explicit_region_is_used(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p as factory:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-result",
                    "--stack-name",
                    "IDP",
                    "--test-run-id",
                    "r1",
                    "--region",
                    "eu-central-1",
                ],
            )

        assert result.exit_code == 0, result.output
        factory.assert_called_once_with(stack_name="IDP", region="eu-central-1")

    def test_without_region_it_reads_aws_region(self, runner, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p as factory:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        factory.assert_called_once_with(stack_name="IDP", region="ap-southeast-2")

    def test_with_no_region_anywhere_it_falls_back_to_us_east_1(
        self, runner, monkeypatch
    ):
        """
        The fallback is a hardcoded `us-east-1`, not an error.

        Worth pinning because it is the surprising half: a Test Studio stack in
        `us-west-2`, read with no `--region` and no `AWS_REGION`, is looked for in
        `us-east-1` and reports "not found" rather than "no region configured".
        `AWS_DEFAULT_REGION` is deliberately not consulted here, unlike in `deploy`.
        """
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p as factory:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        factory.assert_called_once_with(stack_name="IDP", region="us-east-1")


class TestTestResultOutput:
    def test_the_metrics_are_rendered_as_percentages_and_dollars(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        assert "Test Run: fake-w2-20260409-123456" in result.output
        assert "Test Set: fake-w2" in result.output
        assert "Status: COMPLETE" in result.output
        assert "Files: 10/10 completed" in result.output
        # Distinct values per metric, so a swapped pair changes the assertion.
        assert "Overall Accuracy: 93.75%" in result.output
        assert "Precision: 87.50%" in result.output
        assert "Recall: 75.00%" in result.output
        assert "F1 Score: 81.25%" in result.output
        assert "Total Cost: $1.2345" in result.output
        assert "Created: 2026-04-09T12:34:56Z" in result.output
        assert "Completed: 2026-04-09T12:40:00Z" in result.output

    def test_failed_files_are_called_out_only_when_there_are_some(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(
                return_value=_test_run_result(failed_files=3, completed_files=7)
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )
        assert "Failed Files: 3" in result.output

        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p:
            clean = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )
        assert "Failed Files" not in clean.output

    def test_a_breakdown_key_present_with_value_none_renders_as_n_a(self, runner):
        """
        `accuracy_breakdown` puts `None` at a *present* key on any zero-denominator
        or error path, so `.get(key, 0)` returns `None` and `f"{None:.2%}"` raises
        `TypeError`. The command has a formatter for exactly that; this covers its
        non-numeric branch. A failure here is a traceback instead of a result table
        for any run where one metric could not be computed.
        """
        p, client = _patched_client(
            get_test_result=MagicMock(
                return_value=_test_run_result(
                    accuracy_breakdown={
                        "precision": None,
                        "recall": 0.5,
                        "f1_score": None,
                    }
                )
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        assert "Precision: N/A" in result.output
        assert "Recall: 50.00%" in result.output
        assert "F1 Score: N/A" in result.output

    def test_absent_optional_metrics_are_omitted_entirely(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(
                return_value=_test_run_result(
                    overall_accuracy=None,
                    accuracy_breakdown=None,
                    total_cost=None,
                    created_at=None,
                    completed_at=None,
                )
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        assert "Overall Accuracy" not in result.output
        assert "Precision" not in result.output
        assert "Total Cost" not in result.output
        assert "Created:" not in result.output
        assert "Completed:" not in result.output

    def test_defect_a_total_cost_of_exactly_zero_is_not_reported(self, runner):
        """
        DEFECT (pinned as current behaviour, not fixed). The cost line is guarded by
        `if test_result.total_cost:`, a truthiness test rather than an `is not None`
        test, so a run whose cost is genuinely `0.0` prints no cost line at all —
        indistinguishable from a run whose cost could not be determined.

        The observable consequence is narrow but real: a cached or aborted run that
        cost nothing reads as "cost unknown", and a pipeline that greps for the cost
        line to assert a budget sees nothing and concludes the field is missing.
        """
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result(total_cost=0.0))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        assert "Total Cost" not in result.output

    def test_defect_a_failed_test_run_still_exits_zero(self, runner):
        """
        DEFECT (pinned as current behaviour, not fixed). `test-result` reports the
        run's status but never lets it influence the exit code: a run with
        `status="FAILED"` and every file failed exits 0, exactly like a clean pass.

        The observable consequence is that `idp-cli test-result ... && deploy` in a
        pipeline proceeds on a failed evaluation. Reading the status out of the
        printed text is the only way a caller can tell, which defeats the purpose of
        having an exit code. Compare `status`, which does derive an exit code from
        the documents' states.
        """
        p, client = _patched_client(
            get_test_result=MagicMock(
                return_value=_test_run_result(
                    status="FAILED", completed_files=0, failed_files=10
                )
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0
        assert "Status: FAILED" in result.output
        assert "Failed Files: 10" in result.output


class TestTestResultWaitAndExport:
    def test_wait_announces_the_timeout_and_forwards_both(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-result",
                    "--stack-name",
                    "IDP",
                    "--test-run-id",
                    "r1",
                    "--wait",
                    "--timeout",
                    "900",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Waiting for test run to complete (up to 900s)" in result.output
        client.testing.get_test_result.assert_called_once_with(
            test_run_id="r1", wait=True, timeout=900, poll_interval=10
        )

    def test_without_wait_nothing_is_announced_and_the_defaults_are_forwarded(
        self, runner
    ):
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result())
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r1"],
            )

        assert result.exit_code == 0, result.output
        assert "Waiting for test run" not in result.output
        client.testing.get_test_result.assert_called_once_with(
            test_run_id="r1", wait=False, timeout=600, poll_interval=10
        )

    def test_output_dir_writes_the_raw_payload_and_creates_the_directory(
        self, runner, tmp_path
    ):
        """
        The exported file is `raw_data`, not the parsed model, so a consumer sees the
        full service response. Read back and parsed, because the value of the export
        is that `json.load` accepts it: a `default=str` fallback that silently
        stringified the whole payload would still write a file and still exit 0.
        """
        out = tmp_path / "nested" / "results"
        raw = {"testRunId": "r1", "status": "COMPLETE", "metrics": {"a": 1}}
        p, client = _patched_client(
            get_test_result=MagicMock(return_value=_test_run_result(raw_data=raw))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-result",
                    "--stack-name",
                    "IDP",
                    "--test-run-id",
                    "r1",
                    "--output-dir",
                    str(out),
                ],
            )

        assert result.exit_code == 0, result.output
        written = out / "r1-result.json"
        assert written.is_file()
        assert json.loads(written.read_text(encoding="utf-8")) == raw
        assert f"Results saved to: {written}" in result.output

    def test_an_error_from_the_sdk_exits_one_with_the_message(self, runner):
        p, client = _patched_client(
            get_test_result=MagicMock(side_effect=RuntimeError("test run r9 not found"))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-result", "--stack-name", "IDP", "--test-run-id", "r9"],
            )

        assert result.exit_code == 1
        assert "test run r9 not found" in result.output


def _comparison(metrics):
    from idp_sdk.models.testing import TestComparisonResult

    return TestComparisonResult(metrics=metrics)


#: Two runs whose every metric differs from the other's, so a defect that reads the
#: wrong run's column changes an assertion. `run-b` is worse on accuracy and better
#: on cost, so a sign error in any derived figure is visible too.
TWO_RUNS = {
    "run-a": {
        "overallAccuracy": 0.9,
        "accuracyBreakdown": {"precision": 0.8, "recall": 0.7, "f1_score": 0.75},
        "totalCost": 2.5,
        "completedFiles": 10,
        "failedFiles": 0,
    },
    "run-b": {
        "overallAccuracy": 0.6,
        "accuracyBreakdown": {"precision": 0.5, "recall": 0.4, "f1_score": 0.45},
        "totalCost": 1.25,
        "completedFiles": 8,
        "failedFiles": 2,
    },
}


class TestTestCompareRefusals:
    def test_a_single_run_id_is_refused_without_building_a_client(self, runner):
        with patch("idp_sdk.IDPClient") as factory:
            result = runner.invoke(
                cli_module.cli,
                ["test-compare", "--stack-name", "IDP", "--test-run-ids", "run-a"],
            )

        assert result.exit_code == 1
        assert "At least 2 test run IDs required" in result.output
        factory.assert_not_called()

    def test_empty_metrics_exit_one(self, runner):
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison({}))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-compare", "--stack-name", "IDP", "--test-run-ids", "a,b"],
            )

        assert result.exit_code == 1
        assert "No metrics data available for comparison" in result.output

    def test_defect_a_trailing_comma_passes_the_two_id_check_with_one_real_id(
        self, runner
    ):
        """
        DEFECT (pinned as current behaviour, not fixed). The ids are parsed with a
        bare `split(",")` and only counted, never validated, so
        `--test-run-ids "run-a,"` yields `["run-a", ""]`, passes the
        "at least 2" check, and asks the service to compare `run-a` against a run
        whose id is the empty string.

        The observable consequence is a comparison table with a blank column header
        and `N/A` down every row, presented as a successful comparison with exit 0 —
        rather than the "at least 2 test run IDs required" message the user should
        have seen for what is really one id.
        """
        p, client = _patched_client(
            compare_test_runs=MagicMock(
                return_value=_comparison({"run-a": TWO_RUNS["run-a"]})
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-compare", "--stack-name", "IDP", "--test-run-ids", "run-a,"],
            )

        assert result.exit_code == 0, result.output
        client.testing.compare_test_runs.assert_called_once_with(
            test_run_ids=["run-a", ""]
        )
        assert "At least 2 test run IDs required" not in result.output

    def test_an_error_from_the_sdk_exits_one(self, runner):
        p, client = _patched_client(
            compare_test_runs=MagicMock(side_effect=RuntimeError("stack not found"))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["test-compare", "--stack-name", "IDP", "--test-run-ids", "a,b"],
            )

        assert result.exit_code == 1
        assert "stack not found" in result.output


class TestTestCompareRendering:
    def test_ids_are_stripped_and_forwarded_in_order(self, runner):
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison(TWO_RUNS))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    " run-a , run-b ",
                ],
            )

        assert result.exit_code == 0, result.output
        client.testing.compare_test_runs.assert_called_once_with(
            test_run_ids=["run-a", "run-b"]
        )
        assert "Comparing 2 test runs" in result.output

    def test_the_table_carries_both_runs_values_in_their_own_columns(self, runner):
        """
        The table is the primary output and the column order follows the order the
        ids were given. Both runs' figures are asserted because a defect that read
        `metrics[test_run_id_list[0]]` for every column would render a plausible
        table of the first run repeated.
        """
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison(TWO_RUNS))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "90.00%" in result.output and "60.00%" in result.output
        assert "80.00%" in result.output and "50.00%" in result.output
        assert "70.00%" in result.output and "40.00%" in result.output
        assert "75.00%" in result.output and "45.00%" in result.output
        assert "$2.5000" in result.output and "$1.2500" in result.output

    def test_a_run_absent_from_the_metrics_renders_n_a_rather_than_failing(
        self, runner
    ):
        """
        An id the service returned no metrics for must degrade to `N/A` per cell, not
        raise. The nested-path walk is what does that, and it has to survive both a
        missing run and a missing nested key.
        """
        p, client = _patched_client(
            compare_test_runs=MagicMock(
                return_value=_comparison(
                    {
                        "run-a": {"overallAccuracy": 0.9},  # no breakdown, no cost
                        # run-b absent entirely
                    }
                )
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "90.00%" in result.output
        assert "N/A" in result.output

    def test_defect_configuration_differences_can_never_be_shown(self, runner):
        """
        DEFECT (pinned as current behaviour, not fixed). The command's help promises
        "configuration differences between test runs", and there is a whole table
        builder for them — but `configs` is assigned the literal `[]` with a `TODO`
        and is never populated from the comparison result, so `if configs and ...`
        is unreachable and the branch that builds that table is dead code.

        The observable consequence is that `test-compare` always prints
        "No configuration differences to display", including for two runs that
        differ in model, prompt or confidence settings — which is the single most
        useful thing to know when two runs score differently. The message reads as
        "the runs are configured identically", which is a stronger and wrong claim.

        This test therefore asserts the *absence* of the table for input that, were
        the data wired through, would produce one. It is also why the dead branch is
        left uncovered rather than reached by a contrived test.
        """
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison(TWO_RUNS))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "No configuration differences to display" in result.output
        assert "Configuration Differences" not in result.output

    def test_long_run_ids_are_truncated_in_the_table_header(self, runner):
        """Column headers are cut to 20 characters; the CSV uses 30. Both are pinned
        because the two limits are easy to transpose, and a 30-character header in a
        200-column table is a different layout than the one this was tuned for."""
        long_a = "fake-w2-20260409-123456-extra-long-suffix-a"
        long_b = "fake-w2-20260409-123456-extra-long-suffix-b"
        p, client = _patched_client(
            compare_test_runs=MagicMock(
                return_value=_comparison(
                    {long_a: TWO_RUNS["run-a"], long_b: TWO_RUNS["run-b"]}
                )
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    f"{long_a},{long_b}",
                ],
            )

        assert result.exit_code == 0, result.output
        assert long_a[:20] in result.output
        assert long_a not in result.output


class TestTestCompareExport:
    def test_the_csv_has_seven_metric_rows_in_a_fixed_order(self, runner, tmp_path):
        """
        The CSV is a machine-readable export, so its header and row order are the
        contract. Seven metrics are written — two more than the terminal table shows
        (`Files Completed` and `Files Failed`), which is deliberate and is the kind
        of divergence a test that only read the table would miss.

        Values are asserted per cell against the asymmetric fixture, so reading the
        wrong run's column or dropping a row fails here.
        """
        out = tmp_path / "comparisons"
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison(TWO_RUNS))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                    "--output-dir",
                    str(out),
                ],
            )

        assert result.exit_code == 0, result.output
        csv_files = sorted(out.glob("comparison-*.csv"))
        assert len(csv_files) == 1
        with csv_files[0].open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))

        assert rows[0] == ["Metric", "run-a", "run-b"]
        assert [row[0] for row in rows[1:]] == [
            "Overall Accuracy",
            "Precision",
            "Recall",
            "F1 Score",
            "Total Cost",
            "Files Completed",
            "Files Failed",
        ]
        # Floats get four decimal places; integers are written as-is.
        assert rows[1] == ["Overall Accuracy", "0.9000", "0.6000"]
        assert rows[2] == ["Precision", "0.8000", "0.5000"]
        assert rows[5] == ["Total Cost", "2.5000", "1.2500"]
        assert rows[6] == ["Files Completed", "10", "8"]
        assert rows[7] == ["Files Failed", "0", "2"]

    def test_the_csv_writes_n_a_for_a_missing_metric(self, runner, tmp_path):
        out = tmp_path / "comparisons"
        p, client = _patched_client(
            compare_test_runs=MagicMock(
                return_value=_comparison({"run-a": {"overallAccuracy": 0.5}})
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                    "--output-dir",
                    str(out),
                ],
            )

        assert result.exit_code == 0, result.output
        with sorted(out.glob("*.csv"))[0].open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows[1] == ["Overall Accuracy", "0.5000", "N/A"]
        assert rows[2] == ["Precision", "N/A", "N/A"]

    def test_the_json_export_round_trips_the_metrics(self, runner, tmp_path):
        out = tmp_path / "comparisons"
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison(TWO_RUNS))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "run-a,run-b",
                    "--output-dir",
                    str(out),
                ],
            )

        assert result.exit_code == 0, result.output
        json_files = sorted(out.glob("comparison-*.json"))
        assert len(json_files) == 1
        payload = json.loads(json_files[0].read_text(encoding="utf-8"))
        assert payload["metrics"] == TWO_RUNS
        # `configs` is exported as an empty list for the same reason the
        # configuration-differences table never renders — see the defect test above.
        assert payload["configs"] == []
        assert "Comparison JSON saved to" in result.output
        assert "Comparison CSV saved to" in result.output

    def test_the_csv_header_truncates_ids_at_thirty_characters(self, runner, tmp_path):
        out = tmp_path / "comparisons"
        long_a = "a" * 40
        p, client = _patched_client(
            compare_test_runs=MagicMock(return_value=_comparison({long_a: {}}))
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "test-compare",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    f"{long_a},run-b",
                    "--output-dir",
                    str(out),
                ],
            )

        assert result.exit_code == 0, result.output
        with sorted(out.glob("*.csv"))[0].open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        assert header == ["Metric", "a" * 30, "run-b"]


class TestAbortTestRunGuard:
    def test_defect_an_empty_test_run_ids_value_is_not_refused(self, runner):
        """
        DEFECT (pinned as current behaviour, not fixed). `abort-test-run` guards with
        `if not test_run_id_list:` after `test_run_ids.split(",")`, but `str.split`
        never returns an empty list — `"".split(",")` is `[""]`, which is truthy. The
        guard and its "No test run IDs provided" message are therefore unreachable
        code, and `--test-run-ids ""` proceeds to ask the service to abort a test run
        whose id is the empty string.

        The observable consequence is a confirmation prompt listing a blank bullet,
        followed by an abort request for `[""]` that the service can only answer with
        a not-found error — where the user should have been told the option was empty.
        This is also why lines 6622-6623 are left uncovered: there is no input that
        reaches them.
        """
        p, client = _patched_client(
            abort_test_run=MagicMock(
                return_value={"success": True, "message": "ok", "abortedCount": 0}
            )
        )
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "abort-test-run",
                    "--stack-name",
                    "IDP",
                    "--test-run-ids",
                    "",
                    "--force",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "No test run IDs provided" not in result.output
        client.testing.abort_test_run.assert_called_once_with(test_run_ids=[""])

    def test_declining_the_confirmation_aborts_nothing(self, runner):
        """
        The prompt is the only thing between a typo and every running workflow in a
        test run being stopped, so "no" must reach the service not at all. Asserted
        on the SDK call rather than on output text, because a command that printed
        "Aborted by user" *and* sent the request would pass a text-only assertion.
        """
        p, client = _patched_client(abort_test_run=MagicMock())
        with p:
            result = runner.invoke(
                cli_module.cli,
                ["abort-test-run", "--stack-name", "IDP", "--test-run-ids", "r1"],
                input="n\n",
            )

        assert result.exit_code == 0
        assert "Aborted by user" in result.output
        client.testing.abort_test_run.assert_not_called()

    def test_the_confirmation_names_the_stack_the_region_and_every_run(self, runner):
        p, client = _patched_client(abort_test_run=MagicMock())
        with p:
            result = runner.invoke(
                cli_module.cli,
                [
                    "abort-test-run",
                    "--stack-name",
                    "IDP-prod",
                    "--region",
                    "eu-central-1",
                    "--test-run-ids",
                    "r1,r2",
                ],
                input="n\n",
            )

        assert "Stack: IDP-prod" in result.output
        assert "Region: eu-central-1" in result.output
        assert "Test Runs: 2" in result.output
        assert "• r1" in result.output
        assert "• r2" in result.output
        client.testing.abort_test_run.assert_not_called()
