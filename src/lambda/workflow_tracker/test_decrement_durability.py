# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The concurrency decrement must never be lost silently.

A lost decrement leaks a workflow slot permanently: the counter drifts up and,
once it reaches MaxConcurrentWorkflows, the stack stops admitting documents with
no self-healing path. `decrement_counter` used to swallow every exception and
return None, and the handler treated that as success and returned HTTP 200 — so a
single throttled DynamoDB write cost a slot forever, with no retry, no alarm, and
nothing in the response saying so.

For the record, this is a latent gap being closed rather than the cause of the
leak that motivated it: that investigation found ZERO decrement failures across
the entire retained log history, and exactly 177 decrements for 177 terminal
executions through the incident window.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "workflow_tracker_decrement_under_test"


@pytest.fixture
def index_module(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "METRIC_NAMESPACE": "TEST_NS",
        "DECREMENT_MAX_ATTEMPTS": "4",
    }
    fake_docs_service = MagicMock()
    fake_docs_service.create_document_service = MagicMock(return_value=MagicMock())
    for name, mod in {
        "idp_common": MagicMock(),
        "idp_common.models": MagicMock(),
        "idp_common.docs_service": fake_docs_service,
        "idp_common.document_versions": MagicMock(),
        "idp_common.delete_documents": MagicMock(),
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
        module.concurrency_table = MagicMock()
        module.cloudwatch = MagicMock()
        # Keep the real implementation reachable for the telemetry-failure test.
        module.real_emit_counter_metric = module._emit_counter_metric
        module._emit_counter_metric = MagicMock()
        # Don't actually sleep between retries.
        monkeypatch.setattr(module.time, "sleep", lambda *_: None)
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _throttle():
    return ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
    )


def _ok(n):
    return {"Attributes": {"active_count": n}}


@pytest.mark.unit
def test_succeeds_first_try(index_module):
    index_module.concurrency_table.update_item.return_value = _ok(41)
    assert index_module.decrement_counter() == 41
    assert index_module.concurrency_table.update_item.call_count == 1


@pytest.mark.unit
def test_retries_transient_throttling_then_succeeds(index_module):
    index_module.concurrency_table.update_item.side_effect = [
        _throttle(),
        _throttle(),
        _ok(7),
    ]
    assert index_module.decrement_counter() == 7
    assert index_module.concurrency_table.update_item.call_count == 3


@pytest.mark.unit
def test_raises_rather_than_losing_the_slot(index_module):
    """The whole point: a decrement that cannot be applied must NOT return
    quietly, because the event would be acked and the slot lost forever."""
    index_module.concurrency_table.update_item.side_effect = _throttle()
    with pytest.raises(ClientError):
        index_module.decrement_counter()
    assert index_module.concurrency_table.update_item.call_count == 4


@pytest.mark.unit
def test_non_clienterror_is_also_retried_and_raised(index_module):
    index_module.concurrency_table.update_item.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        index_module.decrement_counter()
    assert index_module.concurrency_table.update_item.call_count == 4


@pytest.mark.unit
def test_counter_value_is_published_for_history(index_module):
    """The leak could not be attributed to a moment in time because nothing ever
    recorded the counter — only the end state was visible."""
    index_module.concurrency_table.update_item.return_value = _ok(12)
    index_module.decrement_counter()
    index_module._emit_counter_metric.assert_called_once_with(12)


@pytest.mark.unit
def test_metric_failure_never_breaks_the_decrement(index_module):
    """Telemetry is not allowed to affect the thing it reports on."""
    index_module._emit_counter_metric = index_module.real_emit_counter_metric
    index_module.concurrency_table.update_item.return_value = _ok(3)
    index_module.cloudwatch.put_metric_data.side_effect = RuntimeError("no cloudwatch")
    assert index_module.decrement_counter() == 3
    assert index_module.cloudwatch.put_metric_data.called
    assert index_module.concurrency_table.update_item.call_count == 1


@pytest.mark.unit
def test_metric_raising_outright_does_not_double_decrement(index_module):
    """The decrement is already applied by the time telemetry runs, so telemetry
    must never be able to re-enter the retry loop — that would subtract twice."""
    index_module._emit_counter_metric = MagicMock(side_effect=RuntimeError("boom"))
    index_module.concurrency_table.update_item.return_value = _ok(5)
    assert index_module.decrement_counter() == 5
    assert index_module.concurrency_table.update_item.call_count == 1
