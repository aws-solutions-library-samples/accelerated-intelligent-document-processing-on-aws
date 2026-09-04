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
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent

from idp_common.metrics import emit_control_plane_cost_metric

logger = logging.getLogger(__name__)


class ControlPlaneCostHook(HookProvider):
    """Strands hook that emits Bedrock token counts per agent invocation.

    `event_loop_metrics.accumulated_usage` grows monotonically across
    invocations if the agent is reused. We snapshot the last-seen totals
    per-hook-instance and emit only the delta — so cost accounting is
    per-invocation, not cumulative.
    """

    def __init__(self, component: str, bedrock_model: str):
        """Args:
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

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)

    def _on_after_invocation(self, event: AfterInvocationEvent) -> None:
        try:
            usage = event.agent.event_loop_metrics.accumulated_usage
            total_in = int(usage.get("inputTokens", 0) or 0)
            total_out = int(usage.get("outputTokens", 0) or 0)
        except Exception as exc:
            # Best-effort — never break the agent for telemetry.
            logger.warning("ControlPlaneCostHook: failed to read usage: %s", exc)
            return

        # If either counter regressed below the last-seen baseline, the
        # Strands event loop was reset (e.g. a reused agent whose metrics
        # were cleared between invocations). Treating that as a per-tick
        # delta and clamping to zero would then leave the baseline at the
        # smaller number and cause the NEXT tick to over-emit the tokens
        # that pre-existed the reset. Instead: emit the full new totals
        # (the baseline is stale anyway) and reset our snapshot.
        if total_in < self._last_input_tokens or total_out < self._last_output_tokens:
            delta_in, delta_out = total_in, total_out
        else:
            delta_in = total_in - self._last_input_tokens
            delta_out = total_out - self._last_output_tokens
        self._last_input_tokens = total_in
        self._last_output_tokens = total_out

        if delta_in == 0 and delta_out == 0:
            return

        emit_control_plane_cost_metric(
            component=self.component,
            bedrock_tokens_in=delta_in,
            bedrock_tokens_out=delta_out,
            bedrock_model=self.bedrock_model,
        )
