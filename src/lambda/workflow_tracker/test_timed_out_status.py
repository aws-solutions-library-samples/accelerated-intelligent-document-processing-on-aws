"""#757: a TIMED_OUT execution (the execution-level bound tripped) must be
recognisable on the tracking item.

The document service derives ``WorkflowStatus`` from the document status, so the
tracker overwrites it with ``TIMED_OUT`` after the normal update; an ordinary FAILED
execution must not get that override, and a failed override must not fail the
tracker (it still has to release the concurrency slot).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "workflow_tracker_timed_out_under_test"


class _Status:
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    REDACTED_SUPERSEDED = "REDACTED_SUPERSEDED"


@pytest.fixture
def index_module(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "METRIC_NAMESPACE": "TEST_NS",
        "TRACKING_TABLE": "test-tracking",
        "INPUT_BUCKET": "test-input",
        "OUTPUT_BUCKET": "test-output",
    }
    fake_models = MagicMock()
    fake_models.Status = _Status
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())

    patches = {
        "idp_common": MagicMock(),
        "idp_common.models": fake_models,
        "idp_common.docs_service": fake_docs_service,
        "idp_common.document_versions": MagicMock(),
        "idp_common.delete_documents": MagicMock(),
    }
    for name, mod in patches.items():
        monkeypatch.setitem(sys.modules, name, mod)

    with (
        patch.dict(os.environ, env_vars, clear=False),
        patch("boto3.resource") as mock_resource,
        patch("boto3.client") as mock_client,
    ):
        mock_resource.return_value.Table.return_value = MagicMock()
        mock_client.return_value = MagicMock()
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
        module.cloudwatch = MagicMock()
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _tracking_table(m):
    return m.dynamodb.Table.return_value


def test_timed_out_marks_document_failed_and_records_timed_out_status(index_module):
    m = index_module
    m.update_document_completion("big.pdf", "TIMED_OUT", None)

    # Normal update went through the document service with a FAILED document ...
    m.document_service.update_document.assert_called_once()
    doc = m.document_service.update_document.call_args.args[0]
    assert doc is m.Document.return_value
    assert m.Document.call_args.kwargs["status"] == _Status.FAILED
    # ... and the tracking item then says WHY it failed.
    _tracking_table(m).update_item.assert_called_once()
    kwargs = _tracking_table(m).update_item.call_args.kwargs
    assert kwargs["Key"] == {"PK": "doc#big.pdf", "SK": "none"}
    assert kwargs["ExpressionAttributeValues"] == {":s": "TIMED_OUT"}
    assert "WorkflowStatus" in kwargs["UpdateExpression"]


def test_ordinary_failure_gets_no_override(index_module):
    m = index_module
    m.update_document_completion("ok.pdf", "FAILED", None)
    m.document_service.update_document.assert_called_once()
    _tracking_table(m).update_item.assert_not_called()


def test_override_failure_does_not_fail_the_tracker(index_module):
    m = index_module
    _tracking_table(m).update_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
        "UpdateItem",
    )
    result = m.update_document_completion("big.pdf", "TIMED_OUT", None)
    assert result is m.document_service.update_document.return_value
