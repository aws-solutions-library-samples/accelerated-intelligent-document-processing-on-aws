"""Unit tests for queue_processor.check_circuit_breaker()."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_index_under_test"


@pytest.fixture
def index_module(monkeypatch):
    """Import index with idp_common + boto3 mocked out."""
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:test",
        "MAX_CONCURRENT": "5",
        "CIRCUIT_BREAKER_ENABLED": "true",
        "DOCUMENT_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123/test-queue",
        "RECOVERY_TIMEOUT_SECONDS": "300",
    }

    fake_idp_common = MagicMock()
    fake_models = MagicMock()
    fake_models.Document = MagicMock()
    fake_models.Status = MagicMock()
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_config = MagicMock()

    fake_xray_core = MagicMock()
    fake_xray_core.xray_recorder = MagicMock()
    fake_xray_core.patch_all = MagicMock()

    module_patches = {
        "idp_common": fake_idp_common,
        "idp_common.models": fake_models,
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": fake_config,
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": fake_xray_core,
    }
    for name, mod in module_patches.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with patch.dict(os.environ, env_vars, clear=False), \
         patch("boto3.resource") as mock_resource, \
         patch("boto3.client") as mock_client:
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_client.return_value = MagicMock()

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.concurrency_table = mock_table
        module.sqs = MagicMock()
        yield module
        sys.modules.pop(_MODULE_NAME, None)


class TestCheckCircuitBreaker:
    """Covers all branches of check_circuit_breaker()."""

    def test_disabled_returns_allowed(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = False
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "DISABLED"
        index_module.concurrency_table.get_item.assert_not_called()

    def test_item_missing_returns_closed(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {}
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "CLOSED"

    def test_state_open_blocks(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "OPEN"}
        }
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == "OPEN"

    def test_state_half_open_allows_probe(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.return_value = {
            "Item": {"state": "HALF_OPEN"}
        }
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "HALF_OPEN"

    def test_ddb_error_fails_open(self, index_module):
        """DynamoDB errors must not block traffic - fail open with 'ERROR' state."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}},
            "GetItem",
        )
        allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == "ERROR"


class TestExtendVisibilityForOutage:
    """OPEN-state messages should have their SQS visibility pushed out so
    maxReceiveCount isn't burned during a long Bedrock outage."""

    def test_extends_visibility_to_recovery_timeout(self, index_module):
        index_module.extend_visibility_for_outage("receipt-abc")
        index_module.sqs.change_message_visibility.assert_called_once_with(
            QueueUrl="https://sqs.us-east-1.amazonaws.com/123/test-queue",
            ReceiptHandle="receipt-abc",
            VisibilityTimeout=300,
        )

    def test_noop_when_queue_url_missing(self, index_module):
        index_module.DOCUMENT_QUEUE_URL = ""
        index_module.extend_visibility_for_outage("receipt-abc")
        index_module.sqs.change_message_visibility.assert_not_called()

    def test_sqs_failure_is_non_fatal(self, index_module):
        """A failed visibility update should be logged, not raised - the
        message just retries on the default 30s timeout."""
        index_module.sqs.change_message_visibility.side_effect = ClientError(
            {"Error": {"Code": "ReceiptHandleIsInvalid", "Message": "nope"}},
            "ChangeMessageVisibility",
        )
        index_module.extend_visibility_for_outage("receipt-abc")
