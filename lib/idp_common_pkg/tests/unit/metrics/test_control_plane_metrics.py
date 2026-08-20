# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ``idp_common.metrics.emit_control_plane_cost_metric``.

The helper emits ``IDPControlPlane/*`` CloudWatch metrics that the hourly
rollup Lambda later aggregates into the ``control_plane_hourly`` table.
Every control-plane Lambda that hits Athena or Bedrock calls this at end
of invocation. See docs/reporting-sql-layer.md §10.5.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.metrics import emit_control_plane_cost_metric


@pytest.mark.unit
class TestEmitControlPlaneCostMetric:
    """Metric emission is the sole write path for control-plane cost data.
    Every metric that isn't emitted here is a $0 misattribution on the
    dashboard's Control Plane KPI, so this contract is load-bearing.
    """

    def _run_and_capture(self, **kwargs):
        """Invoke the helper with a mocked CloudWatch client and return
        the ``put_metric_data`` call kwargs (or None if no call was made).

        Defaults ``function_name`` to a fixed value so tests don't depend
        on the ``AWS_LAMBDA_FUNCTION_NAME`` env var — production emits
        that dim automatically from the Lambda runtime.
        """
        kwargs.setdefault("function_name", "TestFn")
        mock_cw = MagicMock()
        with patch("idp_common.metrics.get_cloudwatch_client", return_value=mock_cw):
            emit_control_plane_cost_metric(**kwargs)
        if not mock_cw.put_metric_data.called:
            return None
        return mock_cw.put_metric_data.call_args.kwargs

    def test_athena_only_emits_one_metric(self):
        """An Athena-only Lambda emits exactly one metric —
        ``AthenaBytesScanned`` — with the ``Component`` + ``FunctionName``
        dimensions. FunctionName is required so the rollup can attribute
        cost per-Lambda rather than duplicating a component total across
        every Lambda in the component."""
        call = self._run_and_capture(
            component="monitor-dashboard",
            athena_bytes=12_345_678,
            function_name="MonitorDashboardResolver",
        )
        assert call is not None
        assert call["Namespace"] == "IDPControlPlane"
        assert len(call["MetricData"]) == 1
        m = call["MetricData"][0]
        assert m["MetricName"] == "AthenaBytesScanned"
        assert m["Value"] == 12_345_678.0
        assert m["Unit"] == "Bytes"
        dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
        assert dims == {
            "Component": "monitor-dashboard",
            "FunctionName": "MonitorDashboardResolver",
        }

    def test_bedrock_emits_input_and_output_tokens_with_all_dims(self):
        """Bedrock invocations emit BOTH input + output token metrics.
        Both carry ``Component``, ``FunctionName``, ``Model`` dimensions —
        without ``FunctionName`` the rollup would multiply the component's
        cost across every Lambda in the component (real bug: 6x for the
        analytics-agent component on a live stack)."""
        call = self._run_and_capture(
            component="monitor-agent",
            function_name="ScheduledMonitorAgentLambda",
            bedrock_tokens_in=1000,
            bedrock_tokens_out=250,
            bedrock_model="us.anthropic.claude-opus-4-1",
        )
        assert call is not None
        names = {m["MetricName"] for m in call["MetricData"]}
        assert names == {"BedrockInputTokens", "BedrockOutputTokens"}
        for m in call["MetricData"]:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            assert dims == {
                "Component": "monitor-agent",
                "FunctionName": "ScheduledMonitorAgentLambda",
                "Model": "us.anthropic.claude-opus-4-1",
            }

    def test_function_name_defaults_to_lambda_env_var(self):
        """Production callers don't need to pass ``function_name`` — the
        helper reads ``AWS_LAMBDA_FUNCTION_NAME`` (set by the Lambda
        runtime) automatically. This test locks that convention in."""
        import os

        mock_cw = MagicMock()
        with (
            patch("idp_common.metrics.get_cloudwatch_client", return_value=mock_cw),
            patch.dict(os.environ, {"AWS_LAMBDA_FUNCTION_NAME": "MyProdLambda"}),
        ):
            emit_control_plane_cost_metric(
                component="analytics-agent", athena_bytes=100
            )
        call = mock_cw.put_metric_data.call_args.kwargs
        dims = {d["Name"]: d["Value"] for d in call["MetricData"][0]["Dimensions"]}
        assert dims["FunctionName"] == "MyProdLambda"

    def test_mixed_athena_and_bedrock_emits_all_three(self):
        """A Lambda that hits both Athena AND Bedrock (e.g., the
        analytics agent) emits three metrics in one PutMetricData call —
        one round-trip, not three."""
        call = self._run_and_capture(
            component="analytics-agent",
            athena_bytes=42_000_000,
            bedrock_tokens_in=500,
            bedrock_tokens_out=100,
            bedrock_model="us.anthropic.claude-sonnet-4",
        )
        assert call is not None
        names = [m["MetricName"] for m in call["MetricData"]]
        assert set(names) == {
            "AthenaBytesScanned",
            "BedrockInputTokens",
            "BedrockOutputTokens",
        }

    def test_bedrock_tokens_without_model_are_dropped(self):
        """Bedrock tokens without a model dimension are useless for
        cost math (can't apply per-model pricing). Rather than emitting
        a misleading metric under Model=unknown, drop them and log —
        the operator sees no data for that call, which is honest."""
        call = self._run_and_capture(
            component="monitor-agent",
            bedrock_tokens_in=1000,
            bedrock_tokens_out=250,
            # no bedrock_model
        )
        assert call is None, "Should not emit any metrics if bedrock model is missing"

    def test_no_args_makes_no_api_call(self):
        """Calling with no cost inputs must not hit CloudWatch. Every
        control-plane Lambda invocation will call this helper; a
        no-arg call must be free."""
        call = self._run_and_capture(component="test-set-mgmt")
        assert call is None

    def test_cloudwatch_failure_does_not_raise(self):
        """Best-effort emission: if CloudWatch throws, log and continue.
        Cost telemetry must never fail an invocation — imagine a
        dashboard resolver timing out because it couldn't emit its own
        cost metric."""
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("throttled")

        with patch("idp_common.metrics.get_cloudwatch_client", return_value=mock_cw):
            # Must not raise
            emit_control_plane_cost_metric(
                component="monitor-dashboard", athena_bytes=100
            )

    def test_zero_bytes_still_emits(self):
        """A zero-byte Athena scan is a valid data point (e.g., result
        reuse hit). Emitting Zero is different from emitting nothing —
        it lets the rollup Lambda count invocations that didn't scan."""
        call = self._run_and_capture(component="monitor-dashboard", athena_bytes=0)
        assert call is not None
        assert call["MetricData"][0]["Value"] == 0.0

    def test_zero_tokens_still_emits(self):
        """Same logic as ``test_zero_bytes_still_emits`` — a zero-token
        response can happen and it's real data."""
        call = self._run_and_capture(
            component="monitor-agent",
            bedrock_tokens_in=0,
            bedrock_tokens_out=0,
            bedrock_model="us.anthropic.claude-opus-4-1",
        )
        assert call is not None
        values = {m["MetricName"]: m["Value"] for m in call["MetricData"]}
        assert values["BedrockInputTokens"] == 0.0
        assert values["BedrockOutputTokens"] == 0.0
