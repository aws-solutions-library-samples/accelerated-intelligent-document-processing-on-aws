# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the two destructive `idp-cli` commands that delete data: `delete`, which
deletes a whole CloudFormation stack (optionally emptying its S3 buckets first and
sweeping retained resources afterwards), and `delete-documents`, which deletes
selected documents from the input bucket, the output bucket and the DynamoDB
tracking table.

These commands are judged here on what they REFUSE. Almost every test therefore runs
the command against a real (moto-backed) CloudFormation stack, real S3 buckets and a
real DynamoDB tracking table, and then reads the service state back to see what
survived. Asserting that a `MagicMock` was not called cannot distinguish "the command
declined to delete" from "the command called something the mock silently absorbed",
and for a delete path that distinction is the only thing worth measuring. The
`api_calls` fixture supplies the other half: the exact API operations and parameters
botocore received, so a negative assertion is about calls that genuinely did not
happen.

Two paths cannot be reached through moto, and those use a patched `IDPClient` with
real `idp_sdk` result models rather than bare mocks: a stack already in
DELETE_IN_PROGRESS (moto deletes synchronously, so a stack is never observed
mid-delete) and a DELETE_FAILED outcome. In those tests the assertions are about the
command's own logic — which SDK call it makes, what it prints, and its exit code.

Three defects found while writing these are fixed, and the tests that pinned them now
assert the guarantee instead: `delete-documents` exiting 0 when every single document
deletion failed, `delete --force-delete-all` exiting 0 after printing "Stack deletion
failed!" (both #1230), and `delete` announcing "Stack deleted successfully" for a
deletion it had only initiated — that last one now asserts the guidance a user omitting
`--wait` should see.

⚠️ One boundary worth knowing before reading an exit code here: a *partial*
`delete-documents` failure still exits 0. #1230 enumerated only the total failure, and
`test_a_partial_failure_still_exits_zero_and_that_is_the_residual` says so in its name.
"""

import json
import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

from idp_cli.cli import cli
from idp_sdk.models import (
    CancelUpdateResult,
    StackDeletionResult,
    StackMonitorResult,
    StackOperationInProgress,
    StackStableStateResult,
)

# --------------------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_retry_env_leak():
    """Keep the SDK's teardown path from changing the retry settings of later tests.

    `StackDeployer._cleanup_additional_resources`, which the `--wait` path runs after
    CloudFormation reports DELETE_COMPLETE, assigns `AWS_MAX_ATTEMPTS=10` and
    `AWS_RETRY_MODE=adaptive` into `os.environ` and never restores them. That is a
    process-wide change, so without this fixture a `--wait` test here would silently
    alter how every subsequent test in the session retries — the kind of
    cross-test coupling that makes one suite's result depend on another's order.
    """
    names = ("AWS_MAX_ATTEMPTS", "AWS_RETRY_MODE")
    before = {name: os.environ.get(name) for name in names}
    try:
        yield
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _stack_template(buckets):
    """A valid CloudFormation template declaring one S3 bucket per given name."""
    return json.dumps(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": "Test double for an IDP stack",
            "Resources": {
                f"Bucket{index}": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"BucketName": name},
                }
                for index, name in enumerate(buckets)
            },
        }
    )


def _create_stack(stack_name, buckets=(), region="us-east-1"):
    """Create a real moto stack whose S3 buckets really exist."""
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(StackName=stack_name, TemplateBody=_stack_template(buckets))
    return cfn


def _stack_status(stack_name, region="us-east-1"):
    """The stack's status, or None if CloudFormation no longer knows the name."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError:
        return None


def _keys(bucket, region="us-east-1"):
    s3 = boto3.client("s3", region_name=region)
    return sorted(
        obj["Key"] for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", [])
    )


def _bucket_names(region="us-east-1"):
    s3 = boto3.client("s3", region_name=region)
    return sorted(bucket["Name"] for bucket in s3.list_buckets()["Buckets"])


#: The resources a `delete-documents` run needs to find: an input bucket, an output
#: bucket, a `DocumentQueue` (absent, `StackInfo.get_resources` raises) and a
#: `TrackingTable` keyed the way `idp_common.delete_documents` expects (PK/SK).
def _documents_template(with_outputs=True):
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Test double for an IDP stack with document resources",
        "Resources": {
            "InputBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": "dd-input-bucket"},
            },
            "OutputBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"BucketName": "dd-output-bucket"},
            },
            "DocumentQueue": {
                "Type": "AWS::SQS::Queue",
                "Properties": {"QueueName": "dd-queue"},
            },
            "TrackingTable": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "TableName": "dd-tracking",
                    "KeySchema": [
                        {"AttributeName": "PK", "KeyType": "HASH"},
                        {"AttributeName": "SK", "KeyType": "RANGE"},
                    ],
                    "AttributeDefinitions": [
                        {"AttributeName": "PK", "AttributeType": "S"},
                        {"AttributeName": "SK", "AttributeType": "S"},
                    ],
                    "BillingMode": "PAY_PER_REQUEST",
                },
            },
        },
    }
    if with_outputs:
        template["Outputs"] = {
            "S3InputBucketName": {"Value": "dd-input-bucket"},
            "S3OutputBucketName": {"Value": "dd-output-bucket"},
        }
    return json.dumps(template)


#: (object key, status) for the documents every `delete-documents` test starts from.
#: `batch-1` and `batch-10` are both present on purpose: a batch id is a leading path
#: SEGMENT, so selecting `batch-1` must not take `batch-10`'s document with it.
SEEDED_DOCUMENTS = (
    ("batch-1/first.pdf", "COMPLETED"),
    ("batch-1/second.pdf", "FAILED"),
    ("batch-10/third.pdf", "COMPLETED"),
)


def _seed_documents(stack_name="dd-stack", with_outputs=True, documents=None):
    """Create the stack, then a tracking record plus S3 objects for each document."""
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    cfn.create_stack(
        StackName=stack_name, TemplateBody=_documents_template(with_outputs)
    )

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("dd-tracking")
    s3 = boto3.client("s3", region_name="us-east-1")
    for key, status in documents if documents is not None else SEEDED_DOCUMENTS:
        table.put_item(
            Item={
                "PK": f"doc#{key}",
                "SK": "none",
                "ObjectKey": key,
                "Status": status,
                "QueuedTime": "2026-01-01T00:00:00.000Z",
            }
        )
        s3.put_object(Bucket="dd-input-bucket", Key=key, Body=b"%PDF-1.4")
        s3.put_object(
            Bucket="dd-output-bucket", Key=f"{key}/sections/1/result.json", Body=b"{}"
        )
    return table


def _tracked_documents():
    """The object keys the tracking table still holds a document record for."""
    table = boto3.resource("dynamodb", region_name="us-east-1").Table("dd-tracking")
    return sorted(
        item["ObjectKey"]
        for item in table.scan()["Items"]
        if str(item["PK"]).startswith("doc#")
    )


def _fake_client(**stack_attributes):
    """An `IDPClient` double whose `.stack` methods return real SDK result models.

    Used only for the two stack lifecycle states moto cannot produce. Every attribute
    set here is an `idp_sdk.models` instance rather than a `MagicMock`, so a test
    fails if the command reads a field the real model does not have.
    """
    client = MagicMock()
    client.stack.check_in_progress.return_value = None
    for name, value in stack_attributes.items():
        getattr(client.stack, name).return_value = value
    return client


# --------------------------------------------------------------------------------------
# `delete` — what it refuses
# --------------------------------------------------------------------------------------


class TestDeleteRefusals:
    """The refusal paths of `idp-cli delete`.

    Every test in this class asserts on the AWS calls that did NOT happen, read off
    `api_calls`, and on the service state afterwards. A failure here means the command
    deleted something it was told not to.
    """

    def test_a_missing_stack_is_refused_before_any_delete_call(self, api_calls):
        """A name that does not exist must exit non-zero and delete nothing.

        `--force` is passed to prove the refusal is not merely the confirmation
        prompt doing the work.
        """
        with mock_aws():
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "no-such-stack", "--force"]
            )

        assert result.exit_code == 1
        assert "does not exist" in result.output
        assert "no-such-stack" in result.output
        assert api_calls.of("DeleteStack") == []

    def test_declining_the_confirmation_deletes_nothing(self, api_calls):
        """Answering "n" must leave the stack exactly as it was."""
        with mock_aws():
            _create_stack("keep-me", buckets=["keep-me-bucket"])
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "keep-me"], input="n\n"
            )
            surviving_status = _stack_status("keep-me")
            surviving_buckets = _bucket_names()

        assert result.exit_code == 0
        assert "Deletion cancelled" in result.output
        assert api_calls.of("DeleteStack") == []
        assert surviving_status == "CREATE_COMPLETE"
        assert surviving_buckets == ["keep-me-bucket"]

    def test_the_confirmation_names_the_stack_and_the_region(self):
        """A prompt that does not say what it is about to destroy is a defect.

        The warning block is the only place the user sees which stack and which
        region are in play, and `--region` is easy to get wrong from a shell history
        entry, so both are asserted.
        """
        with mock_aws():
            _create_stack("target-stack", region="us-west-2")
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "target-stack", "--region", "us-west-2"],
                input="n\n",
            )

        assert "WARNING: Stack Deletion" in result.output
        assert "Stack: target-stack" in result.output
        assert "Region: us-west-2" in result.output
        assert "This action cannot be undone." in result.output
        assert "Are you sure you want to delete this stack?" in result.output

    def test_with_no_region_the_prompt_says_default_rather_than_guessing(self):
        """The warning block reports `default` when `--region` was not given.

        It prints the option's value, not the region boto3 will resolve, so the
        wording matters: a reader must not take `Region: default` for a region name.
        """
        with mock_aws():
            _create_stack("target-stack")
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "target-stack"], input="n\n"
            )

        assert "Region: default" in result.output

    def test_declining_the_second_confirmation_leaves_bucket_data_intact(
        self, api_calls
    ):
        """`--empty-buckets` asks twice, and "yes then no" must delete nothing.

        This is the highest-consequence refusal in the command: the first answer has
        already been "yes", so a bug that treated the second prompt as advisory would
        empty the buckets. The object is read back afterwards to prove it survived.
        """
        with mock_aws():
            _create_stack("two-prompts", buckets=["two-prompts-bucket"])
            boto3.client("s3", region_name="us-east-1").put_object(
                Bucket="two-prompts-bucket", Key="invoice.pdf", Body=b"data"
            )
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "two-prompts", "--empty-buckets"],
                input="y\nn\n",
            )
            surviving_keys = _keys("two-prompts-bucket")
            surviving_status = _stack_status("two-prompts")

        assert result.exit_code == 0
        assert "permanently delete all bucket data" in result.output
        assert "Deletion cancelled" in result.output
        assert surviving_keys == ["invoice.pdf"]
        assert surviving_status == "CREATE_COMPLETE"
        assert api_calls.of("DeleteStack") == []
        assert api_calls.of("DeleteObjects") == []
        assert api_calls.of("DeleteObject") == []

    def test_force_delete_all_asks_a_differently_worded_question(self, api_calls):
        """The widest-reaching flag gets its own prompt, and declining stops it."""
        with mock_aws():
            _create_stack("sweep-me", buckets=["sweep-me-bucket"])
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "sweep-me", "--force-delete-all"],
                input="n\n",
            )
            surviving_status = _stack_status("sweep-me")

        assert result.exit_code == 0
        assert "WARNING: FORCE DELETE ALL RESOURCES" in result.output
        assert "ABSOLUTELY sure you want to force delete ALL resources" in result.output
        # The prompt enumerates the resource classes that outlive CloudFormation.
        assert "All S3 buckets (including LoggingBucket)" in result.output
        assert "All CloudWatch Log Groups" in result.output
        assert "All DynamoDB Tables" in result.output
        assert surviving_status == "CREATE_COMPLETE"
        assert api_calls.of("DeleteStack") == []

    def test_force_delete_all_asks_only_once_even_with_empty_buckets(self):
        """`--force-delete-all` subsumes the bucket-emptying question.

        The second `--empty-buckets` prompt is deliberately skipped when
        `--force-delete-all` is also given, because that flag's own prompt already
        covers destroying bucket contents. A single "y" must therefore be enough,
        and the run must not block waiting for input that will never come.
        """
        with mock_aws():
            _create_stack("both-flags", buckets=["both-flags-bucket"])
            result = CliRunner().invoke(
                cli,
                [
                    "delete",
                    "--stack-name",
                    "both-flags",
                    "--force-delete-all",
                    "--empty-buckets",
                ],
                input="y\n",
            )
            remaining_status = _stack_status("both-flags")

        assert result.exit_code == 0
        assert "ABSOLUTELY sure you want to force delete ALL resources" in result.output
        assert "permanently delete all bucket data" not in result.output
        assert remaining_status is None

    def test_a_non_interactive_run_without_force_deletes_nothing(self, api_calls):
        """With no stdin and no `--force`, the command must fail closed.

        This is the shape a CI job takes when someone forgets `--force`: click's
        confirmation hits end-of-input. The only acceptable outcome is a non-zero
        exit with the stack untouched.
        """
        with mock_aws():
            _create_stack("ci-safety", buckets=["ci-safety-bucket"])
            result = CliRunner().invoke(cli, ["delete", "--stack-name", "ci-safety"])
            surviving_status = _stack_status("ci-safety")

        assert result.exit_code != 0
        assert api_calls.of("DeleteStack") == []
        assert surviving_status == "CREATE_COMPLETE"


# --------------------------------------------------------------------------------------
# `delete` — what it actually does
# --------------------------------------------------------------------------------------


class TestDeleteAgainstRealServices:
    """`idp-cli delete` run against moto, with the outcome read back from AWS."""

    def test_force_skips_the_prompt_and_deletes_the_named_stack(self, api_calls):
        """`--force` proceeds with no input, and deletes the stack it was given."""
        with mock_aws():
            _create_stack("delete-me", buckets=["delete-me-bucket"])
            _create_stack("leave-me", buckets=["leave-me-bucket"])
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "delete-me", "--force"]
            )
            deleted_status = _stack_status("delete-me")
            other_status = _stack_status("leave-me")

        assert result.exit_code == 0
        assert "Are you sure" not in result.output
        call = api_calls.only("DeleteStack")
        assert call.params["StackName"] == "delete-me"
        assert deleted_status is None
        assert other_status == "CREATE_COMPLETE"

    def test_empty_buckets_empties_the_buckets_before_deleting_the_stack(
        self, api_calls
    ):
        """The emptying must precede the DeleteStack, or it accomplishes nothing.

        CloudFormation refuses to delete a non-empty bucket, so the ordering is the
        whole point of the flag. Asserting on the recorded call sequence is what
        makes that visible; the read-back afterwards shows both buckets are gone,
        which is only possible if they were empty when CloudFormation got to them.
        """
        with mock_aws():
            _create_stack("empty-first", buckets=["ef-input", "ef-output"])
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.put_object(Bucket="ef-input", Key="a.pdf", Body=b"data")
            s3.put_object(Bucket="ef-output", Key="a.pdf/result.json", Body=b"{}")

            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "empty-first", "--empty-buckets", "--force"],
            )
            remaining_buckets = _bucket_names()

        assert result.exit_code == 0
        operations = api_calls.operations()
        assert "DeleteObjects" in operations
        assert operations.index("DeleteObjects") < operations.index("DeleteStack")
        # Both buckets were emptied, so CloudFormation could remove them.
        assert remaining_buckets == []

    def test_only_the_stacks_own_buckets_are_emptied(self, api_calls):
        """A bucket outside the stack must not be touched by `--empty-buckets`.

        The bucket list comes from `list_stack_resources`, not from `list_buckets`,
        and this is the test that would fail if it ever came from the latter: the
        unrelated bucket's object is read back and must still be there. Every
        `DeleteObjects` call is checked by bucket name as well, so a delete aimed at
        the wrong bucket fails here even if the object survived for another reason.
        """
        with mock_aws():
            _create_stack("scoped", buckets=["scoped-bucket"])
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.put_object(Bucket="scoped-bucket", Key="mine.pdf", Body=b"data")
            s3.create_bucket(Bucket="someone-elses-bucket")
            s3.put_object(Bucket="someone-elses-bucket", Key="theirs.pdf", Body=b"data")

            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "scoped", "--empty-buckets", "--force"]
            )
            outsider_keys = _keys("someone-elses-bucket")

        assert result.exit_code == 0
        assert outsider_keys == ["theirs.pdf"]
        assert {call.params["Bucket"] for call in api_calls.of("DeleteObjects")} == {
            "scoped-bucket"
        }

    def test_without_empty_buckets_the_data_is_left_alone_and_the_user_is_warned(
        self, api_calls
    ):
        """Bucket data is never deleted implicitly; the user is told what to do.

        The command proceeds to `DeleteStack` — against real CloudFormation that
        deletion then fails on the non-empty bucket — but it must not silently
        delete the objects, and the message must name the two flags that would.
        """
        with mock_aws():
            _create_stack("has-data", buckets=["has-data-bucket"])
            boto3.client("s3", region_name="us-east-1").put_object(
                Bucket="has-data-bucket", Key="keep.pdf", Body=b"data"
            )
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "has-data", "--force"]
            )
            surviving_keys = _keys("has-data-bucket")

        assert result.exit_code == 0
        assert "Buckets contain data!" in result.output
        assert "--empty-buckets" in result.output
        assert "--force-delete-all" in result.output
        assert surviving_keys == ["keep.pdf"]
        assert api_calls.of("DeleteObjects") == []
        assert api_calls.of("DeleteObject") == []

    def test_the_bucket_report_distinguishes_empty_from_occupied(self):
        """Each bucket is listed by logical id with its object count."""
        with mock_aws():
            _create_stack("reporting", buckets=["reporting-full", "reporting-empty"])
            boto3.client("s3", region_name="us-east-1").put_object(
                Bucket="reporting-full", Key="one.pdf", Body=b"x" * 2048
            )
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "reporting"], input="n\n"
            )

        assert "S3 Buckets:" in result.output
        assert "1 objects" in result.output
        assert "empty" in result.output

    def test_the_region_option_decides_which_account_region_is_read(self, api_calls):
        """A destructive command operating in the wrong region is a defect class.

        The stack exists only in us-west-2. Without `--region` the command must
        report it as missing (the hermetic test environment's default is us-east-1)
        and issue no DeleteStack; with `--region us-west-2` the same invocation must
        find and delete it. That pair proves the option reaches the CloudFormation
        client rather than being accepted and dropped.
        """
        with mock_aws():
            _create_stack("west-only", buckets=["west-only-bucket"], region="us-west-2")

            without_region = CliRunner().invoke(
                cli, ["delete", "--stack-name", "west-only", "--force"]
            )
            assert without_region.exit_code == 1
            assert "does not exist" in without_region.output
            assert api_calls.of("DeleteStack") == []

            with_region = CliRunner().invoke(
                cli,
                [
                    "delete",
                    "--stack-name",
                    "west-only",
                    "--force",
                    "--region",
                    "us-west-2",
                ],
            )
            remaining = _stack_status("west-only", region="us-west-2")

        assert with_region.exit_code == 0
        assert api_calls.only("DeleteStack").params["StackName"] == "west-only"
        assert remaining is None

    def test_wait_reports_the_terminal_status(self):
        """With `--wait` the reported status is the one CloudFormation ended on."""
        with mock_aws():
            _create_stack("waited", buckets=["waited-bucket"])
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "waited", "--force", "--wait"]
            )

        assert result.exit_code == 0
        assert "Stack deleted successfully!" in result.output
        assert "Status: DELETE_COMPLETE" in result.output
        # The retained LoggingBucket note belongs to a completed deletion.
        assert "LoggingBucket (if exists) is retained by design" in result.output

    def test_without_wait_it_reports_a_deletion_started_rather_than_finished(self):
        """`delete` without `--wait` must not claim the stack is gone.

        The command distinguished the two outcomes with
        `initiated_only = not result.success and result.status == "INITIATED"`, but
        `StackOperation.delete` computes
        `success = result.get("success", status == "INITIATED")` and the underlying
        no-wait path sets no `success` key, so an initiated-but-unwaited deletion
        arrives as `success=True, status="INITIATED"` and that conjunction could never
        be true. A user who omitted `--wait` was told "✓ Stack deleted successfully!"
        while CloudFormation was still deleting, with "Status: INITIATED" as the only
        hint, and never saw the guidance that names the console path and the command
        to wait with. The "retained by design" note printed too, reading as the
        post-mortem of a deletion that had finished.

        Driven through moto, so this is the real no-wait path rather than a
        hand-built result: it is the input a user actually produces.
        """
        with mock_aws():
            _create_stack("unwaited", buckets=["unwaited-bucket"])
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "unwaited", "--force"]
            )

        assert result.exit_code == 0
        assert "Stack deletion initiated!" in result.output
        assert "Stack deleted successfully!" not in result.output
        assert "Monitor progress in the AWS Console" in result.output
        assert "CloudFormation → Stacks → unwaited" in result.output
        assert "idp-cli delete --stack-name unwaited --force --wait" in result.output
        # The retained-bucket note belongs to a completed deletion, not a started one.
        assert "retained by design" not in result.output

    def test_an_sdk_error_is_reported_and_exits_non_zero(self):
        """An unexpected failure must not be swallowed into a zero exit code."""
        client = _fake_client()
        client.stack.exists.return_value = True
        client.stack.get_bucket_info.return_value = []
        client.stack.delete.side_effect = RuntimeError("AccessDenied on DeleteStack")

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "boom", "--force"]
            )

        assert result.exit_code == 1
        assert "AccessDenied on DeleteStack" in result.output


# --------------------------------------------------------------------------------------
# `delete` — stack lifecycle states moto cannot produce
# --------------------------------------------------------------------------------------


class TestDeleteWithAnOperationInProgress:
    """What `delete` does when CloudFormation is already busy with the stack.

    moto completes every stack operation synchronously, so none of these states can
    be produced by creating a real stack. The `IDPClient` is patched, but every value
    it returns is a genuine `idp_sdk.models` result, and the assertions are about the
    command's own decisions: which SDK call it makes next, and whether it deletes.
    """

    def test_a_delete_already_in_progress_switches_to_monitoring(self):
        """A second `delete` attaches to the running one instead of restarting it.

        Issuing another DeleteStack would be harmless against CloudFormation but
        pointless; the interesting assertion is that `stack.delete` is not called and
        the command monitors the operation it found.
        """
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="DELETE", status="DELETE_IN_PROGRESS"
            ),
            monitor=StackMonitorResult(
                success=True,
                operation="DELETE",
                status="DELETE_COMPLETE",
                stack_name="busy",
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["delete", "--stack-name", "busy"])

        assert result.exit_code == 0
        assert "is already being deleted" in result.output
        assert "Switching to monitoring mode" in result.output
        assert "Stack deleted successfully!" in result.output
        client.stack.monitor.assert_called_once_with(operation="DELETE")
        assert client.stack.delete.called is False

    def test_a_delete_in_progress_that_fails_exits_non_zero(self):
        """A DELETE_FAILED seen while monitoring must be reported as a failure."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="DELETE", status="DELETE_IN_PROGRESS"
            ),
            monitor=StackMonitorResult(
                success=False,
                operation="DELETE",
                status="DELETE_FAILED",
                stack_name="busy",
                error="S3 bucket not empty",
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(cli, ["delete", "--stack-name", "busy"])

        assert result.exit_code == 1
        assert "Stack deletion failed!" in result.output
        assert "S3 bucket not empty" in result.output
        assert client.stack.delete.called is False

    def test_answering_no_to_an_in_progress_create_aborts(self):
        """The three-way prompt's "n" must abort without deleting anything."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="CREATE", status="CREATE_IN_PROGRESS"
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "creating"], input="n\n"
            )

        assert result.exit_code == 0
        assert "Deletion cancelled" in result.output
        assert client.stack.delete.called is False
        assert client.stack.monitor.called is False

    def test_the_in_progress_prompt_lists_its_three_options(self):
        """The prompt has to say what Y, w and n each do before it is answered."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="UPDATE", status="UPDATE_IN_PROGRESS"
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "updating"], input="n\n"
            )

        assert "has an operation in progress: UPDATE_IN_PROGRESS" in result.output
        assert "[Y] Cancel the UPDATE and proceed with deletion (default)" in (
            result.output
        )
        assert "[w] Wait for UPDATE to complete first, then delete" in result.output
        assert "[n] Abort - do not delete" in result.output

    def test_answering_w_waits_for_the_operation_then_deletes(self):
        """ "w" monitors the in-flight operation first, then deletes the stack."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="CREATE", status="CREATE_IN_PROGRESS"
            ),
            monitor=StackMonitorResult(
                success=True,
                operation="CREATE",
                status="CREATE_COMPLETE",
                stack_name="creating",
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="creating"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "creating"], input="w\ny\n"
            )

        assert result.exit_code == 0
        client.stack.monitor.assert_called_once_with(operation="CREATE")
        assert "CREATE completed!" in result.output
        assert "Proceeding with stack deletion" in result.output
        client.stack.delete.assert_called_once()

    def test_a_failed_wait_still_offers_to_delete_the_broken_stack(self):
        """A failed CREATE is exactly the stack a user wants to delete.

        The command reports the failure and carries on to the normal confirmation
        rather than exiting, which is deliberate: refusing here would leave a
        ROLLBACK_FAILED stack with no route out through this CLI.
        """
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="CREATE", status="CREATE_IN_PROGRESS"
            ),
            monitor=StackMonitorResult(
                success=False,
                operation="CREATE",
                status="ROLLBACK_COMPLETE",
                stack_name="broken",
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="broken"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "broken"], input="w\ny\n"
            )

        assert result.exit_code == 0
        assert "CREATE failed!" in result.output
        assert "Status: ROLLBACK_COMPLETE" in result.output
        client.stack.delete.assert_called_once()

    def test_an_empty_answer_defaults_to_cancel_and_delete(self):
        """The prompt's documented default is Y, so a bare newline must delete."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="CREATE", status="CREATE_IN_PROGRESS"
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="creating"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "creating"], input="\ny\n"
            )

        assert result.exit_code == 0
        assert "will cancel CREATE in progress" in result.output
        assert client.stack.monitor.called is False
        client.stack.delete.assert_called_once()

    def test_force_cancels_an_in_progress_update_before_deleting(self):
        """`--force` on an UPDATE_IN_PROGRESS stack cancels the update first.

        CloudFormation will not accept a delete while an update is running, so the
        order — CancelUpdateStack, then wait for a stable state, then delete — is the
        behaviour to hold onto.
        """
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="UPDATE", status="UPDATE_IN_PROGRESS"
            ),
            cancel_update=CancelUpdateResult(success=True, message="cancelling"),
            wait_for_stable_state=StackStableStateResult(
                success=True, status="UPDATE_ROLLBACK_COMPLETE"
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="updating"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "updating", "--force"]
            )

        assert result.exit_code == 0
        client.stack.cancel_update.assert_called_once()
        client.stack.wait_for_stable_state.assert_called_once_with(timeout_seconds=1200)
        assert "reached stable state: UPDATE_ROLLBACK_COMPLETE" in result.output
        client.stack.delete.assert_called_once()

    def test_force_does_not_try_to_cancel_an_in_progress_create(self):
        """CancelUpdateStack applies only to updates; a CREATE is just waited out."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="CREATE", status="CREATE_IN_PROGRESS"
            ),
            wait_for_stable_state=StackStableStateResult(
                success=True, status="CREATE_COMPLETE"
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="creating"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "creating", "--force"]
            )

        assert result.exit_code == 0
        assert client.stack.cancel_update.called is False
        client.stack.wait_for_stable_state.assert_called_once()
        client.stack.delete.assert_called_once()

    def test_a_failed_cancel_is_a_warning_rather_than_a_stop(self):
        """A cancel that did not take is reported, and the wait still happens."""
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="UPDATE", status="UPDATE_IN_PROGRESS"
            ),
            cancel_update=CancelUpdateResult(
                success=False, error="CancelUpdateStack cannot be called now"
            ),
            wait_for_stable_state=StackStableStateResult(
                success=True, status="UPDATE_COMPLETE"
            ),
            exists=True,
            get_bucket_info=[],
            delete=StackDeletionResult(
                success=True, status="DELETE_COMPLETE", stack_name="updating"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "updating", "--force"]
            )

        assert result.exit_code == 0
        assert "Could not cancel update" in result.output
        client.stack.delete.assert_called_once()

    def test_a_stack_that_never_stabilises_is_not_deleted(self):
        """If the stable-state wait times out, the delete must not be attempted.

        Deleting a stack still mid-operation is how a stack ends up in a state
        neither CloudFormation nor this CLI can recover, so the timeout is a refusal
        path: exit 1, and no `stack.delete` call.
        """
        client = _fake_client(
            check_in_progress=StackOperationInProgress(
                operation="UPDATE", status="UPDATE_IN_PROGRESS"
            ),
            cancel_update=CancelUpdateResult(success=True),
            wait_for_stable_state=StackStableStateResult(
                success=False, status="TIMEOUT", message="Timed out after 1200s"
            ),
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "stuck", "--force"]
            )

        assert result.exit_code == 1
        assert "Timeout waiting for stable state" in result.output
        assert client.stack.delete.called is False


class TestDeleteFailureReporting:
    """How `delete` reports a CloudFormation deletion that failed."""

    @staticmethod
    def _client_returning(deletion_result):
        return _fake_client(exists=True, get_bucket_info=[], delete=deletion_result)

    def test_a_failed_deletion_exits_non_zero(self):
        client = self._client_returning(
            StackDeletionResult(
                success=False,
                status="DELETE_FAILED",
                stack_name="stubborn",
                error="Resource DocumentQueue could not be deleted",
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "stubborn", "--force"]
            )

        assert result.exit_code == 1
        assert "Stack deletion failed!" in result.output
        assert "Status: DELETE_FAILED" in result.output
        assert "Resource DocumentQueue could not be deleted" in result.output

    def test_a_failure_with_no_error_string_still_says_so(self):
        """`Error: Unknown` is better than a blank line the user cannot act on."""
        client = self._client_returning(
            StackDeletionResult(
                success=False, status="DELETE_FAILED", stack_name="stubborn"
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "stubborn", "--force"]
            )

        assert result.exit_code == 1
        assert "Error: Unknown" in result.output

    def test_a_bucket_related_failure_names_the_flag_that_would_fix_it(self):
        """The commonest deletion failure gets a remedy, keyed off the message."""
        client = self._client_returning(
            StackDeletionResult(
                success=False,
                status="DELETE_FAILED",
                stack_name="stubborn",
                error="The bucket you tried to delete is not empty",
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "stubborn", "--force"]
            )

        assert result.exit_code == 1
        assert "Try again with --empty-buckets or --force-delete-all" in result.output

    def test_an_unrelated_failure_does_not_suggest_the_bucket_flags(self):
        client = self._client_returning(
            StackDeletionResult(
                success=False,
                status="DELETE_FAILED",
                stack_name="stubborn",
                error="Role arn:aws:iam::1234:role/x is invalid",
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli, ["delete", "--stack-name", "stubborn", "--force"]
            )

        assert "Try again with --empty-buckets" not in result.output

    def test_with_force_delete_all_a_failed_deletion_still_runs_cleanup_and_exits_one(
        self,
    ):
        """A failed deletion exits non-zero, and the cleanup phase still runs (#1230).

        The early `sys.exit(1)` is skipped on purpose under `--force-delete-all` so
        that the cleanup of retained resources happens — that is what the flag is
        for. Nothing then set a failing code, so a caller (a CI job, a teardown
        script) could not tell a stack that failed to delete from one that deleted
        cleanly: the output said "Stack deletion failed!" and the process reported
        success. The exit is now at the end, after the cleanup has had its run, so
        both halves hold at once and both are asserted here — an implementation that
        restored the early exit would satisfy the code assertion and fail the
        cleanup one.
        """
        client = self._client_returning(
            StackDeletionResult(
                success=False,
                status="DELETE_FAILED",
                stack_name="stubborn",
                error="The bucket you tried to delete is not empty",
                cleanup_result={},
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "stubborn", "--force", "--force-delete-all"],
            )

        assert result.exit_code == 1
        assert "Stack deletion failed!" in result.output
        assert "continuing with force cleanup" in result.output
        # The cleanup phase ran despite the failing code — the flag's whole purpose.
        assert "Starting force cleanup of retained resources" in result.output
        assert "Cleanup phase complete!" in result.output
        assert "The stack was not deleted" in result.output

    def test_the_cleanup_phase_reports_what_it_deleted(self):
        """The force-cleanup summary lists each resource it removed, by name."""
        client = self._client_returning(
            StackDeletionResult(
                success=True,
                status="DELETE_COMPLETE",
                stack_name="swept",
                cleanup_result={
                    "dynamodb_deleted": ["swept-TrackingTable"],
                    "logs_deleted": ["/swept/lambda/OCRFunction"],
                    "buckets_deleted": ["swept-input", "swept-logging"],
                    "errors": [],
                },
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "swept", "--force", "--force-delete-all"],
            )

        assert result.exit_code == 0
        assert "Cleanup phase complete!" in result.output
        assert "DynamoDB Tables: 1" in result.output
        assert "swept-TrackingTable" in result.output
        assert "CloudWatch Log Groups: 1" in result.output
        assert "/swept/lambda/OCRFunction" in result.output
        assert "S3 Buckets: 2" in result.output
        assert "swept-logging" in result.output
        # The retained-bucket note is for plain deletions; force-cleanup removes it.
        assert "retained by design" not in result.output

    def test_the_cleanup_phase_reports_what_it_could_not_delete(self):
        """A resource the sweep failed on has to be named, or it is leaked silently."""
        client = self._client_returning(
            StackDeletionResult(
                success=True,
                status="DELETE_COMPLETE",
                stack_name="swept",
                cleanup_result={
                    "errors": [
                        {
                            "type": "S3 Bucket",
                            "resource": "swept-logging",
                            "error": "AccessDenied",
                        }
                    ]
                },
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "swept", "--force", "--force-delete-all"],
            )

        assert result.exit_code == 0
        assert "Some resources could not be deleted" in result.output
        assert "S3 Bucket: swept-logging" in result.output
        assert "Error: AccessDenied" in result.output

    def test_a_cleanup_result_of_none_does_not_crash_the_summary(self):
        """`cleanup_result` is optional on the model, so None must be tolerated."""
        client = self._client_returning(
            StackDeletionResult(
                success=True,
                status="DELETE_COMPLETE",
                stack_name="swept",
                cleanup_result=None,
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "swept", "--force", "--force-delete-all"],
            )

        assert result.exit_code == 0
        assert "Cleanup phase complete!" in result.output

    def test_a_malformed_cleanup_result_is_reported_not_raised(self):
        """A cleanup summary that cannot be rendered must not lose the deletion.

        By the time the summary is printed the stack is already gone, so an error
        while formatting it has to be reported and survived rather than propagating
        into the command's outer handler, where it would be shown as though the
        deletion itself had failed. Here `dynamodb_deleted` is a number instead of a
        list, so `len()` raises inside the summary (cli.py:1344).
        """
        client = self._client_returning(
            StackDeletionResult(
                success=True,
                status="DELETE_COMPLETE",
                stack_name="swept",
                cleanup_result={"dynamodb_deleted": 3},
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                ["delete", "--stack-name", "swept", "--force", "--force-delete-all"],
            )

        assert result.exit_code == 0
        assert "Cleanup phase error" in result.output
        assert "Some resources may remain - check AWS Console" in result.output

    @pytest.mark.parametrize("success", [True, False])
    def test_an_initiated_deletion_is_reported_the_same_either_way(self, success):
        """`status="INITIATED"` decides this branch, whatever `success` says.

        Both spellings are exercised because the SDK produces one of them and the
        other was what the old condition required: `StackOperation.delete` computes
        `success = result.get("success", status == "INITIATED")`, so the real no-wait
        result is `success=True, status="INITIATED"` — and a test that only built
        `success=False` by hand, as this one used to, passed against a condition no
        user input could satisfy.
        """
        client = self._client_returning(
            StackDeletionResult(
                success=success, status="INITIATED", stack_name="started"
            )
        )

        with patch("idp_cli.cli.IDPClient", return_value=client):
            result = CliRunner().invoke(
                cli,
                [
                    "delete",
                    "--stack-name",
                    "started",
                    "--force",
                    "--region",
                    "eu-west-1",
                ],
            )

        assert result.exit_code == 0
        assert "Stack deletion initiated!" in result.output
        assert "Monitor progress in the AWS Console" in result.output
        assert "CloudFormation → Stacks → started" in result.output
        assert "idp-cli delete --stack-name started --force --wait" in result.output
        assert "Region: eu-west-1" in result.output


# --------------------------------------------------------------------------------------
# `delete-documents`
# --------------------------------------------------------------------------------------


class TestDeleteDocumentsSelectorValidation:
    """`delete-documents` must be given exactly one way of choosing documents."""

    def test_no_selector_is_refused(self, api_calls):
        """With no selector the command must refuse rather than assume "all"."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli, ["delete-documents", "--stack-name", "dd-stack", "--force"]
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 1
        assert "Must specify one of --document-ids, --batch-id, or --pattern" in (
            result.output
        )
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == []
        assert api_calls.of("DeleteObjects") == []

    def test_the_refusal_happens_before_the_stack_is_even_read(self, api_calls):
        """The selector check runs first, so no AWS call is made at all."""
        with mock_aws():
            result = CliRunner().invoke(
                cli, ["delete-documents", "--stack-name", "dd-stack"]
            )

        assert result.exit_code == 1
        assert api_calls.operations() == []

    def test_two_selectors_are_refused(self, api_calls):
        """Combining selectors is ambiguous, so it is rejected, not unioned."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--pattern",
                    "*.pdf",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 1
        assert "Cannot specify more than one of" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == []

    def test_an_unknown_status_filter_is_rejected_by_the_parser(self, api_calls):
        """`--status-filter` is a closed set; a typo must not widen the selection."""
        with mock_aws():
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--status-filter",
                    "FAILD",
                    "--force",
                ],
            )

        assert result.exit_code == 2
        assert api_calls.operations() == []

    def test_missing_stack_resources_are_reported_rather_than_guessed(self):
        """A stack without the bucket outputs must stop, naming what was missing."""
        with mock_aws():
            _seed_documents(with_outputs=False)
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 1
        assert "Could not find required stack resources" in result.output
        assert "InputBucket:" in result.output
        assert "OutputBucket:" in result.output
        assert "DocumentsTable:" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]


class TestDeleteDocumentsSelection:
    """Which documents each selector actually chooses, read back from AWS.

    These run the real `idp_common.delete_documents` implementation against a real
    tracking table and real buckets, so the assertion is on the documents that
    survived rather than on an argument list handed to a mock.
    """

    def test_a_batch_id_selects_only_that_batch(self, api_calls):
        """`batch-1` must not take `batch-10` with it.

        A batch id is a leading path segment. A substring or bare `startswith` test
        would select `batch-10/third.pdf` here, and this path deletes every object
        version under the key — so the over-match would destroy an unrelated batch's
        output history. Single-digit batch ids are the common case in manual use.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--force",
                ],
            )
            surviving_input = _keys("dd-input-bucket")
            surviving_output = _keys("dd-output-bucket")
            surviving_records = _tracked_documents()

        assert result.exit_code == 0
        assert "Found 2 document(s) in batch" in result.output
        assert "Successfully deleted 2 document(s)" in result.output
        assert surviving_input == ["batch-10/third.pdf"]
        assert surviving_output == ["batch-10/third.pdf/sections/1/result.json"]
        assert surviving_records == ["batch-10/third.pdf"]
        assert {call.params["Key"] for call in api_calls.of("DeleteObject")} == {
            "batch-1/first.pdf",
            "batch-1/second.pdf",
        }

    def test_document_ids_delete_exactly_the_keys_given(self, api_calls):
        """The listed keys are deleted verbatim, and surrounding space is stripped."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--document-ids",
                    " batch-1/first.pdf , batch-10/third.pdf ",
                    "--force",
                ],
            )
            surviving_input = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "Selected 2 document(s) for deletion" in result.output
        assert surviving_input == ["batch-1/second.pdf"]
        assert {call.params["Key"] for call in api_calls.of("DeleteObject")} == {
            "batch-1/first.pdf",
            "batch-10/third.pdf",
        }

    def test_a_status_filter_narrows_a_batch_selection(self):
        """`--status-filter FAILED` leaves the batch's completed document alone."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--status-filter",
                    "FAILED",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "Found 1 document(s) in batch" in result.output
        assert "(filtered by status: FAILED)" in result.output
        assert surviving == ["batch-1/first.pdf", "batch-10/third.pdf"]

    def test_a_wildcard_pattern_selects_across_batches(self):
        """`--pattern` is fnmatch over the whole key, so it can cross batches.

        `*/*ir*.pdf` takes `batch-1/first.pdf` and `batch-10/third.pdf` and leaves
        `batch-1/second.pdf`, which no batch id could express — that reach is the
        reason the option exists, and the reason it deserves the confirmation prompt.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--pattern",
                    "*/*ir*.pdf",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "Found 2 document(s) matching pattern" in result.output
        assert surviving == ["batch-1/second.pdf"]

    def test_a_narrow_pattern_selects_one_document(self):
        """A pattern anchored on the filename selects only the key it names."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--pattern",
                    "*/second.pdf",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "Found 1 document(s) matching pattern" in result.output
        assert surviving == ["batch-1/first.pdf", "batch-10/third.pdf"]

    def test_the_listing_is_truncated_after_ten_documents(self):
        """A long selection prints ten keys and a count, not hundreds of lines."""
        documents = tuple(
            (f"batch-9/doc{index:02d}.pdf", "COMPLETED") for index in range(12)
        )
        with mock_aws():
            _seed_documents(documents=documents)
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-9",
                    "--force",
                ],
            )

        assert result.exit_code == 0
        assert "Found 12 document(s) in batch" in result.output
        assert "... and 2 more" in result.output
        assert "batch-9/doc09.pdf" in result.output
        assert "batch-9/doc11.pdf" not in result.output


class TestDeleteDocumentsRefusals:
    """What `delete-documents` refuses, and what it does when nothing matches."""

    def test_a_pattern_matching_nothing_deletes_nothing(self, api_calls):
        """An empty selection must stop, not fall through to "everything".

        This is the worst defect this command could contain, so it is asserted from
        both directions: the exit is clean, the message names the pattern, and no S3
        or DynamoDB delete was issued at all.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--pattern",
                    "*nothing-matches-this*",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "No documents found matching pattern" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert "DeleteObject" not in api_calls.operations()
        assert "DeleteObjects" not in api_calls.operations()
        assert "DeleteItem" not in api_calls.operations()

    def test_a_batch_id_matching_nothing_deletes_nothing(self, api_calls):
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-does-not-exist",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "No documents found for batch: batch-does-not-exist" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert "DeleteObject" not in api_calls.operations()
        assert "DeleteItem" not in api_calls.operations()

    def test_an_empty_selection_reports_the_status_filter_too(self):
        """ "Nothing matched" is ambiguous unless the filter is repeated back."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-10",
                    "--status-filter",
                    "QUEUED",
                    "--force",
                ],
            )

        assert result.exit_code == 0
        assert "No documents found for batch: batch-10" in result.output
        assert "(with status filter: QUEUED)" in result.output

    def test_an_empty_pattern_selection_reports_the_status_filter_too(self):
        """ "No documents matched" needs the filter repeated back, as for a batch.

        Without it a user cannot tell a pattern that matched nothing from a pattern
        that matched documents in another state, and the natural next move — widening
        the pattern — is the wrong one.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--pattern",
                    "batch-1/*",
                    "--status-filter",
                    "QUEUED",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "No documents found matching pattern: batch-1/*" in result.output
        assert "(with status filter: QUEUED)" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]

    def test_a_pattern_selection_echoes_the_status_filter_it_applied(self):
        """A non-empty pattern selection says which status it was narrowed to."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--pattern",
                    "batch-1/*",
                    "--status-filter",
                    "FAILED",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 0
        assert "Found 1 document(s) matching pattern" in result.output
        assert "(filtered by status: FAILED)" in result.output
        assert surviving == ["batch-1/first.pdf", "batch-10/third.pdf"]

    def test_declining_the_confirmation_deletes_nothing(self, api_calls):
        """Answering "n" must leave every document in place."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                ],
                input="n\n",
            )
            surviving_input = _keys("dd-input-bucket")
            surviving_records = _tracked_documents()

        assert result.exit_code == 0
        assert "Delete 2 document(s) permanently?" in result.output
        assert "Deletion cancelled" in result.output
        assert surviving_input == [key for key, _ in SEEDED_DOCUMENTS]
        assert surviving_records == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == []
        assert api_calls.of("DeleteObjects") == []
        assert api_calls.of("DeleteItem") == []

    def test_the_confirmation_names_the_documents_before_asking(self):
        """The keys are listed above the prompt, so "y" is an informed answer."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                ],
                input="n\n",
            )

        assert "Documents to be deleted:" in result.output
        assert "batch-1/first.pdf" in result.output
        assert "batch-1/second.pdf" in result.output
        listing_position = result.output.index("batch-1/first.pdf")
        prompt_position = result.output.index("Delete 2 document(s) permanently?")
        assert listing_position < prompt_position

    def test_a_non_interactive_run_without_force_deletes_nothing(self, api_calls):
        """No stdin and no `--force` must fail closed, as with `delete`."""
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code != 0
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == []

    def test_dry_run_deletes_nothing_and_asks_nothing(self, api_calls):
        """`--dry-run` reports the selection without touching it or prompting.

        No `--force` is passed and no input is supplied: a dry run that stopped for
        confirmation would hang a script, and one that issued a delete would be
        worse than useless.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--dry-run",
                ],
            )
            surviving_input = _keys("dd-input-bucket")
            surviving_records = _tracked_documents()

        assert result.exit_code == 0
        assert "DRY RUN - No changes will be made" in result.output
        assert "DRY RUN COMPLETE" in result.output
        assert "Would delete 2 document(s)" in result.output
        assert "permanently?" not in result.output
        assert surviving_input == [key for key, _ in SEEDED_DOCUMENTS]
        assert surviving_records == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == []
        assert api_calls.of("DeleteObjects") == []
        assert api_calls.of("DeleteItem") == []

    def test_the_region_option_decides_which_account_region_is_read(self):
        """A stack in another region must not be found without `--region`.

        The document resources live in us-east-1 here, so passing
        `--region us-west-2` has to fail to find the stack rather than silently
        operating somewhere else.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--batch-id",
                    "batch-1",
                    "--force",
                    "--region",
                    "us-west-2",
                ],
            )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 1
        assert "✗ Error:" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]


class TestDeleteDocumentsResultReporting:
    """Exit codes and messages when some or all deletions fail."""

    def test_a_document_ids_value_of_only_commas_is_refused(self, api_calls):
        """`--document-ids ","` is refused, rather than read as two empty object keys.

        That value is a plausible shape for a list a script built from a variable that
        turned out to be empty, and `",".split(",")` is `["", ""]`, so the command used
        to announce "Selected 2 document(s) for deletion", show two blank bullets in the
        confirmation, attempt a `DeleteObject` with a zero-length key twice, print
        "Deleted 0/2 document(s)" and exit 0. A caller could not tell that from a clean
        deletion. `--document-ids ""` was worse only in being shorter: a single document
        whose key is the empty string.

        Blank segments are dropped during parsing now, which is what makes the command's
        own emptiness check reachable, and the refusal is the check firing.

        `api_calls` wraps `BaseClient._make_api_call`, which records a request *before*
        botocore validates it, so it sees the two attempts the old path made even though
        neither was ever sent. That is what makes an empty recording the useful
        assertion here: it says the command stopped before the S3 layer, not merely that
        the bucket survived.
        """
        with mock_aws():
            _seed_documents()
            result = CliRunner().invoke(
                cli,
                [
                    "delete-documents",
                    "--stack-name",
                    "dd-stack",
                    "--document-ids",
                    ",",
                    "--force",
                ],
            )
            surviving = _keys("dd-input-bucket")
            surviving_records = _tracked_documents()

        assert result.exit_code == 1, result.output
        assert "--document-ids contains no document IDs" in result.output
        assert "Selected" not in result.output, (
            "no count may be announced for a list that named nothing"
        )
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]
        assert surviving_records == [key for key, _ in SEEDED_DOCUMENTS]
        assert api_calls.of("DeleteObject") == [], (
            "the refusal comes before any delete is attempted"
        )

    def test_every_deletion_failing_exits_non_zero(self):
        """Nothing deleted must not report success to the shell (#1230).

        A run in which every deletion failed printed "⚠ Deleted 0/2 document(s)" and
        "2 failed" and then exited 0, because the reporting branch had no `sys.exit`.
        An automated cleanup step therefore reported success having deleted nothing.

        The failures are injected at the SDK boundary. This used to be reached with
        `--document-ids ","`, whose two empty keys S3 rejected; that spelling is
        refused now, and the case is reachable with real document IDs the caller has
        no permission to delete.
        """
        failing_result = {
            "success": False,
            "deleted_count": 0,
            "failed_count": 2,
            "total_count": 2,
            "dry_run": False,
            "results": [
                {
                    "success": False,
                    "object_key": "batch-1/first.pdf",
                    "errors": ["Error deleting from input bucket: AccessDenied"],
                },
                {
                    "success": False,
                    "object_key": "batch-1/second.pdf",
                    "errors": ["Error deleting from input bucket: AccessDenied"],
                },
            ],
        }

        with mock_aws():
            _seed_documents()
            with patch(
                "idp_common.delete_documents.delete_documents",
                return_value=failing_result,
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "delete-documents",
                        "--stack-name",
                        "dd-stack",
                        "--document-ids",
                        "batch-1/first.pdf,batch-1/second.pdf",
                        "--force",
                    ],
                )
            surviving = _keys("dd-input-bucket")

        assert result.exit_code == 1
        assert "Selected 2 document(s) for deletion" in result.output
        assert "Deleted 0/2 document(s)" in result.output
        assert "2 failed" in result.output
        # The per-document failure list is printed *before* the exit, which is the
        # half an early `sys.exit` would have lost.
        assert "Failed deletions:" in result.output
        assert surviving == [key for key, _ in SEEDED_DOCUMENTS]

    def test_a_partial_failure_still_exits_zero_and_that_is_the_residual(self):
        """A per-document failure list is the only route to a manual retry.

        Also the boundary of the #1230 fix, stated so the next reader does not have
        to infer it: a run in which *every* deletion failed now exits 1, and a run in
        which *some* did still exits 0. #1230 enumerated only the total failure, so
        that is what was changed; a caller cannot distinguish a partial failure from a
        clean run by exit code, and the per-document list asserted below is the only
        signal. This is a residual, not a contract worth defending.
        """
        failing_result = {
            "success": False,
            "deleted_count": 1,
            "failed_count": 1,
            "total_count": 2,
            "dry_run": False,
            "results": [
                {"success": True, "object_key": "batch-1/first.pdf", "errors": []},
                {
                    "success": False,
                    "object_key": "batch-1/second.pdf",
                    "errors": ["Error deleting from input bucket: AccessDenied"],
                },
            ],
        }

        with mock_aws():
            _seed_documents()
            with patch(
                "idp_common.delete_documents.delete_documents",
                return_value=failing_result,
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "delete-documents",
                        "--stack-name",
                        "dd-stack",
                        "--batch-id",
                        "batch-1",
                        "--force",
                    ],
                )

        assert result.exit_code == 0
        assert "Deleted 1/2 document(s)" in result.output
        assert "1 failed" in result.output
        assert "batch-1/second.pdf" in result.output
        assert "AccessDenied" in result.output

    def test_the_failure_list_is_truncated_after_five(self):
        """Six failures print five plus a count, keeping the summary readable."""
        results = [
            {
                "success": False,
                "object_key": f"batch-1/doc{index}.pdf",
                "errors": ["AccessDenied"],
            }
            for index in range(6)
        ]
        failing_result = {
            "success": False,
            "deleted_count": 0,
            "failed_count": 6,
            "total_count": 6,
            "dry_run": False,
            "results": results,
        }

        with mock_aws():
            _seed_documents()
            with patch(
                "idp_common.delete_documents.delete_documents",
                return_value=failing_result,
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "delete-documents",
                        "--stack-name",
                        "dd-stack",
                        "--batch-id",
                        "batch-1",
                        "--force",
                    ],
                )

        assert "... and 1 more failures" in result.output
        assert "batch-1/doc4.pdf" in result.output
        assert "batch-1/doc5.pdf" not in result.output

    def test_an_unexpected_error_exits_non_zero(self):
        """An exception from the deletion helper must not exit 0."""
        with mock_aws():
            _seed_documents()
            with patch(
                "idp_common.delete_documents.delete_documents",
                side_effect=RuntimeError("tracking table throttled"),
            ):
                result = CliRunner().invoke(
                    cli,
                    [
                        "delete-documents",
                        "--stack-name",
                        "dd-stack",
                        "--batch-id",
                        "batch-1",
                        "--force",
                    ],
                )

        assert result.exit_code == 1
        assert "tracking table throttled" in result.output

    def test_continue_on_error_is_requested_so_one_bad_key_stops_nothing(self):
        """The command asks for `continue_on_error=True` and a real `dry_run` flag.

        These two arguments decide whether a single failing document aborts the rest
        of a batch, and whether anything is deleted at all, so they are asserted on
        the call rather than inferred from the output.
        """
        with mock_aws():
            _seed_documents()
            with patch(
                "idp_common.delete_documents.delete_documents",
                return_value={
                    "success": True,
                    "deleted_count": 2,
                    "failed_count": 0,
                    "total_count": 2,
                    "dry_run": False,
                    "results": [],
                },
            ) as delete_documents:
                CliRunner().invoke(
                    cli,
                    [
                        "delete-documents",
                        "--stack-name",
                        "dd-stack",
                        "--batch-id",
                        "batch-1",
                        "--force",
                    ],
                )

        kwargs = delete_documents.call_args.kwargs
        assert kwargs["continue_on_error"] is True
        assert kwargs["dry_run"] is False
        assert kwargs["object_keys"] == ["batch-1/first.pdf", "batch-1/second.pdf"]
        assert kwargs["input_bucket"] == "dd-input-bucket"
        assert kwargs["output_bucket"] == "dd-output-bucket"
