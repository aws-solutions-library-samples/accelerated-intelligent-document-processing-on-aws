# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reprocessing withdraws the document's review-queue item first.

A document held in a review queue that is reprocessed starts a fresh run, which
routes it again. The leftover item (its claim, its status object) is withdrawn
before the new run is queued; a failed withdrawal is logged and never blocks the
reprocess, because the new run's routing supersedes the leftover. With no HITL
table configured there is no review service and nothing is withdrawn.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("INPUT_BUCKET", "test-in")
os.environ.setdefault("OUTPUT_BUCKET", "test-out")
os.environ.setdefault("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/test-queue")
# index.py builds a document service at import; it needs a table name but makes
# no call, so any value does.
os.environ.setdefault("TRACKING_TABLE", "test-tracking")


def _load_index():
    """Load the handler with the real ``idp_common``, under its own module name.

    ``test_delete_output_data.py`` stubs the ``idp_common`` package in
    ``sys.modules`` at import time; those stubs are dropped here, as
    ``test_scope_fail_closed.py`` does, so this file passes run on its own or with
    its siblings.
    """
    for name in list(sys.modules):
        if name == "idp_common" or name.startswith("idp_common."):
            if isinstance(sys.modules[name], MagicMock):
                del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "reprocess_resolver_index_review_withdraw", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_INDEX = _load_index()


@pytest.fixture
def index():
    _INDEX._REVIEW_SERVICE_CACHE.clear()
    yield _INDEX
    _INDEX._REVIEW_SERVICE_CACHE.clear()


@pytest.fixture
def review_service_module(monkeypatch):
    fake = MagicMock()
    monkeypatch.setitem(sys.modules, "idp_common.review_queues.service", fake)
    return fake


@pytest.mark.unit
class TestReviewService:
    def test_it_is_built_once_when_a_hitl_table_is_configured(
        self, index, review_service_module
    ):
        with patch.dict(os.environ, {"HITL_TABLE_NAME": "test-hitl"}):
            first = index._review_service()
            second = index._review_service()
        built = review_service_module.create_review_queue_service
        assert first is second is built.return_value
        built.assert_called_once_with()

    def test_there_is_none_without_a_hitl_table(self, index, review_service_module):
        with patch.dict(os.environ, {"HITL_TABLE_NAME": ""}):
            assert index._review_service() is None
        review_service_module.create_review_queue_service.assert_not_called()


@pytest.mark.unit
class TestWithdrawReview:
    def test_the_item_is_withdrawn_as_reprocessed(self, index):
        service = MagicMock()
        with patch.object(index, "_review_service", return_value=service):
            index._withdraw_review("claims/a.pdf")
        service.withdraw.assert_called_once_with("claims/a.pdf", "reprocessed")

    def test_without_a_service_nothing_is_withdrawn(self, index):
        with (
            patch.object(index, "_review_service", return_value=None),
            patch.object(index.logger, "error") as error,
        ):
            index._withdraw_review("claims/a.pdf")
        error.assert_not_called()

    def test_a_failed_withdrawal_is_logged_and_not_raised(self, index):
        service = MagicMock()
        service.withdraw.side_effect = RuntimeError("hitl table throttled")
        with (
            patch.object(index, "_review_service", return_value=service),
            patch.object(index.logger, "error") as error,
        ):
            index._withdraw_review("claims/a.pdf")  # must not raise
        error.assert_called_once_with(
            "Failed to withdraw the review item before reprocess", exc_info=True
        )


@pytest.fixture
def reprocess(index):
    """reprocess_document with its collaborators recorded in one call order."""
    order = MagicMock()
    order.send_message.return_value = {"MessageId": "msg-1"}
    order.Document.return_value.to_json.return_value = '{"id": "claims/a.pdf"}'
    with (
        patch.object(index, "_refuse_if_refused_by_the_malware_gate", order.gate),
        patch.object(index.s3_client, "head_object", order.head_object),
        patch.object(index, "_withdraw_review", order.withdraw),
        patch.object(index, "_delete_output_data", order.purge),
        patch.object(index, "Document", order.Document),
        patch.object(index.document_service, "create_document", order.create),
        patch.object(index.sqs_client, "send_message", order.send_message),
    ):
        yield order


def _names(order):
    return [name for name, _, _ in order.mock_calls if "." not in name]


@pytest.mark.unit
class TestReprocessWithdrawsFirst:
    def test_the_review_is_withdrawn_before_outputs_are_purged_and_requeued(
        self, index, reprocess
    ):
        message_id = index.reprocess_document("claims/a.pdf", version="v2")

        assert message_id == "msg-1"
        reprocess.withdraw.assert_called_once_with("claims/a.pdf")
        names = _names(reprocess)
        assert names.index("head_object") < names.index("withdraw")
        assert names.index("withdraw") < names.index("purge")
        assert names.index("purge") < names.index("send_message")
        sent = reprocess.send_message.call_args.kwargs
        assert sent["MessageBody"] == '{"id": "claims/a.pdf"}'
        assert sent["MessageAttributes"]["EventType"]["StringValue"] == (
            "DocumentReprocessed"
        )
        assert reprocess.Document.call_args.kwargs["config_version"] == "v2"

    def test_a_missing_input_object_withdraws_nothing(self, index, reprocess):
        reprocess.head_object.side_effect = RuntimeError("404")

        with pytest.raises(ValueError, match="not found in S3 bucket"):
            index.reprocess_document("claims/a.pdf")

        reprocess.withdraw.assert_not_called()
        reprocess.send_message.assert_not_called()
