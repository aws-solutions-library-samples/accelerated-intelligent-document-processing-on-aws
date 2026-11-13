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
        second = mock_emit.call_args_list[1].kwargs
        assert (second["bedrock_tokens_in"], second["bedrock_tokens_out"]) == (30, 15)
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
        """Ten threads invoking one reused agent emit deltas summing to its total.

        Concurrent invocations of one agent share one
        ``event_loop_metrics.accumulated_usage``, so this is modelled as one
        shared counter that each thread advances by its own invocation's tokens
        before its hook fires. (Giving each thread its own event with a fixed
        total, as this test used to, describes no real agent: a thread holding
        the smaller total that reaches the lock second looks like a counter
        reset under any implementation, so the test failed intermittently on
        scheduling alone.) The sum is exact whatever the interleaving; the
        deterministic interleaving that broke it is pinned by the next test.
        """
        import threading

        hook = ControlPlaneCostHook(
            component="analytics-agent",
            bedrock_model="us.anthropic.claude-3-7-sonnet-20250219-v1:0",
        )
        event = _make_after_invocation_event(0, 0)
        usage = event.agent.event_loop_metrics.accumulated_usage
        advance = threading.Lock()
        emitted_in: list[int] = []
        emitted_out: list[int] = []
        emit_lock = threading.Lock()

        def _capture(**kwargs):
            with emit_lock:
                emitted_in.append(kwargs.get("bedrock_tokens_in") or 0)
                emitted_out.append(kwargs.get("bedrock_tokens_out") or 0)

        def _invocation():
            with advance:
                usage["inputTokens"] += 100
                usage["outputTokens"] += 10
            hook._on_after_invocation(event)

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric",
            side_effect=_capture,
        ):
            threads = [threading.Thread(target=_invocation) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert sum(emitted_in) == 1000, (
            f"input deltas {emitted_in} sum to {sum(emitted_in)}, expected 1000"
        )
        assert sum(emitted_out) == 100, (
            f"output deltas {emitted_out} sum to {sum(emitted_out)}, expected 100"
        )

    def test_a_stale_usage_read_is_not_mistaken_for_a_reset(self):
        """The usage read happens under the lock that orders the deltas.

        Invocation A reads the shared usage (500 / 50) and is held there;
        invocation B then adds its own tokens (to 1000 / 100) and runs its hook
        as far as it can. If A's read was taken outside the lock, B emits
        1000 / 100 first and A's 500 / 50 then looks like a counter that went
        backwards, so it is re-emitted in full: 1500 / 150 for 1000 / 100 used.
        Under the lock, B waits for A and the deltas are 500 / 50 each.

        The schedule is forced, not hoped for: A is held inside its read until
        B has either finished or blocked on the hook's lock, which is detected
        by wrapping the lock rather than by sleeping.
        """
        import threading

        hook = ControlPlaneCostHook(component="analytics-agent", bedrock_model="m")
        a_reading = threading.Event()
        a_go = threading.Event()
        b_stopped = threading.Event()

        class _SharedUsage(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                if threading.current_thread().name == "A" and key == "outputTokens":
                    a_reading.set()
                    assert a_go.wait(10)
                return value

        class _ContendedLock:
            def __init__(self):
                self._lock = threading.Lock()

            def __enter__(self):
                if not self._lock.acquire(blocking=False):
                    if threading.current_thread().name == "B":
                        b_stopped.set()
                    self._lock.acquire()
                return self

            def __exit__(self, *exc):
                self._lock.release()

        hook._state_lock = _ContendedLock()
        usage = _SharedUsage(inputTokens=500, outputTokens=50)
        event = MagicMock()
        event.agent.event_loop_metrics.accumulated_usage = usage
        emitted = []

        def _invoke_b():
            hook._on_after_invocation(event)
            b_stopped.set()

        with patch(
            "idp_common.agents.common.cost_metrics.emit_control_plane_cost_metric",
            side_effect=lambda **k: emitted.append(
                (k["bedrock_tokens_in"], k["bedrock_tokens_out"])
            ),
        ):
            a = threading.Thread(
                target=hook._on_after_invocation, args=(event,), name="A"
            )
            a.start()
            assert a_reading.wait(10)
            usage.update(inputTokens=1000, outputTokens=100)
            b = threading.Thread(target=_invoke_b, name="B")
            b.start()
            assert b_stopped.wait(10)
            a_go.set()
            a.join(10)
            b.join(10)

        assert not a.is_alive() and not b.is_alive()
        assert sum(i for i, _ in emitted) == 1000, emitted
        assert sum(o for _, o in emitted) == 100, emitted

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
