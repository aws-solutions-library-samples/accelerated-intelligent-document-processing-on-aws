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
from unittest.mock import MagicMock, patch

import pytest


# Load the Lambda module by path so we don't need it on the sys.path.
def _load_module():
    spec = importlib.util.spec_from_file_location(
        "data_mart_rollup",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../../src/lambda/data_mart_rollup/index.py",
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

    def test_anchor_from_event_time_pins_retry_to_trigger_time(self, rollup):
        """Async retry must roll up the ORIGINAL trigger's hour, not
        wall-clock. If EventBridge fires at 14:05 UTC for hour 13 and
        the first attempt fails at 15:07 UTC (crossing hour boundary),
        the retry must still target hour 13 — not accidentally start
        rolling up hour 14 by using datetime.now()."""
        event = {"time": "2026-08-18T14:05:00Z"}
        anchor = rollup._parse_anchor_time(event)
        assert anchor == datetime(2026, 8, 18, 14, 5, tzinfo=timezone.utc)
        date, hour = rollup._previous_hour(anchor)
        assert (date, hour) == ("2026-08-18", "13")

    def test_anchor_without_event_time_falls_back_to_now(self, rollup):
        """Manual `aws lambda invoke` payloads don't include a `time`
        field. Fall back to wall-clock so the escape hatch keeps working."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(
                2026, 8, 18, 15, 30, tzinfo=timezone.utc
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # patch also patches datetime.fromisoformat inside the module,
            # so provide a passthrough
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            anchor = rollup._parse_anchor_time({})
        assert anchor.hour == 15

    def test_anchor_malformed_event_time_falls_back_to_now(self, rollup):
        """A garbage `time` field must not crash the rollup — fall back
        to now() with a warning. Prod ain't the place to enforce ISO 8601."""
        with patch.object(rollup, "datetime") as mock_dt:
            mock_dt.now.return_value = datetime(
                2026, 8, 18, 15, 30, tzinfo=timezone.utc
            )
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # fromisoformat is called on the raw string — make it raise
            mock_dt.fromisoformat.side_effect = ValueError("not iso")
            anchor = rollup._parse_anchor_time({"time": "definitely-not-a-timestamp"})
        assert anchor.hour == 15


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
            patch.object(rollup, "_require_hourly_matches_raw_metering"),
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

    def test_daily_raises_when_hourly_missing_a_populated_hour(self, rollup):
        """Missing any hour that RAW metering has data for must abort the
        daily rollup — that's the "hourly rollup transiently failed" case
        the guard exists to catch. Async retry will replay after the
        hourly catches up."""

        # Simulate: raw has hours 00-23 with data; metering_hourly missing hour 23
        # (the exact boundary case where the 23:xx hourly rollup failed retries).
        raw_hours = [[f"{h:02d}"] for h in range(24)]
        hourly_hours = [[f"{h:02d}"] for h in range(23)]

        def fake_query(sql):
            return (
                raw_hours
                if '"metering"' in sql and "hourly" not in sql
                else hourly_hours
            )

        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
        ):
            with pytest.raises(RuntimeError, match="missing hours"):
                rollup._run_daily()

    def test_daily_check_passes_when_hourly_matches_raw(self, rollup):
        """Sanity — every hour that has raw data is rolled up. Guard passes
        even when the day is only partially populated (e.g. deploy day)."""
        # Deploy-day scenario: raw metering has hours 12-19 only.
        # metering_hourly has the same hours 12-19. Should PASS the guard.
        partial_hours = [[f"{h:02d}"] for h in range(12, 20)]
        captured_sql = []

        def fake_query(_sql):
            return partial_hours  # both queries return the same partial set

        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
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

    def test_daily_check_passes_when_day_has_no_data_at_all(self, rollup):
        """A day with zero raw metering data (idle stack, holiday) should
        NOT block the daily rollup — the INSERT writes zero rows, but the
        partition is 'sealed' with a legitimate empty result."""
        empty = []

        def fake_query(_sql):
            return empty

        captured_sql = []
        with (
            patch.object(rollup, "_partition_already_written", return_value=False),
            patch.object(
                rollup, "_run_athena_query_with_results", side_effect=fake_query
            ),
            patch.object(
                rollup,
                "_run_athena",
                side_effect=lambda sql: captured_sql.append(sql) or "qid-empty",
            ),
        ):
            result = rollup._run_daily()
        assert result["skipped"] is False
        assert any("INSERT INTO" in s for s in captured_sql)


@pytest.mark.unit
class TestControlPlaneDiscovery:
    """Discovery is subtractive: all IDP Lambdas in the stack TREE minus
    data-plane (whitelist model, §10.3). Nested-stack Lambdas MUST be
    included — a Lambda in a nested stack carries the nested stack's
    aws:cloudformation:stack-name tag, not the root stack's. Filtering by
    root name alone missed 57 of 68 Lambdas on this repo's live topology."""

    def test_stack_tree_bfs_walks_nested_stacks(self, rollup):
        """Regression: enumerate_stack_tree must recurse into nested stacks.
        Skipping this walk was the bug that silently invisible-ed every
        Lambda under nested/api-resolvers/ (test-results, test-runner,
        config-mgmt, capacity-planner, finetuning, user-mgmt, etc.)."""
        # Map of stack name → its list_stack_resources page(s).
        pages_by_stack = {
            "root": [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-APIRESOLVERSTACK/abc",
                        },
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-BEDROCKKB/def",
                        },
                        {
                            "ResourceType": "AWS::Lambda::Function",
                            "PhysicalResourceId": "root-something",
                        },
                    ]
                }
            ],
            "root-APIRESOLVERSTACK": [
                {
                    "StackResourceSummaries": [
                        {
                            "ResourceType": "AWS::CloudFormation::Stack",
                            "PhysicalResourceId": "arn:aws:cloudformation:us-east-1:1:stack/root-APIRESOLVERSTACK-DEEP/xyz",
                        }
                    ]
                }
            ],
        }

        def paginate(**kwargs):
            return iter(
                pages_by_stack.get(
                    kwargs["StackName"], [{"StackResourceSummaries": []}]
                )
            )

        paginator = MagicMock()
        paginator.paginate.side_effect = paginate
        cfn = MagicMock()
        cfn.get_paginator.return_value = paginator

        with patch("boto3.client", return_value=cfn):
            tree = rollup._enumerate_stack_tree("root")
        # BFS: root, then its direct children, then grandchildren.
        assert tree == [
            "root",
            "root-APIRESOLVERSTACK",
            "root-BEDROCKKB",
            "root-APIRESOLVERSTACK-DEEP",
        ]

    def test_discovery_subtracts_data_plane_across_full_tree(self, rollup):
        """Discovery scopes BOTH the ``all_idp`` list AND the ``data_plane``
        subtraction to the full stack tree, not just the root. On a shared
        account with multiple IDP stacks, only THIS stack's data-plane
        Lambdas are subtracted from THIS stack's control-plane list."""
        stack_tree = ["idp-test-stack", "idp-test-stack-APIRESOLVERSTACK-abc"]
        # Root-stack Lambda (previously the only one seen) + a nested one
        # (which the old code missed).
        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:OCRFunction",  # root, data plane
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",  # nested, control
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",  # nested, control
        ]
        data_plane = ["arn:aws:lambda:us-east-1:1:function:OCRFunction"]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": stack_tree}:
                return list(all_idp)
            if tags == {
                "aws:cloudformation:stack-name": stack_tree,
                "idp:plane": ["data"],
            }:
                return list(data_plane)
            return []

        with (
            patch.object(rollup, "_enumerate_stack_tree", return_value=stack_tree),
            patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get),
        ):
            control = rollup._discover_control_plane_lambdas()
        assert set(control) == {
            "arn:aws:lambda:us-east-1:1:function:TestResultsResolver",
            "arn:aws:lambda:us-east-1:1:function:ConfigResolver",
        }

    def test_warns_on_probable_untagged_data_plane(self, rollup, caplog):
        """A Lambda with a doc-processing name that lacks the data tag
        is drift-detector fodder — WARN log for the operator to fix."""
        import logging

        stack_tree = ["idp-test-stack"]
        all_idp = [
            "arn:aws:lambda:us-east-1:1:function:MyExtractionFunctionRedacted",
        ]

        def fake_get(tags):
            if tags == {"aws:cloudformation:stack-name": stack_tree}:
                return list(all_idp)
            return []

        with (
            patch.object(rollup, "_enumerate_stack_tree", return_value=stack_tree),
            patch.object(rollup, "_get_resources_by_tag", side_effect=fake_get),
        ):
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
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(512, "x86_64")
        ):
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
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
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

    def test_lambda_cost_scales_with_actual_memory_at_same_arch(self, rollup):
        """Memory contribution: 4 GB → 8× the cost of 512 MB, holding
        architecture (and per-request cost) constant."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(4096, "arm64")
        ):
            rows_4gb = rollup._build_control_plane_rows(
                function_name="BigFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        with patch.object(rollup, "_get_lambda_memory_mb", return_value=(512, "arm64")):
            rows_512 = rollup._build_control_plane_rows(
                function_name="SmallFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        # Ratio isn't exactly 8 because the per-request cost is a fixed
        # additive term that doesn't scale with memory. Loosening the
        # tolerance to ±0.1 keeps the intent — 4 GB is materially more
        # than 512 MB — while accounting for the constant request term.
        assert (
            abs(rows_4gb[0]["est_lambda_cost"] / rows_512[0]["est_lambda_cost"] - 8)
            < 0.1
        )

    def test_x86_64_priced_higher_than_arm64_at_same_memory(self, rollup):
        """Same memory, same duration, different arch → x86_64 is
        ~25% more per GB-second. Confirms the arch dim isn't ignored."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "arm64")
        ):
            rows_arm = rollup._build_control_plane_rows(
                function_name="ArmFn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(1024, "x86_64")
        ):
            rows_x86 = rollup._build_control_plane_rows(
                function_name="X86Fn",
                component="test-runner",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={"duration_ms": 5000.0, "invocations": 1.0},
            )
        ratio = rows_x86[0]["est_lambda_cost"] / rows_arm[0]["est_lambda_cost"]
        # 0.0000166667 / 0.0000133334 ≈ 1.25 (per-request cost is tiny).
        assert 1.20 < ratio < 1.30

    def test_lambda_cost_includes_per_request_price(self, rollup):
        """Per-request cost ($0.20/1M) must be added — previously omitted
        entirely, undercounting by ~20% for high-invocation, short-duration
        Lambdas (LookupFunction: 100+ req/hour, <100ms each)."""
        with patch.object(
            rollup, "_get_lambda_memory_mb", return_value=(128, "x86_64")
        ):
            rows = rollup._build_control_plane_rows(
                function_name="ManyInvokesFn",
                component="api-dispatch",
                hour_ts=datetime(2026, 8, 18, 13, 0, tzinfo=timezone.utc),
                metrics={
                    "duration_ms": 0.001,  # essentially zero duration
                    "invocations": 1_000_000.0,
                },
            )
        # 1M invocations × $0.20/1M = $0.20 request cost dominates.
        assert rows[0]["est_lambda_cost"] >= 0.20 - 0.001

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
