# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

# Mock boto3 before importing the Lambda module to prevent NoRegionError
# The Lambda creates boto3 clients at module level which requires AWS region
with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_resource.return_value = Mock()
    mock_client.return_value = Mock()

    # Import the specific lambda module using importlib to avoid conflicts
    spec = importlib.util.spec_from_file_location(
        "results_index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/test_results_resolver/index.py",
        ),
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load test_results_resolver module")
    index = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(index)


@pytest.mark.unit
def test_get_test_results_structure():
    """Test test results data structure"""
    test_run_id = "test-run-123"
    metadata = {
        "TestSetName": "lending-test",
        "Status": "COMPLETE",
        "FilesCount": 2,
        "CompletedFiles": 2,
        "FailedFiles": 0,
        "CreatedAt": "2025-01-01T00:00:00Z",
    }

    result = {
        "testRunId": test_run_id,
        "testSetName": metadata.get("TestSetName"),
        "status": metadata.get("Status"),
        "totalFiles": metadata.get("FilesCount", 0),
        "completedFiles": metadata.get("CompletedFiles", 0),
        "failedFiles": metadata.get("FailedFiles", 0),
        "overallAccuracy": 85.5,
        "averageConfidence": 78.2,
        "accuracyBreakdown": {
            "precision": 0.95,
            "recall": 0.90,
            "f1_score": 0.925,
            "false_alarm_rate": 0.05,
            "false_discovery_rate": 0.03,
        },
        "totalCost": 12.45,
        "createdAt": metadata.get("CreatedAt"),
    }

    assert result["testRunId"] == "test-run-123"
    assert result["testSetName"] == "lending-test"
    assert result["status"] == "COMPLETE"
    assert result["totalFiles"] == 2
    assert result["accuracyBreakdown"]["precision"] == 0.95
    assert result["accuracyBreakdown"]["f1_score"] == 0.925


# NOTE: These tests are commented out as they test the old Parquet-based cost retrieval
# which has been replaced with Athena-based queries in the test_results_resolver Lambda

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": "test-bucket"})
# @patch("boto3.client")
# @patch("pyarrow.parquet.read_table")
# @patch("pyarrow.fs.S3FileSystem")
# @patch("pyarrow.compute.equal")
# def test_get_document_costs_from_parquet_success(
#     mock_pc_equal, mock_s3fs, mock_read_table, mock_boto3
# ):
#     """Test successful Parquet cost retrieval"""
#     pass

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": "test-bucket"})
# @patch("boto3.client")
# def test_get_document_costs_no_files_found(mock_boto3):
#     """Test when no Parquet files are found"""
#     pass

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": ""})
# def test_get_document_costs_no_bucket():
#     """Test when REPORTING_BUCKET is not set"""
#     pass


@pytest.mark.unit
def test_accuracy_breakdown_structure():
    """Test accuracy breakdown data structure"""
    accuracy_breakdown = {
        "precision": 0.95,
        "recall": 0.90,
        "f1_score": 0.925,
        "false_alarm_rate": 0.05,
        "false_discovery_rate": 0.03,
    }

    # Verify all expected metrics are present
    expected_metrics = [
        "precision",
        "recall",
        "f1_score",
        "false_alarm_rate",
        "false_discovery_rate",
    ]
    for metric in expected_metrics:
        assert metric in accuracy_breakdown
        assert isinstance(accuracy_breakdown[metric], float)
        assert 0 <= accuracy_breakdown[metric] <= 1


@pytest.mark.unit
def test_get_test_run_status_evaluating():
    """Test test run status with EVALUATING state"""
    test_run_status = {
        "testRunId": "test-run-456",
        "status": "EVALUATING",
        "filesCount": 3,
        "completedFiles": 2,
        "failedFiles": 0,
        "evaluatingFiles": 1,
        "progress": 66.7,
    }

    assert test_run_status["status"] == "EVALUATING"
    assert test_run_status["completedFiles"] == 2
    assert test_run_status["evaluatingFiles"] == 1
    assert test_run_status["progress"] == 66.7


@pytest.mark.unit
def test_get_test_run_status_partial_complete():
    """Test test run status with PARTIAL_COMPLETE state"""
    test_run_status = {
        "testRunId": "test-run-789",
        "status": "PARTIAL_COMPLETE",
        "filesCount": 5,
        "completedFiles": 3,
        "failedFiles": 2,
        "evaluatingFiles": 0,
        "progress": 60.0,
    }

    assert test_run_status["status"] == "PARTIAL_COMPLETE"
    assert test_run_status["completedFiles"] == 3
    assert test_run_status["failedFiles"] == 2
    assert test_run_status["evaluatingFiles"] == 0
    assert test_run_status["progress"] == 60.0


@pytest.mark.unit
def test_compare_test_runs_structure():
    """Test test run comparison structure"""
    results = {
        "run-1": {"overall_accuracy": 85.5, "total_cost": 12.45},
        "run-2": {"overall_accuracy": 90.2, "total_cost": 15.30},
    }

    metrics_comparison = [
        {
            "metric": "Overall Accuracy",
            "values": {
                k: f"{v.get('overall_accuracy', 0)}%" for k, v in results.items()
            },
        },
        {
            "metric": "Total Cost",
            "values": {k: f"${v.get('total_cost', 0)}" for k, v in results.items()},
        },
    ]

    assert len(metrics_comparison) == 2
    assert metrics_comparison[0]["values"]["run-1"] == "85.5%"
    assert metrics_comparison[1]["values"]["run-2"] == "$15.3"


@pytest.mark.unit
def test_build_config_comparison():
    """Test configuration comparison"""
    configs = {
        "run-1": {"model": "claude-3", "temperature": 0.1},
        "run-2": {"model": "claude-4", "temperature": 0.2},
    }

    all_keys = set()
    for config in configs.values():
        all_keys.update(config.keys())

    config_diff = [
        {
            "setting": key,
            "values": {k: str(v.get(key, "N/A")) for k, v in configs.items()},
        }
        for key in all_keys
    ]

    assert len(config_diff) == 2
    assert "model" in [item["setting"] for item in config_diff]
    assert "temperature" in [item["setting"] for item in config_diff]


@pytest.mark.unit
def test_get_test_results_missing_metrics_returns_partial_not_raises():
    """When processing reached a terminal state but the evaluation aggregation
    never cached testRunResult (timed out / failed silently on a large run),
    get_test_results returns a structured partial TestRun instead of raising an
    opaque ValueError that leaves the UI spinning on "Loading..." (issue #358)."""
    test_run_id = "TEST-SET-ID"
    metadata = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        # Already terminal, so the status-refresh branch is skipped and we fall
        # straight through to the "no cached metrics" else branch.
        "Status": "COMPLETE",
        "TestSetId": "set-1",
        "TestSetName": "big-classification-set",
        "FilesCount": 3463,
        "CompletedFiles": 3460,
        "FailedFiles": 3,
        "CreatedAt": "2025-01-01T00:00:00Z",
        "Context": "ctx",
        "ConfigVersion": "v7",
        # No "testRunResult" key -> aggregation hasn't written metrics yet.
    }

    mock_table = Mock()
    mock_table.get_item.return_value = {"Item": metadata}

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        result = index.get_test_results(test_run_id)

    assert result["testRunId"] == test_run_id
    # Reports the true terminal status rather than fabricating one.
    assert result["status"] == "COMPLETE"
    assert result["filesCount"] == 3463
    assert result["completedFiles"] == 3460
    assert result["failedFiles"] == 3
    assert result["testSetId"] == "set-1"
    assert result["configVersion"] == "v7"
    # Metric fields are absent (not yet computed) but must not be required.
    assert "overallAccuracy" not in result or result["overallAccuracy"] is None


def _stale_cache_metadata(test_run_id, cached_metrics, status="COMPLETE"):
    """Terminal test run whose testRunResult is present but may be stale."""
    return {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": status,
        "TestSetId": "set-1",
        "TestSetName": "lending-test",
        "FilesCount": 10,
        "CompletedFiles": 10,
        "FailedFiles": 0,
        "CreatedAt": "2025-01-01T00:00:00Z",
        "testRunResult": cached_metrics,
    }


# A cache written before gradedPacketMetrics existed: every key the guard knew
# about at the time is present, so this is the exact shape of every historical
# test run's cache.
_PRE_GRADED_CACHE = {
    "overallAccuracy": 0.85,
    "weightedOverallScores": {"doc1.pdf": 0.9},
    "averageConfidence": 0.77,
    "accuracyBreakdown": {"precision": 0.9},
    "confusionMatrix": {"tp": 5},
    "fieldMetrics": {"Name": {"accuracy": 1.0}},
    "splitClassificationMetrics": {"page_level_accuracy": 0.9},
    "totalCost": 1.23,
    "costBreakdown": {},
}


@pytest.mark.unit
def test_stale_cache_serves_cached_metrics_and_queues_reaggregation():
    """A cache missing a key added by a later release must still resolve.

    The staleness check is a presence check, so every run cached before a new
    key landed trips it exactly once. If that path returned nothing,
    getTestRun would resolve to null — the UI renders "No test results found"
    and compareTestRuns silently drops the run — permanently, since nothing
    else re-enqueues a cache update for a run whose testRunResult exists.
    So: serve what we have, and recompute asynchronously.
    """
    test_run_id = "run-pre-graded"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, _PRE_GRADED_CACHE)
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    # The regression this pins: must not be None.
    assert result is not None
    assert result["testRunId"] == test_run_id
    # Metrics that WERE cached are still served, not discarded.
    assert result["overallAccuracy"] == 0.85
    assert result["splitClassificationMetrics"] == {"page_level_accuracy": 0.9}
    assert result["fieldMetrics"] == {"Name": {"accuracy": 1.0}}
    # The key the old cache lacks degrades to the "no data" shape the UI
    # already treats as "hide this panel".
    assert result["gradedPacketMetrics"] == {}
    # And a re-aggregation was queued so the next view has real values.
    mock_sqs.send_message.assert_called_once()
    queued_body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert queued_body == {"testRunId": test_run_id}


@pytest.mark.unit
def test_fresh_cache_does_not_requeue_when_graded_metrics_legitimately_empty():
    """Convergence guard: no infinite re-aggregation loop.

    handle_cache_update_request always writes gradedPacketMetrics (defaulting
    to {}), so a run whose aggregation legitimately produces no graded metrics
    — single-section docs, or no gt/pred page overlap — must satisfy the
    presence check after one pass and never be re-queued again.
    """
    test_run_id = "run-post-graded-empty"
    # A "fresh" post-release cache: adds every key the guard now checks for
    # (gradedPacketMetrics + excludedDocumentCount + classificationErrors as of
    # this release). Add the new key here whenever one joins metrics_to_cache,
    # otherwise this test fails for the right reason — the guard would re-queue
    # a cache that is in fact complete.
    fresh_cache = dict(
        _PRE_GRADED_CACHE,
        gradedPacketMetrics={},
        excludedDocumentCount=0,
        classificationErrors={},
    )
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, fresh_cache)
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result is not None
    assert result["gradedPacketMetrics"] == {}
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_stale_cache_still_resolves_when_queueing_fails():
    """Re-aggregation is best-effort — a broken/unconfigured queue must not
    turn a readable (if stale) result into a failed query."""
    test_run_id = "run-no-queue"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, _PRE_GRADED_CACHE)
    }
    mock_sqs = Mock()
    mock_sqs.send_message.side_effect = Exception("queue unavailable")

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result is not None
    assert result["overallAccuracy"] == 0.85


@pytest.mark.unit
def test_handler_field_routing():
    """Test GraphQL field routing"""

    def handler(event, context):
        field_name = event["info"]["fieldName"]

        if field_name == "getTestResults":
            return {"testRunId": event["arguments"]["testRunId"]}
        elif field_name == "getTestRuns":
            return [{"testRunId": "run-1"}]
        elif field_name == "compareTestRuns":
            return {"metrics": []}

        raise ValueError(f"Unknown field: {field_name}")

    # Test getTestResults
    event1 = {
        "info": {"fieldName": "getTestResults"},
        "arguments": {"testRunId": "test-123"},
    }
    result1 = handler(event1, {})
    assert result1["testRunId"] == "test-123"  # type: ignore[index]

    # Test getTestRuns
    event2 = {"info": {"fieldName": "getTestRuns"}, "arguments": {}}
    result2 = handler(event2, {})
    assert len(result2) == 1

    # Test unknown field
    event3 = {"info": {"fieldName": "unknownField"}, "arguments": {}}
    with pytest.raises(ValueError, match="Unknown field"):
        handler(event3, {})


# ---------------------------------------------------------------------------
# Issue #619: a test set name containing characters outside the SQL *identifier*
# allow-list (spaces, parentheses, quotes) made the Athena helpers raise, which
# threw away a perfectly good Stickler aggregation, cached nothing, and left the
# run permanently reporting EVALUATING with every file processed.
# ---------------------------------------------------------------------------

# The exact name that triggered the incident on the IDP1 stack.
_UNSAFE_RUN_ID = "ConfBench (light noise)-20260813-132501"


@pytest.mark.unit
def test_identifier_allow_list_still_rejects_injection():
    """The identifier guard must stay strict — it protects the database name."""
    for bad in ['db"; DROP TABLE x', "db; DROP TABLE x", "db name", "db'x", "db*", ""]:
        with pytest.raises(ValueError):
            index._validate_sql_input(bad, "database")

    # Hyphens and dots are legal — real database names need them.
    assert index._validate_sql_input("idp1-reporting-db", "database")


@pytest.mark.unit
def test_sql_literal_escapes_quotes():
    """Doubling `'` is the complete escape for a Trino single-quoted literal."""
    assert index._sql_literal("O'Brien", "test_run_id") == "O''Brien"
    # A classic literal-context break-out becomes inert data.
    assert index._sql_literal("x' OR '1'='1", "test_run_id") == "x'' OR ''1''=''1"
    # Backslash is NOT an escape character in Trino literals, so it must be left
    # alone here — doubling it would corrupt the matched value.
    assert index._sql_literal("a\\b", "test_run_id") == "a\\b"
    with pytest.raises(ValueError):
        index._sql_literal("", "test_run_id")


@pytest.mark.unit
def test_sql_like_prefix_neutralises_wildcards():
    """LIKE wildcards in a user-chosen name must match literally."""
    # `_` and `%` would otherwise widen the prefix match to other runs.
    assert index._sql_like_prefix("W2_Set", "test_run_id") == "W2\\_Set"
    assert index._sql_like_prefix("50%-set", "test_run_id") == "50\\%-set"
    # The escape character itself is escaped first, so it can't swallow the
    # character that follows it.
    assert index._sql_like_prefix("a\\_b", "test_run_id") == "a\\\\\\_b"
    # Spaces and parentheses — the issue #619 trigger — pass through untouched.
    assert index._sql_like_prefix(_UNSAFE_RUN_ID, "test_run_id") == _UNSAFE_RUN_ID


@pytest.mark.unit
def test_athena_evaluation_metrics_accepts_name_with_spaces_and_parens():
    """The regression: a parenthesised test set name must not raise.

    Before the fix `_validate_sql_input` was applied to test_run_id, which sits
    in a string-literal context, so any run whose test set name contained a
    space or paren failed aggregation outright.
    """
    captured = []

    def fake_execute(query, database):
        captured.append(query)
        return [{}]

    with (
        patch.dict(os.environ, {"ATHENA_DATABASE": "idp1-reporting-db"}),
        patch.object(index, "_execute_athena_query", side_effect=fake_execute),
    ):
        result = index._get_evaluation_metrics_from_athena(_UNSAFE_RUN_ID)

    assert result == {}  # empty Athena result set, but no exception
    assert captured, "query should have been built and executed"
    # The name is interpolated verbatim (nothing to escape) and paired with the
    # ESCAPE clause that _sql_like_prefix's output requires.
    assert f"LIKE '{_UNSAFE_RUN_ID}%'" in captured[0]
    assert "ESCAPE '\\'" in captured[0]


@pytest.mark.unit
def test_athena_cost_query_accepts_name_with_spaces_and_parens():
    """Same regression for the cost/metering query."""
    captured = []

    with (
        patch.dict(os.environ, {"ATHENA_DATABASE": "idp1-reporting-db"}),
        patch.object(
            index,
            "_execute_athena_query",
            side_effect=lambda q, d: captured.append(q) or [],
        ),
        patch.object(index, "_lookup_test_run_completed_at", return_value=None),
    ):
        result = index._get_cost_data_from_athena(_UNSAFE_RUN_ID)

    assert result == {"total_cost": 0, "cost_breakdown": {}}
    assert f"LIKE '{_UNSAFE_RUN_ID}/%'" in captured[0]
    # The embedded YYYYMMDD is still parsed out for partition pruning. With no
    # CompletedAt to size the window from, we fall back to the bounded 2-day
    # ``date IN (run_date, run_date+1)`` — see TestCostQueryDateWindow for the
    # derived-window cases.
    assert "date IN ('2026-08-13', '2026-08-14')" in captured[0]


@pytest.mark.unit
class TestCostQueryDateWindow:
    """``metering.date`` became COMPLETION time in the Phase-1 partitioning
    change, so a window fixed at ``run_date``/``run_date+1`` silently drops any
    run whose documents finish more than ~24h after the date embedded in its ID
    (HITL review, throttled or very large batches). The window is now derived
    from the run's own ``CompletedAt``.

    The opposite failure matters too: an unbounded upper edge scanned days of
    raw metering, hit ``HIVE_S3_THROTTLING`` and timed out the resolver's poll
    loop, leaving the UI's cost section empty. Hence the clamp.
    """

    RUN_ID = "lending-test-20260813-101500"

    def test_same_day_completion_keeps_the_two_day_window(self):
        """The overwhelmingly common case must not get more expensive: a run
        that completes the same day yields exactly the pre-change partitions."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-13T11:02:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_completion_just_before_midnight_still_covers_the_next_day(self):
        """A document completing at 23:58 has its metering row written moments
        later, possibly in the next date partition — that's the +1 day."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-13T23:58:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_multi_day_run_widens_the_window(self):
        """The regression this fixes: a 3-day HITL run's later completions used
        to fall outside the window and vanish from the reported cost."""
        assert index._cost_query_date_filter(self.RUN_ID, "2026-08-16T09:00:00Z") == (
            "AND date IN ('2026-08-13', '2026-08-14', '2026-08-15', "
            "'2026-08-16', '2026-08-17')"
        )

    def test_window_is_clamped_to_the_configured_maximum(self):
        """A pathological run (abandoned, or a clock problem putting
        CompletedAt months out) must not scan the whole lake."""
        sql = index._cost_query_date_filter(self.RUN_ID, "2026-09-12T09:00:00Z")
        assert sql.count("'") == 2 * (index._COST_QUERY_MAX_PARTITION_DAYS + 1)
        assert "'2026-08-13'" in sql  # run date is always the lower bound
        assert "'2026-09-12'" not in sql  # far edge dropped by the clamp

    def test_unparseable_completed_at_falls_back(self):
        assert (
            index._cost_query_date_filter(self.RUN_ID, "not-a-timestamp")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_completed_at_before_run_date_falls_back_rather_than_inverting(self):
        """If the ID's date and the tracking row disagree, take the wider of the
        two — never emit an empty or inverted range."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-01T09:00:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_naive_completed_at_is_treated_as_utc(self):
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-14T09:00:00")
            == "AND date IN ('2026-08-13', '2026-08-14', '2026-08-15')"
        )

    def test_run_id_without_a_date_leaves_the_query_unpruned(self):
        """Pre-existing behavior, unchanged: no parseable date means no filter
        (the selective ``document_id LIKE`` predicate still bounds the result)."""
        assert (
            index._cost_query_date_filter("no-date-here", "2026-08-14T09:00:00Z") == ""
        )

    def test_lookup_ignores_non_string_completed_at(self):
        """A stubbed DynamoDB client returns Mocks, not None. Only a real ISO
        string is usable; anything else must fall back quietly rather than reach
        the parser."""
        fake_table = Mock()
        fake_table.get_item.return_value = {"Item": {"CompletedAt": Mock()}}
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert index._lookup_test_run_completed_at(self.RUN_ID) is None

    def test_lookup_returns_the_stored_string(self):
        fake_table = Mock()
        fake_table.get_item.return_value = {
            "Item": {"CompletedAt": "2026-08-16T09:00:00Z"}
        }
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert (
                    index._lookup_test_run_completed_at(self.RUN_ID)
                    == "2026-08-16T09:00:00Z"
                )

    def test_lookup_failure_is_survivable(self):
        fake_table = Mock()
        fake_table.get_item.side_effect = RuntimeError("throttled")
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert index._lookup_test_run_completed_at(self.RUN_ID) is None


@pytest.mark.unit
def test_stickler_metrics_survive_athena_failure():
    """A failing Athena supplement must not discard good Stickler metrics.

    This is the core of issue #619: Stickler had already computed
    overall_accuracy=0.7232 for the run, but an exception from the *optional*
    Athena split-metrics call propagated out of _aggregate_test_run_metrics and
    into handle_cache_update_request's bare except, so nothing was ever cached.
    """
    stickler_body = {
        "overall_accuracy": 0.7232142857142857,
        "document_count": 10,
        "weighted_overall_scores": {"doc1.pdf": 0.6784},
        "average_confidence": 0.81,
        "confusion_matrix": {"tp": 5},
        "field_metrics": {"Name": {"accuracy": 1.0}},
        "graded_packet_metrics": {"mean": {"final_score": 0.7}},
    }
    mock_payload = Mock()
    mock_payload.read.return_value = json.dumps(
        {"statusCode": 200, "body": json.dumps(stickler_body)}
    )
    mock_lambda = Mock()
    mock_lambda.invoke.return_value = {"Payload": mock_payload}

    with (
        patch.dict(
            os.environ,
            {
                "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN": "arn:aws:lambda:::function:agg"
            },
        ),
        patch.object(index, "lambda_client", mock_lambda),
        patch.object(index, "_get_test_run_config", return_value={}),
        patch.object(index, "_invoke_mlflow_logger"),
        # Both Athena supplements blow up, exactly as they did on the live stack.
        patch.object(
            index,
            "_get_evaluation_metrics_from_athena",
            side_effect=ValueError("test_run_id contains invalid characters"),
        ),
        patch.object(
            index,
            "_get_cost_data_from_athena",
            side_effect=ValueError("test_run_id contains invalid characters"),
        ),
    ):
        result = index._aggregate_test_run_metrics(_UNSAFE_RUN_ID)

    # The Stickler numbers survive...
    assert result["overall_accuracy"] == 0.7232142857142857
    assert result["document_count"] == 10
    assert result["field_metrics"] == {"Name": {"accuracy": 1.0}}
    assert result["average_confidence"] == 0.81
    # ...and the Athena-only extras degrade to their documented "no data" shape
    # rather than taking the whole aggregation down with them.
    assert result["split_classification_metrics"] == {}
    assert result["total_cost"] == 0
    assert result["cost_breakdown"] == {}


@pytest.mark.unit
def test_classification_errors_are_cached_and_served():
    """The aggregator's per-section class detail must survive the cache round-trip.

    Three hops have to agree for this to reach the UI — the aggregation Lambda's
    snake_case key, the camelCase key written to testRunResult, and the read path
    — and each is in a different file, so a rename in one is invisible until the
    panel is silently empty.
    """
    test_run_id = "run-with-class-errors"
    payload = {
        "errors": [
            {
                "doc_key": "d1.pdf",
                "section_id": "section_1",
                "kind": "class",
                "expected_class": "Invoice",
                "predicted_class": "Receipt",
                "expected_pages": [0],
                "predicted_pages": [0],
            }
        ],
        "total": 1,
        "documents_affected": 1,
        "truncated": False,
    }
    mock_table = Mock()
    mock_sqs = Mock()

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(
            index,
            "_aggregate_test_run_metrics",
            return_value={"classification_errors": payload},
        ),
    ):
        index.handle_cache_update_request(
            {"Records": [{"body": json.dumps({"testRunId": test_run_id})}]}, None
        )

    cached = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
        ":metrics"
    ]
    assert cached["classificationErrors"] == payload

    # And the read path serves it rather than dropping it on the floor.
    mock_read_table = Mock()
    mock_read_table.get_item.return_value = {
        "Item": _stale_cache_metadata(
            test_run_id,
            dict(
                _PRE_GRADED_CACHE,
                gradedPacketMetrics={},
                excludedDocumentCount=0,
                classificationErrors=payload,
            ),
        )
    }
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_read_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result["classificationErrors"] == payload


@pytest.mark.unit
def test_a_cache_written_before_this_release_serves_an_empty_panel():
    """An older cache must render as "nothing to show", not crash the query."""
    test_run_id = "run-pre-class-errors"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, dict(_PRE_GRADED_CACHE))
    }

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result["classificationErrors"] == {}


@pytest.mark.unit
def test_missing_metrics_requeues_aggregation():
    """get_test_results must re-enqueue when a terminal run has no metrics.

    Nothing else will: the enqueue in get_test_run_status only fires on a status
    *transition*, and the stale-cache re-enqueue only fires when testRunResult
    already exists. Without this the run stays metric-less forever.
    """
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": {
            "PK": f"testrun#{_UNSAFE_RUN_ID}",
            "SK": "metadata",
            "Status": "COMPLETE",
            "FilesCount": 10,
            "CompletedFiles": 10,
            "FailedFiles": 0,
            # No testRunResult -> aggregation failed on its one attempt.
        }
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_results(_UNSAFE_RUN_ID)

    assert result["status"] == "COMPLETE"
    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert body == {"testRunId": _UNSAFE_RUN_ID}


@pytest.mark.unit
def test_aborted_run_without_metrics_does_not_requeue():
    """An ABORTED run is not eligible for aggregation, matching the enqueue
    condition on the status-transition path."""
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": {
            "PK": "testrun#aborted-run",
            "SK": "metadata",
            "Status": "ABORTED",
            "FilesCount": 10,
            "CompletedFiles": 4,
            "FailedFiles": 0,
            "CompletedFilesCounted": True,
        }
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
    ):
        result = index.get_test_results("aborted-run")

    assert result["status"] == "ABORTED"
    mock_sqs.send_message.assert_not_called()


def _status_table_for(
    test_run_id, files, stored_status, with_metrics=False, queued_at=None
):
    """Mock tracking table where every file is fully processed and evaluated."""
    metadata = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": stored_status,
        "Files": files,
        "FilesCount": len(files),
        "CompletedAt": "2026-08-13T13:27:11.869523Z",
    }
    if with_metrics:
        metadata["testRunResult"] = {"overallAccuracy": 0.72}
    if queued_at is not None:
        metadata["CacheUpdateQueuedAt"] = queued_at

    def get_item(Key):
        if Key["PK"] == f"testrun#{test_run_id}":
            return {"Item": metadata}
        return {"Item": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}}

    mock_table = Mock()
    mock_table.get_item.side_effect = get_item
    return mock_table


@pytest.mark.unit
def test_status_selfheals_when_already_terminal_without_metrics():
    """The observed symptom: EVALUATING badge with 10/10 processed, 0 evaluating.

    The run already reached COMPLETE on an earlier call, so the transition
    enqueue does not fire. This call must enqueue anyway, otherwise the badge
    never clears.
    """
    files = [f"doc{i}.pdf" for i in range(10)]
    mock_table = _status_table_for(_UNSAFE_RUN_ID, files, stored_status="COMPLETE")
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_run_status(_UNSAFE_RUN_ID)

    # The reported state that looked self-contradictory to the user.
    assert result["status"] == "EVALUATING"
    assert result["completedFiles"] == 10
    assert result["evaluatingFiles"] == 0
    # ...now accompanied by a recovery attempt.
    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_status_does_not_requeue_once_metrics_are_cached():
    """Convergence: once testRunResult exists, COMPLETE is reported and no
    further aggregation is enqueued."""
    files = [f"doc{i}.pdf" for i in range(10)]
    mock_table = _status_table_for(
        "good-run", files, stored_status="COMPLETE", with_metrics=True
    )
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
    ):
        result = index.get_test_run_status("good-run")

    assert result["status"] == "COMPLETE"
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_cache_update_throttle_collapses_concurrent_enqueues():
    """Three concurrent readers raced during the incident, each firing its own
    redundant aggregation. The conditional-write claim collapses them to one."""
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index, "sqs", mock_sqs),
        # Loser of the race: the condition expression failed.
        patch.object(index, "_claim_cache_update_slot", return_value=False),
    ):
        assert index._queue_cache_update("run-1") is False

    mock_sqs.send_message.assert_not_called()

    # throttle_seconds=0 bypasses the claim entirely (used for forced backfills).
    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=False),
    ):
        assert index._queue_cache_update("run-1", throttle_seconds=0) is True

    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_claim_cache_update_slot_fails_open():
    """A broken throttle must never block a legitimate recompute."""
    mock_table = Mock()
    mock_table.update_item.side_effect = Exception("dynamo unavailable")

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        assert index._claim_cache_update_slot("run-1", 300) is True


@pytest.mark.unit
def test_claim_cache_update_slot_denies_on_condition_failure():
    """A ConditionalCheckFailedException means someone else already claimed it."""
    from botocore.exceptions import ClientError

    mock_table = Mock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        assert index._claim_cache_update_slot("run-1", 300) is False


# ---------------------------------------------------------------------------
# Follow-up to #619 review: the "terminal but no metrics -> EVALUATING" rule is
# now defined once, and the throttle window is read from the item already in
# hand rather than by attempting a conditional write on every 5-second poll.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,has_metrics,expected",
    [
        # Terminal and metrics missing -> the aggregation really is outstanding.
        ("COMPLETE", False, "EVALUATING"),
        ("PARTIAL_COMPLETE", False, "EVALUATING"),
        # Metrics present -> report the true status.
        ("COMPLETE", True, "COMPLETE"),
        ("PARTIAL_COMPLETE", True, "PARTIAL_COMPLETE"),
        # ABORTED is terminal but is NOT eligible for aggregation, so it must
        # never be masked as EVALUATING however its metrics look.
        ("ABORTED", False, "ABORTED"),
        ("ABORTED", True, "ABORTED"),
        # Non-terminal statuses pass straight through.
        ("RUNNING", False, "RUNNING"),
        ("QUEUED", False, "QUEUED"),
    ],
)
def test_display_status_rule(status, has_metrics, expected):
    """One truth table for the badge, shared by all three resolvers."""
    item = {"testRunResult": {"overallAccuracy": 0.7}} if has_metrics else {}
    assert index._display_status(item, status) == expected
    assert index._awaiting_metrics(item, status) is (expected == "EVALUATING")


@pytest.mark.unit
def test_build_test_run_list_uses_shared_rule_but_does_not_enqueue():
    """The list view shows EVALUATING but must not fan out an enqueue per row.

    One list render covers an arbitrary number of runs; enqueueing for each stuck
    row would turn a page load into a burst of multi-minute aggregations. The
    per-row getTestRunStatus poll drives recovery instead.
    """
    items = [
        {
            "TestRunId": "stuck-1",
            "Status": "COMPLETE",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "stuck-2",
            "Status": "PARTIAL_COMPLETE",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "aborted-1",
            "Status": "ABORTED",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "good-1",
            "Status": "COMPLETE",
            "testRunResult": {"overallAccuracy": 0.72},
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
    ]
    mock_sqs = Mock()

    with patch.object(index, "sqs", mock_sqs):
        result = index._build_test_run_list(items)

    by_id = {r["testRunId"]: r["status"] for r in result}
    assert by_id["stuck-1"] == "EVALUATING"
    assert by_id["stuck-2"] == "EVALUATING"
    assert by_id["aborted-1"] == "ABORTED"
    assert by_id["good-1"] == "COMPLETE"
    # The regression this pins: rendering a list is not a write path.
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_cache_update_recently_queued_window():
    """Recent -> suppress; stale/absent/garbage -> fall through to the claim."""
    now = datetime.now(timezone.utc)

    # Inside the window.
    assert index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now - timedelta(seconds=30)).isoformat()}, 300
    )
    # Outside the window -> due for another attempt.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now - timedelta(seconds=301)).isoformat()}, 300
    )
    # Never queued.
    assert not index._cache_update_recently_queued({}, 300)
    # Throttling explicitly disabled.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": now.isoformat()}, 0
    )
    # Unreadable value must not silently suppress recovery.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": "not-a-timestamp"}, 300
    )
    assert not index._cache_update_recently_queued({"CacheUpdateQueuedAt": 12345}, 300)
    # A naive timestamp is treated as UTC rather than raising on the subtraction.
    assert index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": now.replace(tzinfo=None).isoformat()}, 300
    )
    # A future timestamp (clock skew) is not treated as "recent" in a way that
    # could suppress recovery forever -- negative age falls through.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now + timedelta(seconds=60)).isoformat()}, 300
    )


@pytest.mark.unit
def test_status_poll_skips_conditional_write_when_recently_queued():
    """The hot path: a 5-second poll on a stuck run must not write to DynamoDB.

    A rejected conditional write still consumes write capacity, so with the
    aggregation already in flight the poll should not attempt one at all.
    """
    files = [f"doc{i}.pdf" for i in range(10)]
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    mock_table = _status_table_for(
        "in-flight-run", files, stored_status="COMPLETE", queued_at=recent
    )
    mock_sqs = Mock()
    mock_claim = Mock(return_value=True)

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", mock_claim),
    ):
        result = index.get_test_run_status("in-flight-run")

    # Still reports the outstanding aggregation to the user...
    assert result["status"] == "EVALUATING"
    assert result["completedFiles"] == 10
    # ...without a duplicate enqueue or the conditional write behind it.
    mock_sqs.send_message.assert_not_called()
    mock_claim.assert_not_called()
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_status_poll_retries_once_throttle_window_expires():
    """Convergence: after the window lapses, recovery is attempted again."""
    files = [f"doc{i}.pdf" for i in range(10)]
    stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    mock_table = _status_table_for(
        "stale-claim-run", files, stored_status="COMPLETE", queued_at=stale
    )
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_run_status("stale-claim-run")

    assert result["status"] == "EVALUATING"
    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
class TestDraftLabelingRunsAreNotAwaitingMetrics:
    """A draft-labeling run CREATES the baseline, so it can never have metrics.

    Treating one as "awaiting" cost twice: it badged EVALUATING indefinitely
    (observed live — a run COMPLETE with 100/100 documents processed and
    CompletedAt set, still EVALUATING three days later), and every view enqueued a
    full aggregation, re-reading every document's results.json from S3, to compute
    extraction metrics that are structurally empty because there is nothing to
    score against.
    """

    def test_a_draft_labeling_run_is_never_awaiting_metrics(self):
        item = {
            "Status": "COMPLETE",
            "Purpose": "draft-labeling",
            "FilesCount": 100,
            "CompletedFiles": 100,
        }

        assert index._awaiting_metrics(item, "COMPLETE") is False
        # And therefore does not render the EVALUATING badge.
        assert index._display_status(item, "COMPLETE") == "COMPLETE"

    def test_a_scoring_run_with_no_metrics_still_awaits(self):
        """The behaviour this must not break: a real scored run genuinely is
        pending its aggregation, and the badge is how that is communicated."""
        item = {"Status": "COMPLETE", "Purpose": "scoring"}

        assert index._awaiting_metrics(item, "COMPLETE") is True
        assert index._display_status(item, "COMPLETE") == "EVALUATING"

    def test_a_scoring_run_with_metrics_does_not_await(self):
        item = {"Status": "COMPLETE", "Purpose": "scoring", "testRunResult": {"x": 1}}

        assert index._awaiting_metrics(item, "COMPLETE") is False

    def test_a_run_predating_Purpose_falls_back_to_its_context(self):
        """Records created before Purpose was persisted carry only the free-text
        Context. Matched exactly, not as a substring: a user-typed context that
        merely mentions labeling must not silently suppress a real run's badge."""
        legacy = {"Status": "COMPLETE", "Context": "Draft labeling run"}
        assert index._awaiting_metrics(legacy, "COMPLETE") is False

        lookalike = {"Status": "COMPLETE", "Context": "Draft labeling run for Q3"}
        assert index._awaiting_metrics(lookalike, "COMPLETE") is True

    def test_purpose_wins_over_a_misleading_context(self):
        """A persisted Purpose is authoritative; Context is user-supplied text."""
        item = {
            "Status": "COMPLETE",
            "Purpose": "scoring",
            "Context": "Draft labeling run",
        }

        assert index._awaiting_metrics(item, "COMPLETE") is True

    def test_the_purpose_is_reported_to_the_ui_not_just_used_internally(self):
        """The rule has one home, and the UI has to be able to reach the verdict.

        Without this the UI can only guess from the free-text Context, which is the
        very thing the exact-match fallback exists to distrust — and it warned that
        accuracy metrics "are not available" on a run that can never have them,
        describing the expected outcome as a fault.
        """
        draft = {"Status": "COMPLETE", "Purpose": "draft-labeling"}
        scoring = {"Status": "COMPLETE", "Purpose": "scoring"}

        assert index._is_draft_labeling_run(draft) is True
        assert index._is_draft_labeling_run(scoring) is False

        # The same distrust of Context that _awaiting_metrics applies.
        assert index._is_draft_labeling_run({"Context": "Draft labeling run"}) is True
        assert (
            index._is_draft_labeling_run({"Context": "Draft labeling run for Q3"})
            is False
        )
