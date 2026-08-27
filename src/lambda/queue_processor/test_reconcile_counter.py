# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for queue_processor.reconcile_counter().

The workflow-concurrency counter is incremented before StartExecution and
decremented by the workflow tracker on the completion event. A missed decrement
drifts it upward permanently, and once it reaches MAX_CONCURRENT the stack stops
admitting documents with no self-healing path. Observed live: active_count pinned
at 100 with 29 executions actually running, 2,532 messages held in flight, and no
document started for hours.

Reconciliation therefore has to be *safe*, because wrongly lowering the counter
over-admits work. These tests pin the three safeguards: two samples a grace
period apart, never raising the counter, and a conditional write.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

_INDEX_PATH = os.path.join(os.path.dirname(__file__), "index.py")
_MODULE_NAME = "queue_processor_reconcile_under_test"

SM_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:test"


@pytest.fixture
def index_module(monkeypatch):
    env_vars = {
        "CONCURRENCY_TABLE": "test-concurrency",
        "STATE_MACHINE_ARN": SM_ARN,
        "MAX_CONCURRENT": "100",
        "RECONCILE_GRACE_SECONDS": "300",
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
        mock_table = MagicMock()
        mock_resource.return_value.Table.return_value = mock_table
        mock_client.return_value = MagicMock()

        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _INDEX_PATH)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)

        module.concurrency_table = mock_table
        module.sfn = MagicMock()
        # Metric emission is telemetry; silence it unless a test asserts on it.
        module._emit_drift_metric = MagicMock()
        yield module
        sys.modules.pop(_MODULE_NAME, None)


def _counter(active, drift_at=None, drift_running=None):
    item = {"active_count": active}
    if drift_at is not None:
        item["drift_observed_at"] = drift_at
        item["drift_running"] = drift_running
    return {"Item": item}


def _running(n):
    return {"executions": [{"name": str(i)} for i in range(n)]}


class TestNoCorrectionWhenHealthy:
    def test_counter_at_zero_is_ignored(self, index_module):
        index_module.concurrency_table.get_item.return_value = _counter(0)
        assert index_module.reconcile_counter() is None
        index_module.sfn.list_executions.assert_not_called()

    def test_no_drift_when_running_matches(self, index_module):
        index_module.concurrency_table.get_item.return_value = _counter(30)
        index_module.sfn.list_executions.return_value = _running(30)
        assert index_module.reconcile_counter() is None
        index_module.concurrency_table.update_item.assert_not_called()

    def test_more_running_than_counter_is_not_a_leak(self, index_module):
        """Can happen transiently; must never raise the counter."""
        index_module.concurrency_table.get_item.return_value = _counter(30)
        index_module.sfn.list_executions.return_value = _running(35)
        assert index_module.reconcile_counter() is None
        index_module.concurrency_table.update_item.assert_not_called()

    def test_stale_drift_sample_is_cleared_once_healthy(self, index_module):
        index_module.concurrency_table.get_item.return_value = _counter(
            30, drift_at=1, drift_running=5
        )
        index_module.sfn.list_executions.return_value = _running(30)
        assert index_module.reconcile_counter() is None
        expr = index_module.concurrency_table.update_item.call_args.kwargs[
            "UpdateExpression"
        ]
        assert "REMOVE drift_observed_at" in expr


class TestTwoSampleRequirement:
    def test_first_observation_only_records_a_sample(self, index_module):
        """A single sample must NOT correct: an increment precedes its execution."""
        index_module.concurrency_table.get_item.return_value = _counter(100)
        index_module.sfn.list_executions.return_value = _running(29)
        assert index_module.reconcile_counter() is None
        expr = index_module.concurrency_table.update_item.call_args.kwargs[
            "UpdateExpression"
        ]
        assert "SET drift_observed_at" in expr
        assert "active_count" not in expr

    def test_second_observation_inside_grace_still_waits(self, index_module):
        with patch.object(index_module.time, "time", return_value=1_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100,
                drift_at=900,
                drift_running=29,  # 100s ago, grace is 300s
            )
            index_module.sfn.list_executions.return_value = _running(29)
            assert index_module.reconcile_counter() is None
        index_module.concurrency_table.update_item.assert_not_called()

    def test_existing_sample_timestamp_is_never_overwritten(self, index_module):
        """Rewriting the sample on every refusal RESETS the grace clock.

        Regression guard for a bug found in live verification: refusals arrive
        far more often than the grace period under load, so re-recording the
        sample each time meant the window never elapsed and the counter was never
        corrected — it sat at its ceiling indefinitely while the timestamp
        advanced on every invocation.
        """
        with patch.object(index_module.time, "time", return_value=1_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100,
                drift_at=990,
                drift_running=29,  # 10s ago
            )
            index_module.sfn.list_executions.return_value = _running(29)
            index_module.reconcile_counter()
        # No write at all: not a correction, and crucially not a fresh sample.
        index_module.concurrency_table.update_item.assert_not_called()

    def test_stale_sample_from_an_old_episode_is_discarded_not_trusted(
        self, index_module
    ):
        """A sample can outlive its episode, and must not authorize a shortcut.

        Reconciliation only runs on a refused increment, so once capacity returns
        the sample stops being revisited and lingers. If a LATER leak then found
        that stale sample already past the grace window, it would be corrected on
        its very first observation — defeating the two-sample safeguard.

        Round-17 review fix: the round-16 ``attribute_not_exists`` guard
        silently no-oped this stale-discard replacement because the
        stale sample DID exist. Now the discard path uses compare-and-
        swap on the observed timestamp, so the write actually fires
        while still protecting against concurrent writers.
        """
        with patch.object(index_module.time, "time", return_value=100_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100,
                drift_at=1_000,
                drift_running=29,  # ~99,000s old
            )
            index_module.sfn.list_executions.return_value = _running(29)
            assert index_module.reconcile_counter() is None
        # The write MUST have been issued (not silently blocked by the
        # round-16 guard) and MUST carry the compare-and-swap condition
        # against the stale sample's timestamp.
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        expr = kwargs["UpdateExpression"]
        assert "SET drift_observed_at" in expr, "should restart the window"
        assert "active_count" not in expr, "must not correct on a stale sample"
        # CaS: only replace the sample we actually observed. Guards the
        # write against a concurrent fresh sample from another refusal
        # path clobbering us — the ``attribute_not_exists`` from
        # round-16 alone would silently no-op here (stale sample DOES
        # exist), which the round-17 review flagged as a total no-op.
        assert "drift_observed_at = :prev" in kwargs["ConditionExpression"]
        assert kwargs["ExpressionAttributeValues"][":prev"] == 1_000

    def test_second_observation_after_grace_corrects(self, index_module):
        with patch.object(index_module.time, "time", return_value=2_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100,
                drift_at=1_000,
                drift_running=29,  # 1000s ago > 300s grace
            )
            index_module.sfn.list_executions.return_value = _running(29)
            assert index_module.reconcile_counter() == 29
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert "SET active_count = :new" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":new"] == 29


class TestCorrectionIsConservative:
    def test_corrects_to_the_higher_of_the_two_samples(self, index_module):
        """Never below what we know is in flight."""
        with patch.object(index_module.time, "time", return_value=2_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100, drift_at=1_000, drift_running=40
            )
            index_module.sfn.list_executions.return_value = _running(29)
            assert index_module.reconcile_counter() == 40

    def test_conditional_write_guards_against_concurrent_updates(self, index_module):
        with patch.object(index_module.time, "time", return_value=2_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100, drift_at=1_000, drift_running=29
            )
            index_module.sfn.list_executions.return_value = _running(29)
            index_module.reconcile_counter()
        kwargs = index_module.concurrency_table.update_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == "active_count = :expected"
        assert kwargs["ExpressionAttributeValues"][":expected"] == 100

    def test_concurrent_change_aborts_the_correction(self, index_module):
        index_module.concurrency_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        with patch.object(index_module.time, "time", return_value=2_000):
            index_module.concurrency_table.get_item.return_value = _counter(
                100, drift_at=1_000, drift_running=29
            )
            index_module.sfn.list_executions.return_value = _running(29)
            assert index_module.reconcile_counter() is None


class TestFailsSafe:
    def test_list_executions_error_declines_to_act(self, index_module):
        index_module.concurrency_table.get_item.return_value = _counter(100)
        index_module.sfn.list_executions.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "ListExecutions"
        )
        assert index_module.reconcile_counter() is None

    def test_counter_read_error_declines_to_act(self, index_module):
        index_module.concurrency_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
        )
        assert index_module.reconcile_counter() is None
        index_module.sfn.list_executions.assert_not_called()

    def test_paginates_running_executions(self, index_module):
        index_module.sfn.list_executions.side_effect = [
            {"executions": [{"name": str(i)} for i in range(100)], "nextToken": "t"},
            {"executions": [{"name": "x"}]},
        ]
        assert index_module._count_running_executions() == 101

    def test_stops_paging_once_ceiling_exceeded(self, index_module, monkeypatch):
        """Round-15 review fix: the cap must scale with MAX_CONCURRENT.
        The previous hardcoded ``limit=1000`` blocked reconciliation on
        large stacks with MAX_CONCURRENT > 1000. Now once we have counted
        MAX_CONCURRENT + margin running executions, the counter (capped
        at MAX_CONCURRENT) mathematically cannot have leaked, so we
        return None and stop paging — regardless of the absolute
        threshold.
        """
        # Test-scoped MAX_CONCURRENT is 100; ceiling = 100 + 100 = 200.
        # Two pages of 100 pushes total to 200 (not yet > ceiling) → keeps
        # paging. A third page of 1 pushes it to 201 → return None.
        index_module.sfn.list_executions.side_effect = [
            {"executions": [{"name": str(i)} for i in range(100)], "nextToken": "t1"},
            {"executions": [{"name": str(i)} for i in range(100)], "nextToken": "t2"},
            {"executions": [{"name": "over"}], "nextToken": "t3"},
            # Fourth page should never be called — we returned already.
            {"executions": [{"name": "should-not-see"}]},
        ]
        assert index_module._count_running_executions() is None
        # 3 calls, not 4 — we short-circuited.
        assert index_module.sfn.list_executions.call_count == 3

    def test_ceiling_scales_with_configured_max_concurrent(self, monkeypatch):
        """Regression pin for the round-15 bug: a customer running a large
        stack with MAX_CONCURRENT=2000 previously hit the hardcoded 1000
        cap and reconciliation refused to act on a real leak. With the
        ceiling now derived from MAX_CONCURRENT, 1500 running executions
        must return the actual count (leak still detectable) rather than
        None.
        """
        env_vars = {
            "CONCURRENCY_TABLE": "test-concurrency",
            "STATE_MACHINE_ARN": SM_ARN,
            "MAX_CONCURRENT": "2000",
            "RECONCILE_GRACE_SECONDS": "300",
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
            patch("boto3.client"),
            patch("boto3.resource"),
        ):
            spec = importlib.util.spec_from_file_location(
                "queue_processor_large_stack", _INDEX_PATH
            )
            assert spec and spec.loader
            large_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(large_mod)
        large_mod.concurrency_table = MagicMock()
        large_mod.sfn = MagicMock()
        # 15 pages of 100 = 1500 running — well past the old 1000 cap
        # but comfortably under the new 2000 + 100 ceiling.
        pages = [
            {"executions": [{"name": str(i)} for i in range(100)], "nextToken": "t"}
            for _ in range(14)
        ] + [{"executions": [{"name": "last"}]}]
        large_mod.sfn.list_executions.side_effect = pages
        assert large_mod._count_running_executions() == 1401
