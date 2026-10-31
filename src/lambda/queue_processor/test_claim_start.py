# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The last-moment start claim, measured from the handler's own tree (#1612).

``claim_start`` re-takes the "is this document ABORTED?" decision with a
conditional write immediately before ``StartExecution``, and ``process_message``
hands the concurrency slot back and acks the message when the claim says no.
The behaviour is exercised end to end against moto in
``lib/idp_common_pkg/tests/unit/lambdas/test_queued_abort_race.py``; these
hermetic tests pin the same branches here, where the ``src_lambda`` coverage
tree measures this file.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_claim_start_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test-Workflow"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/test-DocumentQueue"
MESSAGE_ID = "3f0c4e2a-8b7d-4c1e-9a2f-6d5b4c3a2b1e"
KEY = "input/w2/form.pdf"


@pytest.fixture
def qp(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": SM_ARN,
        "MAX_CONCURRENT": "100",
        "METRIC_NAMESPACE": "TestStack",
        "DOCUMENT_QUEUE_URL": QUEUE_URL,
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_xray_core = MagicMock()
    fake_xray_core.xray_recorder.capture.return_value = lambda fn: fn
    for name, mod in {
        "idp_common": MagicMock(),
        "idp_common.models": MagicMock(),
        "idp_common.docs_service": fake_docs_service,
        "idp_common.config": MagicMock(),
        "aws_xray_sdk": MagicMock(),
        "aws_xray_sdk.core": fake_xray_core,
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

        doc = MagicMock()
        doc.input_key = KEY
        doc.id = KEY
        doc.config_version = "default"
        doc.config_revision = 3
        doc.workflow_execution_arn = None
        doc.to_dict = MagicMock(return_value={"id": KEY})
        module.Document.load_document = MagicMock(return_value=doc)
        svc = module.document_service
        # Not aborted when the message is read; the claim decides afterwards.
        svc.get_document = MagicMock(return_value=None)
        svc.update_document = MagicMock(return_value=doc)
        svc.claim_for_execution = MagicMock(return_value=True)
        module.check_circuit_breaker = MagicMock(return_value=(True, "CLOSED"))
        module.update_counter = MagicMock(return_value=True)
        module.sfn = MagicMock()
        module.sfn.start_execution = MagicMock(
            return_value={
                "executionArn": f"{SM_ARN.replace('stateMachine', 'execution')}:e1"
            }
        )
        module.sqs = MagicMock()
        module._doc = doc
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _doc_with(module, status):
    current = MagicMock()
    current.status = status
    return current


def _abort_after_message_read(module):
    """The claim is refused and the consistent re-read finds ABORTED."""
    svc = module.document_service
    svc.claim_for_execution.return_value = False
    aborted = _doc_with(module, module.Status.ABORTED)

    def get_document(key, consistent_read=False):
        return aborted if consistent_read else None

    svc.get_document = MagicMock(side_effect=get_document)


class TestClaimStart:
    def test_a_granted_claim_starts(self, qp):
        assert qp.claim_start(KEY) is True
        qp.document_service.claim_for_execution.assert_called_once_with(KEY)

    def test_a_refused_claim_on_an_aborted_document_does_not_start(self, qp):
        _abort_after_message_read(qp)
        assert qp.claim_start(KEY) is False
        qp.document_service.get_document.assert_called_once_with(
            KEY, consistent_read=True
        )

    def test_a_refused_claim_with_no_tracking_record_starts(self, qp):
        qp.document_service.claim_for_execution.return_value = False
        qp.document_service.get_document = MagicMock(return_value=None)
        assert qp.claim_start(KEY) is True

    def test_a_refused_claim_on_a_document_that_is_not_aborted_starts(self, qp):
        qp.document_service.claim_for_execution.return_value = False
        qp.document_service.get_document = MagicMock(
            return_value=_doc_with(qp, qp.Status.RUNNING)
        )
        assert qp.claim_start(KEY) is True

    def test_a_claim_that_raises_starts(self, qp):
        qp.document_service.claim_for_execution.side_effect = RuntimeError("ddb")
        assert qp.claim_start(KEY) is True

    def test_a_refused_claim_whose_re_read_raises_starts(self, qp):
        qp.document_service.claim_for_execution.return_value = False
        qp.document_service.get_document = MagicMock(side_effect=RuntimeError("ddb"))
        assert qp.claim_start(KEY) is True


def _record():
    return {
        "messageId": MESSAGE_ID,
        "receiptHandle": "rh-1",
        "body": json.dumps({"id": KEY}),
    }


class TestAbortedBeforeStart:
    def test_start_workflow_raises_and_creates_no_execution(self, qp):
        _abort_after_message_read(qp)
        with pytest.raises(qp.DocumentAbortedBeforeStart):
            qp.start_workflow(qp._doc, "form.pdf-x")
        qp.sfn.start_execution.assert_not_called()

    def test_process_message_returns_the_slot_and_acks(self, qp):
        _abort_after_message_read(qp)
        success, message_id = qp.process_message(_record())
        assert (success, message_id) == (True, MESSAGE_ID)
        qp.sfn.start_execution.assert_not_called()
        assert [c.kwargs for c in qp.update_counter.call_args_list] == [
            {"increment": True},
            {"increment": False},
        ]
        qp.sqs.delete_message.assert_called_once()
        assert qp.sqs.delete_message.call_args.kwargs["ReceiptHandle"] == "rh-1"

    def test_a_failed_slot_return_still_acks(self, qp):
        _abort_after_message_read(qp)
        qp.update_counter.side_effect = [True, RuntimeError("ddb down")]
        qp._emit_concurrency_event_metric = MagicMock()
        success, _ = qp.process_message(_record())
        assert success is True
        qp._emit_concurrency_event_metric.assert_called_once_with(
            "ConcurrencyCounterUpdateFailed"
        )
        qp.sqs.delete_message.assert_called_once()


class _Refused(Exception):
    """What the document service raises for a refused condition."""

    error_code = "ConditionalCheckFailedException"


EXECUTION_ARN = f"{SM_ARN.replace('stateMachine', 'execution')}:e1"


class TestPostStartWrite:
    """The write after StartExecution is refused for an ABORTED document (#1612).

    End to end against moto in ``test_queued_abort_race.py``; pinned here for
    the ``src_lambda`` coverage tree.
    """

    def test_the_write_is_conditional_on_the_document_not_being_aborted(self, qp):
        success, _ = qp.process_message(_record())

        assert success is True
        qp.document_service.update_document.assert_called_once_with(
            qp._doc, unless_status=qp.Status.ABORTED
        )
        qp.sfn.stop_execution.assert_not_called()

    def test_a_refused_write_stops_the_execution_and_keeps_the_slot(self, qp):
        qp.document_service.update_document.side_effect = _Refused("aborted")

        success, message_id = qp.process_message(_record())

        assert (success, message_id) == (True, MESSAGE_ID)
        qp.sfn.stop_execution.assert_called_once()
        assert qp.sfn.stop_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
        # The execution owns the slot; the tracker releases it on ABORTED.
        assert [c.kwargs for c in qp.update_counter.call_args_list] == [
            {"increment": True}
        ]
        qp.sqs.delete_message.assert_called_once()

    def test_any_other_write_failure_stops_nothing(self, qp):
        qp.document_service.update_document.side_effect = RuntimeError("throttled")

        success, _ = qp.process_message(_record())

        assert success is True
        qp.sfn.stop_execution.assert_not_called()
        assert [c.kwargs for c in qp.update_counter.call_args_list] == [
            {"increment": True}
        ]

    def test_stop_aborted_start_reports_a_stop(self, qp):
        assert qp.stop_aborted_start(KEY, EXECUTION_ARN) is True
        qp.sfn.stop_execution.assert_called_once_with(
            executionArn=EXECUTION_ARN,
            error="DocumentAborted",
            cause="The document was aborted while its workflow was being started",
        )

    def test_stop_aborted_start_reports_a_failed_stop(self, qp):
        qp.sfn.stop_execution.side_effect = RuntimeError("throttled")
        assert qp.stop_aborted_start(KEY, EXECUTION_ARN) is False

    @pytest.mark.parametrize("arn", [None, ""])
    def test_stop_aborted_start_without_an_arn_calls_nothing(self, qp, arn):
        assert qp.stop_aborted_start(KEY, arn) is False
        qp.sfn.stop_execution.assert_not_called()

    def test_a_failed_write_on_a_document_found_aborted_stops_it(self, qp):
        """A throttled write says nothing about an abort; the re-read does."""
        qp.document_service.update_document.side_effect = RuntimeError("throttled")
        _abort_after_message_read(qp)
        qp.document_service.claim_for_execution.return_value = True

        success, _ = qp.process_message(_record())

        assert success is True
        qp.document_service.get_document.assert_called_with(KEY, consistent_read=True)
        qp.sfn.stop_execution.assert_called_once()
        assert qp.sfn.stop_execution.call_args.kwargs["executionArn"] == EXECUTION_ARN
        assert [c.kwargs for c in qp.update_counter.call_args_list] == [
            {"increment": True}
        ]

    def test_a_failed_write_whose_re_read_fails_stops_nothing(self, qp):
        qp.document_service.update_document.side_effect = RuntimeError("throttled")
        reads = []

        def get_document(key, consistent_read=False):
            reads.append(consistent_read)
            if consistent_read:
                raise RuntimeError("throttled too")
            return None

        qp.document_service.get_document = MagicMock(side_effect=get_document)

        success, _ = qp.process_message(_record())

        assert success is True
        assert reads == [False, True]
        qp.sfn.stop_execution.assert_not_called()

    def test_a_failed_write_on_a_document_that_is_not_aborted_stops_nothing(self, qp):
        qp.document_service.update_document.side_effect = RuntimeError("throttled")
        qp.document_service.get_document = MagicMock(
            return_value=_doc_with(qp, qp.Status.QUEUED)
        )

        success, _ = qp.process_message(_record())

        assert success is True
        qp.document_service.get_document.assert_called_with(KEY, consistent_read=True)
        qp.sfn.stop_execution.assert_not_called()
