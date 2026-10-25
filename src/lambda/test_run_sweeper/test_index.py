# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The test-run sweeper handler: admission, bounding, and the enqueue contract.

The derivation itself lives in ``idp_common.testrun_status`` and is tested in
``lib/idp_common_pkg/tests/unit/test_testrun_status.py``. This suite pins what
the handler adds, and the assertions are chosen so that a sweep which silently
does nothing fails — that being the failure mode #1620 is about, and the one a
green exit code cannot distinguish from success.
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_INDEX = Path(__file__).with_name("index.py")
# The handler builds its SQS and CloudWatch clients at module scope, so a region
# has to be in place before the import — not before the first call.
_ENV = {
    "TRACKING_TABLE": "tracking-table",
    "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/queue",
    "METRIC_NAMESPACE": "test-stack",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
}


def _load(env=None):
    """Import ``index.py`` fresh under ``env``."""
    name = "test_run_sweeper_index_under_test"
    with patch.dict(os.environ, dict(_ENV if env is None else env), clear=False):
        spec = importlib.util.spec_from_file_location(name, _INDEX)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod():
    """The handler with its AWS clients replaced and its DDB client unset.

    ⚠️ The environment stays patched for the whole test, not just for the
    import. The handler reads ``METRIC_NAMESPACE`` and the queue URL at *call*
    time, so a fixture that dropped the patch after importing would make every
    such read miss — and the symptom is a sweep that silently skips the enqueue,
    which is the behaviour under test.

    Each test installs its own tracking-table client into ``_CLIENT``, so a test
    that forgets to raises rather than reaching real DynamoDB.
    """
    with patch.dict(os.environ, _ENV, clear=False):
        module = _load()
        module.sqs = MagicMock()
        module.cloudwatch = MagicMock()
        module._CLIENT.clear()
        yield module


class _Context:
    def __init__(self, remaining_ms=300_000):
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self):
        return self._remaining


def _run(test_run_id="r1", status="RUNNING", files=("a",), **extra):
    item = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": status,
        "Files": list(files),
        "FilesCount": len(files),
    }
    item.update(extra)
    return item


def _stub_client(mod, runs, docs_by_key=None, pages=None):
    """Install a fake tracking-table client and return it for assertions."""
    docs_by_key = docs_by_key or {}
    client = MagicMock()

    def batch_get_items(keys, **_kwargs):
        out = []
        for key in keys:
            pk = key["PK"]
            if pk.startswith("testrun#"):
                for run in runs:
                    if run["PK"] == pk:
                        out.append(run)
            else:
                if pk in docs_by_key:
                    item = dict(docs_by_key[pk])
                    item["PK"] = pk
                    out.append(item)
        return out

    client.batch_get_items.side_effect = batch_get_items

    # Discovery projects, so the handler re-reads the full record for each run it
    # reconciles. Serve those from the same `runs` list the GSI pages are built
    # from, so a test cannot accidentally reconcile a record the index never
    # offered.
    by_pk = {run["PK"]: run for run in runs}

    def get_item(Key=None, **_kwargs):
        item = by_pk.get((Key or {}).get("PK"))
        return {"Item": item} if item is not None else {}

    client.table.get_item.side_effect = get_item

    # Paged like the real GSI: one page per `pages` entry, each ending with a
    # LastEvaluatedKey except the last. Placing a due run on a later page is how
    # the "older stalled run behind newer settled ones" case is reproduced.
    groups = pages if pages is not None else [runs]
    responses = []
    for i, group in enumerate(groups):
        response = {"Items": [{"PK": r["PK"], "SK": "metadata"} for r in group]}
        if i < len(groups) - 1:
            response["LastEvaluatedKey"] = {"PK": group[-1]["PK"]}
        responses.append(response)
    client.table.query.side_effect = responses
    mod._CLIENT["c"] = client
    return client


@pytest.mark.unit
class TestEnvironmentReading:
    @pytest.mark.parametrize("raw", ["0", "-1", "abc", ""])
    def test_an_unusable_override_falls_back_rather_than_failing(self, mod, raw):
        # The sweep runs unattended. A malformed variable must not take it out,
        # because "nothing ran and nobody noticed" is the defect being fixed.
        with patch.dict(os.environ, {"SWEEP_WINDOW_DAYS": raw}):
            assert mod._int_env("SWEEP_WINDOW_DAYS", 7) == 7

    def test_a_valid_override_is_used(self, mod):
        with patch.dict(os.environ, {"SWEEP_WINDOW_DAYS": "30"}):
            assert mod._int_env("SWEEP_WINDOW_DAYS", 7) == 30

    @pytest.mark.parametrize("event", [None, "nope", [], {}, {"windowDays": None}])
    def test_event_overrides_degrade_to_the_fallback(self, mod, event):
        assert mod._override(event, "windowDays", 7) == 7

    def test_an_event_override_widens_the_window(self, mod):
        # This is the documented backfill route for runs that stalled before the
        # sweeper was deployed and now sit outside the scheduled window.
        assert mod._override({"windowDays": 90}, "windowDays", 7) == 90

    @pytest.mark.parametrize("value", [0, -5, "x"])
    def test_a_nonsense_event_override_is_ignored(self, mod, value):
        assert mod._override({"windowDays": value}, "windowDays", 7) == 7


@pytest.mark.unit
class TestDeadlineGuard:
    def test_plenty_of_time_keeps_going(self, mod):
        assert mod._running_out_of_time(_Context(300_000)) is False

    def test_little_time_stops(self, mod):
        assert mod._running_out_of_time(_Context(1_000)) is True

    def test_a_context_without_the_method_keeps_going(self, mod):
        # Reading "no method" as "no time left" would make the sweep do nothing
        # at all — strictly worse than the bug it fixes.
        assert mod._running_out_of_time(object()) is False

    def test_a_raising_context_keeps_going(self, mod):
        broken = MagicMock()
        broken.get_remaining_time_in_millis.side_effect = RuntimeError("boom")
        assert mod._running_out_of_time(broken) is False

    def test_the_sweep_defers_the_runs_it_could_not_visit(self, mod):
        runs = [_run(f"r{i}", status="COMPLETE") for i in range(3)]
        _stub_client(mod, runs)
        result = mod.handler({}, _Context(1_000))
        # Nothing visited, and the counts say so rather than reporting success.
        assert result["runsDue"] == 3
        assert result["deferredToNextSweep"] == 3
        assert result["statusesAdvanced"] == 0
        assert result["stoppedEarly"] == "deadline"


@pytest.mark.unit
class TestAdmission:
    def test_a_finished_run_with_metrics_is_not_visited(self, mod):
        runs = [_run(status="COMPLETE", testRunResult={"overallAccuracy": 1})]
        client = _stub_client(mod, runs)
        result = mod.handler({}, _Context())
        assert result["runsDue"] == 0
        # And crucially: no document reads at all for such a run.
        doc_calls = [
            call
            for call in client.batch_get_items.call_args_list
            if call.args and call.args[0] and "doc#" in call.args[0][0]["PK"]
        ]
        assert doc_calls == []

    def test_a_stalled_run_is_visited(self, mod):
        runs = [_run(status="RUNNING")]
        _stub_client(mod, runs)
        assert mod.handler({}, _Context())["runsDue"] == 1

    def test_a_terminal_run_missing_metrics_is_visited(self, mod):
        # The permanent-EVALUATING-badge state (#619).
        runs = [_run(status="COMPLETE")]
        _stub_client(mod, runs)
        assert mod.handler({}, _Context())["runsDue"] == 1


@pytest.mark.unit
class TestReconcileRun:
    def test_a_finished_run_is_advanced_and_its_metrics_enqueued(self, mod):
        run = _run(status="RUNNING", files=("a", "b"))
        docs = {
            "doc#r1/a": {
                "ObjectStatus": "COMPLETED",
                "EvaluationStatus": "COMPLETED",
                "CompletionTime": "2026-10-01T10:00:00Z",
            },
            "doc#r1/b": {
                "ObjectStatus": "COMPLETED",
                "EvaluationStatus": "COMPLETED",
                "CompletionTime": "2026-10-01T12:00:00Z",
            },
        }
        client = _stub_client(mod, [run], docs)
        verdict = mod.reconcile_run(run)

        assert verdict["status"] == "COMPLETE"
        assert verdict["statusChanged"] is True
        assert verdict["metricsQueued"] is True

        # The status write happened, and carried CompletedAt from the documents.
        status_write = next(
            call
            for call in client.table.update_item.call_args_list
            if "#status" in (call.kwargs.get("ExpressionAttributeNames") or {})
        )
        values = status_write.kwargs["ExpressionAttributeValues"]
        assert values[":status"] == "COMPLETE"
        assert values[":completedAt"] == "2026-10-01T12:00:00Z"
        assert values[":completedFiles"] == 2
        assert values[":failedFiles"] == 0

        mod.sqs.send_message.assert_called_once()
        assert "r1" in mod.sqs.send_message.call_args.kwargs["MessageBody"]

    def test_a_still_running_run_is_not_advanced(self, mod):
        run = _run(status="RUNNING", files=("a",))
        docs = {"doc#r1/a": {"ObjectStatus": "EXTRACTING"}}
        client = _stub_client(mod, [run], docs)
        verdict = mod.reconcile_run(run)
        assert verdict["statusChanged"] is False
        assert verdict["metricsQueued"] is False
        client.table.update_item.assert_not_called()

    def test_an_aborted_run_is_never_recalculated(self, mod):
        # Recomputing it from documents that are still finishing would take it
        # back out of a terminal state.
        run = _run(status="ABORTED", files=("a",))
        client = _stub_client(mod, [run], {})
        verdict = mod.reconcile_run(run)
        assert verdict["status"] == "ABORTED"
        assert verdict["statusChanged"] is False
        client.table.update_item.assert_not_called()
        mod.sqs.send_message.assert_not_called()

    def test_an_existing_completed_at_is_preserved(self, mod):
        run = _run(status="RUNNING", files=("a",), CompletedAt="2026-09-01T00:00:00Z")
        docs = {
            "doc#r1/a": {
                "ObjectStatus": "COMPLETED",
                "EvaluationStatus": "COMPLETED",
                "CompletionTime": "2026-10-01T10:00:00Z",
            }
        }
        client = _stub_client(mod, [run], docs)
        mod.reconcile_run(run)
        status_write = next(
            call
            for call in client.table.update_item.call_args_list
            if "#status" in (call.kwargs.get("ExpressionAttributeNames") or {})
        )
        assert (
            status_write.kwargs["ExpressionAttributeValues"][":completedAt"]
            == "2026-09-01T00:00:00Z"
        )

    def test_a_baseline_failure_lands_as_partial_complete(self, mod):
        run = _run(status="RUNNING", files=("a",), BaselineFailedFiles=1)
        run["FilesCount"] = 2
        docs = {
            "doc#r1/a": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}
        }
        _stub_client(mod, [run], docs)
        assert mod.reconcile_run(run)["status"] == "PARTIAL_COMPLETE"

    def test_an_unrecognised_evaluation_status_is_reported(self, mod):
        # The #1330 alarm. It must reach a counter, not just a log line.
        run = _run(status="RUNNING", files=("a",))
        docs = {"doc#r1/a": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "WAT"}}
        _stub_client(mod, [run], docs)
        assert mod.reconcile_run(run)["unrecognisedEvalStatuses"] == 1

    def test_a_terminal_run_whose_documents_are_all_gone_is_left_alone(self, mod):
        """Expired `doc#` items must not regress a finished run to QUEUED.

        The `doc#` rows carry the tracking table's TTL; the `testrun#` metadata
        row outlives them. A tally over documents none of which exist reads as
        "nothing has started", which for a run already terminal is almost
        certainly "they expired" — and acting on it would rewrite a finished run
        as QUEUED, on a timer, with nothing to notice it.
        """
        run = _run(status="COMPLETE", files=("a", "b"))
        client = _stub_client(mod, [run], {})
        verdict = mod.reconcile_run(run)
        assert verdict["status"] == "COMPLETE"
        assert verdict["statusChanged"] is False
        client.table.update_item.assert_not_called()

    def test_a_new_run_with_no_documents_yet_is_unaffected_by_that_guard(self, mod):
        # The guard is scoped to terminal statuses precisely so a run whose
        # documents have not been created yet still reads as QUEUED.
        run = _run(status="QUEUED", files=("a",))
        _stub_client(mod, [run], {})
        assert mod.reconcile_run(run)["status"] == "QUEUED"

    def test_a_missing_document_does_not_advance_the_run(self, mod):
        run = _run(status="QUEUED", files=("a",))
        _stub_client(mod, [run], {})
        verdict = mod.reconcile_run(run)
        assert verdict["status"] == "QUEUED"
        assert verdict["statusChanged"] is False


@pytest.mark.unit
class TestEnqueueThrottle:
    def test_the_claim_records_an_attempt(self, mod):
        from idp_common import testrun_status as trs

        client = MagicMock()
        mod._CLIENT["c"] = client
        mod._claim_cache_update_slot("r1", 900)
        kwargs = client.table.update_item.call_args.kwargs
        # The counter is what the backoff is computed from, so the claim and the
        # record of it have to be the same atomic write.
        assert (
            f"ADD {trs.CACHE_UPDATE_ATTEMPTS_ATTR} :one" in kwargs["UpdateExpression"]
        )
        assert kwargs["ExpressionAttributeValues"][":one"] == 1

    def test_the_throttle_widens_with_recorded_attempts(self, mod):
        from idp_common import testrun_status as trs

        client = MagicMock()
        mod._CLIENT["c"] = client
        mod._queue_cache_update("r1", prior_attempts=3)
        cutoff = client.table.update_item.call_args.kwargs["ExpressionAttributeValues"][
            ":cutoff"
        ]
        now = datetime.now(timezone.utc)
        age = (now - datetime.fromisoformat(cutoff)).total_seconds()
        # Four attempts in, the window is eight times the base, not the base.
        assert age == pytest.approx(trs.sweep_aggregation_throttle(3), rel=0.05)

    def test_a_runs_recorded_attempts_reach_the_throttle(self, mod):
        run = _run(status="COMPLETE", files=("a",))
        run["CacheUpdateAttempts"] = 2
        docs = {
            "doc#r1/a": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}
        }
        client = _stub_client(mod, [run], docs)
        mod.reconcile_run(run)
        cutoff = client.table.update_item.call_args.kwargs["ExpressionAttributeValues"][
            ":cutoff"
        ]
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(cutoff)
        ).total_seconds()
        from idp_common import testrun_status as trs

        assert age == pytest.approx(trs.sweep_aggregation_throttle(2), rel=0.05)

    def test_the_claim_uses_the_shared_attribute_name(self, mod):
        from idp_common import testrun_status as trs

        client = MagicMock()
        mod._CLIENT["c"] = client
        assert mod._claim_cache_update_slot("r1", 300) is True
        kwargs = client.table.update_item.call_args.kwargs
        # Spelled from the constant, not inline: the resolver claims the same
        # slot, and a rename on one side only would let both enqueue.
        assert trs.CACHE_UPDATE_QUEUED_AT_ATTR in kwargs["UpdateExpression"]
        assert trs.CACHE_UPDATE_QUEUED_AT_ATTR in kwargs["ConditionExpression"]
        assert "attribute_not_exists" in kwargs["ConditionExpression"]

    def test_a_lost_claim_suppresses_the_enqueue(self, mod):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        mod._CLIENT["c"] = client
        assert mod._queue_cache_update("r1") is False
        mod.sqs.send_message.assert_not_called()

    def test_an_unexpected_error_fails_open(self, mod):
        # A broken throttle must never block a legitimate recompute.
        client = MagicMock()
        client.table.update_item.side_effect = RuntimeError("boom")
        mod._CLIENT["c"] = client
        assert mod._queue_cache_update("r1") is True
        mod.sqs.send_message.assert_called_once()

    def test_no_queue_url_is_reported_rather_than_raised(self, mod):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TEST_RESULT_CACHE_UPDATE_QUEUE_URL", None)
            assert mod._queue_cache_update("r1") is False

    def test_a_send_failure_is_reported_rather_than_raised(self, mod):
        client = MagicMock()
        mod._CLIENT["c"] = client
        mod.sqs.send_message.side_effect = RuntimeError("boom")
        assert mod._queue_cache_update("r1") is False


@pytest.mark.unit
class TestHandlerResilience:
    def test_one_run_failing_does_not_end_the_sweep(self, mod):
        runs = [_run("r1", status="RUNNING"), _run("r2", status="RUNNING")]
        _stub_client(mod, runs)
        calls = []

        def flaky(run_item):
            calls.append(run_item["PK"])
            if run_item["PK"] == "testrun#r1":
                raise RuntimeError("boom")
            return {
                "testRunId": "r2",
                "storedStatus": "RUNNING",
                "status": "COMPLETE",
                "statusChanged": True,
                "metricsQueued": True,
                "unrecognisedEvalStatuses": 0,
            }

        with patch.object(mod, "reconcile_run", side_effect=flaky):
            result = mod.handler({}, _Context())

        # The next run may be the stuck one, and the sweep is the only thing
        # that will look at it.
        assert calls == ["testrun#r1", "testrun#r2"]
        assert result["failures"] == 1
        assert result["statusesAdvanced"] == 1

    def test_discovery_failure_raises_rather_than_reporting_success(self, mod):
        client = MagicMock()
        client.table.query.side_effect = RuntimeError("ddb down")
        mod._CLIENT["c"] = client
        # A sweep that cannot even list runs must not exit green — that is
        # indistinguishable from a sweep with nothing to do.
        with pytest.raises(RuntimeError):
            mod.handler({}, _Context())

    def test_metrics_are_emitted_with_the_counters(self, mod):
        runs = [_run(status="COMPLETE", files=("a",))]
        docs = {
            "doc#r1/a": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}
        }
        _stub_client(mod, runs, docs)
        mod.handler({}, _Context())
        mod.cloudwatch.put_metric_data.assert_called_once()
        emitted = {
            m["MetricName"]: m["Value"]
            for m in mod.cloudwatch.put_metric_data.call_args.kwargs["MetricData"]
        }
        # Every counter the response reports is also emitted, under the same
        # name — including the sweep's own failure modes, which were log-only.
        assert emitted["TestRunsExamined"] == 1
        assert emitted["TestRunsDue"] == 1
        for name in (
            "TestRunsReconcileAttempted",
            "TestRunsReconciled",
            "TestRunMetricsQueued",
            "TestRunsReconcileFailed",
            "TestRunsDeferred",
            "TestRunsUnrecognisedEvalStatus",
        ):
            assert name in emitted, name

    def test_a_metric_failure_does_not_fail_the_sweep(self, mod):
        runs = [_run(status="RUNNING")]
        _stub_client(mod, runs)
        mod.cloudwatch.put_metric_data.side_effect = RuntimeError("boom")
        assert mod.handler({}, _Context())["runsDue"] == 1

    def test_no_namespace_skips_emission(self, mod):
        runs = [_run(status="RUNNING")]
        _stub_client(mod, runs)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("METRIC_NAMESPACE", None)
            mod.handler({}, _Context())
        mod.cloudwatch.put_metric_data.assert_not_called()

    def test_an_empty_window_sweeps_nothing_and_says_so(self, mod):
        client = MagicMock()
        client.table.query.return_value = {"Items": []}
        client.batch_get_items.return_value = []
        mod._CLIENT["c"] = client
        result = mod.handler({}, _Context())
        assert result == {
            "runsExamined": 0,
            "runsDue": 0,
            "runsReconcileAttempted": 0,
            "statusesAdvanced": 0,
            "metricsQueued": 0,
            "unrecognisedEvalStatuses": 0,
            "failures": 0,
            "deferredToNextSweep": 0,
            "stoppedEarly": None,
        }


@pytest.mark.unit
class TestDiscovery:
    def test_discovery_queries_the_gsi_newest_first(self, mod):
        runs = [_run(status="RUNNING")]
        client = _stub_client(mod, runs)
        mod.handler({}, _Context())
        kwargs = client.table.query.call_args.kwargs
        assert kwargs["IndexName"] == "TypeDateIndex"
        # Newest first, so a truncating cap keeps the runs most likely to move.
        assert kwargs["ScanIndexForward"] is False
        assert kwargs["Limit"] == mod._EXAMINE_CHUNK

    def test_a_due_run_beyond_the_first_page_is_still_reconciled(self, mod):
        """Admission reaches the whole window, not just its first page.

        This is the property that makes the two bounds load-bearing: a stack can
        hold far more runs in the window than any one sweep should reconcile
        (1,076 on one measured stack), so if examination stopped at a page the
        oldest due runs would never be seen. Examination pages through; only the
        per-document work is budgeted.
        """
        settled = [
            _run(f"s{i}", status="COMPLETE", testRunResult={"overallAccuracy": 1})
            for i in range(3)
        ]
        stalled = _run("old", status="RUNNING", files=("a",))
        docs = {
            "doc#old/a": {
                "ObjectStatus": "COMPLETED",
                "EvaluationStatus": "COMPLETED",
            }
        }
        _stub_client(mod, settled + [stalled], docs, pages=[settled, [stalled]])
        result = mod.handler({}, _Context())
        assert result["runsExamined"] == 4
        assert result["runsDue"] == 1
        assert result["statusesAdvanced"] == 1

    def test_admission_reads_a_projection_not_whole_records(self, mod):
        """A sweep that finds nothing must not read the window in full.

        A full run record carries the whole ``Files`` list and the gzipped
        captured configuration; admission reads four attributes. Most sweeps find
        nothing due, so reading full records to decide is the dominant cost.
        """
        runs = [_run(status="COMPLETE", testRunResult={"ok": 1})]
        client = _stub_client(mod, runs)
        mod.handler({}, _Context())

        kwargs = client.batch_get_items.call_args.kwargs
        projection = kwargs["projection_expression"]
        names = kwargs["expression_attribute_names"]
        # Status is a DynamoDB reserved word, so it can only appear aliased.
        assert names["#s"] == "Status"
        assert "#s" in projection
        assert "Status" not in projection.replace("#s", "")
        # Everything needs_reconciliation reads must be present, or it reads as
        # absent — which for testRunResult admits every terminal run every sweep.
        for attr in ("PK", "SK", "testRunResult", "Purpose", "CacheUpdateAttempts"):
            assert attr in projection, attr
        # And no document reads happened at all for a settled run.
        assert client.table.get_item.call_count == 0

    def test_a_due_run_is_hydrated_in_full_before_reconciliation(self, mod):
        run = _run(status="RUNNING", files=("a",))
        docs = {
            "doc#r1/a": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}
        }
        client = _stub_client(mod, [run], docs)
        mod.handler({}, _Context())
        # Projected for admission, then read in full for the one run that needed
        # it — Files and the captured config are not in the projection.
        assert client.table.get_item.call_count == 1

    def test_a_run_deleted_between_discovery_and_reconciliation_is_skipped(self, mod):
        run = _run(status="RUNNING", files=("a",))
        client = _stub_client(mod, [run], {})
        client.table.get_item.side_effect = lambda **_k: {}
        result = mod.handler({}, _Context())
        assert result["failures"] == 0
        assert result["statusesAdvanced"] == 0

    def test_the_budget_stops_paging_rather_than_counting_the_rest(self, mod):
        """Spending the budget must stop the reads, not just the writes.

        Continuing to page would query and read every remaining record in the
        window solely to increment ``deferred`` — the expense the projection
        exists to avoid. ``deferred`` is a floor once this fires, and
        ``stoppedEarly`` is what says so.
        """
        page1 = [_run(f"a{i}", status="COMPLETE") for i in range(3)]
        page2 = [_run(f"b{i}", status="COMPLETE") for i in range(3)]
        client = _stub_client(mod, page1 + page2, pages=[page1, page2])
        result = mod.handler({"maxReconciled": 2}, _Context())
        assert result["stoppedEarly"] == "budget"
        # The second page was never queried.
        assert client.table.query.call_count == 1
        assert result["runsExamined"] == 3

    def test_the_examination_cap_still_bounds_an_enormous_table(self, mod):
        settled = [
            _run(f"s{i}", status="COMPLETE", testRunResult={"ok": 1}) for i in range(3)
        ]
        _stub_client(mod, settled, pages=[settled, settled])
        assert mod.handler({"maxExamined": 2}, _Context())["runsExamined"] == 2

    def test_the_reconcile_budget_defers_rather_than_drops(self, mod):
        runs = [_run(f"r{i}", status="COMPLETE") for i in range(4)]
        _stub_client(mod, runs)
        result = mod.handler({"maxReconciled": 2}, _Context())
        # Every due run is still counted, and what was not done is reported as
        # deferred rather than silently skipped.
        assert result["runsDue"] == 4
        assert result["deferredToNextSweep"] == 2
        assert result["stoppedEarly"] == "budget"

    def test_the_budget_counts_attempts_not_status_changes(self, mod):
        """The budget bounds the work, not the outcome.

        ⚠️ Every run here is reconciled and every run holds its status, which is
        the ordinary shape on a healthy stack and the shape the sibling test
        above cannot exercise: its runs have no document items, so each derives
        to QUEUED and each therefore *changes* status — the one input where "runs
        reconciled" and "runs whose status moved" coincide. The fan-out over a
        run's documents is paid whether or not the status moves, so the budget
        has to charge on entry; charging for status changes would leave this
        shape unbounded under any budget.
        """
        runs = [_run(f"r{i}", status="COMPLETE", files=("a",)) for i in range(4)]
        docs = {
            f"doc#r{i}/a": {
                "ObjectStatus": "COMPLETED",
                "EvaluationStatus": "COMPLETED",
            }
            for i in range(4)
        }
        _stub_client(mod, runs, docs)
        result = mod.handler({"maxReconciled": 2}, _Context())

        # Due because they are terminal with no cached metrics, and each derives
        # back to COMPLETE — so nothing changes status.
        assert result["runsDue"] == 4
        assert result["statusesAdvanced"] == 0
        # The budget still stopped at two, charged for the reads rather than the
        # writes.
        assert result["runsReconcileAttempted"] == 2
        assert result["deferredToNextSweep"] == 2
        assert result["stoppedEarly"] == "budget"
