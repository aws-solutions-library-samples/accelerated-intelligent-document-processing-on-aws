"""Unit tests for the workflow tracker's review-queue paths.

With review queues enabled (``HITL_TABLE_NAME`` set) the tracker classifies each
terminal event as an ordinary run, phase 1 of a held document, or a finalize run,
and for the two review kinds:

- a stale or redelivered event (no live review context) releases only the
  concurrency slot and writes no document status, which would overwrite a later
  outcome;
- the run is recorded with its review run kind, except a rejected finalize, which
  has no final output to pin;
- the review writes run before the decrement, and the decrement runs exactly once
  even when those writes raise;
- reporting receives the finalize run's own usage plus the review payload, while the
  tracking row keeps the merged document.

``idp_common`` is stubbed, as in the other suites in this directory; the review
tracker and store are MagicMocks, so these tests pin the handler's wiring rather
than the library's behaviour (covered in ``lib/idp_common_pkg``).
"""

import importlib.util
import io
import json
import os
import sys
from enum import Enum
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "workflow_tracker_review_queue_under_test"
EXEC_ARN = "arn:aws:states:us-west-2:1:execution:sm:exec"
FINALIZE_TOKEN = "rvw-" + "ab" * 16


class _Status(str, Enum):
    """The ``Status`` members the tracker reads, as an Enum (it uses ``.value``)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    REDACTED_SUPERSEDED = "REDACTED_SUPERSEDED"


@pytest.fixture
def stubs(monkeypatch):
    """The stubbed library modules, so a test can reach the ones it configures."""
    fake_models = MagicMock()
    fake_models.Status = _Status
    fake_models.terminal_document_status = lambda workflow_status, _wrapper: (
        _Status.COMPLETED if workflow_status == "SUCCEEDED" else _Status.FAILED
    )
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    review_queues = MagicMock()
    s3_module = MagicMock()
    # `import idp_common.s3 as x` binds the parent's attribute, not sys.modules.
    parent = MagicMock(s3=s3_module, review_queues=review_queues)
    modules = {
        "idp_common": parent,
        "idp_common.models": fake_models,
        "idp_common.docs_service": fake_docs_service,
        "idp_common.document_versions": MagicMock(),
        "idp_common.delete_documents": MagicMock(),
        "idp_common.s3": s3_module,
        "idp_common.review_queues": review_queues,
        "idp_common.review_queues.store": MagicMock(),
        "idp_common.review_queues.notify": review_queues.notify,
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return SimpleNamespace(
        models=fake_models,
        store_module=modules["idp_common.review_queues.store"],
        tracker=review_queues.tracker,
        notify=review_queues.notify,
        s3=modules["idp_common.s3"],
    )


@pytest.fixture
def index_module(stubs, monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "METRIC_NAMESPACE": "TEST_NS",
        "TRACKING_TABLE": "test-tracking",
        "INPUT_BUCKET": "test-input",
        "OUTPUT_BUCKET": "test-output",
        "HITL_TABLE_NAME": "test-hitl",
    }
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


@pytest.fixture
def handler_mocks(index_module, monkeypatch):
    """Everything below the handler, recorded in one call order."""
    m = index_module
    order = MagicMock()
    doc = MagicMock()
    doc.status = _Status.COMPLETED
    doc.completion_time = "2026-10-19T00:00:00Z"
    doc.workflow_execution_arn = None
    order.update_document_completion.return_value = doc
    order.decrement_counter.return_value = 4
    order.record_document_run.return_value = "run-ref"
    for name in (
        "update_document_completion",
        "decrement_counter",
        "record_document_run",
        "put_latency_metrics",
        "notify_circuit_breaker_success",
    ):
        monkeypatch.setattr(m, name, getattr(order, name))
    return SimpleNamespace(order=order, doc=doc)


def _event(status="SUCCEEDED", finalize=False):
    input_data = {"document": {"document_id": "claim.pdf"}}
    if finalize:
        input_data["review"] = {"finalizeToken": FINALIZE_TOKEN}
    return {
        "detail": {
            "input": json.dumps(input_data),
            "output": json.dumps({"document": {"status": "HITL_IN_PROGRESS"}}),
            "status": status,
            "executionArn": EXEC_ARN,
            "stopDate": 1700000000000,
        }
    }


def _review_ctx(kind="hold", outcome=None):
    return SimpleNamespace(kind=kind, outcome=outcome)


# --------------------------------------------------------------------------
# The store and tracker are built lazily, once
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_review_store_is_created_once_when_the_feature_is_on(index_module, stubs):
    m = index_module
    m._REVIEW_STORE_CACHE.clear()
    with patch.dict(os.environ, {"HITL_TABLE_NAME": "test-hitl"}):
        first = m.review_store()
        second = m.review_store()
    assert first is second is stubs.store_module.create_review_store.return_value
    stubs.store_module.create_review_store.assert_called_once_with()


@pytest.mark.unit
def test_without_a_hitl_table_there_is_no_review_store(index_module, stubs):
    m = index_module
    m._REVIEW_STORE_CACHE.clear()
    with patch.dict(os.environ, {"HITL_TABLE_NAME": ""}):
        assert m.review_store() is None
    stubs.store_module.create_review_store.assert_not_called()


@pytest.mark.unit
def test_the_review_tracker_is_the_library_module(index_module, stubs):
    assert index_module.review_tracker() is stubs.tracker


# --------------------------------------------------------------------------
# The handler
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_an_ordinary_run_takes_the_existing_path(index_module, stubs, handler_mocks):
    """classify_run says "none": no context is loaded and no review write runs."""
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    stubs.tracker.classify_run.return_value = "none"

    resp = m.handler(_event(), None)

    assert resp["statusCode"] == 200
    stubs.tracker.load_context.assert_not_called()
    stubs.tracker.after_completion.assert_not_called()
    assert mocks.order.update_document_completion.call_args == call(
        "claim.pdf", "SUCCEEDED", {"document": {"status": "HITL_IN_PROGRESS"}}, None
    )
    # The run is recorded as an ordinary run, with the stable stopDate timestamp.
    assert mocks.order.record_document_run.call_args == call(
        mocks.doc, run_timestamp="2023-11-14T22:13:20+00:00", run_kind=None
    )
    assert mocks.doc.workflow_execution_arn == EXEC_ARN
    assert mocks.order.decrement_counter.call_args_list == [call(EXEC_ARN)]


@pytest.mark.unit
def test_a_stale_review_event_releases_only_the_slot(
    index_module, stubs, handler_mocks
):
    """No live context (D70): the document status is not rewritten."""
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    stubs.tracker.classify_run.return_value = "finalize"
    stubs.tracker.finalize_token_of.return_value = FINALIZE_TOKEN
    stubs.tracker.load_context.return_value = None

    resp = m.handler(_event(finalize=True), None)

    assert resp == {
        "statusCode": 200,
        "body": {
            "object_key": "claim.pdf",
            "workflow_status": "SUCCEEDED",
            "completion_time": None,
            "counter_value": 4,
        },
    }
    assert stubs.tracker.load_context.call_args == call(
        "finalize",
        FINALIZE_TOKEN,
        "claim.pdf",
        EXEC_ARN,
        stubs.store_module.create_review_store.return_value,
    )
    mocks.order.update_document_completion.assert_not_called()
    mocks.order.record_document_run.assert_not_called()
    assert mocks.order.decrement_counter.call_args_list == [call(EXEC_ARN)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ctx", "run_kind"),
    [
        (_review_ctx("hold"), "review_phase1"),
        (_review_ctx("finalize", "complete"), "review_final"),
    ],
)
def test_a_review_run_is_recorded_and_written_before_the_decrement(
    index_module, stubs, handler_mocks, ctx, run_kind
):
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    stubs.tracker.classify_run.return_value = ctx.kind
    stubs.tracker.load_context.return_value = ctx
    stubs.tracker.after_completion.side_effect = lambda *a, **k: (
        mocks.order.after_completion(*a, **k)
    )

    resp = m.handler(_event(), None)

    assert resp["statusCode"] == 200
    assert mocks.order.update_document_completion.call_args.args[3] is ctx
    assert mocks.order.record_document_run.call_args.kwargs["run_kind"] == run_kind
    kwargs = stubs.tracker.after_completion.call_args.kwargs
    assert stubs.tracker.after_completion.call_args.args == (ctx,)
    assert kwargs["run_ref"] == "run-ref"
    assert kwargs["document"] is mocks.doc
    assert kwargs["workflow_status"] == "SUCCEEDED"
    assert kwargs["notifier"] is stubs.notify
    assert kwargs["s3"] is stubs.s3
    assert kwargs["output_bucket"] == "test-output"
    names = [c[0] for c in mocks.order.mock_calls]
    assert names.index("after_completion") < names.index("decrement_counter")
    assert mocks.order.decrement_counter.call_count == 1


@pytest.mark.unit
def test_a_rejected_document_records_no_run(index_module, stubs, handler_mocks):
    """A rejection has no final output to pin, so no version is recorded."""
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    ctx = _review_ctx("finalize", "rejected")
    stubs.tracker.classify_run.return_value = "finalize"
    stubs.tracker.load_context.return_value = ctx

    m.handler(_event(finalize=True), None)

    mocks.order.record_document_run.assert_not_called()
    assert stubs.tracker.after_completion.call_args.kwargs["run_ref"] is None
    assert mocks.order.decrement_counter.call_args_list == [call(EXEC_ARN)]


@pytest.mark.unit
def test_a_failed_review_write_still_decrements_exactly_once_and_raises(
    index_module, stubs, handler_mocks
):
    """The sweeper heals the review; the slot must not leak or be released twice."""
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    stubs.tracker.classify_run.return_value = "hold"
    stubs.tracker.load_context.return_value = _review_ctx("hold")
    stubs.tracker.after_completion.side_effect = RuntimeError("hitl table throttled")

    with pytest.raises(RuntimeError, match="hitl table throttled"):
        m.handler(_event(), None)

    assert mocks.order.decrement_counter.call_args_list == [call(EXEC_ARN)]


@pytest.mark.unit
def test_run_record_and_metrics_failures_do_not_fail_the_event(
    index_module, stubs, handler_mocks
):
    m, mocks = index_module, handler_mocks
    m._REVIEW_STORE_CACHE.clear()
    stubs.tracker.classify_run.return_value = "none"
    mocks.order.record_document_run.side_effect = RuntimeError("versions down")
    mocks.order.put_latency_metrics.side_effect = RuntimeError("cw down")

    with patch.object(m.logger, "error") as error:
        resp = m.handler(_event(), None)

    assert resp["statusCode"] == 200
    logged = " ".join(str(c.args[0]) for c in error.call_args_list)
    assert "Failed to record document run: versions down" in logged
    assert "Failed to publish metrics: cw down" in logged
    mocks.order.notify_circuit_breaker_success.assert_called_once_with()
    assert mocks.order.decrement_counter.call_args_list == [call(EXEC_ARN)]


# --------------------------------------------------------------------------
# update_document_completion with a review context
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_a_finalize_run_reports_its_own_usage_with_the_review_payload(
    index_module, stubs, monkeypatch
):
    """Reporting gets the finalize run's document; tracking keeps the merged one."""
    m = index_module
    ctx = _review_ctx("finalize", "complete")
    processed = MagicMock(metering={"phase2": 1}, sections=[])
    tracked = MagicMock(metering={"phase1": 1, "phase2": 1}, sections=[])
    reporting = MagicMock(metering={"phase2": 1}, sections=[])
    reporting.rule_validation_result = None
    reporting.to_dict.return_value = {"id": "claim.pdf", "usage": "finalize-only"}
    stubs.models.Document.load_document.return_value = processed
    stubs.tracker.apply_overrides.return_value = (tracked, reporting)
    stubs.tracker.reporting_info.return_value.model_dump.return_value = {"q": "a"}
    stored = MagicMock(metering={})
    m.document_service.update_document = MagicMock(return_value=stored)
    monkeypatch.setattr(m, "review_tracker", lambda: stubs.tracker)
    monkeypatch.setattr(m, "REPORTING_BUCKET", "test-reporting")
    monkeypatch.setattr(m, "SAVE_REPORTING_FUNCTION_NAME", "save-reporting")
    m.lambda_client = MagicMock()
    m.lambda_client.invoke.return_value = {
        "Payload": io.BytesIO(json.dumps({"statusCode": 200}).encode())
    }

    result = m.update_document_completion(
        "claim.pdf", "SUCCEEDED", {"document": {"id": "claim.pdf"}}, ctx
    )

    stubs.tracker.apply_overrides.assert_called_once_with(ctx, processed)
    m.document_service.update_document.assert_called_once_with(tracked)
    payload = json.loads(m.lambda_client.invoke.call_args.kwargs["Payload"])
    assert payload["document"] == {"id": "claim.pdf", "usage": "finalize-only"}
    assert payload["data_to_save"] == ["document_reviews", "metering"]
    assert payload["review"] == {"q": "a"}
    stubs.tracker.reporting_info.return_value.model_dump.assert_called_once_with(
        mode="json"
    )
    # The returned document carries the merged metering the review writes read.
    assert result is stored
    assert result.metering == {"phase1": 1, "phase2": 1}
