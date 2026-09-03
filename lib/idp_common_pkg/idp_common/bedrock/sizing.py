# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Model-aware auto-sizing of shard budgets and confidence list-batch sizes.

The right shard size and confidence list-batch size depend on the MODEL's input
context window and output cap — values a user can't reasonably set by hand. This
module is the single source of truth that derives them from the model's limits
(``bedrock.model_utils``) minus a configurable **context buffer**, so the
pipeline auto-sizes and the user only ever sets one intuitive knob
(``extraction.context_buffer``, default 0.30).

Every derivation is returned as a structured ``SizingPlan`` AND logged (INFO) so
the exact numbers — and the reasoning behind them — are traceable in CloudWatch
and surfaced in the processing report.

Two independent budgets are derived:

- **Input (shard) budget** — how much OCR text + images one agent/shard may hold.
  ``usable_input = max_input_tokens × (1 - context_buffer)``; from that we
  subtract an output reserve (the model may emit up to its output cap) and an
  image reserve (page images cost ~1600 tokens each), and what remains is the
  per-shard OCR-text budget. Pages are then grouped to fit.
- **Output (list-batch) budget** — how many list rows one confidence call may
  score. ``usable_output = max_output_tokens × (1 - context_buffer)``; divided
  by an estimated per-row confidence-output cost (bigger for bbox geometry).

Pure/importable (no boto3/PIL); safe to call from any path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Approx Claude vision tokens per attached page image at typical resolution.
# Deliberately generous so the input reserve does not under-count images (the
# failure mode that overflows the window). Anthropic's own estimate is
# ~(w*h)/750; a full-page image lands around this figure.
_TOKENS_PER_IMAGE = 1600

# Per-row confidence OUTPUT token estimate (mirrors assessment.batching): a
# confidence leaf per column + a short reason. Larger under bbox geometry.
_CONF_ROW_TOKENS = 40
_CONF_ROW_TOKENS_BBOX = 120

# Fallbacks when the model is unknown (limits lookup fails). Conservative so an
# unknown model still shards rather than trying a giant single call.
_FALLBACK_INPUT_TOKENS = 180_000
_FALLBACK_OUTPUT_TOKENS = 8_000

# Ceiling on the output reserve, as a fraction of the USABLE INPUT window.
#
# The input budget reserves room for the model's own response by subtracting the
# full usable output window. That is only sensible while a model's output cap is
# small relative to its context window — which held for every Claude/Nova/GPT
# family. xAI Grok 4.6 breaks it: a 524,288-token output cap against a 500,000
# input window means the naive reserve (367,001) EXCEEDS the usable input
# (350,000), driving the shard budget negative and clamping it to
# _MIN_SHARD_TOKEN_BUDGET — so the model with the largest context window would
# shard into 2,000-token pieces.
#
# 0.65 is deliberate, and the constraint is TWO-SIDED — read both directions
# before changing it:
#
#   * LOWERING it re-shards existing models. The binding case is the 200K/128K
#     Claude families: their usable output is 89,600, which is exactly 64.00% of
#     their 140,000 usable input, so any fraction >= 0.64 leaves them untouched
#     while 0.63 raises their shard budget to 19,800 and 0.50 to 38,000.
#     0.64 is the true boundary; 0.65 sits just above it.
#   * RAISING it starves the model this exists for. A bigger cap means less
#     clamping, so Grok's shard budget falls: 90,500 at 0.65, 73,001 at 0.70,
#     3,000 at 0.90, and back to the floor at 1.00 (which is the unclamped
#     behavior).
#
# So 0.65 maximizes the new model's shard budget subject to disturbing no
# existing model. Note the no-op is buffer-INDEPENDENT: the comparison reduces to
# max_output/max_input, so it holds at every ``context_buffer``, not just the
# 0.30 default. test_reserve_clamp_does_not_change_existing_models pins every
# pre-existing row across a range of buffers.
_MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT = 0.65

# Floors/ceilings so derived values stay sane regardless of arithmetic.
_MIN_SHARD_TOKEN_BUDGET = 2_000
_MIN_LIST_BATCH = 1
# Cap list-batch well below the raw output-token math: a very large batch (e.g.
# 500 rows) is a RELIABILITY risk (the model under-enumerates long lists) and a
# LATENCY risk (one slow call), independent of whether the tokens fit. The
# self-healing ladder can always shrink further; starting moderate is safer.
_ABS_MAX_LIST_BATCH = 50


@dataclass
class SizingPlan:
    """The derived, model-aware sizing decisions for one document/section.

    All token figures are estimates (chars/4 + image heuristic). ``notes`` holds
    a human-readable trace of the calculation for the processing report.
    """

    model_id: str
    context_buffer: float
    max_input_tokens: int
    max_output_tokens: int
    # Input side
    image_reserve_tokens: int
    output_reserve_tokens: int
    shard_token_budget: int
    max_pages_per_shard: int
    # Output side
    list_batch_size: int
    geometry_mode: str | None = None
    # Whether each value was auto-derived (True) or came from an explicit override.
    overrides: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "context_buffer": self.context_buffer,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "image_reserve_tokens": self.image_reserve_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "shard_token_budget": self.shard_token_budget,
            "max_pages_per_shard": self.max_pages_per_shard,
            "list_batch_size": self.list_batch_size,
            "geometry_mode": self.geometry_mode,
            "overrides": self.overrides,
        }


def _resolve_limits(model_id: str | None) -> tuple[int, int, bool]:
    """Return (max_input_tokens, max_output_tokens, resolved_ok)."""
    if not model_id:
        return _FALLBACK_INPUT_TOKENS, _FALLBACK_OUTPUT_TOKENS, False
    from idp_common.bedrock.model_utils import (
        get_model_max_input_tokens,
        get_model_max_output_tokens,
    )

    try:
        max_in = get_model_max_input_tokens(model_id)
    except Exception:  # noqa: BLE001 - unknown model → conservative fallback
        max_in = _FALLBACK_INPUT_TOKENS
        resolved_in = False
    else:
        resolved_in = True
    try:
        max_out = get_model_max_output_tokens(model_id)
    except Exception:  # noqa: BLE001
        max_out = _FALLBACK_OUTPUT_TOKENS
        resolved_out = False
    else:
        resolved_out = True
    return max_in, max_out, (resolved_in and resolved_out)


def compute_sizing_plan(
    *,
    model_id: str | None,
    context_buffer: float = 0.30,
    geometry_mode: str | None = None,
    max_images_per_agent: int = 20,
    default_max_pages_per_shard: int = 5,
    # Explicit overrides (None = auto-derive). Kept so power users / tests can pin.
    shard_token_budget_override: int | None = None,
    max_pages_per_shard_override: int | None = None,
    list_batch_size_override: int | None = None,
    log_label: str = "extraction",
) -> SizingPlan:
    """Derive shard + list-batch sizing from the model's context/output windows.

    ``context_buffer`` (0.0-0.95) is the fraction of each window kept free as
    safety headroom (default 0.30 — never use more than 70% of a window). All
    outputs are floored to sane minimums. Explicit ``*_override`` args short-
    circuit the corresponding derivation (auto-sizing only fills the gaps).
    """
    buffer = min(max(float(context_buffer), 0.0), 0.95)
    max_in, max_out, resolved = _resolve_limits(model_id)

    usable_input = int(max_in * (1.0 - buffer))
    usable_output = int(max_out * (1.0 - buffer))

    # --- Input (shard) budget ---
    # Reserve room for the model's own output and for page images, then the rest
    # is the per-shard OCR-text budget. The agent may emit up to a full response,
    # but the reserve is capped so a model whose output cap rivals its context
    # window cannot starve its own shard budget (see the constant's comment).
    output_reserve = min(
        usable_output, int(usable_input * _MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT)
    )
    image_reserve = int(max_images_per_agent) * _TOKENS_PER_IMAGE
    derived_shard_budget = max(
        _MIN_SHARD_TOKEN_BUDGET, usable_input - output_reserve - image_reserve
    )
    shard_token_budget = (
        int(shard_token_budget_override)
        if shard_token_budget_override
        else derived_shard_budget
    )
    max_pages_per_shard = (
        int(max_pages_per_shard_override)
        if max_pages_per_shard_override is not None
        else default_max_pages_per_shard
    )

    # --- Output (list-batch) budget ---
    per_row = (
        _CONF_ROW_TOKENS_BBOX
        if (geometry_mode or "").lower() in ("llm", "llm_grounded")
        else _CONF_ROW_TOKENS
    )
    derived_list_batch = max(
        _MIN_LIST_BATCH, min(_ABS_MAX_LIST_BATCH, usable_output // per_row)
    )
    list_batch_size = (
        int(list_batch_size_override)
        if list_batch_size_override
        else derived_list_batch
    )

    overrides = {
        k: v
        for k, v in {
            "shard_token_budget": shard_token_budget_override,
            "max_pages_per_shard": max_pages_per_shard_override,
            "list_batch_size": list_batch_size_override,
        }.items()
        if v is not None
    }

    plan = SizingPlan(
        model_id=model_id or "(unknown)",
        context_buffer=buffer,
        max_input_tokens=max_in,
        max_output_tokens=max_out,
        image_reserve_tokens=image_reserve,
        output_reserve_tokens=output_reserve,
        shard_token_budget=shard_token_budget,
        max_pages_per_shard=max_pages_per_shard,
        list_batch_size=list_batch_size,
        geometry_mode=geometry_mode,
        overrides=overrides,
    )

    # Full, traceable calculation log (this is the "trace how the doc is sized"
    # requirement — every number and its derivation is here).
    logger.info(
        "Model-aware sizing (%s): model=%s resolved=%s buffer=%.2f "
        "input_window=%d output_cap=%d | usable_in=%d usable_out=%d "
        "image_reserve=%d(%dimg) output_reserve=%d -> shard_token_budget=%d "
        "max_pages_per_shard=%d | per_row_out=%d(geometry=%s) -> "
        "list_batch_size=%d | overrides=%s",
        log_label,
        plan.model_id,
        resolved,
        buffer,
        max_in,
        max_out,
        usable_input,
        usable_output,
        image_reserve,
        max_images_per_agent,
        output_reserve,
        shard_token_budget,
        max_pages_per_shard,
        per_row,
        geometry_mode,
        list_batch_size,
        overrides or "none",
    )
    return plan
