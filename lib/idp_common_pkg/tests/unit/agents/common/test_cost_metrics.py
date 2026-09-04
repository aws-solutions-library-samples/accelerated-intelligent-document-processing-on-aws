# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Regression tests for ``ControlPlaneCostHook``.

Phase-2 wiring — see docs/reporting-sql-layer.md §10.5. The hook is what
makes ``control_plane_hourly.bedrock_tokens_in / bedrock_tokens_out /
est_bedrock_cost`` stop being permanently zero for the analytics agent
and marketplace monitor-agent.
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.agents.common.cost_metrics import ControlPlaneCostHook


def _make_after_invocation_event(input_tokens: int, output_tokens: int):
    """Build a stand-in for ``AfterInvocationEvent`` — the hook only
    reads ``event.agent.event_loop_metrics.accumulated_usage``, so a
    duck-typed shim is enough. Keeping this a plain function instead of
    importing Strands' actual event class avoids coupling the test to
    the Strands API surface.
    """
    event = MagicMock()
    event.agent.event_loop_metrics.accumulated_usage = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
    }
    return event


@pytest.mark.unit
class TestControlPlaneCostHookDeltaEmission:
    """The hook subtracts the last-emitted totals from the current
    cumulative usage. If a caller reuses one agent across many
    invocations, we want per-invocation deltas — not the cumulative
    total — landing in CloudWatch each time.
    """

    def test_first_invocation_emits_full_usage(self):
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )
        event = _make_after_invocation_event(input_tokens=100, output_tokens=50)

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(event)

        mock_emit.assert_called_once_with(
            component="analytics-agent",
            bedrock_tokens_in=100,
            bedrock_tokens_out=50,
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

    def test_second_invocation_emits_delta_only(self):
        """A reused agent's accumulated_usage keeps growing. The hook
        must emit ONLY the delta or CloudWatch will double-count."""
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(_make_after_invocation_event(100, 50))
            hook._on_after_invocation(_make_after_invocation_event(180, 90))

        assert mock_emit.call_count == 2
        _, second_kwargs = mock_emit.call_args_list[1]
        # 180 - 100 = 80 input, 90 - 50 = 40 output
        assert second_kwargs["bedrock_tokens_in"] == 80
        assert second_kwargs["bedrock_tokens_out"] == 40

    def test_regressing_usage_emits_new_totals_not_over_delta(self):
        """Regression: if ``accumulated_usage`` regresses (Strands
        event-loop metrics were reset) the previous clamp-to-zero
        implementation would still update the baseline to the smaller
        number, so the NEXT tick over-emitted the pre-existing tokens.
        The fix treats a regression as a fresh baseline — emit the new
        totals as-is, and the next non-regressing tick emits an honest
        delta against the reset value.
        """
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(_make_after_invocation_event(200, 100))
            # Regression: metrics reset to 30/15.
            hook._on_after_invocation(_make_after_invocation_event(30, 15))
            # Normal growth from the new baseline.
            hook._on_after_invocation(_make_after_invocation_event(80, 40))

        # Tick 1: fresh baseline, full 200/100.
        # Tick 2: regression, emit the new totals 30/15 as-is (not clamped
        #         to zero, which would leave a stale 200/100 baseline).
        # Tick 3: 80-30=50 in, 40-15=25 out — honest delta over the reset.
        assert mock_emit.call_count == 3
        third = mock_emit.call_args_list[2].kwargs
        assert third["bedrock_tokens_in"] == 50
        assert third["bedrock_tokens_out"] == 25

    def test_no_new_tokens_no_emission(self):
        """When accumulated_usage didn't change (e.g. the agent
        short-circuited without hitting Bedrock) the hook must skip
        the CloudWatch call entirely — a zero-token metric row wastes
        `PutMetricData` credit.
        """
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(_make_after_invocation_event(100, 50))
            # Same totals — no delta.
            hook._on_after_invocation(_make_after_invocation_event(100, 50))

        assert mock_emit.call_count == 1

    def test_missing_metrics_does_not_raise(self):
        """Telemetry must never break the agent — if the metrics
        object shape changes (Strands API drift) the hook should log
        a warning and continue.
        """
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        # Build a purpose-built type so setting the property doesn't
        # mutate the shared MagicMock class (which would leak the
        # raising property into unrelated tests via other MagicMock
        # instances).
        class _BrokenAgent:
            @property
            def event_loop_metrics(self):
                raise RuntimeError("no metrics")

        broken_event = MagicMock()
        broken_event.agent = _BrokenAgent()

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(broken_event)  # must not raise

        mock_emit.assert_not_called()
