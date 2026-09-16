# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One SQS message starts at most one workflow, however many times it is delivered.

Issue #904: under a saturated queue most QueueProcessor invocations timed out.
Lambda reports a batch outcome only when the function returns, so a timeout
handed the entire batch back to SQS, including messages whose StartExecution had
already succeeded, and each redelivery minted a fresh execution because none had
a name. One message was delivered 24 times and started 6 workflows; the batch was
over-processed 3.36x.

Two independent defences are pinned here:

1. The execution name is derived from the SQS message id, so a redelivery gets
   ``ExecutionAlreadyExists`` and is acked without a second execution (and
   without keeping the slot it took for a start that did not happen).
2. Each message is deleted the moment its execution exists, so a batch that dies
   later no longer redelivers it at all.
"""

import importlib.util
import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_idempotent_start_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test-Workflow"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/test-DocumentQueue"
MESSAGE_ID = "3f0c4e2a-8b7d-4c1e-9a2f-6d5b4c3a2b1e"


def _already_exists() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
        "StartExecution",
    )


@pytest.fixture
def index_module(monkeypatch):
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
        doc.input_key = "input/statements/bank statement (march).pdf"
        doc.id = doc.input_key
        doc.config_version = "default"
        doc.config_revision = 3
        doc.workflow_execution_arn = None
        doc.to_dict = MagicMock(return_value={"id": doc.id})
        module.Document.load_document = MagicMock(return_value=doc)
        module.document_service.get_document = MagicMock(return_value=None)
        module.document_service.update_document = MagicMock(return_value=doc)
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


def _record(message_id=MESSAGE_ID, receipt="receipt-1"):
    return {
        "messageId": message_id,
        "receiptHandle": receipt,
        "body": json.dumps({"id": "input/statements/bank statement (march).pdf"}),
    }


class TestExecutionName:
    def test_is_deterministic_and_carries_the_message_id(self, index_module):
        name = index_module.execution_name_for("input/a/b.pdf", MESSAGE_ID)
        assert name == index_module.execution_name_for("input/a/b.pdf", MESSAGE_ID)
        assert name == f"b.pdf-{MESSAGE_ID}"

    def test_different_messages_for_the_same_document_get_different_names(
        self, index_module
    ):
        first = index_module.execution_name_for("input/a.pdf", MESSAGE_ID)
        second = index_module.execution_name_for(
            "input/a.pdf", "0" * 8 + MESSAGE_ID[8:]
        )
        assert first != second

    @pytest.mark.parametrize(
        "key",
        [
            "input/bank statement (march).pdf",
            "input/ünïcödé: файл/report#1;2,3.PDF",
            "input/" + "x" * 300 + ".pdf",
            "",
            "trailing/slash/",
            "input/...",
        ],
    )
    def test_names_are_always_valid_step_functions_names(self, index_module, key):
        name = index_module.execution_name_for(key, MESSAGE_ID)
        assert name is not None
        assert 1 <= len(name) <= 80
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name)
        assert name.endswith(MESSAGE_ID)

    def test_an_unsafe_message_id_is_hashed_rather_than_rejected(self, index_module):
        name = index_module.execution_name_for("a.pdf", "not a uuid / at all")
        assert re.fullmatch(r"a\.pdf-[0-9a-f]{32}", name)
        assert name == index_module.execution_name_for("a.pdf", "not a uuid / at all")

    def test_no_message_id_means_no_name(self, index_module):
        assert index_module.execution_name_for("a.pdf", "") is None

    def test_arn_for_name_matches_step_functions_layout(self, index_module):
        assert (
            index_module.execution_arn_for("b.pdf-x")
            == "arn:aws:states:us-east-1:123456789012:execution:test-Workflow:b.pdf-x"
        )


class TestStartWorkflowPassesTheName:
    def test_name_reaches_start_execution(self, index_module):
        index_module.start_workflow(index_module._doc, "b.pdf-abc")
        kwargs = index_module.sfn.start_execution.call_args.kwargs
        assert kwargs["name"] == "b.pdf-abc"
        assert kwargs["stateMachineArn"] == SM_ARN

    def test_without_a_name_step_functions_picks_one(self, index_module):
        index_module.start_workflow(index_module._doc)
        assert "name" not in index_module.sfn.start_execution.call_args.kwargs

    def test_already_exists_becomes_a_typed_signal_with_the_arn(self, index_module):
        index_module.sfn.start_execution.side_effect = _already_exists()
        with pytest.raises(index_module.ExecutionAlreadyStarted) as info:
            index_module.start_workflow(index_module._doc, "b.pdf-abc")
        assert info.value.execution_arn.endswith(":execution:test-Workflow:b.pdf-abc")

    def test_already_exists_without_a_name_is_an_ordinary_error(self, index_module):
        index_module.sfn.start_execution.side_effect = _already_exists()
        with pytest.raises(ClientError):
            index_module.start_workflow(index_module._doc)


class TestRedelivery:
    def test_redelivered_message_is_acked_without_a_second_execution(
        self, index_module
    ):
        index_module.sfn.start_execution.side_effect = _already_exists()

        success, message_id = index_module.process_message(_record())

        assert (success, message_id) == (True, MESSAGE_ID)
        index_module.sfn.start_execution.assert_called_once()
        assert index_module.sfn.start_execution.call_args.kwargs["name"].endswith(
            MESSAGE_ID
        )
        index_module.document_service.update_document.assert_not_called()

    def test_redelivery_hands_back_the_slot_it_took(self, index_module):
        index_module.sfn.start_execution.side_effect = _already_exists()

        index_module.process_message(_record())

        calls = [
            c.kwargs.get("increment", c.args[0] if c.args else True)
            for c in index_module.update_counter.call_args_list
        ]
        assert calls == [True, False]

    def test_redelivery_deletes_the_message_so_it_stops_coming_back(self, index_module):
        index_module.sfn.start_execution.side_effect = _already_exists()

        index_module.process_message(_record(receipt="rh-dup"))

        index_module.sqs.delete_message.assert_called_once_with(
            QueueUrl=QUEUE_URL, ReceiptHandle="rh-dup"
        )

    def test_redelivery_is_acked_even_if_the_decrement_fails(self, index_module):
        index_module.sfn.start_execution.side_effect = _already_exists()
        index_module.update_counter.side_effect = [True, RuntimeError("ddb down")]

        success, _ = index_module.process_message(_record())

        assert success is True


class TestPerMessageAck:
    def test_message_is_deleted_as_soon_as_its_execution_exists(self, index_module):
        order = []
        index_module.sfn.start_execution.side_effect = lambda **kw: (
            order.append("start") or {"executionArn": "arn:e1"}
        )
        index_module.sqs.delete_message.side_effect = lambda **kw: order.append("ack")
        index_module.document_service.update_document.side_effect = lambda d: (
            order.append("track") or d
        )

        success, _ = index_module.process_message(_record(receipt="rh-1"))

        assert success is True
        assert order == ["start", "ack", "track"]
        index_module.sqs.delete_message.assert_called_once_with(
            QueueUrl=QUEUE_URL, ReceiptHandle="rh-1"
        )

    def test_a_failed_start_deletes_nothing_and_releases_the_slot(self, index_module):
        index_module.sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "StartExecution",
        )

        success, _ = index_module.process_message(_record())

        assert success is False
        index_module.sqs.delete_message.assert_not_called()
        assert index_module.update_counter.call_args_list[-1].kwargs == {
            "increment": False
        }

    def test_delete_failure_is_not_fatal(self, index_module):
        index_module.sqs.delete_message.side_effect = ClientError(
            {"Error": {"Code": "ReceiptHandleIsInvalid", "Message": "gone"}},
            "DeleteMessage",
        )

        success, _ = index_module.process_message(_record())

        assert success is True
        index_module.document_service.update_document.assert_called_once()

    def test_no_queue_url_skips_the_ack_quietly(self, index_module):
        index_module.DOCUMENT_QUEUE_URL = ""

        success, _ = index_module.process_message(_record())

        assert success is True
        index_module.sqs.delete_message.assert_not_called()


class TestHandler:
    def test_a_batch_mixing_fresh_and_redelivered_messages_reports_no_failures(
        self, index_module
    ):
        index_module.sfn.start_execution.side_effect = [
            {"executionArn": "arn:e1"},
            _already_exists(),
            {"executionArn": "arn:e3"},
        ]
        event = {
            "Records": [
                _record("m1", "rh-1"),
                _record("m2", "rh-2"),
                _record("m3", "rh-3"),
            ]
        }

        result = index_module.handler(event, MagicMock())

        assert result == {"batchItemFailures": []}
        names = [
            c.kwargs["name"] for c in index_module.sfn.start_execution.call_args_list
        ]
        assert [n.rsplit("-", 1)[-1] for n in names] == ["m1", "m2", "m3"]
        assert index_module.sqs.delete_message.call_count == 3
