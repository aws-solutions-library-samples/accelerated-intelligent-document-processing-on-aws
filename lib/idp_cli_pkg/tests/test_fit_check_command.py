# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``idp-cli fit-check``: option wiring, streams, and the active-profile guard."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from idp_cli import cli as cli_module

RECORD = (
    Path(__file__).resolve().parents[2]
    / "idp_sdk/tests/unit/fixtures/fit_check/synthetic-run-record.json"
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.mark.unit
def test_from_record_needs_no_stack_and_prints_a_summary(runner, tmp_path):
    result = runner.invoke(
        cli_module.cli,
        ["fit-check", "--from-record", str(RECORD), "--output", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Fit check: 6 document(s), 3 succeeded, 3 failed (2 refused" in result.stdout
    assert "confidence is the model's own estimate and is not accuracy" in result.stdout
    assert (tmp_path / "fit-report.json").exists()
    assert (tmp_path / "fit-report.md").exists()


@pytest.mark.unit
def test_json_writes_only_the_report_to_stdout(runner, tmp_path):
    result = runner.invoke(
        cli_module.cli,
        [
            "fit-check",
            "--from-record",
            str(RECORD),
            "--output",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert "Wrote" in result.stderr  # progress stays on stderr


@pytest.mark.unit
def test_a_live_run_needs_a_stack_and_samples(runner):
    result = runner.invoke(cli_module.cli, ["fit-check", "--samples", "."])
    assert result.exit_code == 2
    assert "--stack-name and --samples are required" in result.stderr


@pytest.mark.unit
def test_options_reach_the_sdk(runner, tmp_path):
    client = MagicMock()
    client.fit_check.run.return_value.report = json.loads(
        json.dumps(
            __import__(
                "idp_sdk._core.fit_check.report", fromlist=["build_report"]
            ).build_report(json.loads(RECORD.read_text()))
        )
    )
    with patch("idp_sdk.IDPClient", return_value=client) as ctor:
        result = runner.invoke(
            cli_module.cli,
            [
                "fit-check",
                "--stack-name",
                "S",
                "--samples",
                str(tmp_path),
                "--description",
                "invoices",
                "--config-profile",
                "p1",
                "--cleanup",
                "--max-documents",
                "7",
                "--timeout",
                "60",
                "--region",
                "eu-central-1",
            ],
        )
    assert result.exit_code == 0, result.output
    ctor.assert_called_once_with(stack_name="S", region="eu-central-1")
    args, kwargs = client.fit_check.run.call_args
    assert args == (str(tmp_path), "S")
    assert kwargs["description"] == "invoices"
    assert kwargs["config_profile"] == "p1"
    assert kwargs["cleanup"] is True
    assert kwargs["max_documents"] == 7
    assert kwargs["timeout_seconds"] == 60
    assert kwargs["from_record"] is None


@pytest.mark.unit
def test_cleanup_and_from_record_are_off_unless_given(runner, tmp_path):
    client = MagicMock()
    client.fit_check.run.side_effect = __import__(
        "idp_sdk.exceptions", fromlist=["IDPValidationError"]
    ).IDPValidationError("stop here")
    with patch("idp_sdk.IDPClient", return_value=client):
        result = runner.invoke(
            cli_module.cli,
            ["fit-check", "--stack-name", "S", "--samples", str(tmp_path)],
        )
    assert result.exit_code == 1
    assert "stop here" in result.stderr
    kwargs = client.fit_check.run.call_args.kwargs
    assert kwargs["cleanup"] is False
    assert kwargs["from_record"] is None
