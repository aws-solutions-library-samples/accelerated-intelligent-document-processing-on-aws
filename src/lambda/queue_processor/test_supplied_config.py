# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A document uploaded with `config-uri` metadata is staged before it takes a slot.

`stage_supplied_config` validates the named configuration and swaps
`document.config_uri` for an immutable snapshot, so every step reads the same
bytes. A configuration that cannot be used rejects the document outright —
FAILED, message acked, no concurrency slot taken — while a transient S3 error
leaves the message to be retried. The validation itself is
`idp_common.config.config_uri`'s and is tested there; these tests pin what the
queue processor does with its verdict.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_supplied_config_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"
WORKING = "test-working-bucket"
SOURCE = "s3://test-input-bucket/configs/w2.json"
SNAPSHOT = f"s3://{WORKING}/config_snapshots/abc123.json"


class _ConfigUriError(ValueError):
    """Stands in for idp_common.config.ConfigUriError, which the suite stubs."""


class _Doc:
    def __init__(self, config_uri=None):
        self.id = "input/w2.pdf"
        self.input_key = "input/w2.pdf"
        self.input_bucket = "test-input-bucket"
        self.config_uri = config_uri
        self.metadata = {}
        self.errors = []
        self.status = None
        self.completion_time = None
        self.trace_id = None


@pytest.fixture
def index_module(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": SM_ARN,
        "MAX_CONCURRENT": "100",
        "WORKING_BUCKET": WORKING,
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    for name, mod in {
        "idp_common": MagicMock(),
        "idp_common.models": MagicMock(),
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": MagicMock(),
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": MagicMock(),
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with (
        patch.dict(os.environ, env_vars, clear=False),
        patch("boto3.resource") as mock_resource,
        patch("boto3.client") as mock_client,
    ):
        mock_resource.return_value.Table.return_value = MagicMock()
        mock_client.return_value = MagicMock()

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.SNAPSHOT_PREFIX = "config_snapshots"
        module.ConfigUriError = _ConfigUriError
        module.prepare_config_snapshot = MagicMock(return_value=SNAPSHOT)
        module.document_service.get_document = MagicMock(return_value=None)
        module.document_service.update_document = MagicMock()
        module.check_circuit_breaker = MagicMock(return_value=(True, "CLOSED"))
        module.update_counter = MagicMock(return_value=True)
        module.ack_message = MagicMock(return_value=True)
        module.start_workflow = MagicMock(return_value={"executionArn": "e"})
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _process(index_module, doc):
    index_module.Document.load_document = MagicMock(return_value=doc)
    record = {
        "body": json.dumps({"input_key": doc.input_key}),
        "messageId": "m-1",
        "receiptHandle": "rh-1",
    }
    return index_module.process_message(record)


@pytest.mark.unit
class TestStageSuppliedConfig:
    def test_a_document_without_config_uri_is_untouched(self, index_module):
        doc = _Doc()
        assert index_module.stage_supplied_config(doc) is None
        index_module.prepare_config_snapshot.assert_not_called()
        assert doc.config_uri is None

    def test_the_source_is_replaced_by_its_snapshot(self, index_module):
        """Every step then reads the snapshot, which cannot change under it."""
        doc = _Doc(config_uri=SOURCE)
        assert index_module.stage_supplied_config(doc) is None
        assert doc.config_uri == SNAPSHOT
        assert doc.metadata["config_source_uri"] == SOURCE
        index_module.prepare_config_snapshot.assert_called_once_with(
            SOURCE, allowed_bucket="test-input-bucket", working_bucket=WORKING
        )

    def test_an_existing_snapshot_is_not_staged_again(self, index_module):
        """A snapshot URI is in the working bucket, which the input-bucket rule
        would otherwise reject."""
        doc = _Doc(config_uri=SNAPSHOT)
        assert index_module.stage_supplied_config(doc) is None
        index_module.prepare_config_snapshot.assert_not_called()

    def test_an_unusable_configuration_returns_the_reason(self, index_module):
        index_module.prepare_config_snapshot.side_effect = _ConfigUriError("bad dpi")
        doc = _Doc(config_uri=SOURCE)
        assert index_module.stage_supplied_config(doc) == "bad dpi"


@pytest.mark.unit
class TestRejectionInProcessMessage:
    def test_a_rejected_document_is_failed_acked_and_holds_no_slot(self, index_module):
        index_module.prepare_config_snapshot.side_effect = _ConfigUriError(
            "use_bda: true is not supported"
        )
        doc = _Doc(config_uri=SOURCE)

        ok, mid = _process(index_module, doc)

        assert (ok, mid) == (True, "m-1"), "rejection is final — do not retry"
        assert doc.status == index_module.Status.FAILED
        assert doc.errors == ["use_bda: true is not supported"]
        assert doc.completion_time
        index_module.document_service.update_document.assert_called_once_with(doc)
        index_module.ack_message.assert_called_once_with("rh-1")
        index_module.update_counter.assert_not_called()
        index_module.start_workflow.assert_not_called()

    def test_a_transient_s3_error_retries_without_rejecting(self, index_module):
        index_module.prepare_config_snapshot.side_effect = ClientError(
            {"Error": {"Code": "SlowDown", "Message": "slow down"}}, "GetObject"
        )
        doc = _Doc(config_uri=SOURCE)

        ok, _ = _process(index_module, doc)

        assert ok is False, "the message must come back"
        assert doc.status is None
        index_module.document_service.update_document.assert_not_called()
        index_module.update_counter.assert_not_called()

    def test_a_valid_configuration_starts_the_workflow_on_its_snapshot(
        self, index_module
    ):
        doc = _Doc(config_uri=SOURCE)

        ok, _ = _process(index_module, doc)

        assert ok is True
        started = index_module.start_workflow.call_args.args[0]
        assert started.config_uri == SNAPSHOT
