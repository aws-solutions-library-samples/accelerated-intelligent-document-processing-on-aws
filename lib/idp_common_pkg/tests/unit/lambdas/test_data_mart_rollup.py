# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the data-mart rollup Lambda.

Two modes: ``hourly`` (writes ``metering_hourly`` + ``control_plane_hourly``)
and ``daily`` (writes ``metering_daily`` from ``metering_hourly``).

Coverage focus:
- Mode dispatch (hourly vs daily)
- Idempotency (skip if partition already written)
- Time-window math (previous UTC hour / previous UTC day)
- Metering SQL shape (INSERT INTO ... SELECT with correct WHERE)
- Control-plane discovery (all IDP Lambdas minus data-plane)
- CloudWatch metric aggregation → row shape
- Component-label mapping heuristic
"""

import importlib.util
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


# Load the Lambda module by path so we don't need it on the sys.path.
def _load_module():
    spec = importlib.util.spec_from_file_location(
        "data_mart_rollup",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../../patterns/unified/src/data_mart_rollup_function/index.py",
        ),
    )
    assert spec and spec.loader, "Could not load rollup Lambda module"
    with patch.dict(
        os.environ,
        {
            "REPORTING_DATABASE": "idp-reporting",
            "REPORTING_BUCKET": "test-reporting-bucket",
            "STACK_NAME": "idp-test-stack",
            "ATHENA_WORKGROUP": "primary",
        },
    ):
        with patch("boto3.client"):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    return module


@pytest.fixture
def rollup():
    """Reload the module fresh per test so global boto3 mocks don't leak."""
    return _load_module()


@pytest.mark.unit
class TestModeDispatch:
    def test_default_mode_is_hourly(self, rollup):
        """Ad-hoc console invocations with no payload default to hourly
        — the more common case. Failing safe = do the more useful thing."""
        with (
            patch.object(rollup, "_run_hourly", return_value={"mode": "hourly"}) as h,
            patch.object(rollup, "_run_daily") as d,
        ):
            result = rollup.handler({}, None)
        h.assert_called_once()
        d.assert_not_called()
        assert result["mode"] == "hourly"

    def test_explicit_daily_mode(self, rollup):
        with (
            patch.object(rollup, "_run_hourly") as h,
            patch.object(rollup, "_run_daily", return_value={"mode": "daily"}) as d,
        ):
            result = rollup.handler({"mode": "daily"}, None)
        h.assert_not_called()
        d.assert_called_once()
        assert result["mode"] == "daily"

    def test_unknown_mode_raises(self, rollup):
        with pytest.raises(ValueError, match="Unknown rollup mode"):
            rollup.handler({"mode": "weekly"}, None)


@pytest.mark.unit
class TestTimeWindows:
    """Time math is load-bearing — a bug here writes the wrong partition
    and either misses a whole hour of data or double-counts it."""

    def test_previous_hour_wraps_over_midnight(self, rollup):
        """00:00 UTC processes the previous day's last hour (23)."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date, hour = rollup._previous_hour()
        assert date == "2026-08-17"
        assert hour == "23"

    def test_previous_hour_normal_case(self, rollup):
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date, hour = rollup._previous_hour()
        assert date == "2026-08-18"
        assert hour == "13"

    def test_previous_day(self, rollup):
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 8, 18, 0, 15, tzinfo=timezone.utc)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            date = rollup._previous_day()
        assert date == "2026-08-17"

    def test_hour_window_returns_utc_bounds(self, rollup):
        start, end = rollup._hour_window("2026-08-18", "14")
        assert start == datetime(2026, 8, 18, 14, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)


@pytest.mark.unit
class TestMeteringHourlyRollup:
    """The core value-add: partition + skip-if-exists + INSERT SQL."""

    def test_skips_when_partition_already_written(self, rollup):
        """Idempotency guard — if the target partition has any rows, we
        MUST NOT run the INSERT again. Duplicate EventBridge fires would
        double-count otherwise."""
        with (
            patch.object(rollup, "_partition_already_written", return_value=True),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            result = rollup._rollup_metering_hourly("2026-08-18", "13")
        assert result["skipped"] is True
        assert result["reason"] == "partition_exists"
        mock_athena.assert_not_called()

    def test_insert_sql_filters_to_target_partition(self, rollup):
        """The INSERT ... SELECT must scope to the target (date, hour)
        via a WHERE clause with partition columns — otherwise we'd
        scan (and re-aggregate) the whole table."""
        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql: captured_sql.append(sql) or "qid-123",
            ),
        ):
            result = rollup._rollup_metering_hourly("2026-08-18", "13")
        assert result["skipped"] is False
        assert result["query_execution_id"] == "qid-123"
        sql = captured_sql[0]
        assert "INSERT INTO" in sql
        assert '"metering_hourly"' in sql
        assert "FROM" in sql and '"metering"' in sql
        assert "date = '2026-08-18'" in sql
        assert "hour = '13'" in sql
        # Must GROUP BY the rollup dimensions
        assert "GROUP BY" in sql


@pytest.mark.unit
class TestMeteringDailyRollup:
    def test_reads_from_metering_hourly_not_raw(self, rollup):
        """The daily rollup reads the already-aggregated ``metering_hourly``
        table, not raw ``metering``. Reading raw would defeat the purpose
        of the hourly rollup (same GB scan, twice the work).
        """
        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(rollup, "_require_all_24_hours_present"),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql: captured_sql.append(sql) or "qid-456",
            ),
        ):
            rollup._run_daily()
        sql = captured_sql[0]
        assert '"metering_daily"' in sql
        assert '"metering_hourly"' in sql
        assert (
            '"metering"' not in sql or 'FROM "idp-reporting"."metering_hourly"' in sql
        )
        # Uses the renamed n_doc_events column (was n_docs).
        assert "n_doc_events" in sql
        assert "SUM(n_docs)" not in sql

    def test_daily_skip_when_partition_exists(self, rollup):
        with (
            patch.object(rollup, "_partition_already_written", return_value=True),
            patch.object(rollup, "_run_athena") as mock_athena,
        ):
            result = rollup._run_daily()
        assert result["skipped"] is True
        mock_athena.assert_not_called()

    def test_daily_raises_when_hourly_incomplete(self, rollup):
        """Missing any of the 24 hourly partitions must abort the daily
        rollup — otherwise the partition writes with under-counted totals
        and locks in permanently via the idempotency check."""

        # Simulate: hours 00-22 present, hour 23 missing (the exact
        # boundary case where the 23:xx hourly rollup failed retries).
        present_hours = [[f"{h:02d}"] for h in range(23)]
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", return_value=present_hours
            ),
        ):
            with pytest.raises(RuntimeError, match="missing hours"):
                rollup._run_daily()

    def test_daily_hourly_check_passes_when_all_24_present(self, rollup):
        """Sanity — 24 hours present, the check passes and INSERT runs."""
        present_hours = [[f"{h:02d}"] for h in range(24)]
        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", return_value=present_hours
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql: captured_sql.append(sql) or "qid-789",
            ),
        ):
            result = rollup._run_daily()
        assert result["skipped"] is False
        # The INSERT must actually run when the check passes.
        assert any("INSERT INTO" in s for s in captured_sql)


@pytest.mark.unit
class TestControlPlaneDiscovery:
    """Discovery is subtractive: all IDP Lambdas minus data-plane
    (whitelist model, §10.3). Correctness of this set determines
    whether Control Plane Cost KPI is accurate."""

    def test_subtracts_data_plane_arns_stack_scoped(self, rollup):
        """Discovery must scope BOTH the ``all_idp`` list AND the
        ``data_plane`` subtraction to this stack via the
        ``aws:cloudformation:stack-name`` tag. An account with multiple
        IDP stacks would otherwise subtract sibling stacks' data-plane
        Lambdas from this stack's list (harmless), or worse — return
        their Lambdas' ARNs (bug)."""
        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:OCRFunction",
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",
        ]
        data_plane = ["arn:aws:lambda:us-east-1:1:function:OCRFunction"]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": ["idp-test-stack"]}:
                return list(all_idp)
            # BOTH filters combined → stack-scoped data plane subtraction.
            if tags == {
                "aws:cloudformation:stack-name": ["idp-test-stack"],
                "idp:plane": ["data"],
            }:
                return list(data_plane)
            return []

        with patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get):
            control = rollup._discover_control_plane_lambdas()
        assert set(control) == {
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",
        }

    def test_warns_on_probable_untagged_data_plane(self, rollup, caplog):
        """A Lambda with a doc-processing name that lacks the data tag
        is drift-detector fodder — WARN log for the operator to fix."""
        import logging

        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:MyExtractionFunctionRedacted",
        ]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": ["idp-test-stack"]}:
                return list(all_idp)
            return []

        with patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get):
            with caplog.at_level(logging.WARNING):
                control = rollup._discover_control_plane_lambdas()

        assert control  # still returned
        assert any("untagged data-plane Lambda" in m for m in caplog.messages), (
            f"Expected WARN about probable untagged data-plane Lambda, "
            f"got: {caplog.messages!r}"
        )

    def test_returns_empty_when_stack_name_missing(self, rollup):
        with patch.object(rollup, "STACK_NAME", ""):
            assert rollup._discover_control_plane_lambdas() == []


@pytest.mark.unit
class TestComponentMapping:
    """Component labels drive the dashboard's drill-down grouping. A
    wrong label makes the row appear under the wrong bucket, not the
    wrong cost total — mild but user-visible."""

    def test_monitoring_dashboard(self, rollup):
        assert (
            rollup._component_for_function("MonitoringMetricsServiceFn")
            == "monitor-dashboard"
        )

    def test_monitor_agent(self, rollup):
        assert (
            rollup._component_for_function("ScheduledMonitorAgentLambda")
            == "monitor-agent"
        )

    def test_test_set_mgmt(self, rollup):
        assert (
            rollup._component_for_function("TestSetResolverFunction") == "test-set-mgmt"
        )

    def test_test_runner(self, rollup):
        assert rollup._component_for_function("TestRunnerFunction") == "test-runner"

    def test_test_file_copier(self, rollup):
        """Regression: ``TestFileCopierFunction`` used to fall through to
        ``other-control`` because the heuristic only matched ``filecopy``
        (missing the 'ier' variant). Now covered explicitly."""
        assert rollup._component_for_function("TestFileCopierFunction") == "test-runner"

    def test_doc_chat_maps_correctly(self, rollup):
        """User chat with a specific document lands under ``doc-chat``,
        separate from the analytics-agent chat (which is SQL-driven)."""
        assert (
            rollup._component_for_function("ChatWithDocumentProcessorFunction")
            == "doc-chat"
        )
        assert (
            rollup._component_for_function("ChatStreamProcessorFunction") == "doc-chat"
        )

    def test_user_mgmt_maps_correctly(self, rollup):
        assert rollup._component_for_function("UserManagementFunction") == "user-mgmt"
        assert rollup._component_for_function("UserSyncFunction") == "user-mgmt"

    def test_api_dispatch_maps_correctly(self, rollup):
        """Every UI page load hits these — high-volume, worth breaking out."""
        assert rollup._component_for_function("LookupFunction") == "api-dispatch"
        assert rollup._component_for_function("ApiHandlerFunction") == "api-dispatch"
        assert (
            rollup._component_for_function("HttpApiDispatcherFunction")
            == "api-dispatch"
        )

    def test_agent_processor_folds_into_analytics_agent(self, rollup):
        assert (
            rollup._component_for_function("AgentProcessorFunction")
            == "analytics-agent"
        )

    def test_config_mgmt(self, rollup):
        assert rollup._component_for_function("ConfigResolverFunction") == "config-mgmt"

    def test_rollup_self(self, rollup):
        assert (
            rollup._component_for_function("DataMartRollupFunction") == "rollup-lambda"
        )

    def test_unknown_falls_back_to_other_control(self, rollup):
        assert rollup._component_for_function("SomeNewFeatureLambda") == "other-control"


@pytest.mark.unit
class TestControlPlaneRowBuilding:
    def test_zero_activity_yields_no_row(self, rollup):
        """A control-plane Lambda that didn't run this hour must not
        emit a row — otherwise ``control_plane_hourly`` is padded with
        zero-cost noise the dashboard has to filter."""
        rows = rollup._build_control_plane_rows(
            function_name="MyFn",
            component="monitor-dashboard",
            hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
            metrics={"duration_ms": 0.0, "invocations": 0.0},
        )
        assert rows == []

    def test_row_no_bedrock_emits_one_null_model_row(self, rollup):
        """Component that didn't call Bedrock this hour → one row with
        bedrock_model=None capturing Lambda+Athena cost only."""
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=512):
            rows = rollup._build_control_plane_rows(
                function_name="TestRunnerFunction",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 5000.0,
                    "invocations": 3.0,
                    "athena_bytes": 1_000_000.0,
                    "bedrock_by_model": {},
                },
            )
        assert len(rows) == 1
        r = rows[0]
        assert r["bedrock_model"] is None
        assert r["invocations"] == 3
        assert r["duration_ms_sum"] == 5000
        assert r["athena_bytes_sum"] == 1_000_000
        assert r["bedrock_tokens_in"] == 0
        assert r["bedrock_tokens_out"] == 0
        assert r["est_lambda_cost"] > 0
        assert r["est_athena_cost"] > 0
        assert r["est_bedrock_cost"] == 0.0

    def test_row_per_bedrock_model(self, rollup):
        """A component that called two Bedrock models emits one row per
        model, each with the correct per-model pricing applied."""
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=1024):
            rows = rollup._build_control_plane_rows(
                function_name="AnalyticsAgentFn",
                component="analytics-agent",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 10_000.0,
                    "invocations": 5.0,
                    "athena_bytes": 0.0,
                    "bedrock_by_model": {
                        "us.anthropic.claude-opus-4-1": {"in": 1000, "out": 500},
                        "us.anthropic.claude-haiku-4-5": {"in": 2000, "out": 1000},
                    },
                },
            )
        assert len(rows) == 2
        by_model = {r["bedrock_model"]: r for r in rows}
        # Opus pricing: $15 in / $75 out per 1K
        opus = by_model["us.anthropic.claude-opus-4-1"]
        assert opus["bedrock_tokens_in"] == 1000
        assert opus["bedrock_tokens_out"] == 500
        assert (
            abs(opus["est_bedrock_cost"] - (1000 * 15 / 1000 + 500 * 75 / 1000)) < 1e-6
        )
        # Haiku pricing: $0.80 in / $4 out per 1K
        haiku = by_model["us.anthropic.claude-haiku-4-5"]
        assert (
            abs(haiku["est_bedrock_cost"] - (2000 * 0.80 / 1000 + 1000 * 4 / 1000))
            < 1e-6
        )

    def test_lambda_cost_scales_with_actual_memory(self, rollup):
        """Cost estimate uses the Lambda's real MemorySize (not the
        previous fixed 512 MB assumption). 4 GB memory → 8× the
        cost that a 512 MB assumption would give at the same duration."""
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=4096):
            rows_4gb = rollup._build_control_plane_rows(
                function_name="BigFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=512):
            rows_512 = rollup._build_control_plane_rows(
                function_name="SmallFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        assert (
            abs(rows_4gb[0]["est_lambda_cost"] / rows_512[0]["est_lambda_cost"] - 8)
            < 0.01
        )

    def test_flatten_cw_response_missing_values(self, rollup):
        """CloudWatch returns an empty Values list when there's no data
        for the query. We treat missing as 0.0 — the alternative
        (raising) would fail rollups for any Lambda that idled all hour."""
        response = {
            "MetricDataResults": [
                {"Id": "duration", "Values": [1234.5]},
                {"Id": "invocations", "Values": []},
                {"Id": "athena_bytes", "Values": []},
            ]
        }
        result = rollup._flatten_cw_response(response)
        assert result["duration"] == 1234.5
        assert result["invocations"] == 0.0
        assert result["athena_bytes"] == 0.0

    def test_flatten_cw_response_filters_nan(self, rollup):
        """A NaN value slipping through would poison a downstream int()
        cast. _flatten_cw_response must drop NaN before summing."""
        response = {
            "MetricDataResults": [
                {"Id": "duration", "Values": [1000.0, float("nan"), 500.0]},
            ]
        }
        result = rollup._flatten_cw_response(response)
        assert result["duration"] == 1500.0


@pytest.mark.unit
class TestBedrockPricing:
    """Pricing lookup must handle new/unknown model IDs gracefully —
    dashboard shows an over- or under-estimate, but never fails."""

    def test_opus_pricing(self, rollup):
        p = rollup._bedrock_price_for_model("us.anthropic.claude-opus-4-1-abc")
        assert p["in"] == 15.0
        assert p["out"] == 75.0

    def test_sonnet_pricing(self, rollup):
        p = rollup._bedrock_price_for_model("us.anthropic.claude-sonnet-4-20250514")
        assert p["in"] == 3.0

    def test_unknown_model_falls_back(self, rollup):
        p = rollup._bedrock_price_for_model("some-brand-new-model")
        assert p == rollup.DEFAULT_BEDROCK_PRICE

    def test_none_model_falls_back(self, rollup):
        p = rollup._bedrock_price_for_model(None)
        assert p == rollup.DEFAULT_BEDROCK_PRICE
