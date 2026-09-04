# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Emit control-plane Bedrock cost metrics from a Strands agent.

Register `ControlPlaneCostHook(component=..., bedrock_model=...)` on any
control-plane Strands agent (analytics chat, monitor-agent, error-analyzer,
etc.). On every `AfterInvocationEvent` it reads the delta between the
agent's cumulative `event_loop_metrics.accumulated_usage` and the
last-emitted snapshot, then calls
`idp_common.metrics.emit_control_plane_cost_metric` so the rows in
`control_plane_hourly` gain non-zero `bedrock_tokens_in / bedrock_tokens_out
/ est_bedrock_cost` columns.

Phase-2 wiring — see docs/reporting-sql-layer.md §10.5.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable, List, Optional

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent

from idp_common.metrics import emit_control_plane_cost_metric

logger = logging.getLogger(__name__)


def with_cost_hook(
    hooks: Optional[Iterable[Any]], component: str, bedrock_model: str
) -> List[Any]:
    """Return a new hook list with `ControlPlaneCostHook` appended.

    Helper used by every in-tree Strands agent creator so the pattern
    ``list(kwargs.get("hooks") or []) + [ControlPlaneCostHook(...)]``
    lives in one place. Accepts ``None`` (returns just the cost hook)
    or any iterable (returns iterable + cost hook). Never mutates the
    caller's list.
    """
    return list(hooks or []) + [ControlPlaneCostHook(component, bedrock_model)]


class ControlPlaneCostHook(HookProvider):
    """Strands hook that emits Bedrock token counts per agent invocation.

    `event_loop_metrics.accumulated_usage` grows monotonically across
    invocations if the agent is reused. We snapshot the last-seen totals
    per-hook-instance and emit only the delta — so cost accounting is
    per-invocation, not cumulative.
    """

    def __init__(self, component: str, bedrock_model: str):
        """Configure the per-invocation Bedrock cost emitter for one agent.

        Args:
            component: Control-plane component label — must be one of the
                fixed set in docs/reporting-sql-layer.md §10.2 (e.g.
                ``analytics-agent``, ``monitor-agent``, ``monitor-dashboard``).
            bedrock_model: Bedrock model ID actually invoked by this agent
                (e.g. ``us.anthropic.claude-3-7-sonnet-20250219-v1:0``).
        """
        self.component = component
        self.bedrock_model = bedrock_model
        self._last_input_tokens: int = 0
        self._last_output_tokens: int = 0
        # Guards the delta-vs-snapshot state so concurrent invocations of
        # the same reused agent (parallel ``stream_async`` from the same
        # warm container) can't interleave a read-modify-write and
        # misattribute token counts. Held only around the arithmetic +
        # attribute update — NOT around the CloudWatch emit call, which
        # would otherwise serialise every hook's PutMetricData across
        # threads.
        self._state_lock = threading.Lock()

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)

    def _on_after_invocation(self, event: AfterInvocationEvent) -> None:
        try:
            usage = event.agent.event_loop_metrics.accumulated_usage
            # Do NOT ``or 0`` the get() result — falsy non-None shapes
            # (empty string, empty dict, False) should surface as
            # TypeError via the narrow except below rather than silently
            # rounding down to 0. Missing keys use the explicit 0
            # default; genuine 0 counts flow through as integers.
            total_in = int(usage.get("inputTokens", 0))
            total_out = int(usage.get("outputTokens", 0))
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            # Strands API drift (renamed field, changed shape) — log at
            # ERROR so operators see it in log-based alarms. The hook
            # itself must not break the agent for telemetry, so we return
            # instead of raising. A genuine programming bug (e.g.
            # RuntimeError from a downstream helper) is NOT caught here
            # and propagates for the agent's own error handling.
            logger.error(
                "ControlPlaneCostHook: Strands API drift or missing usage "
                "for component=%s (%s: %s) — no tokens emitted this "
                "invocation. Investigate if control_plane_hourly starts "
                "showing 0 est_bedrock_cost for this component.",
                self.component,
                type(exc).__name__,
                exc,
            )
            return

        # If either counter regressed below the last-seen baseline, the
        # Strands event loop was reset for that counter. Handle each
        # counter INDEPENDENTLY — treating the whole hook as reset when
        # only one counter drops would re-emit the still-growing counter
        # in full (e.g. input 100→150 while output 50→30 shouldn't emit
        # 150 input tokens, only the true 50-token delta). Compute the
        # delta and update the snapshot atomically under the lock so
        # concurrent invocations can't interleave read-modify-write.
        with self._state_lock:
            if total_in >= self._last_input_tokens:
                delta_in = total_in - self._last_input_tokens
            else:
                delta_in = total_in  # reset — new baseline starts at 0
            if total_out >= self._last_output_tokens:
                delta_out = total_out - self._last_output_tokens
            else:
                delta_out = total_out
            self._last_input_tokens = total_in
            self._last_output_tokens = total_out

        if delta_in == 0 and delta_out == 0:
            return

        # Pass None (not 0) for a zero-side counter so
        # ``emit_control_plane_cost_metric`` skips its ``PutMetricData``
        # datum — publishing a 0-valued CloudWatch datum burns metric
        # budget and adds a stream that will never yield useful signal.
        emit_control_plane_cost_metric(
            component=self.component,
            bedrock_tokens_in=delta_in if delta_in > 0 else None,
            bedrock_tokens_out=delta_out if delta_out > 0 else None,
            bedrock_model=self.bedrock_model,
        )
