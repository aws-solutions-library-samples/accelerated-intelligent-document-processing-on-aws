# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A concurrency slot has exactly one owner, and StartExecution transfers it.

`process_message` increments the counter, then starts a Step Functions execution.
Before the execution exists, this function owns the slot and must release it on
any failure. Once StartExecution returns, the *execution* owns it and the workflow
tracker releases it on the terminal event.

The bug these tests pin: the post-start tracking update lived inside the same
`try` as StartExecution, so a failure there did both wrong things at once —
decremented a slot the running execution still held (counter one BELOW the true
in-flight count, which over-admits work), and returned failure so SQS redelivered
the message and a SECOND workflow was started for the same document.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_slot_ownership_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


@pytest.fixture
def index_module(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": SM_ARN,
        "MAX_CONCURRENT": "100",
        "METRIC_NAMESPACE": "TestStack",
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    fake_xray_core = MagicMock()
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

        # A document that loads cleanly and is not aborted.
        doc = MagicMock()
        doc.input_key = "input/test.pdf"
        doc.id = "doc-1"
        module.Document.load_document = MagicMock(return_value=doc)
        module.document_service.get_document = MagicMock(return_value=None)
        module.check_circuit_breaker = MagicMock(return_value=(True, "CLOSED"))
        module.update_counter = MagicMock(return_value=True)
        module.start_workflow = MagicMock(
            return_value={"executionArn": f"{SM_ARN.replace('stateMachine', 'execution')}:e1"}
        )
        module.document_service.update_document = MagicMock(return_value=doc)
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _record():
    return {
        "body": json.dumps({"input_key": "input/test.pdf"}),
        "messageId": "m-1",
        "receiptHandle": "rh-1",
    }


def _decrement_calls(update_counter):
    return [c for c in update_counter.call_args_list if c.kwargs.get("increment") is False]


@pytest.mark.unit
class TestSlotIsReleasedOnlyBeforeTheExecutionExists:
    def test_happy_path_never_decrements(self, index_module):
        ok, mid = index_module.process_message(_record())
        assert (ok, mid) == (True, "m-1")
        assert _decrement_calls(index_module.update_counter) == []

    def test_start_failure_releases_the_slot_and_retries(self, index_module):
        """No execution was created, so this function still owns the slot."""
        index_module.start_workflow.side_effect = RuntimeError("StartExecution denied")
        ok, _ = index_module.process_message(_record())
        assert ok is False, "the message must be retried — no workflow was started"
        assert len(_decrement_calls(index_module.update_counter)) == 1

    def test_post_start_failure_keeps_the_slot_and_acks(self, index_module):
        """The execution exists and holds the slot; the tracker will release it."""
        index_module.document_service.update_document.side_effect = RuntimeError(
            "DynamoDB throttled"
        )
        ok, _ = index_module.process_message(_record())
        assert ok is True, (
            "the message must NOT be retried — a retry starts a duplicate workflow "
            "for a document that is already being processed"
        )
        assert _decrement_calls(index_module.update_counter) == [], (
            "decrementing here leaves the counter BELOW the true in-flight count"
        )

    def test_aborted_document_releases_nothing_because_it_took_nothing(
        self, index_module
    ):
        aborted = MagicMock()
        aborted.status = index_module.Status.ABORTED
        index_module.document_service.get_document.return_value = aborted
        ok, _ = index_module.process_message(_record())
        assert ok is True
        index_module.update_counter.assert_not_called()
        index_module.start_workflow.assert_not_called()
