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
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Approx Claude vision tokens per attached page image at typical resolution.
# Deliberately generous so the input reserve does not under-count images (the
# failure mode that overflows the window). Anthropic's own estimate is
# ~(w*h)/750; a full-page image lands around this figure.
_TOKENS_PER_IMAGE = 1600

# --- Per-row confidence OUTPUT token estimate -------------------------------
# These used to be duplicated: this module carried a flat per-ROW figure (40, or
# 120 under bbox geometry) while assessment.batching carried a per-CELL figure
# and multiplied by the row's column count. The two disagreed by a factor of the
# column count, because the flat 120 IS the per-cell formula evaluated at exactly
# one column (1 x 40 x 3.0) frozen as if it were universal. This module now owns
# the single estimator (``confidence_rows_per_call``) and assessment.batching
# imports it, so a wide table can never be sized as if it were one column wide.
#
# Fraction of the model's output cap a single assessment call may target. A batch
# sized to the FULL cap truncates on any per-row estimate error, so leave
# headroom for the scalar/group assessments that ride every call and for the
# model being chattier than a chars/4 estimate. 0.5 (was 0.7) because confidence
# output is variable — the model over-generates on a heavy multimodal prompt — and
# sitting on the truncation edge makes the first call rely on the adaptive
# splitter to bisect, which doubles the sequential call count and, under slow
# Bedrock, ran shard Lambdas into their 900s wall.
_OUTPUT_SAFETY_FRACTION = 0.5
# Confidence output per row is driven by the row's COLUMN COUNT, not by the length
# of the extracted values: each scalar column emits a fixed-ish leaf like
# ``"ColumnName": {"confidence": 0.97},`` (column-name tokens + envelope), plus an
# occasional short ``confidence_reason`` on low-confidence cells. Empirically
# (Nova Lite, live-measured on a 6-column financial row) a clean per-cell cost is
# ~28 output tokens; budget ~40 for the column name, the reason allowance on
# sub-0.9 cells, and model chattiness.
_PER_CELL_CONFIDENCE_TOKENS = 40.0
# ``geometry.mode`` in {"llm", "llm_grounded"} appends a per-cell bounding-box
# instruction, so each leaf also emits ``bbox``/``page`` coordinates — measured at
# ~3x the output. Applied ON TOP of the per-cell cost.
_BBOX_GEOMETRY_MULTIPLIER = 3.0
# Column count assumed when the caller cannot supply one (``compute_sizing_plan``
# runs per section BEFORE extraction, so no rows exist yet). Six is the width of a
# typical transaction table and is deliberately not 1: under-estimating the width
# is what produced a permissive batch size. The AUTHORITATIVE size is recomputed
# from the real rows at assessment time by ``compute_token_aware_batch_size``.
_ASSUMED_LIST_COLUMNS = 6
# Output cap assumed when the model is unknown or its limits lookup fails. Small
# on purpose: an unknown model must batch conservatively rather than optimistically.
_FALLBACK_CONFIDENCE_OUTPUT_CAP = 8_000

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
# Per-model-family ceilings on rows per confidence call, applied UNDER the token
# math and under the operator's ``list_batch_size``. These are not token limits:
# they are where a model's greedy decoding was MEASURED to degenerate on the
# repetitive output a confidence batch asks for. Nova Lite (v1) at temperature 0,
# asked to score 25 near-identical rows, emitted the same
# ``{"Date": {"confidence": 1.0}, ...}`` object 189 times until it hit its
# 10,000-token cap — on 4/4 offline replays and on every live small_narrow run
# (each costing ~60 s and 10,000 output tokens before the adaptive splitter
# recovered the rows at 12). The same inputs at 13 rows: 0/4 offline, 1/5 live;
# at 8 rows: 0/8. The fit-by-tokens answer for that row shape is 41, so no token
# constant expresses this. Measured on nova-lite; nova-micro is the smaller
# sibling and gets the same ceiling; Nova Pro/Premier/Nova 2 are unmeasured and
# deliberately NOT listed (an unmeasured cap would be a guess in either direction).
_MODEL_LIST_BATCH_CEILINGS: tuple[tuple[str, int], ...] = (
    (r"amazon\.nova-(lite|micro)", 12),
)
# Floor and per-call overhead for the OUTPUT budget a confidence call requests
# (``confidence_output_budget``). A degenerate response — the repetition loop
# above — otherwise runs to the model's full cap; requesting only what a correct
# answer needs turns a 10,000-token / 60 s failure into a ~2,000-token / 8 s one,
# and the adaptive splitter recovers either way. The overhead covers the scalar
# and group leaves, the JSON envelope and a code fence; the floor keeps a
# scalar-only document from ever being budgeted below what a verbose answer needs.
_CONFIDENCE_OUTPUT_OVERHEAD_TOKENS = 1_500
_MIN_CONFIDENCE_OUTPUT_BUDGET = 2_000


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


def confidence_per_row_tokens(
    num_columns: int | None, geometry_mode: str | None
) -> float:
    """Estimated confidence OUTPUT tokens for one list row.

    ``num_columns`` is the row's scalar column count; ``None``/non-positive falls
    back to :data:`_ASSUMED_LIST_COLUMNS`. Bounding-box geometry modes multiply
    the per-cell cost because each leaf also emits coordinates.
    """
    cols = num_columns if (num_columns and num_columns > 0) else _ASSUMED_LIST_COLUMNS
    per_row = cols * _PER_CELL_CONFIDENCE_TOKENS
    if (geometry_mode or "").lower() in ("llm", "llm_grounded"):
        per_row *= _BBOX_GEOMETRY_MULTIPLIER
    return per_row


def confidence_rows_for_per_row_tokens(
    output_cap: int | None,
    per_row_tokens: float,
    ceiling: int | None = None,
) -> int:
    """Rows that fit one confidence call, given a per-row output-token estimate.

    ``floor(output_cap x _OUTPUT_SAFETY_FRACTION / per_row_tokens)``, clamped to
    ``[_MIN_LIST_BATCH, ceiling]``. ``ceiling`` defaults to
    :data:`_ABS_MAX_LIST_BATCH`; a caller with a user-configured ceiling passes the
    smaller of the two. An unknown ``output_cap`` uses
    :data:`_FALLBACK_CONFIDENCE_OUTPUT_CAP` rather than trusting a configured value.
    """
    cap = (
        int(output_cap)
        if (output_cap and output_cap > 0)
        else _FALLBACK_CONFIDENCE_OUTPUT_CAP
    )
    # _ABS_MAX_LIST_BATCH applies ONLY when auto-deriving (no explicit ceiling). An
    # explicit ceiling is a deliberate user choice and is honoured in full: clamping
    # it here would silently turn a pinned 75 into 50, which earlier releases did
    # not do on the path that decides the real batch. The reliability cap therefore
    # bounds what the SYSTEM picks, never what the operator asked for.
    if ceiling is not None:
        limit = max(_MIN_LIST_BATCH, ceiling)
    else:
        limit = _ABS_MAX_LIST_BATCH
    if per_row_tokens <= 0:
        return limit
    derived = int(cap * _OUTPUT_SAFETY_FRACTION // per_row_tokens)
    return max(_MIN_LIST_BATCH, min(limit, derived))


def model_list_batch_ceiling(model_id: str | None) -> int | None:
    """The measured per-family ceiling on rows per confidence call, or None.

    See :data:`_MODEL_LIST_BATCH_CEILINGS` for why this exists and how it was
    measured; it bounds what the SYSTEM derives and also what the operator's
    ``list_batch_size`` allows, because above it the model does not truncate on
    tokens — it loops until the cap, whatever the cap is.
    """
    if not model_id:
        return None
    lowered = model_id.lower()
    for pattern, ceiling in _MODEL_LIST_BATCH_CEILINGS:
        if re.search(pattern, lowered):
            return ceiling
    return None


def confidence_rows_per_call(
    output_cap: int | None,
    num_columns: int | None,
    geometry_mode: str | None,
    ceiling: int | None = None,
    model_id: str | None = None,
) -> int:
    """Rows one confidence call can score without truncating.

    There is no single correct constant here — the answer is a function of the
    model's output cap, the geometry mode and the column count. On a 10,000-cap
    model with bounding boxes it is 41 rows for a 1-column list, 13 for 3 columns
    and 5 for 8; on Sonnet 5 (128,000) the reliability ceiling binds first. When
    ``model_id`` names a family with a measured degeneration ceiling
    (:func:`model_list_batch_ceiling`) the result never exceeds it — so Nova Lite
    itself gives 12, 12 and 5 for those three shapes.
    """
    rows = confidence_rows_for_per_row_tokens(
        output_cap, confidence_per_row_tokens(num_columns, geometry_mode), ceiling
    )
    family = model_list_batch_ceiling(model_id)
    if family is not None:
        rows = max(_MIN_LIST_BATCH, min(rows, family))
    return rows


def confidence_output_budget(
    extraction_results: Any,
    geometry_mode: str | None,
    output_cap: int | None,
) -> int:
    """maxTokens to request for ONE confidence call over ``extraction_results``.

    A correct answer emits one leaf per scalar and one leaf per cell of every
    list row (:func:`confidence_per_row_tokens` per row, which is already ~2x the
    measured clean cost), plus :data:`_CONFIDENCE_OUTPUT_OVERHEAD_TOKENS`. The
    budget is that sum, never below :data:`_MIN_CONFIDENCE_OUTPUT_BUDGET` and
    never above the model's cap. Requesting only this — instead of the model's
    full cap — bounds the cost of a degenerate response (see
    :data:`_MODEL_LIST_BATCH_CEILINGS`): a loop is cut off after the budget and
    the batcher's truncation path recovers the rows exactly as before.

    Rows are counted at every depth (a list of instances carrying their own
    lists is scored row by row too); a list whose items are scalars counts one
    leaf per item.
    """
    per_cell = confidence_per_row_tokens(1, geometry_mode)
    leaves = 0.0

    def _walk(node: Any) -> None:
        nonlocal leaves
        if isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    _walk(item)
                else:
                    leaves += 1
        else:
            leaves += 1

    _walk(extraction_results)
    budget = int(leaves * per_cell) + _CONFIDENCE_OUTPUT_OVERHEAD_TOKENS
    budget = max(_MIN_CONFIDENCE_OUTPUT_BUDGET, budget)
    cap = int(output_cap) if (output_cap and output_cap > 0) else None
    if cap is not None:
        budget = min(budget, cap)
    return budget


def compute_sizing_plan(
    *,
    model_id: str | None,
    context_buffer: float = 0.30,
    geometry_mode: str | None = None,
    max_images_per_agent: int = 20,
    default_max_pages_per_shard: int = 5,
    # The model that runs the CONFIDENCE pass. Defaults to ``model_id`` only for
    # back-compat; callers should pass ``extraction.confidence.model``, because the
    # list-batch figure is meaningless when derived from the extraction model.
    confidence_model_id: str | None = None,
    # Scalar column count of the list being scored, when known. ``compute_sizing_plan``
    # runs before extraction, so this is normally None and a conservative width is
    # assumed; the authoritative value is recomputed from real rows at assessment time.
    list_columns: int | None = None,
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
    # This is a CONFIDENCE-pass figure, so it must be derived from the confidence
    # model's output cap, not the extraction model's. It previously used
    # ``usable_output`` (the extraction model), which on a Sonnet-5-extracts /
    # Nova-Lite-scores setup computed 128,000 x 0.7 / 120 = 746, clamped to 50, and
    # reported "50 rows" while the assessment path actually used 13. The value here
    # is an UPPER-BOUND estimate for the processing report: the authoritative size
    # is recomputed per field from the real rows by
    # ``assessment.batching.compute_token_aware_batch_size``.
    conf_model = confidence_model_id or model_id
    _, conf_max_out, conf_resolved = _resolve_limits(conf_model)
    per_row = confidence_per_row_tokens(list_columns, geometry_mode)
    derived_list_batch = confidence_rows_per_call(
        conf_max_out if conf_resolved else None,
        list_columns,
        geometry_mode,
        model_id=conf_model,
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
        "max_pages_per_shard=%d | confidence_model=%s(cap=%d resolved=%s) "
        "cols=%s per_row_out=%d(geometry=%s) -> list_batch_size=%d(estimate) "
        "| overrides=%s",
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
        conf_model or "(unknown)",
        conf_max_out,
        conf_resolved,
        list_columns if list_columns else f"assumed {_ASSUMED_LIST_COLUMNS}",
        per_row,
        geometry_mode,
        list_batch_size,
        overrides or "none",
    )
    return plan
