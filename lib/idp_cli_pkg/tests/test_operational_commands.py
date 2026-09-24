# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the three operational `idp-cli` commands: `stop-workflows`, which purges the
document queue and stops running Step Functions executions; `load-test`, which floods
the input bucket with copies of one file; and `remove-deleted-stack-resources`, which
sweeps AWS resources left behind by IDP stacks that no longer exist.

The three need different treatment, and the split below follows what each command's
own body actually decides.

`stop-workflows` and `load-test` are thin: each builds an `IDPClient`, makes one SDK
call, and turns the result into output and an exit code. The SDK call behind them
reaches SQS, Step Functions and DynamoDB (`stop-workflows`) or loops for minutes
copying objects (`load-test`), so these tests patch `IDPClient` and assert on the
command's own logic — the exact keyword arguments it translates its options into, the
region and stack name it builds the client for, what it prints for each shape of
result, and its exit code. The SDK layers underneath already have their own tests in
`test_stop_workflows.py` and `test_load_test.py`, which exercise `WorkflowStopper` and
`LoadTester` directly and not the CLI commands.

`remove-deleted-stack-resources` is the opposite case. Its whole safety question is
which resources it considers in scope, and that is answered by real CloudFormation
stack states, so those tests run against moto: a deleted IDP stack, a live one, and a
stack that is not an IDP stack at all, each with a log group, with the surviving log
groups read back afterwards and `api_calls` consulted for the deletes that were not
issued. A second class covers the summary rendering and exit code with a patched
client, where the result shapes (disabled distributions, errors) can be produced
exactly.

One observation is pinned as a test rather than reported only in prose: the summary
never shows the resources the sweep deliberately protected, so a resource type where
everything was skipped is reported as "No resources found".
"""

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws
from rich.console import Console

from idp_cli.cli import cli
from idp_sdk._core.cleanup_orphaned import OrphanedResourceCleanup
from idp_sdk.models import (
    DocumentsAbortedResult,
    ExecutionsStoppedResult,
    LoadTestResult,
    OrphanedResourceCleanupResult,
    StopWorkflowsResult,
)

# --------------------------------------------------------------------------------------
# `stop-workflows`
# --------------------------------------------------------------------------------------


def _stop_workflows_client(result):
    client = MagicMock()
    client.batch.stop_workflows.return_value = result
    return client


class TestStopWorkflowsArguments:
    """What `stop-workflows` asks the SDK to do.

    `stop-workflows` is destructive without asking: it purges the SQS queue and stops
    every running execution, with no confirmation prompt and no dry-run mode. There is
    therefore nothing here to test a refusal against — what matters instead is that the
    two skip flags are honoured exactly, because they are the only way to limit what it
    touches, and that the stack and region it acts on are the ones named.
    """

    def test_by_default_it_asks_for_both_the_purge_and_the_stop(self):
        client = _stop_workflows_client(StopWorkflowsResult())

        with patch("idp_sdk.IDPClient", return_value=client) as idp_client:
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        idp_client.assert_called_once_with(stack_name="my-stack", region=None)
        client.batch.stop_workflows.assert_called_once_with(
            skip_purge=False, skip_stop=False
        )

    def test_skip_purge_is_forwarded_without_also_skipping_the_stop(self):
        """The two flags are independent; mixing them up would purge nothing."""
        client = _stop_workflows_client(StopWorkflowsResult())

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack", "--skip-purge"]
            )

        assert result.exit_code == 0
        client.batch.stop_workflows.assert_called_once_with(
            skip_purge=True, skip_stop=False
        )

    def test_skip_stop_is_forwarded_without_also_skipping_the_purge(self):
        client = _stop_workflows_client(StopWorkflowsResult())

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack", "--skip-stop"]
            )

        assert result.exit_code == 0
        client.batch.stop_workflows.assert_called_once_with(
            skip_purge=False, skip_stop=True
        )

    def test_the_region_reaches_the_client_constructor(self):
        """A destructive command acting in the wrong region is a defect class.

        `stop-workflows` resolves its queue and state machine from the stack, so the
        region decides which account's workflows are stopped; it is asserted on the
        constructor call because that is where the value either arrives or is dropped.
        """
        client = _stop_workflows_client(StopWorkflowsResult())

        with patch("idp_sdk.IDPClient", return_value=client) as idp_client:
            result = CliRunner().invoke(
                cli,
                ["stop-workflows", "--stack-name", "my-stack", "--region", "eu-west-2"],
            )

        assert result.exit_code == 0
        idp_client.assert_called_once_with(stack_name="my-stack", region="eu-west-2")

    def test_it_never_waits_for_input(self):
        """There is no confirmation prompt, so an empty stdin must not block.

        This pins the current design rather than endorsing it: unlike `delete` and
        `delete-documents`, this command stops production workflows without asking. A
        change that added a prompt would fail here and should be reviewed as a
        deliberate interface change.
        """
        client = _stop_workflows_client(StopWorkflowsResult())

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"], input=""
            )

        assert result.exit_code == 0
        assert "?" not in result.output
        client.batch.stop_workflows.assert_called_once()

    def test_a_missing_stack_name_is_rejected_by_the_parser(self):
        with patch("idp_sdk.IDPClient") as idp_client:
            result = CliRunner().invoke(cli, ["stop-workflows"])

        assert result.exit_code == 2
        assert idp_client.called is False


class TestStopWorkflowsReporting:
    """What `stop-workflows` prints, and when it fails."""

    def test_a_stop_error_exits_non_zero(self):
        """A failure to stop executions must not be reported as success.

        Exiting 0 here would tell an operator that processing has halted when it has
        not, which is the whole purpose of running the command.
        """
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(
                    error="AccessDeniedException on ListExecutions"
                )
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 1
        assert "AccessDeniedException on ListExecutions" in result.output

    def test_a_clean_stop_reports_the_count_and_the_verification(self):
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(
                    total_stopped=7, total_failed=0, remaining=0
                )
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "Stopped 7 executions" in result.output
        assert "Verified: No running executions remaining" in result.output
        assert "still running" not in result.output

    def test_executions_still_running_are_flagged_with_the_remedy(self):
        """Executions started during the stop are the normal cause, so say so.

        The exit code stays 0 deliberately — the command did stop what it found — but
        the message has to tell the operator to run it again, or they will believe the
        queue is drained when it is not.
        """
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(
                    total_stopped=5, total_failed=0, remaining=2
                )
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "2 executions still running" in result.output
        assert "New executions may have started during stop operation" in result.output
        assert "Run command again to stop remaining executions" in result.output

    def test_executions_that_failed_to_stop_are_counted_separately(self):
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(
                    total_stopped=3, total_failed=2, remaining=0
                )
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "Stopped 3 executions" in result.output
        assert "2 failed to stop" in result.output

    def test_aborted_queued_documents_are_reported(self):
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(total_stopped=1),
                documents_aborted=DocumentsAbortedResult(documents_aborted=42),
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "Updated 42 queued documents to ABORTED status" in result.output

    def test_a_failure_to_abort_queued_documents_is_a_warning_not_a_failure(self):
        """The executions are already stopped, so this is a tidy-up that can fail.

        The tracking table is left with documents stuck in QUEUED, which is worth
        printing, but exiting non-zero would misreport the part that succeeded.
        """
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(total_stopped=1),
                documents_aborted=DocumentsAbortedResult(
                    error="ProvisionedThroughputExceededException"
                ),
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "Could not abort queued documents" in result.output
        assert "ProvisionedThroughputExceededException" in result.output

    def test_zero_aborted_documents_prints_no_abort_line(self):
        """Nothing was queued, so there is nothing to report about it."""
        client = _stop_workflows_client(
            StopWorkflowsResult(
                executions_stopped=ExecutionsStoppedResult(total_stopped=1),
                documents_aborted=DocumentsAbortedResult(documents_aborted=0),
            )
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 0
        assert "ABORTED" not in result.output

    def test_skipping_the_stop_prints_no_execution_summary(self):
        """With `--skip-stop` the SDK returns no execution result to report."""
        client = _stop_workflows_client(
            StopWorkflowsResult(executions_stopped=None, queue_purged=True)
        )

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack", "--skip-stop"]
            )

        assert result.exit_code == 0
        assert "executions" not in result.output
        assert "Stopping workflows for stack: my-stack" in result.output

    def test_an_sdk_exception_exits_non_zero(self):
        client = MagicMock()
        client.batch.stop_workflows.side_effect = RuntimeError("stack not found")

        with patch("idp_sdk.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["stop-workflows", "--stack-name", "my-stack"]
            )

        assert result.exit_code == 1
        assert "stack not found" in result.output


# --------------------------------------------------------------------------------------
# `load-test`
# --------------------------------------------------------------------------------------


def _load_test_client(result=None):
    client = MagicMock()
    client.testing.load_test.return_value = result or LoadTestResult(
        success=True, total_files=100, duration_minutes=1
    )
    return client


class TestLoadTestArguments:
    """How `load-test` translates its options into the SDK call.

    Every one of these values decides how much load is put on a live deployment, so a
    mistranslated option is not cosmetic: `--rate` and `--duration` multiply into the
    number of objects written to the input bucket, and each of those triggers a
    document processing workflow that costs money to run.
    """

    def test_the_options_are_forwarded_with_the_names_the_sdk_expects(self, tmp_path):
        source = tmp_path / "invoice.pdf"
        source.write_bytes(b"%PDF-1.4")
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client) as idp_client:
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    str(source),
                    "--rate",
                    "250",
                    "--duration",
                    "3",
                    "--dest-prefix",
                    "soak",
                    "--region",
                    "eu-central-1",
                ],
            )

        assert result.exit_code == 0
        idp_client.assert_called_once_with(stack_name="my-stack", region="eu-central-1")
        client.testing.load_test.assert_called_once_with(
            source_file=str(source),
            stack_name="my-stack",
            rate=250,
            duration=3,
            schedule_file=None,
            dest_prefix="soak",
            config_version=None,
        )

    def test_the_defaults_are_the_documented_ones(self):
        """100 files a minute for one minute into `load-test/`, and no schedule."""
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                ],
            )

        assert result.exit_code == 0
        kwargs = client.testing.load_test.call_args.kwargs
        assert kwargs["rate"] == 100
        assert kwargs["duration"] == 1
        assert kwargs["dest_prefix"] == "load-test"
        assert kwargs["schedule_file"] is None

    def test_an_s3_source_is_passed_through_untouched(self):
        """`--source-file` accepts an s3:// URI, which must not be path-normalised."""
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://my-bucket/docs/invoice.pdf",
                ],
            )

        assert (
            client.testing.load_test.call_args.kwargs["source_file"]
            == "s3://my-bucket/docs/invoice.pdf"
        )

    def test_a_schedule_file_is_forwarded_as_schedule_file(self, tmp_path):
        """The option is `--schedule`; the SDK parameter is `schedule_file`."""
        schedule = tmp_path / "schedule.csv"
        schedule.write_text("minute,count\n1,100\n2,200\n", encoding="utf-8")
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                    "--schedule",
                    str(schedule),
                ],
            )

        assert result.exit_code == 0
        assert client.testing.load_test.call_args.kwargs["schedule_file"] == str(
            schedule
        )

    def test_a_missing_schedule_file_is_refused_before_any_load_starts(self, tmp_path):
        """`--schedule` is `click.Path(exists=True)`, so a typo stops the run.

        This is the useful refusal in this command: without the existence check the
        SDK would be constructed and the schedule parse would fail somewhere inside a
        run that may already have started copying objects.
        """
        missing = tmp_path / "not-there.csv"

        with patch("idp_cli.cli.IDPClient") as idp_client:
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                    "--schedule",
                    str(missing),
                ],
            )

        assert result.exit_code == 2
        assert idp_client.called is False

    @pytest.mark.parametrize("flag", ["--config-profile", "--config-version"])
    def test_either_config_profile_spelling_sets_config_version(self, flag):
        """`--config-version` is the former name of `--config-profile`.

        Both still have to reach the SDK's `config_version` parameter: a load test
        tagged with the wrong configuration profile measures the wrong pipeline.
        """
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                    flag,
                    "v2",
                ],
            )

        assert result.exit_code == 0
        assert client.testing.load_test.call_args.kwargs["config_version"] == "v2"

    def test_a_non_numeric_rate_is_rejected_by_the_parser(self):
        with patch("idp_cli.cli.IDPClient") as idp_client:
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                    "--rate",
                    "fast",
                ],
            )

        assert result.exit_code == 2
        assert idp_client.called is False

    def test_a_missing_source_file_option_is_rejected(self):
        with patch("idp_cli.cli.IDPClient") as idp_client:
            result = CliRunner().invoke(cli, ["load-test", "--stack-name", "my-stack"])

        assert result.exit_code == 2
        assert idp_client.called is False


class TestLoadTestReporting:
    """`load-test` exit codes. A load test that failed must not look like a pass."""

    def test_a_failed_load_test_exits_non_zero_and_names_the_reason(self):
        client = _load_test_client(
            LoadTestResult(
                success=False,
                total_files=0,
                duration_minutes=0,
                error="Input bucket not found in stack",
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                ],
            )

        assert result.exit_code == 1
        assert "Load test failed" in result.output
        assert "Input bucket not found in stack" in result.output

    def test_a_successful_load_test_prints_nothing_of_its_own(self):
        """Progress reporting belongs to the SDK's live display, not to this command."""
        client = _load_test_client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                ],
            )

        assert result.exit_code == 0
        assert result.output == ""

    def test_an_sdk_exception_exits_non_zero(self):
        client = MagicMock()
        client.testing.load_test.side_effect = RuntimeError("source file unreadable")

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "load-test",
                    "--stack-name",
                    "my-stack",
                    "--source-file",
                    "s3://bucket/invoice.pdf",
                ],
            )

        assert result.exit_code == 1
        assert "source file unreadable" in result.output


# --------------------------------------------------------------------------------------
# `remove-deleted-stack-resources`
# --------------------------------------------------------------------------------------

#: A template carrying the description the sweep identifies IDP stacks by.
IDP_STACK_TEMPLATE = json.dumps(
    {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "AWS GenAI IDP Accelerator",
        "Resources": {"Topic": {"Type": "AWS::SNS::Topic", "Properties": {}}},
    }
)


@pytest.fixture
def sweep_console(monkeypatch):
    """Pin the cleanup module's own Rich console, which conftest does not cover.

    `idp_sdk._core.cleanup_orphaned` has a module-level `console` of its own, so the
    autouse fixture that pins `idp_cli.cli`'s consoles leaves it at Rich's no-tty
    default of 80 columns. Its per-resource prompt and discovery lines are asserted
    on below, and at 80 columns they wrap mid-sentence.
    """
    monkeypatch.setattr(
        "idp_sdk._core.cleanup_orphaned.console",
        Console(width=200, force_terminal=False),
    )


@pytest.fixture
def cloudfront_policies_unavailable(monkeypatch):
    """Stand in for the one AWS API moto does not implement here.

    `cleanup_cloudfront_policies` calls `ListResponseHeadersPolicies`, which moto
    answers with a 404. The SDK turns that into an entry in the `errors` list, which
    makes the command exit 1 — so without this every scope test below would be
    asserting against a failure that has nothing to do with what it is testing, and
    would flip meaning the day moto implements the call. The exit-1-on-errors path is
    covered on its own in `TestRemoveDeletedStackResourcesReporting`.
    """
    monkeypatch.setattr(
        OrphanedResourceCleanup,
        "cleanup_cloudfront_policies",
        lambda self, dry_run=False, auto_approve=False: {
            "deleted": [],
            "skipped": [],
            "errors": [],
        },
    )


def _log_groups(region="us-east-1"):
    logs = boto3.client("logs", region_name=region)
    return sorted(
        group["logGroupName"] for group in logs.describe_log_groups()["logGroups"]
    )


def _idp_stack(name, region="us-east-1", deleted=False):
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(StackName=name, TemplateBody=IDP_STACK_TEMPLATE)
    if deleted:
        cfn.delete_stack(StackName=name)


def _lambda_log_group(stack_name, region="us-east-1"):
    """A log group named the way the sweep's stack-name extraction expects."""
    name = f"/{stack_name}-PATTERN2-abcdef/lambda/OCRFunction"
    boto3.client("logs", region_name=region).create_log_group(logGroupName=name)
    return name


@pytest.mark.usefixtures("sweep_console", "cloudfront_policies_unavailable")
class TestRemoveDeletedStackResourcesScope:
    """Which resources the sweep considers in scope, measured against real stacks.

    Every test here runs against moto with the stacks and log groups it describes,
    and reads the surviving log groups back afterwards. That is the only way to
    answer the question this command lives or dies by: a resource belonging to a
    stack that still exists must never be deleted.
    """

    def test_a_dry_run_deletes_nothing_and_names_the_orphan(self, api_calls):
        """`--dry-run` is the documented first step, so it must touch nothing."""
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            orphan = _lambda_log_group("IDP-Gone")

            result = CliRunner().invoke(
                cli,
                [
                    "remove-deleted-stack-resources",
                    "--region",
                    "us-east-1",
                    "--dry-run",
                ],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert "DRY RUN - No changes will be made" in result.output
        assert f"{orphan} (stack: IDP-Gone) [DRY RUN]" in result.output
        assert surviving == [orphan]
        assert api_calls.of("DeleteLogGroup") == []

    def test_an_orphan_is_deleted_and_a_live_stacks_resource_is_not(self, api_calls):
        """The central safety property: an active stack's resources are protected.

        Both log groups are named identically apart from the stack name, and only
        one of the two stacks has been deleted. If the sweep ever stopped consulting
        stack state — or consulted it and got the sense of the answer wrong — this is
        the test that fails, and it fails on the surviving resource rather than on a
        log line.
        """
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            _idp_stack("IDP-Live")
            orphan = _lambda_log_group("IDP-Gone")
            protected = _lambda_log_group("IDP-Live")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert surviving == [protected]
        deletions = api_calls.of("DeleteLogGroup")
        assert [call.params["logGroupName"] for call in deletions] == [orphan]
        assert "AUTO-APPROVE" in result.output

    def test_a_resource_from_an_unrecognised_stack_is_left_alone(self, api_calls):
        """A stack that is not a known IDP stack is UNKNOWN, and UNKNOWN is skipped.

        `/OTHER-Thing-PATTERN2-…` matches the log-group shape the sweep looks for,
        but no IDP stack called `OTHER-Thing` was discovered, so its state cannot be
        established — and a resource whose owner cannot be established must survive.
        """
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            orphan = _lambda_log_group("IDP-Gone")
            stranger = _lambda_log_group("OTHER-Thing")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert surviving == [stranger]
        assert [
            call.params["logGroupName"] for call in api_calls.of("DeleteLogGroup")
        ] == [orphan]

    def test_a_stack_name_that_is_both_deleted_and_live_is_protected(self, api_calls):
        """Re-deploying a stack under a name that was deleted must not be swept.

        CloudFormation keeps the deleted stack in `list_stacks` alongside the new
        one, so the same name appears twice with different states. The resources in
        the account belong to the live stack, and treating the name as deleted would
        delete a running deployment's log groups.
        """
        with mock_aws():
            _idp_stack("IDP-Reused", deleted=True)
            _idp_stack("IDP-Reused")
            log_group = _lambda_log_group("IDP-Reused")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert surviving == [log_group]
        assert api_calls.of("DeleteLogGroup") == []
        assert "No deleted IDP stacks found" in result.output

    def test_declining_the_per_resource_prompt_deletes_nothing(self, api_calls):
        """Without `--yes` each resource is confirmed, and "n" must mean no.

        The prompt is also asserted to name the resource and the stack it came from:
        answering the question needs both, and the account may hold resources from
        several deleted stacks.
        """
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            orphan = _lambda_log_group("IDP-Gone")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1"],
                input="n\n",
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert "Delete orphaned CloudWatch Log Group?" in result.output
        assert f"Resource: {orphan}" in result.output
        assert "Originally from stack: IDP-Gone" in result.output
        assert surviving == [orphan]
        assert api_calls.of("DeleteLogGroup") == []

    def test_confirming_the_prompt_deletes_that_resource(self, api_calls):
        """The other half of the prompt: "y" goes through to the delete."""
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            orphan = _lambda_log_group("IDP-Gone")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1"],
                input="y\n",
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert surviving == []
        assert api_calls.only("DeleteLogGroup").params["logGroupName"] == orphan

    def test_nothing_is_swept_when_no_stack_has_been_deleted(self, api_calls):
        """With no deleted stacks the sweep stops before looking at any resource."""
        with mock_aws():
            _idp_stack("IDP-Live")
            log_group = _lambda_log_group("IDP-Live")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert "No deleted IDP stacks found" in result.output
        assert surviving == [log_group]
        assert api_calls.of("DeleteLogGroup") == []
        # It stopped before reaching any resource type at all.
        assert "CLOUDFRONT DISTRIBUTIONS:" not in result.output

    def test_the_region_list_is_parsed_and_tolerates_whitespace(self, api_calls):
        """`--check-stack-regions` decides which regions are searched for stacks.

        The deleted stack lives in eu-west-1, which the default list does not
        include, while its log group is in the `--region` the command sweeps. With
        the default list the stack is never discovered and the log group survives;
        adding `eu-west-1` — with a space after the comma, as a human would type it —
        finds it and the log group goes. That pair proves both that the list is
        honoured and that each entry is stripped.
        """
        with mock_aws():
            _idp_stack("IDP-Far", region="eu-west-1", deleted=True)
            orphan = _lambda_log_group("IDP-Far")

            with_defaults = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            assert with_defaults.exit_code == 0
            assert "No deleted IDP stacks found" in with_defaults.output
            assert _log_groups() == [orphan]
            assert api_calls.of("DeleteLogGroup") == []

            with_eu = CliRunner().invoke(
                cli,
                [
                    "remove-deleted-stack-resources",
                    "--region",
                    "us-east-1",
                    "--yes",
                    "--check-stack-regions",
                    "us-east-1, eu-west-1",
                ],
            )
            surviving = _log_groups()

        assert with_eu.exit_code == 0
        assert "IDP-Far (eu-west-1)" in with_eu.output
        assert surviving == []
        assert api_calls.only("DeleteLogGroup").params["logGroupName"] == orphan

    def test_a_protected_resource_is_summarised_as_no_resources_found(self):
        """DEFECT: the summary never reports what the sweep protected or skipped.

        The SDK returns a `skipped` list along`deleted`, `disabled`, `updated` and
        `errors`, and cli.py:4165-4192 reads every key except `skipped`. When the
        only log group belongs to a live stack, the sweep finds it, protects it, and
        the command prints "LOG GROUPS: No resources found" (cli.py:4189-4192,
        reached because none of the four keys it looks at is populated).

        The consequence is a misleading report in exactly the situation the user
        wants reassurance about: they cannot tell "there was nothing there" from
        "there was something and I left it alone", and a resource skipped because its
        owning stack could not be verified is invisible. Nothing is deleted wrongly,
        so this is a reporting defect rather than a safety one.
        """
        with mock_aws():
            _idp_stack("IDP-Gone", deleted=True)
            _idp_stack("IDP-Live")
            protected = _lambda_log_group("IDP-Live")

            result = CliRunner().invoke(
                cli,
                ["remove-deleted-stack-resources", "--region", "us-east-1", "--yes"],
            )
            surviving = _log_groups()

        assert result.exit_code == 0
        assert surviving == [protected]
        assert "LOG GROUPS:" in result.output
        assert "No resources found" in result.output
        assert protected not in result.output
        # No section of the summary reports skipped resources at all.
        assert "Skipped (" not in result.output


class TestRemoveDeletedStackResourcesReporting:
    """The summary the command renders, and the exit code it derives from it.

    These use a patched `IDPClient` because the result shapes — a disabled CloudFront
    distribution, an updated resource policy, an error — cannot all be produced
    against moto, and because the arguments the command passes down are the other
    half of its contract.
    """

    @staticmethod
    def _client(results=None, has_errors=False, has_disabled=False):
        client = MagicMock()
        client.stack.cleanup_orphaned.return_value = OrphanedResourceCleanupResult(
            results=results or {},
            has_errors=has_errors,
            has_disabled=has_disabled,
        )
        return client

    def test_the_flags_and_the_region_list_reach_the_sdk(self):
        """Four values decide what the sweep does; none may be dropped in passing."""
        client = self._client()

        with patch("idp_cli.cli.IDPClient", return_value=client) as idp_client:
            result = CliRunner().invoke(
                cli,
                [
                    "remove-deleted-stack-resources",
                    "--region",
                    "eu-central-1",
                    "--dry-run",
                    "--yes",
                    "--profile",
                    "teardown-profile",
                    "--check-stack-regions",
                    "us-east-1,eu-central-1",
                ],
            )

        assert result.exit_code == 0
        idp_client.assert_called_once_with(region="eu-central-1")
        client.stack.cleanup_orphaned.assert_called_once_with(
            dry_run=True,
            auto_approve=True,
            regions=["us-east-1", "eu-central-1"],
            profile="teardown-profile",
        )

    def test_the_defaults_are_the_documented_ones(self):
        """us-west-2 for resources, three regions for stack discovery, nothing else."""
        client = self._client()

        with patch("idp_cli.cli.IDPClient", return_value=client) as idp_client:
            result = CliRunner().invoke(cli, ["remove-deleted-stack-resources"])

        assert result.exit_code == 0
        idp_client.assert_called_once_with(region="us-west-2")
        client.stack.cleanup_orphaned.assert_called_once_with(
            dry_run=False,
            auto_approve=False,
            regions=["us-east-1", "us-west-2", "eu-central-1"],
            profile=None,
        )

    def test_an_empty_result_still_prints_a_summary_and_exits_zero(self):
        """`run_cleanup` returns `{}` when there is nothing to do."""
        client = self._client()

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["remove-deleted-stack-resources"])

        assert result.exit_code == 0
        assert "CLEANUP SUMMARY" in result.output

    def test_each_outcome_is_listed_under_its_resource_type(self):
        client = self._client(
            results={
                "cloudfront_distributions": {
                    "deleted": ["E1234567890ABC"],
                    "skipped": [],
                    "errors": [],
                },
                "logs_resource_policies": {
                    "updated": ["AWSLogDeliveryWrite20150319"],
                    "skipped": [],
                    "errors": [],
                },
                "s3_buckets": {"deleted": [], "skipped": [], "errors": []},
            }
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["remove-deleted-stack-resources"])

        assert result.exit_code == 0
        assert "CLOUDFRONT DISTRIBUTIONS:" in result.output
        assert "Deleted (1):" in result.output
        assert "E1234567890ABC" in result.output
        assert "LOGS RESOURCE POLICIES:" in result.output
        assert "Updated (1):" in result.output
        assert "AWSLogDeliveryWrite20150319" in result.output
        assert "S3 BUCKETS:" in result.output
        assert "No resources found" in result.output

    def test_errors_are_listed_and_exit_the_command_non_zero(self):
        """A sweep that could not finish must not report success.

        The resources it failed on are still in the account costing money, and the
        operator's next step is to re-run it, so the exit code has to say so.
        """
        client = self._client(
            results={
                "iam_policies": {
                    "deleted": [],
                    "skipped": [],
                    "errors": [
                        "Failed to delete policy IDP-Gone-Boundary: DeleteConflict"
                    ],
                }
            },
            has_errors=True,
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["remove-deleted-stack-resources"])

        assert result.exit_code == 1
        assert "Errors (1):" in result.output
        assert "DeleteConflict" in result.output

    def test_disabled_distributions_print_the_second_run_instructions(self):
        """CloudFront needs two passes, and the second one has to be prompted.

        A distribution can only be deleted once it is disabled and the disable has
        propagated, so a run that disabled one has not finished the job. The re-run
        command is printed with the region the user gave, since the default is not
        the region they were working in.
        """
        client = self._client(
            results={
                "cloudfront_distributions": {
                    "deleted": [],
                    "disabled": ["E1234567890ABC"],
                    "skipped": [],
                    "errors": [],
                }
            },
            has_disabled=True,
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["remove-deleted-stack-resources", "--region", "eu-central-1"]
            )

        assert result.exit_code == 0
        assert "Disabled (1):" in result.output
        assert "NEXT STEPS" in result.output
        assert "Wait 15-20 minutes, then re-run this command" in result.output
        assert (
            "idp-cli remove-deleted-stack-resources --region eu-central-1"
            in result.output
        )

    def test_errors_and_disabled_together_print_both_and_still_exit_non_zero(self):
        client = self._client(
            results={
                "cloudfront_distributions": {
                    "disabled": ["E1234567890ABC"],
                    "errors": ["Failed to delete E999: DistributionNotDisabled"],
                }
            },
            has_errors=True,
            has_disabled=True,
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["remove-deleted-stack-resources"])

        assert result.exit_code == 1
        assert "NEXT STEPS" in result.output
        assert "DistributionNotDisabled" in result.output

    def test_an_sdk_exception_exits_non_zero(self):
        client = MagicMock()
        client.stack.cleanup_orphaned.side_effect = RuntimeError(
            "The config profile (teardown) could not be found"
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["remove-deleted-stack-resources", "--profile", "teardown"]
            )

        assert result.exit_code == 1
        assert "could not be found" in result.output
