"""Unit tests for queue_processor.check_circuit_breaker()."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError

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


def _ddb_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "GetItem")


class TestCheckCircuitBreakerStateReadFailures:
    """A failed state read is judged by whether a retry can fix it (#934 item 4).

    Transient -> REFUSE admission: the message goes back on DocumentQueue, whose
    500 x 60 s redrive budget absorbs roughly 8 hours of this, so a blip cannot
    reach the dead-letter queue. Terminal -> ALLOW admission: it cannot be
    retried away, so refusing would send the whole backlog to the DLQ for a
    misconfiguration while the pipeline is otherwise healthy.
    """

    # Codes DynamoDB really returns, one per class. The transient ones are
    # classified by idp_common.utils.transient_errors, which the conftest loads
    # for real precisely so this split is not asserted against a mock's opinion.
    TRANSIENT_CODES = (
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "InternalServerError",
        "RequestLimitExceeded",
    )
    TERMINAL_CODES = (
        "AccessDeniedException",
        "ResourceNotFoundException",
        "ValidationException",
        "UnrecognizedClientException",
    )

    @pytest.mark.parametrize("code", TRANSIENT_CODES)
    def test_transient_ddb_error_refuses_admission(self, index_module, code):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(code)
        with patch("boto3.client") as mock_client:
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False, (
            f"{code} is retryable, so admission must be refused rather than "
            f"letting a coincident DynamoDB fault switch the breaker off"
        )
        assert state == index_module.CB_STATE_ERROR_TRANSIENT
        _assert_failure_metric(mock_client, "TRANSIENT")

    @pytest.mark.parametrize("code", TERMINAL_CODES)
    def test_terminal_ddb_error_admits_but_reports(self, index_module, code):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(code)
        with patch("boto3.client") as mock_client:
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is True, (
            f"{code} cannot be retried away; refusing every message would drain "
            f"the backlog into the dead-letter queue"
        )
        assert state == index_module.CB_STATE_ERROR_TERMINAL
        _assert_failure_metric(mock_client, "TERMINAL")

    def test_network_failure_is_transient(self, index_module):
        """BotoCoreError bypasses ClientError and carries no error code."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = EndpointConnectionError(
            endpoint_url="https://dynamodb.example"
        )
        with patch("boto3.client"):
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == index_module.CB_STATE_ERROR_TRANSIENT

    def test_unclassifiable_failure_is_treated_as_terminal(self, index_module):
        """An error the classifier has no verdict for must not stall the queue."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = BotoCoreError()
        with patch("boto3.client"):
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is True
        assert state == index_module.CB_STATE_ERROR_TERMINAL

    def test_metric_failure_does_not_change_the_decision(self, index_module):
        """Telemetry is best effort; it must not raise out of the check."""
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "ThrottlingException"
        )
        with patch("boto3.client") as mock_client:
            mock_client.return_value.put_metric_data.side_effect = RuntimeError("no")
            allowed, state = index_module.check_circuit_breaker()
        assert allowed is False
        assert state == index_module.CB_STATE_ERROR_TRANSIENT

    def test_disabled_breaker_never_reads_the_table(self, index_module):
        """The blast radius of everything above is opt-in deployments only:
        CircuitBreakerEnabled defaults to "false" in template.yaml, and on that
        default the table is never touched, so no DynamoDB fault can be
        classified either way."""
        index_module.CIRCUIT_BREAKER_ENABLED = False
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "AccessDeniedException"
        )
        allowed, state = index_module.check_circuit_breaker()
        assert (allowed, state) == (True, index_module.CB_STATE_DISABLED)
        index_module.concurrency_table.get_item.assert_not_called()


def _assert_failure_metric(mock_client: MagicMock, classification: str) -> None:
    """The degraded read must be visible in CloudWatch, with which half it was."""
    mock_client.assert_any_call("cloudwatch")
    calls = mock_client.return_value.put_metric_data.call_args_list
    assert len(calls) == 1, f"expected one PutMetricData call, got {len(calls)}"
    datum = calls[0].kwargs["MetricData"][0]
    assert datum["MetricName"] == "CircuitBreakerCheckFailed"
    assert datum["Dimensions"] == [{"Name": "Classification", "Value": classification}]


class TestCircuitBreakerAdmissionTable:
    """Guard against the admission decision being inverted, or a new state
    silently inheriting whichever branch it happens to fall into.

    The table is the contract. It is compared against every ``CB_STATE_*``
    constant the module defines, in both directions, so adding a state without
    deciding what it admits fails here rather than at 3am.
    """

    #: state -> may the queue processor start a workflow in it?
    EXPECTED_ADMISSION = {
        "CLOSED": True,
        "OPEN": False,
        "HALF_OPEN": True,
        "DISABLED": True,
        "ERROR_TRANSIENT": False,
        "ERROR_TERMINAL": True,
    }

    def test_every_declared_state_is_in_the_table(self, index_module):
        declared = {
            value
            for name, value in vars(index_module).items()
            if name.startswith("CB_STATE_") and isinstance(value, str)
        }
        assert declared == set(self.EXPECTED_ADMISSION), (
            "a circuit-breaker state was added or renamed without deciding "
            "whether it admits work; add it to EXPECTED_ADMISSION"
        )

    @pytest.mark.parametrize("state,expected", sorted(EXPECTED_ADMISSION.items()))
    def test_admission_matches_the_table(self, index_module, state, expected):
        index_module.CIRCUIT_BREAKER_ENABLED = state != "DISABLED"
        if state == "ERROR_TRANSIENT":
            index_module.concurrency_table.get_item.side_effect = _ddb_error(
                "ThrottlingException"
            )
        elif state == "ERROR_TERMINAL":
            index_module.concurrency_table.get_item.side_effect = _ddb_error(
                "AccessDeniedException"
            )
        else:
            index_module.concurrency_table.get_item.return_value = {
                "Item": {"state": state}
            }

        with patch("boto3.client"):
            allowed, reported = index_module.check_circuit_breaker()

        assert allowed is expected, (
            f"circuit breaker state {state} must "
            f"{'allow' if expected else 'block'} new workflows"
        )
        assert reported == state


class TestCallerHonoursTheRefusal:
    """A decision nobody acts on is the defect this whole area keeps producing,
    so assert the refusal reaches SQS rather than only the return value."""

    @staticmethod
    def _record() -> dict:
        return {
            "body": json.dumps({"id": "doc-1"}),
            "messageId": "msg-1",
            "receiptHandle": "receipt-1",
        }

    def test_transient_read_failure_starts_no_workflow(self, index_module):
        index_module.CIRCUIT_BREAKER_ENABLED = True
        index_module.sfn = MagicMock()
        index_module.concurrency_table.get_item.side_effect = _ddb_error(
            "ThrottlingException"
        )
        with patch("boto3.client"):
            success, message_id = index_module.process_message(self._record())

        assert success is False, "the message must be reported as a batch failure"
        assert message_id == "msg-1"
        index_module.sfn.start_execution.assert_not_called()
        index_module.concurrency_table.update_item.assert_not_called()
        # Only OPEN extends visibility: a transient read failure should be gone
        # by the next 60 s delivery, and a 300 s push would add five minutes of
        # latency per document for a fault that lasted milliseconds.
        index_module.sqs.change_message_visibility.assert_not_called()
        # Nothing was acked, so the message stays on DocumentQueue.
        index_module.sqs.delete_message.assert_not_called()


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
