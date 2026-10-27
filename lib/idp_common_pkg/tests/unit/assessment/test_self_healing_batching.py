# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Self-healing assessment: token-aware batch sizing (1.1) + escalation ladder (1.2).

These lock in the two backend fixes for the investigated failure where advanced
mode left 34/68 transaction rows with ``confidence: null``: a Nova-Lite-cap
confidence model truncated a 25-row + per-cell-bbox batch, and the same-model
adaptive splitter recovered 0 rows.

- **1.1** ``compute_token_aware_batch_size`` shrinks the FIRST-pass batch to fit
  the model's output cap, so a small-cap model batches small from the start.
- **1.2** the escalation ladder re-assesses still-missing rows on a stronger
  model (bigger output cap) — the rung that actually recovers the rows a
  small-cap model keeps truncating.
"""

from __future__ import annotations

import threading

import pytest

from idp_common.assessment import batching as mod
from idp_common.assessment.batching import (
    assess_results_batched,
    compute_token_aware_batch_size,
)
from idp_common.assessment.service import AssessmentCoreResult

NOVA_LITE = "us.amazon.nova-lite-v1:0"
NOVA_PRO = "us.amazon.nova-pro-v1:0"  # 10K output cap
CLAUDE_SONNET = "us.anthropic.claude-sonnet-4-20250514-v1:0"  # 64K output cap
CLAUDE3_HAIKU = "us.anthropic.claude-3-haiku-20240307-v1:0"  # 8,192 output cap


def _rows(n: int) -> list[dict]:
    return [{"date": f"2020-01-{i:02d}", "amount": f"{i}.00"} for i in range(1, n + 1)]


# --------------------------------------------------------------------------- #
# 1.1 compute_token_aware_batch_size
# --------------------------------------------------------------------------- #
def test_token_aware_small_cap_with_bbox_shrinks_batch():
    """Nova Lite (10K cap) + llm_grounded (per-cell bbox ~3× output) → a batch
    much smaller than the configured 25, so the first pass does not truncate."""
    sample = {"date": "2020-01-01", "amount": "1000.00", "description": "ACME CORP"}
    derived = compute_token_aware_batch_size(
        NOVA_LITE, sample, "llm_grounded", configured_batch_size=25
    )
    assert 1 <= derived < 25


def test_token_aware_large_output_model_keeps_configured():
    """A big-output model (Claude Sonnet, 64K) fits the full configured batch —
    token-aware sizing only ever shrinks, never grows past the ceiling."""
    sample = {"date": "2020-01-01", "amount": "1000.00"}
    derived = compute_token_aware_batch_size(
        CLAUDE_SONNET, sample, "ocr_only", configured_batch_size=25
    )
    assert derived == 25


def test_token_aware_bbox_shrinks_more_than_ocr_only():
    """The per-cell bbox block makes each row's output larger, so llm_grounded
    yields a smaller (or equal) batch than ocr_only for the same model."""
    sample = {"date": "2020-01-01", "amount": "1000.00", "description": "ACME CORP"}
    with_bbox = compute_token_aware_batch_size(NOVA_LITE, sample, "llm_grounded", 25)
    without_bbox = compute_token_aware_batch_size(NOVA_LITE, sample, "ocr_only", 25)
    assert with_bbox <= without_bbox


def test_token_aware_unknown_model_sizes_conservatively():
    """An unknown model (no entry in model_config_limits) must NOT keep the
    configured size — trusting a permissive configured value on a model whose cap
    is unknown is how a 25-row batch reached a 10,000-token model. It sizes from a
    conservative fallback cap instead, and never exceeds the configured ceiling."""
    sample = {"date": "2020-01-01", "amount": "1.00"}
    derived = compute_token_aware_batch_size(
        "some.unknown.model-v9:0", sample, "ocr_only", configured_batch_size=25
    )
    assert 1 <= derived <= 25
    # With bbox geometry the same unknown model must size down further still.
    bbox = compute_token_aware_batch_size(
        "some.unknown.model-v9:0", sample, "llm_grounded", configured_batch_size=25
    )
    assert bbox < derived


def test_token_aware_never_returns_zero():
    """Even a huge per-row payload clamps to at least 1 row per batch."""
    huge_row = {"blob": "x" * 500_000}
    derived = compute_token_aware_batch_size(NOVA_LITE, huge_row, "llm", 25)
    assert derived >= 1


def test_token_aware_no_model_sizes_conservatively():
    """No model id → cannot resolve a cap → size conservatively, do NOT fall back to
    the configured size (see test_token_aware_unknown_model_sizes_conservatively)."""
    assert 1 <= compute_token_aware_batch_size(None, {"a": 1}, "ocr_only", 25) <= 25


def test_token_aware_sizes_by_column_count_not_value_length():
    """B2: per-row cost is driven by COLUMN COUNT, not value length. A wide row of
    SHORT values must shrink the batch (the Truist regression: 6 short columns
    sat at batch=25 and truncated). A narrow row of long values must NOT shrink
    more than the wide one — proving the estimate no longer keys off value length."""
    wide_short = {  # 6 columns, all short values
        "Symbol": "DQY",
        "Description": "DOY CORPORATION",
        "Shares": 936.0,
        "Price": 175.7,
        "MarketValue": 164455.2,
        "GainLoss": -731.41,
    }
    narrow_long = {  # 2 columns, long values
        "a": "x" * 200,
        "b": "y" * 200,
    }
    # Nova PRO: the same 10K cap as Nova Lite but no measured loop ceiling, so the
    # token math is what is asserted here (Nova Lite would clamp both to 12).
    wide = compute_token_aware_batch_size(NOVA_PRO, wide_short, "ocr_only", 25)
    narrow = compute_token_aware_batch_size(NOVA_PRO, narrow_long, "ocr_only", 25)
    # 10K cap, 0.5 fraction, ~40 tok/cell: 6 cols → floor(5000/240)=20.
    assert wide == 20
    # The 2-column row is NOT shrunk more than the 6-column row, even though its
    # values are far longer — value length no longer drives the estimate.
    assert narrow >= wide
    # Nested/list sub-fields ARE counted, leaf by leaf: the model scores a nested
    # group and each item of a nested list. 6 + 1 + 3 = 10 leaves -> 5000/400 = 12.
    with_nested = {**wide_short, "extra": {"nested": "obj"}, "items": [1, 2, 3]}
    assert compute_token_aware_batch_size(NOVA_PRO, with_nested, "ocr_only", 25) == 12


def _scalar_heavy_section(n_rows: int) -> tuple[dict, dict]:
    """A section whose scalars, which ride every slice unsliced, carry more output
    than the half of a 10,000-token cap the safety fraction leaves free once each
    leaf also emits a bounding box: 60 x 40 x 3 = 7,200 tokens."""
    scalars = {f"Field{i}": f"value {i}" for i in range(60)}
    return {"transactions": _rows(n_rows), **scalars}, scalars


class _RowCountingService:
    """Scores every row cleanly and records how many rows each call carried."""

    def __init__(self, list_field: str):
        self.list_field = list_field
        self.calls: list[int] = []

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        self.calls.append(len(rows))
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: [{"amount": {"confidence": 0.9}} for _ in rows],
            },
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 5}},
        )


def test_batched_path_reserves_the_output_that_rides_every_slice():
    """The primary pass must size its batch against what is left of the cap after
    the scalars that every slice repeats — not against the full safety fraction."""
    data, scalars = _scalar_heavy_section(60)
    rows = data["transactions"]
    reserved = compute_token_aware_batch_size(
        NOVA_PRO, rows, "llm_grounded", 25, fixed_results=scalars
    )
    unreserved = compute_token_aware_batch_size(NOVA_PRO, rows, "llm_grounded", 25)
    # 2 leaves x 120 per row: (10,000 - 7,200 - 1,500) // 240 = 5, against
    # 5,000 // 240 = 20 with nothing reserved.
    assert (reserved, unreserved) == (5, 20)

    svc = _RowCountingService("transactions")
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        confidence_model_id=NOVA_PRO,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
    )
    assert result["split_stats"]["derived_batch_size"] == reserved
    assert svc.calls and max(svc.calls) == reserved


def test_escalation_rung_reserves_the_output_that_rides_every_slice():
    """The escalation batch is sized the same way: the scalars ride the stronger
    model's calls too, and they are sized for the ESCALATION model's cap, not the
    primary's. Nova Pro as the escalation model keeps the cap small enough for the
    reserve to bind; Claude 3 Haiku as the primary has a smaller cap that, with the
    same reserve, leaves room for only one row, so sizing the escalation batch off
    the primary model would show up as one-row escalation calls."""
    data, scalars = _scalar_heavy_section(12)
    svc = LadderService("transactions", NOVA_PRO, primary_max_rows=0)
    assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        confidence_model_id=CLAUDE3_HAIKU,
        geometry_mode="llm_grounded",
        escalation_enabled=True,
        escalation_model=NOVA_PRO,
        max_escalation_rounds=1,
    )
    expected = compute_token_aware_batch_size(
        NOVA_PRO, data["transactions"], "llm_grounded", 25, fixed_results=scalars
    )
    assert expected == 5
    assert (
        compute_token_aware_batch_size(
            CLAUDE3_HAIKU,
            data["transactions"],
            "llm_grounded",
            25,
            fixed_results=scalars,
        )
        == 1
    )
    assert svc.escalation_calls and max(svc.escalation_calls) == expected


def test_the_longest_list_is_sliced_and_every_other_list_is_reserved():
    """With two lists the longest is the one sliced, so it is the other one — not
    the longest — that rides every call and is reserved. Reserving the wrong one
    would size a 200-row list's batch as if 200 rows rode with every slice."""
    data = {
        "fees": [{"label": f"fee {i}", "amount": "1.00"} for i in range(10)],
        "transactions": _rows(200),
    }
    svc = _RowCountingService("transactions")
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        confidence_model_id=NOVA_PRO,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
    )
    expected = compute_token_aware_batch_size(
        NOVA_PRO,
        data["transactions"],
        "llm_grounded",
        25,
        fixed_results={"fees": data["fees"]},
    )
    # 20 fee leaves x 120 = 2,400 reserved: min(5,000, 10,000 - 2,400 - 1,500)
    # // 240 = 20.
    assert expected == 20
    assert result["split_stats"]["derived_batch_size"] == expected


# --------------------------------------------------------------------------- #
# 1.2 Escalation ladder
# --------------------------------------------------------------------------- #
class TruncatesUnlessEscalated(AssessmentCoreResult):
    pass


class LadderService:
    """Confidence model stand-in whose behavior depends on the model id.

    - The PRIMARY (small-cap) model truncates any call carrying more than
      ``primary_max_rows`` rows AND, crucially, keeps truncating even down to 1
      row (mimicking the investigated case where shrinking alone recovered 0).
    - The ESCALATION model scores every row it is handed cleanly.

    Records calls per model so tests can assert which rung recovered the rows.
    """

    def __init__(self, list_field: str, escalation_model: str, primary_max_rows: int):
        self.list_field = list_field
        self.escalation_model = escalation_model
        self.primary_max_rows = primary_max_rows
        self.primary_calls: list[int] = []
        self.escalation_calls: list[int] = []

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        model = kw.get("model_id_override")
        if model == self.escalation_model:
            self.escalation_calls.append(len(rows))
            return AssessmentCoreResult(
                enhanced_assessment={
                    self.list_field: [{"amount": {"confidence": 0.92}} for _ in rows],
                    "account_holder": {"confidence": 0.95},
                },
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=1.0,
                metering={"Assessment/bedrock/model": {"outputTokens": 50}},
            )
        # Primary (small-cap) model: truncate above the cap, and never score
        # even a small slice cleanly — shrinking alone cannot win.
        self.primary_calls.append(len(rows))
        return AssessmentCoreResult(
            enhanced_assessment={
                k: {"confidence": 0.5, "confidence_reason": "default"}
                for k in kw["extraction_results"]
            },
            parsing_succeeded=False,
            truncated=True,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 10000}},
        )

    # The ladder resolves the escalation model via this method on the service.
    def _resolve_confidence_escalation_model(self, class_label: str):
        return self.escalation_model


def test_escalation_recovers_rows_the_primary_model_cannot():
    """Primary model truncates every slice (shrinking recovers nothing); the
    ladder escalates to the stronger model and recovers ALL rows, recording the
    model chain in split_stats."""
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(8), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=4,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
    )

    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 8
    # Every row scored by the escalation model — no null/default survivors.
    for row in assessed:
        assert row["amount"]["confidence"] == 0.92

    stats = result["split_stats"]
    assert result["split_stats"]["unrecoverable_rows"] == 0
    assert stats["escalation_model"] == CLAUDE_SONNET
    assert stats["escalation_rounds"] >= 1
    assert stats["rows_recovered_by_escalation"] == 8
    assert svc.escalation_calls  # escalation model was actually invoked


def test_escalation_disabled_leaves_rows_unrecoverable():
    """With escalation off, the primary model's truncation cannot be healed —
    rows stay unrecoverable (the pre-fix behavior), proving the ladder is what
    recovers them."""
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(6)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=3,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
    )

    assert not svc.escalation_calls  # never escalated
    assert result["split_stats"]["unrecoverable_rows"] == 6
    assert result["split_stats"]["rows_recovered_by_escalation"] == 0


def test_escalation_bounded_by_max_rounds():
    """When even the escalation model cannot score the rows, the ladder stops
    after at most ``max_escalation_rounds`` (a round with no progress stops
    early) — no infinite loop."""

    class NeverScores(LadderService):
        def assess_results(self, **kw):
            rows = kw["extraction_results"].get(self.list_field, [])
            model = kw.get("model_id_override")
            if model == self.escalation_model:
                self.escalation_calls.append(len(rows))
            else:
                self.primary_calls.append(len(rows))
            # Both models truncate — nothing is ever recovered.
            return AssessmentCoreResult(
                enhanced_assessment={
                    k: {"confidence": 0.5, "confidence_reason": "default"}
                    for k in kw["extraction_results"]
                },
                parsing_succeeded=False,
                truncated=True,
                duration_seconds=1.0,
                metering={},
            )

    svc = NeverScores("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(4)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=2,
        confidence_model_id=NOVA_LITE,
        geometry_mode="ocr_only",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
    )

    # A no-progress escalation round stops the ladder early → exactly 1 round.
    assert result["split_stats"]["escalation_rounds"] == 1
    assert result["split_stats"]["unrecoverable_rows"] == 4


# --------------------------------------------------------------------------- #
# 1.5 Wall-clock deadline guard
# --------------------------------------------------------------------------- #
def test_deadline_stops_escalation_before_big_model_round():
    """A tiny remaining-time budget makes the ladder SKIP the slow escalation
    round, keep whatever was recovered, and flag deadline_reached instead of
    risking a Lambda timeout."""
    import time as _time

    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(6)}

    # Deadline is essentially now → remaining < safety reserve → escalation must
    # be skipped entirely.
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=3,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
        deadline_epoch=_time.time() + 1.0,  # 1s left → below the 90s reserve
    )

    assert not svc.escalation_calls  # escalation never fired
    assert result["split_stats"]["deadline_reached"] is True
    assert result["split_stats"]["escalation_rounds"] == 0
    # Rows stay unrecovered but the call returns cleanly (no raise).
    assert result["split_stats"]["unrecoverable_rows"] == 6


def test_deadline_stops_adaptive_split_recursion():
    """A near-now deadline stops the adaptive-split recursion (which doubles the
    number of sequential model calls on every truncation) instead of fanning a
    truncating batch into dozens of ~60s calls and running the Lambda into its
    900s wall. The call returns cleanly with deadline_reached set."""
    import time as _time

    # Primary model that ALWAYS truncates (even down to 1 row) and takes a
    # non-trivial per-call duration, with NO escalation model available — so the
    # only thing that can stop the storm is the wall-clock guard.
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(16)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=16,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,  # no escalation: only the split guard can stop it
        deadline_epoch=_time.time() + 1.0,  # 1s left → below the 90s reserve
    )

    # Guard tripped and the call returned without raising.
    assert result["split_stats"]["deadline_reached"] is True
    # The recursion did NOT fully bisect 16 → 1 (which would be many more calls);
    # it stopped early. Primary calls stay well under the ~31 a full 16→1 tree
    # would make.
    assert len(svc.primary_calls) < 10


class WarmThenTruncatingService:
    """Scores the first call cleanly and truncates every later one, on a clock.

    The shape a test of the concurrent fan-out needs. A clean FIRST call keeps the
    cache-warm batch from bisecting, so ``min_batch_size_used`` stays at the
    configured size and B3 does not re-chunk the fan-out down to single rows —
    single-row chunks cannot bisect, so a worker could never reach its own
    wall-clock guard. Every later call truncates with more than one row in it,
    which is exactly the condition that guard tests.

    Each call charges the driven clock ``_ESTIMATED_MODEL_CALL_SECONDS``, i.e. what
    the guards budget for a call, so a budget is spent by doing work rather than by
    wall-clock luck. ``duration_seconds`` is 0 so the guard prices a call at the
    floor rather than at a measured value.
    """

    def __init__(self, list_field: str, clock: dict):
        self.list_field = list_field
        self.clock = clock
        self.calls: list[int] = []
        # Called from several fan-out threads at once; see PartialProgressService.
        self._lock = threading.Lock()

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        with self._lock:
            first = not self.calls
            self.calls.append(len(rows))
            self.clock["now"] += mod._ESTIMATED_MODEL_CALL_SECONDS
        if first:
            return AssessmentCoreResult(
                enhanced_assessment={
                    self.list_field: [{"amount": {"confidence": 0.9}} for _ in rows],
                    "account_holder": {"confidence": 0.95},
                },
                parsing_succeeded=True,
                truncated=False,
                duration_seconds=0.0,
                metering={"Assessment/bedrock/model": {"outputTokens": 100}},
            )
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: [],
                "account_holder": {"confidence": 0.5},
            },
            parsing_succeeded=False,
            truncated=True,
            duration_seconds=0.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 10000}},
        )


def test_deadline_reached_propagates_from_concurrent_workers(monkeypatch):
    """A wall-clock cutoff hit inside a CONCURRENT fan-out worker must survive the
    post-join merge and still set deadline_reached (regression: the merge loop
    summed counters but dropped the deadline_reached bool, so
    assessment_deadline_reached never fired on the concurrent path).

    ⚠️ **The budget has to be spent by the work, not by the wall clock.** Every
    guard in this module measures `deadline_epoch - time.time()` against the same
    per-call estimate, and no real time passes in a run with a stand-in model — so
    a fixed near-now deadline refuses the cache-warm batch and nothing is ever
    dispatched concurrently, while a generous one refuses nothing anywhere. The
    driven clock below charges each call what the guards budget for it, which puts
    the cutoff inside the workers: the budget covers the warm batch and the single
    fan-out wave, and the wave's own calls then consume it, so each worker's
    bisection guard refuses to split.

    The assertions pin that the flag came through the **merge** and not from the
    fan-out's own guard: all six batches ran, so nothing was refused at wave level,
    and `splits == 0`, so the refusal happened where a worker was about to bisect.
    """
    clock = {"now": 6_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    # 60 rows at batch 10 is 6 batches: 1 to warm the cache and 5 fanned out
    # across 5 workers, i.e. one wave. Budget: the warm call plus that wave.
    deadline = (
        clock["now"]
        + (2 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )

    svc = WarmThenTruncatingService("transactions", clock)
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(60), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=10,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        max_concurrent_batches=5,  # force concurrent fan-out
        max_retries=0,  # no recovery rung, so only a worker can set the flag
        deadline_epoch=deadline,
    )

    # The fan-out must actually have run concurrently, or the merge this test is
    # about never happened.
    assert result["split_stats"]["concurrent_batches"] == 5
    # Every planned batch was dispatched: the fan-out's own wave guard refused
    # nothing, so it is not the thing that set the flag below.
    assert len(svc.calls) == 6
    # The workers were refused a BISECTION rather than performing one — that
    # refusal is the cutoff, and it happened inside a worker's own stats dict.
    assert result["split_stats"]["splits"] == 0
    assert result["split_stats"]["truncated_calls"] == 5
    # The flag set inside a worker must survive the merge.
    assert result["split_stats"]["deadline_reached"] is True


def test_no_deadline_allows_escalation():
    """With no deadline threaded in (local/non-Lambda), escalation runs normally
    and deadline_reached stays False."""
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(6)}
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=3,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
        deadline_epoch=None,
    )
    assert svc.escalation_calls  # escalation fired
    assert result["split_stats"]["deadline_reached"] is False
    assert result["split_stats"]["unrecoverable_rows"] == 0


class PartialProgressService:
    """Confidence model stand-in that makes PROGRESS on every call and never
    truncates — the one shape the wall-clock guard used to miss (#958).

    Each call scores the first ``scored_per_call`` rows of whatever slice it is
    handed and returns ``confidence: None`` for the rest, with
    ``truncated=False``. Nothing here trips the other two stopping conditions: the
    adaptive splitter never bisects (no truncation, so no recursion and no
    oversized-row flag), and every retry round recovers at least one row, so the
    "a round that recovered nothing stops the rung" break never fires either.
    Before #958 the only bound left was ``max_retries``, and each round was a real
    model call — which is how a series of partially-successful calls could run a
    900 s Lambda into its wall.
    """

    def __init__(
        self,
        list_field: str,
        scored_per_call: int = 1,
        clock: dict | None = None,
        seconds_per_call: float = 0.0,
    ):
        self.list_field = list_field
        self.scored_per_call = scored_per_call
        # Optional driven clock: a {"now": float} dict the test also patches
        # `mod.time.time` to read, so a call costs time deterministically.
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.primary_calls: list[int] = []
        # The concurrent fan-out calls this from several threads at once, where
        # `clock["now"] += x` is a read-modify-write and can lose an update. A lost
        # update advances the clock LESS than the work cost, which makes a deadline
        # test pass for the wrong reason, so the increment is serialized.
        self._lock = threading.Lock()

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        with self._lock:
            self.primary_calls.append(len(rows))
            if self.clock is not None and self.seconds_per_call:
                self.clock["now"] += self.seconds_per_call
        scored = [
            {"amount": {"confidence": 0.9 if i < self.scored_per_call else None}}
            for i in range(len(rows))
        ]
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: scored,
                "account_holder": {"confidence": 0.95},
            },
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 100}},
        )


def _partial_progress_run(deadline_epoch, *, max_retries=4, rows=12):
    svc = PartialProgressService("transactions")
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(rows), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=4,
        max_retries=max_retries,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,  # isolate rung 1: only its own guard can stop it
        deadline_epoch=deadline_epoch,
    )
    return svc, result


def test_deadline_stops_a_retry_round_that_is_still_making_progress(monkeypatch):
    """#958: the same-model retry rung consults the wall-clock deadline too.

    The stand-in recovers a row on every call and never truncates, so neither of
    the rung's pre-existing stopping conditions applies — before this change the
    rung ran all ``max_retries`` rounds however little time the Lambda had left.
    It must stop at the round boundary, flag ``deadline_reached`` and return
    cleanly.

    ⚠️ **The clock has to advance with the work for this to test anything.** Every
    deadline guard in this module measures `deadline_epoch - time.time()` against
    the same per-call estimate, and in a test with stand-in model calls no real
    time passes — so a fixed near-now deadline refuses the *first pass* as well
    (#1488 made that pass consult the deadline too, which is the whole point of
    it) and a generous one refuses nothing. Neither isolates the rung. The clock
    below reads `start + _ESTIMATED_MODEL_CALL_SECONDS × calls_made`, i.e. it
    charges each model call exactly what the guards budget for it, so the first
    pass legitimately consumes the budget and the retry round is refused for the
    reason it would be refused in a Lambda.
    """
    start = 1_000_000.0
    svc_holder: dict = {}

    def _clock():
        svc = svc_holder.get("svc")
        calls = len(svc.primary_calls) if svc is not None else 0
        return start + (mod._ESTIMATED_MODEL_CALL_SECONDS * calls)

    monkeypatch.setattr(mod.time, "time", _clock)

    # 3 first-pass batches cost 3 × 60 = 180s. Allow those plus the reserve, and
    # nothing more: the retry round's next call cannot fit.
    deadline = (
        start
        + (3 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + (mod._DEADLINE_SAFETY_RESERVE_SECONDS)
    )

    svc = PartialProgressService("transactions")
    svc_holder["svc"] = svc
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(12), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=4,
        max_retries=4,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,  # isolate rung 1: only its own guard can stop it
        deadline_epoch=deadline,
    )

    assert result["split_stats"]["deadline_reached"] is True
    # The whole first pass ran: 12 rows at batch 4 → 3 batches. No retry round.
    assert len(svc.primary_calls) == 3
    # Rows stay unscored and are REPORTED as such, rather than the call raising.
    assert result["split_stats"]["unrecoverable_rows"] > 0


def test_generous_deadline_still_runs_the_retry_rounds():
    """The guard must bound the rung, not disable it.

    Same stand-in and same round budget as the test above, with a deadline far
    enough out that every round fits: the retry rounds run, more calls are made
    than the first pass alone, and ``deadline_reached`` stays False. Without this
    assertion the guard above would also pass if rung 1 had simply been removed.
    """
    import time as _time

    svc, result = _partial_progress_run(_time.time() + 3600.0)

    assert result["split_stats"]["deadline_reached"] is False
    assert len(svc.primary_calls) > 3  # first pass (3) plus at least one retry round
    assert result["split_stats"]["rows_recovered_by_retry"] > 0


def test_no_deadline_leaves_the_retry_rung_running():
    """No deadline threaded in (local / non-Lambda) is a no-op for the new guard,
    exactly as it is for the escalation and bisection guards."""
    svc, result = _partial_progress_run(None)

    assert result["split_stats"]["deadline_reached"] is False
    assert len(svc.primary_calls) > 3


def test_a_large_list_with_ample_time_recovers_exactly_as_with_no_deadline():
    """The guard must bound the rung by TIME, not cap its chunk count.

    This is the case an earlier round-level version of the guard got wrong, and it
    is the shape the ladder exists for. A round-level check has to price the whole
    round before making any call, and the only safe price is a worst case
    (chunks × 60s); at 200 rows and the token-aware batch size of 4 that priced a
    round at 2,280s and refused it outright with 800s in hand — throwing away
    recovery that measurably took under a second, and reporting an error-severity
    ``assessment_incomplete`` where the unguarded code returned clean. Pricing ONE
    call at a time makes the guard a real time bound: with ample budget the outcome
    must be byte-identical to having no deadline at all.
    """
    import time as _time

    rows = 200
    guarded = _partial_progress_run(_time.time() + 800.0, rows=rows)
    unguarded = _partial_progress_run(None, rows=rows)

    assert guarded[1]["split_stats"]["deadline_reached"] is False
    for key in ("rows_recovered_by_retry", "unrecoverable_rows"):
        assert guarded[1]["split_stats"][key] == unguarded[1]["split_stats"][key], key
    assert len(guarded[0].primary_calls) == len(unguarded[0].primary_calls)


def test_a_mid_pass_stop_keeps_the_rows_it_already_recovered(monkeypatch):
    """Stopping is per chunk, so the rows already spliced back are kept.

    The clock is DRIVEN rather than slept on: ``_deadline_allows`` reads
    ``time.time`` from this module, so a fake counter that advances one call-time per
    model call makes the budget expire at an exact, reproducible chunk. An earlier
    version of this test slept instead, and failed 20 runs out of 20 under CPU load
    when executed alone — the initial pass consumed the whole budget before the retry
    rung ran.
    """
    monkeypatch.setattr(mod, "_ESTIMATED_MODEL_CALL_SECONDS", 1.0)
    monkeypatch.setattr(mod, "_DEADLINE_SAFETY_RESERVE_SECONDS", 0.0)

    clock = {"now": 1000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])

    # 40 rows at batch 4 → 10 chunks in the initial pass, then retry rounds. Advance
    # the fake clock one call-time per call, and allow 14 calls' worth of budget: the
    # initial pass spends 10, so the retry pass gets 4 chunks and is stopped on its
    # fifth — mid-pass, with rows both recovered and outstanding.
    svc = PartialProgressService("transactions", clock=clock, seconds_per_call=1.0)
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(40), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=4,
        max_retries=4,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        deadline_epoch=clock["now"] + 14.0,
    )

    stats = result["split_stats"]
    assert stats["deadline_reached"] is True
    # Stopped mid-pass rather than refusing the pass: some rows were recovered and
    # some were not, and the recovered ones are still scored.
    assert stats["rows_recovered_by_retry"] > 0
    assert stats["unrecoverable_rows"] > 0
    scored = [
        r
        for r in result["assessment"]["transactions"]
        if r.get("amount", {}).get("confidence") is not None
    ]
    assert len(scored) == 40 - stats["unrecoverable_rows"]


class TruncatesOnceThenPartial:
    """Truncates the FIRST call with a long duration, then makes partial progress.

    The long first call trips the bisection guard, which sets ``deadline_reached`` on
    the shared stats accumulator. Every later call is fast and fits comfortably. So
    the flag is already True when the retry rung starts, while the per-chunk guard is
    permitting every call it makes.
    """

    def __init__(self, list_field: str, first_call_seconds: float = 750.0):
        self.list_field = list_field
        self.first_call_seconds = first_call_seconds
        self.calls = 0

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        self.calls += 1
        if self.calls == 1:
            return AssessmentCoreResult(
                enhanced_assessment={
                    k: {"confidence": 0.5} for k in kw["extraction_results"]
                },
                parsing_succeeded=False,
                truncated=True,
                duration_seconds=self.first_call_seconds,
                metering={"Assessment/bedrock/model": {"outputTokens": 9999}},
            )
        scored = [
            {"amount": {"confidence": 0.9 if i < 1 else None}} for i in range(len(rows))
        ]
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: scored,
                "account_holder": {"confidence": 0.95},
            },
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=0.01,
            metering={"Assessment/bedrock/model": {"outputTokens": 100}},
        )


def test_an_earlier_deadline_flag_does_not_end_the_retry_rung():
    """``deadline_reached`` is sticky and shared, so the rung must not read it.

    The bisection guard sets that flag, and it is OR-merged out of concurrent workers
    before ``_retry_missing_rows`` is called. A rung that stopped on the flag would
    therefore run exactly one round whenever anything earlier had tripped it — even
    while its own per-chunk guard was permitting every call — which is the same
    "refuse work the budget allows" defect the per-chunk check was introduced to fix.
    The rung must key off whether THIS pass stopped early.
    """
    import time as _time

    svc = TruncatesOnceThenPartial("transactions")
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(12), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=4,
        max_retries=4,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        deadline_epoch=_time.time() + 800.0,
    )

    stats = result["split_stats"]
    # The long first call tripped the bisection guard...
    assert stats["deadline_reached"] is True
    # ...but the retry rung still ran its rounds and recovered more than the single
    # round a sticky-flag break would have allowed.
    assert stats["rows_recovered_by_retry"] > 3


def test_a_deadline_stop_with_rows_outstanding_names_the_budget_in_the_issue():
    """The cause must reach the issue an operator actually sees.

    ``build_assessment_issues`` emits the FIRST matching rung, and
    ``unrecoverable_rows > 0`` outranks ``deadline_reached``. Since the guard can
    only fire while rows are still missing, the ``assessment_deadline_reached``
    warning is unreachable from this path — so the ``assessment_incomplete`` error
    has to carry the time-budget cause and its remedy itself, or an operator is
    told rows are unscorable when they were simply never attempted.
    """
    import time as _time

    _svc, result = _partial_progress_run(_time.time() + 1.0)
    issues = build_assessment_issues(result["split_stats"], section_id="1")

    assert [(i.code, i.severity) for i in issues] == [
        ("assessment_incomplete", "error")
    ]
    message = issues[0].message
    assert "Lambda time budget" in message
    assert "not re-attempted" in message
    # The remedy must be time/work, not "use a different model".
    assert "list_batch_size" in message


# --------------------------------------------------------------------------- #
# Wrapper signature regression (the standalone AssessmentStep path)
# --------------------------------------------------------------------------- #
def test_wrapper_process_document_section_accepts_and_forwards_deadline():
    """The backward-compat wrapper `idp_common.assessment.AssessmentService`
    (used by the standalone Assessment Lambda) must accept AND forward
    `deadline_epoch` to the real service — a mismatch here crashed simple-mode
    documents with 'unexpected keyword argument deadline_epoch' even though the
    real service and the handler were both updated (regression guard)."""
    import inspect

    from idp_common.assessment import AssessmentService as Wrapper

    sig = inspect.signature(Wrapper.process_document_section)
    assert "deadline_epoch" in sig.parameters

    # Forwarding: stub the inner service and assert the kwarg is passed through.
    captured = {}

    class _StubInner:
        def process_document_section(self, document, section_id, deadline_epoch=None):
            captured["deadline_epoch"] = deadline_epoch
            return document

    w = Wrapper.__new__(Wrapper)  # bypass __init__ (no config/boto needed)
    w._service = _StubInner()
    w.process_document_section("doc", "sec-1", deadline_epoch=12345.0)
    assert captured["deadline_epoch"] == 12345.0


# --------------------------------------------------------------------------- #
# Item 4: concurrent assessment batches (cache-warm then fan-out)
# --------------------------------------------------------------------------- #
class _OrderTrackingService:
    """Scores each row with a confidence encoding its DATE index, so the test can
    verify concurrent batches are re-assembled in the correct row order. Records
    call arrival order to confirm the cache-warm-first behavior."""

    def __init__(self, list_field: str):
        self.list_field = list_field
        import threading

        self._lock = threading.Lock()
        self.call_order: list[int] = []

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        with self._lock:
            self.call_order.append(len(rows))
        enhanced = {
            self.list_field: [
                {"amount": {"confidence": 0.9, "confidence_reason": r["amount"]}}
                for r in rows
            ],
            "account_holder": {"confidence": 0.95},
        }
        return AssessmentCoreResult(
            enhanced_assessment=enhanced,
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 5}},
        )


def test_concurrent_batches_preserve_row_order():
    """With max_concurrent_batches>1, batches fan out after a cache-warm call but
    the concatenated per-row result stays index-aligned with the input rows."""
    svc = _OrderTrackingService("transactions")
    # 30 unique rows, batch 5 -> 6 batches; amount carries the row index.
    data = {
        "transactions": [{"amount": f"row-{i}"} for i in range(30)],
        "account_holder": "Jane",
    }
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=5,
        max_concurrent_batches=4,
    )
    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 30
    # Each row's confidence_reason (=original amount) must line up with its index.
    for i, row in enumerate(assessed):
        assert row["amount"]["confidence_reason"] == f"row-{i}", (
            f"row {i} misaligned after concurrent batching"
        )
    stats = result["split_stats"]
    assert stats["batch_count"] == 6
    assert stats["concurrent_batches"] and stats["concurrent_batches"] > 1


def test_sequential_when_concurrency_disabled():
    """max_concurrent_batches<=1 keeps the original sequential behavior."""
    svc = _OrderTrackingService("transactions")
    data = {"transactions": [{"amount": f"r{i}"} for i in range(12)]}
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=4,
        max_concurrent_batches=1,
    )
    assert len(result["assessment"]["transactions"]) == 12
    assert result["split_stats"]["concurrent_batches"] == 1


# --------------------------------------------------------------------------- #
# Schema-mismatch guard: skip futile escalation for off-schema/non-array fields
# (the IDP1 rvl_cdip failure — extraction returned a list for an attribute the
# class schema does not define as an array, so the enhancer collapsed it to a
# single default leaf that NO model can turn into per-row scores).
# --------------------------------------------------------------------------- #
from idp_common.assessment.batching import (  # noqa: E402
    _schema_field_mismatch_reason,
    build_assessment_issues,
)

# A class schema where "publications" is NOT an array (declared scalar) and
# "ftc_testing_conditions" is entirely absent (off-schema/hallucinated).
_MISMATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "publications": {"type": "string"},  # scalar, but extraction returned a list
        "account_holder": {"type": "string"},
    },
}


class CollapsingService:
    """Stand-in mirroring AssessmentService's real behavior for a list field the
    schema does NOT define as an array: the enhancer collapses it to a single
    ``{"confidence": 0.5}`` scalar leaf (see service.py's non-list branch). A
    stronger escalation model repeats the identical collapse — so the ladder can
    never recover the rows. Records every call so tests can assert escalation was
    NOT wasted."""

    def __init__(self, list_field: str, escalation_model: str):
        self.list_field = list_field
        self.escalation_model = escalation_model
        self.primary_calls = 0
        self.escalation_calls = 0

    def assess_results(self, **kw):
        if kw.get("model_id_override") == self.escalation_model:
            self.escalation_calls += 1
        else:
            self.primary_calls += 1
        # Collapse: the list field comes back as a single scalar confidence leaf,
        # NOT a per-row list — exactly what the enhancer produces for a non-array
        # attribute. Reconciliation then pads it to null placeholders.
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: {"confidence": 0.5, "confidence_reason": "collapsed"},
                "account_holder": {"confidence": 0.95},
            },
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 50}},
        )

    def _resolve_confidence_escalation_model(self, class_label: str):
        return self.escalation_model


def test_schema_mismatch_reason_detects_absent_and_scalar_fields():
    """The helper flags a field missing from the schema and a scalar-typed one,
    and passes a validly array-typed field."""
    assert _schema_field_mismatch_reason("ftc_testing_conditions", _MISMATCH_SCHEMA)
    assert _schema_field_mismatch_reason("publications", _MISMATCH_SCHEMA)
    array_schema = {"type": "object", "properties": {"txns": {"type": "array"}}}
    assert _schema_field_mismatch_reason("txns", array_schema) is None
    # No schema threaded in → never blocks (unchanged behavior).
    assert _schema_field_mismatch_reason("anything", None) is None


def test_schema_mismatch_reason_names_the_dereferenced_type():
    """A ``$ref`` property must be reported by its REAL declared type.

    ``{"$ref": "#/$defs/Foo"}`` carries no ``type`` of its own, so the reason
    used to say "declared as 'scalar'" for what is actually an object group —
    sending whoever reads the skip reason hunting for a ``type: string`` that
    does not exist (GitHub issue #678). The skip DECISION is unchanged: a
    ``$defs`` group resolves to ``object``, still not ``array``.
    """
    ref_schema = {
        "type": "object",
        "properties": {
            "signatures": {"$ref": "#/$defs/Signatures"},
            "txns": {"$ref": "#/$defs/TxnList"},
        },
        "$defs": {
            "Signatures": {
                "type": "object",
                "properties": {"sig1": {"type": "boolean"}},
            },
            "TxnList": {"type": "array", "items": {"type": "string"}},
        },
    }

    reason = _schema_field_mismatch_reason("signatures", ref_schema)
    assert reason is not None
    assert "declared as 'object'" in reason
    assert "scalar" not in reason

    # A $ref that DOES resolve to an array is validly list-typed → no skip.
    assert _schema_field_mismatch_reason("txns", ref_schema) is None

    # A dangling $ref still degrades to the un-dereferenced reading.
    dangling = {"type": "object", "properties": {"x": {"$ref": "#/$defs/Gone"}}}
    dangling_reason = _schema_field_mismatch_reason("x", dangling)
    assert dangling_reason is not None
    assert "declared as 'scalar'" in dangling_reason


def test_schema_mismatch_skips_escalation_and_flags_root_cause():
    """A list extracted for a scalar-typed schema attribute must NOT trigger
    escalation (a stronger model can't fix a schema mismatch); the run records
    the field in schema_mismatch_fields and emits an assessment_schema_mismatch
    error naming the field and the real fix."""
    svc = CollapsingService("publications", CLAUDE_SONNET)
    data = {"publications": _rows(5), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="resume",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        confidence_model_id=NOVA_LITE,
        geometry_mode="ocr_only",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
        class_schema=_MISMATCH_SCHEMA,
    )

    stats = result["split_stats"]
    assert stats["schema_mismatch_fields"] == ["publications"]
    # The whole point: the stronger model was NEVER called for a schema mismatch.
    assert svc.escalation_calls == 0
    assert stats["rows_recovered_by_escalation"] == 0

    issues = build_assessment_issues(stats, section_id="4", confidence_model=NOVA_LITE)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "assessment_schema_mismatch"
    assert issue.severity == "error"
    assert "publications" in issue.message
    assert "schema" in issue.message.lower()


def test_schema_mismatch_issue_takes_precedence_over_incomplete():
    """When both a schema mismatch and leftover unrecoverable rows are present,
    the schema-mismatch issue (the true root cause) is emitted, not the generic
    assessment_incomplete."""
    stats = {
        "schema_mismatch_fields": ["ftc_testing_conditions"],
        "unrecoverable_rows": 6,
        "escalation_rounds": 0,
    }
    issues = build_assessment_issues(stats, section_id="2")
    assert len(issues) == 1
    assert issues[0].code == "assessment_schema_mismatch"


def test_validly_typed_list_field_still_escalates():
    """Regression guard: a field the schema DOES define as an array is never
    blocked by the mismatch guard — escalation still runs for it."""
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(8), "account_holder": "Jane Doe"}
    array_schema = {
        "type": "object",
        "properties": {
            "transactions": {"type": "array"},
            "account_holder": {"type": "string"},
        },
    }

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=4,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=True,
        escalation_model=CLAUDE_SONNET,
        max_escalation_rounds=2,
        class_schema=array_schema,
    )

    assert svc.escalation_calls  # escalation still happened for the valid list
    assert result["split_stats"]["rows_recovered_by_escalation"] == 8
    assert not result["split_stats"]["schema_mismatch_fields"]


# --------------------------------------------------------------------------- #
# #1488 — the primary fan-out must not start work the invocation cannot hold
# --------------------------------------------------------------------------- #


def _clean_scorer_run(
    deadline_epoch,
    *,
    clock,
    rows=50,
    batch_size=10,
    max_concurrent_batches=1,
    seconds_per_call=None,
):
    """A run whose stand-in scores every row it is handed, with a driven clock.

    `scored_per_call >= batch_size` makes every row of each slice come back with a
    real confidence, so "which rows were scored" is readable from the output — the
    property these tests are about. `seconds_per_call` charges each model call a
    fixed cost, defaulting to exactly what the deadline guards budget for it, so
    the budget below is spent by doing the work rather than by wall-clock luck. A
    test that wants a model FASTER than the estimate passes a smaller value.
    """
    svc = PartialProgressService(
        "transactions",
        scored_per_call=batch_size,
        clock=clock,
        seconds_per_call=(
            mod._ESTIMATED_MODEL_CALL_SECONDS
            if seconds_per_call is None
            else seconds_per_call
        ),
    )
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(rows), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=batch_size,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        max_concurrent_batches=max_concurrent_batches,
        deadline_epoch=deadline_epoch,
        max_retries=0,  # isolate the first pass; no recovery rung
    )
    return svc, result


def _scored_flags(result):
    """Per row, whether the output carries a real confidence."""
    return [
        row.get("amount", {}).get("confidence") is not None
        for row in result["assessment"]["transactions"]
    ]


def test_primary_batches_stop_at_the_deadline_instead_of_overrunning(monkeypatch):
    """The first pass is bounded, and the rows it skips are a reported suffix.

    #1488: a Lambda killed on the wall clock raises no exception, so the handler's
    deterministic-failure degrade path (#901) never runs and a complete, validated,
    already-persisted extraction is discarded along with the document. The fan-out
    therefore has to refuse a batch it cannot finish rather than start it.
    """
    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    # Budget for exactly two of the five batches.
    deadline = (
        clock["now"]
        + (2 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )

    _svc, result = _clean_scorer_run(deadline, clock=clock)

    assert result["split_stats"]["deadline_reached"] is True

    # The skipped rows are a TRAILING suffix: rows accumulate positionally, so
    # dropping a middle batch would slide later rows' confidence and bounding boxes
    # onto earlier rows — the defect 0.6.12 closed by making partial per-shard
    # collation discard rather than emit.
    flags = _scored_flags(result)
    assert flags[:20] == [True] * 20, "the batches that DID run must be scored"
    assert flags[20:] == [False] * 30, (
        "the skipped rows must be a contiguous unscored suffix, not a hole"
    )

    # ⚠️ Not asserted on the model-call count: the guard bounds CHUNKS, and the
    # adaptive splitter can make several calls inside one chunk, so a call count
    # measures the splitter rather than this guard.


def test_no_deadline_leaves_the_primary_fan_out_unbounded(monkeypatch):
    """With no deadline (local / non-Lambda) every batch still runs.

    The guard must be inert where `deadline_epoch` is None, or it would silently
    shorten every local run.
    """
    clock = {"now": 3_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])

    _svc, result = _clean_scorer_run(None, clock=clock)

    assert result["split_stats"]["deadline_reached"] is False
    assert _scored_flags(result) == [True] * 50


def test_the_concurrent_fan_out_stops_at_a_wave_boundary(monkeypatch):
    """The concurrent path is bounded too, and what it skips is a trailing suffix.

    200 rows at batch 10 is 20 batches: one to warm the cache and 19 fanned out
    across 2 workers. Each model call costs the full estimate, so the budget below
    pays for the warm batch and two waves, and the third wave is refused.
    """
    clock = {"now": 4_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    # 1 warm call + 2 waves × 2 calls = 5 calls' worth of budget.
    deadline = (
        clock["now"]
        + (5 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )

    _svc, result = _clean_scorer_run(
        deadline, clock=clock, rows=200, batch_size=10, max_concurrent_batches=2
    )

    assert result["split_stats"]["deadline_reached"] is True
    assert result["split_stats"]["concurrent_batches"] == 2

    # Warm batch + 4 fanned-out batches = 50 rows scored, and the 150 unscored
    # rows are a contiguous suffix rather than a hole: rows accumulate
    # positionally, so dropping a middle batch would slide later rows' confidence
    # and bounding boxes onto earlier rows.
    flags = _scored_flags(result)
    assert flags[:50] == [True] * 50, "every dispatched batch must be scored"
    assert flags[50:] == [False] * 150, (
        "the skipped rows must be a contiguous unscored suffix, not a hole"
    )


def test_a_fast_model_is_not_refused_work_the_clock_allowed(monkeypatch):
    """The fan-out is bounded by TIME, not by a chunk count priced up front.

    This is the #958 argument applied one level up. Pricing the whole fan-out
    before dispatching any of it means pricing it at a worst case —
    `(budget // one_call) × workers` chunks — which is a cap on chunk COUNT, not a
    bound on time: here 19 batches across 5 workers against a 180s budget prices
    out at 2 waves, so 9 batches (90 rows) would be refused. The model in this run
    answers in 1s, so all 19 fit in 19s with 161s to spare, and re-reading the
    clock per wave is what lets the later waves run on that evidence. Those 90 rows
    coming back `confidence: null` is what trips the coverage check's error rung on
    a section that scored completely.
    """
    clock = {"now": 5_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    deadline = (
        clock["now"]
        + (3 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )

    svc, result = _clean_scorer_run(
        deadline,
        clock=clock,
        rows=200,
        batch_size=10,
        max_concurrent_batches=5,
        seconds_per_call=1.0,
    )

    # 20 batches: the whole fan-out ran, so no row is left unscored and the
    # deadline flag never had to be set.
    assert len(svc.primary_calls) == 20
    assert result["split_stats"]["deadline_reached"] is False
    assert _scored_flags(result) == [True] * 200
    # The budget really was tight relative to the chunk COUNT — the point of the
    # test is that the count is not what the guard measures.
    assert (deadline - clock["now"]) > mod._DEADLINE_SAFETY_RESERVE_SECONDS


class ShortAnswerService:
    """Answers about only the first ``answered`` rows of every slice handed to it.

    A genuinely SHORT response rather than a truncated one: ``truncated`` is False,
    so the adaptive splitter does not bisect and reconciliation is what pads the
    rows the model skipped. That is the other cause of an unscored row, and the
    point of the service is to produce it in the SAME run as a deadline stop, so
    the two per-row reasons can be told apart by index.
    """

    def __init__(self, list_field: str, clock: dict, answered: int):
        self.list_field = list_field
        self.clock = clock
        self.answered = answered
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        with self._lock:
            self.calls.append(len(rows))
            self.clock["now"] += mod._ESTIMATED_MODEL_CALL_SECONDS
        return AssessmentCoreResult(
            enhanced_assessment={
                self.list_field: [
                    {"amount": {"confidence": 0.9}} for _ in rows[: self.answered]
                ],
                "account_holder": {"confidence": 0.95},
            },
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=0.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 100}},
        )


def test_a_skipped_row_says_the_time_budget_and_not_a_short_response(monkeypatch):
    """The per-row reason names the cause that applies to THAT row.

    ``confidence_reason`` is the string an operator reads per field in the UI, and
    the two causes of an empty confidence have different remedies: a model that
    answered about fewer rows than it was asked about points at the output budget,
    where a batch that was never dispatched points at the time budget. Reporting
    the first for the second sends an operator to the wrong knob, and the
    section-level issue naming the time budget one level up does not undo a row-level
    string that contradicts it.

    One run produces both: the model answers about 6 rows of every 10-row batch,
    and the budget covers only 2 of the 5 batches.
    """
    clock = {"now": 7_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    deadline = (
        clock["now"]
        + (2 * mod._ESTIMATED_MODEL_CALL_SECONDS)
        + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )

    svc = ShortAnswerService("transactions", clock, answered=6)
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results={"transactions": _rows(50), "account_holder": "Jane Doe"},
        document_text="...",
        page_images=[],
        batch_size=10,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        max_concurrent_batches=1,
        max_retries=0,  # no recovery rung: every unscored row stays unscored
        deadline_epoch=deadline,
    )

    assert result["split_stats"]["deadline_reached"] is True
    # Two batches ran, and `batch_count` reports what RAN rather than what was
    # planned — the processing report and the UI both render it as batches the
    # confidence pass ran in.
    assert len(svc.calls) == 2
    assert result["split_stats"]["batch_count"] == 2

    rows_out = result["assessment"]["transactions"]
    assert len(rows_out) == 50

    def _reason(i: int) -> str | None:
        return rows_out[i]["amount"].get("confidence_reason")

    # Rows the model answered about.
    for i in (0, 5, 10, 15):
        assert rows_out[i]["amount"]["confidence"] == 0.9

    # Rows inside a DISPATCHED batch that the model's answer did not cover: the
    # model really did return fewer items than were extracted.
    for i in (6, 9, 16, 19):
        assert rows_out[i]["amount"]["confidence"] is None
        assert _reason(i) == mod._UNASSESSED_SHORT_RESPONSE_REASON

    # Rows whose batch was never dispatched. No call was made for them, so the
    # reason above would be false; the deadline reason names the time budget and
    # its levers.
    for i in (20, 35, 49):
        assert rows_out[i]["amount"]["confidence"] is None
        assert _reason(i) == mod._UNASSESSED_DEADLINE_REASON
        assert "time budget" in (_reason(i) or "")
        assert "returned fewer items" not in (_reason(i) or "")

    # Every never-dispatched row carries the deadline reason, and no dispatched
    # row does — the boundary is the last dispatched chunk, not a guess.
    deadline_reason_rows = [
        i for i in range(50) if _reason(i) == mod._UNASSESSED_DEADLINE_REASON
    ]
    assert deadline_reason_rows == list(range(20, 50))


def _map_counting_pool_class(record: list):
    """A ThreadPoolExecutor that records how many times ``map`` was called."""
    import concurrent.futures as _cf

    class _Counting(_cf.ThreadPoolExecutor):
        def map(self, *args, **kwargs):
            record.append(1)
            return super().map(*args, **kwargs)

    return _Counting


def test_a_deadline_free_fan_out_keeps_one_continuous_map(monkeypatch):
    """With no deadline the fan-out is NOT dispatched in waves.

    A wave boundary costs a straggler — the wave is only as fast as its slowest
    call, where one continuous ``pool.map`` refills a free worker the moment it
    frees up. That cost buys the observation point the budget needs, so a caller
    with no ``deadline_epoch`` must not pay it: there is nothing to re-read and
    nothing it could refuse. 20 batches, 19 of them fanned out, must therefore
    reach the pool in ONE map call rather than ten.
    """
    clock = {"now": 8_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    maps: list[int] = []
    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _map_counting_pool_class(maps)
    )

    svc, result = _clean_scorer_run(
        None, clock=clock, rows=200, batch_size=10, max_concurrent_batches=2
    )

    assert len(maps) == 1
    assert len(svc.primary_calls) == 20  # 1 cache-warm + 19 fanned out
    assert result["split_stats"]["deadline_reached"] is False
    assert _scored_flags(result) == [True] * 200


def test_a_deadline_bearing_fan_out_dispatches_in_waves(monkeypatch):
    """The other side of the branch above: with a deadline, one map per wave.

    Same shape and a budget nothing is refused on, so the two tests differ only in
    whether a deadline was threaded in. 19 fanned-out batches across 2 workers is
    10 waves, so a single map call here would mean the clock is read once and the
    guard cannot stop anything after the first wave.
    """
    clock = {"now": 9_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])
    maps: list[int] = []
    monkeypatch.setattr(
        "concurrent.futures.ThreadPoolExecutor", _map_counting_pool_class(maps)
    )
    deadline = (
        clock["now"] + 10_000.0 + mod._DEADLINE_SAFETY_RESERVE_SECONDS
    )  # generous: nothing is refused

    svc, result = _clean_scorer_run(
        deadline,
        clock=clock,
        rows=200,
        batch_size=10,
        max_concurrent_batches=2,
        seconds_per_call=1.0,
    )

    assert len(maps) == 10  # ceil(19 / 2)
    assert len(svc.primary_calls) == 20
    assert result["split_stats"]["deadline_reached"] is False
    assert _scored_flags(result) == [True] * 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
