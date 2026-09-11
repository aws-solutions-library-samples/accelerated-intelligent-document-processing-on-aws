"""
Config-revision plumbing: CLI flag -> SDK -> object metadata / run payload.

Why this file exists
--------------------
`--config-revision` was documented in the CHANGELOG, in
`docs/configuration-profiles.md`, in `docs/idp-cli.md`, and in the PR that
"added" it — and it was never actually in the CLI. Every check passed, because
nothing asserted the command's option surface: the SDK gained the parameter, the
CLI never did, and no test looked.

These tests assert the flag exists, reaches the SDK, and survives to the two
places it has to arrive: S3 object metadata (for a document) and the test-runner
payload (for a test set). A silently dropped revision is worse than a rejected
one — the caller believes they pinned r7 while the run used whatever the profile
currently holds, and those numbers then go into a comparison.
"""

from unittest.mock import MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

from idp_cli.cli import cli


@pytest.mark.unit
@pytest.mark.parametrize("command", ["process", "run-inference"])
def test_the_flag_is_exposed(command):
    result = CliRunner().invoke(cli, [command, "--help"])
    assert result.exit_code == 0
    assert "--config-revision" in result.output, (
        f"`{command} --help` does not offer --config-revision, which the CHANGELOG "
        f"and docs/idp-cli.md both promise"
    )


@pytest.mark.unit
@pytest.mark.parametrize("command", ["process", "run-inference"])
def test_the_flag_reaches_the_sdk(command):
    """The value must arrive at batch.process(), not just be accepted and dropped."""
    with patch("idp_sdk.IDPClient") as mock_client_cls:
        client = MagicMock()
        mock_client_cls.return_value = client
        client.batch.process.return_value = {
            "batch_id": "b1",
            "document_ids": [],
            "queued": 0,
            "uploaded": 0,
            "failed": 0,
        }
        CliRunner().invoke(
            cli,
            [
                command,
                "--stack-name",
                "s",
                "--dir",
                ".",
                "--config-version",
                "lending",
                "--config-revision",
                "7",
            ],
        )
        assert client.batch.process.called, "batch.process was never reached"
        kwargs = client.batch.process.call_args.kwargs
        assert kwargs.get("config_version") == "lending"
        assert kwargs.get("config_revision") == 7


@pytest.mark.unit
def test_the_flag_is_typed_as_an_integer():
    """A revision is a number; `--config-revision abc` must be rejected up front."""
    result = CliRunner().invoke(
        cli,
        ["process", "--stack-name", "s", "--dir", ".", "--config-revision", "abc"],
    )
    assert result.exit_code != 0
    assert "abc" in result.output


@pytest.mark.unit
def test_object_metadata_carries_the_revision():
    """A pinned revision travels to the pipeline as `config-revision` metadata."""
    with patch("idp_sdk._core.batch_processor.StackInfo") as mock_stack_info:
        mock_stack_info.return_value.validate_stack.return_value = True
        mock_stack_info.return_value.get_resources.return_value = {
            "InputBucket": "test-input-bucket",
            "TestSetBucket": "test-set-bucket",
        }
        with (
            patch("boto3.client") as mock_boto3_client,
            patch("boto3.resource"),
        ):
            from idp_sdk._core.batch_processor import BatchProcessor

            mock_s3 = Mock()
            mock_boto3_client.return_value = mock_s3
            processor = BatchProcessor("test-stack", "us-east-1")
            processor._copy_s3_file(
                {"path": "s3://src/doc.pdf", "filename": "doc.pdf"},
                "batch-1",
                "lending",
                7,
            )
            metadata = mock_s3.copy_object.call_args.kwargs["Metadata"]
            assert metadata["config-version"] == "lending"
            assert metadata["config-revision"] == "7"


@pytest.mark.unit
def test_the_test_runner_payload_carries_the_revision():
    """
    The test-set path is a separate code path from documents, and it silently
    dropped the revision until this was fixed — the run recorded a profile while
    processing under whatever that profile currently held.
    """
    from idp_sdk.operations.batch import BatchOperation

    ops = BatchOperation.__new__(BatchOperation)
    ops._client = MagicMock()
    ops._client._region = "us-east-1"
    ops._client._require_stack.return_value = "stack"
    processor = MagicMock()
    with patch("boto3.client") as mock_boto3_client:
        lambda_client = MagicMock()
        mock_boto3_client.return_value = lambda_client
        lambda_client.get_paginator.return_value.paginate.return_value = [
            {"Functions": [{"FunctionName": "stack-APIRESOLVE-TestRunnerFunction-x"}]}
        ]
        body = MagicMock()
        body.read.return_value = b'{"testRunId": "run-1"}'
        lambda_client.invoke.return_value = {"Payload": body}
        try:
            ops._process_test_set(processor, "ts1", None, None, "lending", 7)
        except Exception:
            # Later steps (monitoring, document ids) are out of scope; the payload
            # is asserted from the invoke call that already happened.
            pass
        payloads = [
            call.kwargs.get("Payload", "")
            for call in lambda_client.invoke.call_args_list
        ]
        assert any('"configRevision": 7' in p for p in payloads), payloads
