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

import pytest

from idp_common.assessment.batching import (
    assess_results_batched,
    compute_token_aware_batch_size,
)
from idp_common.assessment.service import AssessmentCoreResult

NOVA_LITE = "us.amazon.nova-lite-v1:0"  # 10K output cap
CLAUDE_SONNET = "us.anthropic.claude-sonnet-4-20250514-v1:0"  # 64K output cap


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


def test_token_aware_unknown_model_falls_back():
    """An unknown model (no entry in model_config_limits) → keep configured size."""
    sample = {"date": "2020-01-01", "amount": "1.00"}
    derived = compute_token_aware_batch_size(
        "some.unknown.model-v9:0", sample, "ocr_only", configured_batch_size=25
    )
    assert derived == 25


def test_token_aware_never_returns_zero():
    """Even a huge per-row payload clamps to at least 1 row per batch."""
    huge_row = {"blob": "x" * 500_000}
    derived = compute_token_aware_batch_size(NOVA_LITE, huge_row, "llm", 25)
    assert derived >= 1


def test_token_aware_no_model_keeps_configured():
    """No model id → cannot resolve a cap → keep the configured size."""
    assert compute_token_aware_batch_size(None, {"a": 1}, "ocr_only", 25) == 25


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
    wide = compute_token_aware_batch_size(NOVA_LITE, wide_short, "ocr_only", 25)
    narrow = compute_token_aware_batch_size(NOVA_LITE, narrow_long, "ocr_only", 25)
    # Nova Lite 10K cap, 0.5 fraction, ~40 tok/cell: 6 cols → floor(5000/240)=20.
    assert wide == 20
    # The 2-column row is NOT shrunk more than the 6-column row, even though its
    # values are far longer — value length no longer drives the estimate.
    assert narrow >= wide
    # Nested/list sub-fields are NOT counted as scalar confidence columns.
    with_nested = {**wide_short, "extra": {"nested": "obj"}, "items": [1, 2, 3]}
    assert (
        compute_token_aware_batch_size(NOVA_LITE, with_nested, "ocr_only", 25) == wide
    )


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


def test_deadline_reached_propagates_from_concurrent_workers():
    """A wall-clock cutoff hit inside a CONCURRENT fan-out worker must survive the
    post-join merge and still set deadline_reached (regression: the merge loop
    summed counters but dropped the deadline_reached bool, so
    assessment_deadline_reached never fired on the concurrent path)."""
    import time as _time

    # Always-truncating primary, no escalation → the adaptive splitter in each
    # worker hits the deadline guard. Many rows + concurrency forces multiple
    # fanned-out workers (not just the cache-warm batch).
    svc = LadderService("transactions", CLAUDE_SONNET, primary_max_rows=0)
    data = {"transactions": _rows(60)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=10,
        confidence_model_id=NOVA_LITE,
        geometry_mode="llm_grounded",
        escalation_enabled=False,
        max_concurrent_batches=5,  # force concurrent fan-out
        deadline_epoch=_time.time() + 1.0,  # below the 90s reserve
    )

    assert result["split_stats"]["concurrent_batches"] and (
        result["split_stats"]["concurrent_batches"] > 1
    )
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
