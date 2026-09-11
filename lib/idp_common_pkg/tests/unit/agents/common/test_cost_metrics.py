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

    def test_asymmetric_regression_only_resets_the_dropped_counter(self):
        """Regression pin: if one counter grows and the other regresses,
        the growing counter must still emit an honest DELTA — not its
        cumulative total. The pre-fix code treated any regression as a
        full reset and re-emitted the still-growing counter from scratch
        (input 100→150 while output 50→30 would emit 150 input tokens
        instead of the true 50-token delta).
        """
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(_make_after_invocation_event(100, 50))
            # Input grew (100 → 150); output regressed (50 → 30).
            hook._on_after_invocation(_make_after_invocation_event(150, 30))

        assert mock_emit.call_count == 2
        second = mock_emit.call_args_list[1].kwargs
        assert second["bedrock_tokens_in"] == 50, (
            "input counter grew from 100 to 150 — honest delta is 50, "
            "not the pre-fix over-emit of 150"
        )
        assert second["bedrock_tokens_out"] == 30, (
            "output counter regressed to 30 — treated as fresh baseline, "
            "emit the new total"
        )

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

    def test_concurrent_invocations_no_torn_delta(self):
        """Regression: two threads invoking the same reused agent
        concurrently (e.g. parallel ``stream_async`` from a warm
        container) must not misattribute tokens by interleaving a
        read-modify-write on ``_last_input_tokens`` /
        ``_last_output_tokens``. The state update is guarded by
        ``self._state_lock``; the sum of emitted deltas across N
        concurrent invocations should equal the total observed
        accumulated_usage from all ticks.
        """
        import threading

        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        # 10 ticks presenting monotonically-increasing accumulated_usage
        # numbers. Interleaved firing from multiple threads must still
        # produce deltas that sum to the final total (1000 in / 100 out).
        events = [_make_after_invocation_event(i * 100, i * 10) for i in range(1, 11)]
        emitted_in: list[int] = []
        emitted_out: list[int] = []
        emit_lock = threading.Lock()

        def _capture(**kwargs):
            with emit_lock:
                emitted_in.append(kwargs.get("bedrock_tokens_in") or 0)
                emitted_out.append(kwargs.get("bedrock_tokens_out") or 0)

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric",
            side_effect=_capture,
        ):
            threads = [
                threading.Thread(target=hook._on_after_invocation, args=(ev,))
                for ev in events
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # The final observed totals were 1000 / 100. Regardless of the
        # order threads happened to run, the emitted deltas must sum to
        # exactly these totals — any torn read-modify-write would show
        # up as an under- or over-count on one side.
        assert sum(emitted_in) == 1000, (
            f"input deltas {emitted_in} sum to {sum(emitted_in)}, expected 1000"
        )
        assert sum(emitted_out) == 100, (
            f"output deltas {emitted_out} sum to {sum(emitted_out)}, expected 100"
        )

    def test_missing_metrics_does_not_raise(self):
        """Telemetry must never break the agent — if the metrics
        object shape changes (Strands API drift), the hook catches
        the narrow set of expected shape errors (AttributeError,
        KeyError, TypeError, ValueError) and returns without
        emitting. It does NOT catch RuntimeError or arbitrary
        exceptions — a genuine programming bug should surface via
        the agent's normal error handling, not be silently swallowed.
        """
        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )

        # Build a purpose-built type so setting the property doesn't
        # mutate the shared MagicMock class (which would leak the
        # raising property into unrelated tests via other MagicMock
        # instances). AttributeError simulates Strands API drift
        # (accumulated_usage renamed / removed).
        class _BrokenAgent:
            @property
            def event_loop_metrics(self):
                raise AttributeError("no accumulated_usage")

        broken_event = MagicMock()
        broken_event.agent = _BrokenAgent()

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric"
        ) as mock_emit:
            hook._on_after_invocation(broken_event)  # must not raise

        mock_emit.assert_not_called()
