# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Shared large-list assessment batching (idp_common.assessment.batching).

These lock in the safety net that lets the STANDALONE (non-agentic) Assessment
step handle large lists on its own — the capability that previously lived only in
granular assessment. A single assessment inference over a 120-row table
under-enumerates or omits the list; ``assess_results_batched`` slices it into
``list_batch_size`` chunks, assesses each with the shared scalars/context, and
reconciles the concatenated per-row assessments to full per-cell coverage.
"""

from __future__ import annotations

import pytest

from idp_common.assessment.batching import (
    assess_results_batched,
    format_split_stats_report,
    merge_split_stats,
    reconcile_assessment_to_data,
    split_stats_are_notable,
)
from idp_common.assessment.service import AssessmentCoreResult


class FakeAssessmentService:
    """Stand-in exposing the ``assess_results`` core the batcher calls.

    Records each call's row count for the batched list field and returns a
    per-row assessment covering only the rows it was handed — mimicking a model
    that DOES enumerate the rows it sees but is only ever shown a slice.
    """

    def __init__(self, list_field: str):
        self.list_field = list_field
        self.calls: list[int] = []

    def assess_results(
        self,
        *,
        class_label: str,
        extraction_results: dict,
        document_text: str,
        page_images: list,
        ocr_text_confidence: str = "",
    ) -> AssessmentCoreResult:
        rows = extraction_results.get(self.list_field, [])
        self.calls.append(len(rows))
        enhanced = {
            self.list_field: [
                {"confidence": 0.9, "confidence_reason": "clear"} for _ in rows
            ],
            "account_holder": {"confidence": 0.95, "confidence_reason": "header"},
        }
        return AssessmentCoreResult(
            enhanced_assessment=enhanced,
            confidence_threshold_alerts=[{"attribute_name": self.list_field}],
            metering={
                "Assessment/bedrock/model": {"inputTokens": 10, "outputTokens": 5}
            },
            parsing_succeeded=True,
            duration_seconds=1.0,
        )


def _rows(n: int) -> list[dict]:
    return [{"date": f"2020-01-{i:02d}", "amount": f"{i}.00"} for i in range(1, n + 1)]


def test_large_list_is_batched_and_fully_covered():
    """A 120-row list with batch_size 25 → 5 sequential calls, all 120 rows
    assessed with per-column confidence, and the scalar assessment preserved."""
    svc = FakeAssessmentService("transactions")
    data = {"transactions": _rows(120), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
    )

    # 120 rows / 25 = ceil -> 5 sequential batches (never a 20-way fan-out).
    assert svc.calls == [25, 25, 25, 25, 20]

    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 120  # every row present
    # Every list cell got per-column confidence (fanned out from per-row score).
    assert all(set(row.keys()) == {"date", "amount"} for row in assessed), (
        "each row must carry a per-column leaf for date and amount"
    )
    assert all(row["date"]["confidence"] == 0.9 for row in assessed)

    # Scalar/group assessment from the first batch survives.
    assert result["assessment"]["account_holder"]["confidence"] == 0.95

    # Metering accumulated across all 5 calls; duration summed; parse ok.
    assert result["metering"]["Assessment/bedrock/model"]["inputTokens"] == 50
    assert result["metering"]["Assessment/bedrock/model"]["outputTokens"] == 25
    assert result["duration_seconds"] == pytest.approx(5.0)
    assert result["parsing_succeeded"] is True
    # The fake re-alerts the same attribute_name on every call; the merge now
    # collapses those repeats to one finding (each batch re-assesses the same
    # scalars, so per-batch re-alerts are copies, not new findings).
    assert len(result["alerts"]) == 1


def test_small_list_single_call_still_reconciles():
    """A list at/under the batch size uses a single call — still reconciled to
    full per-cell coverage (no unassessed rows leak through)."""
    svc = FakeAssessmentService("transactions")
    data = {"transactions": _rows(10), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
    )

    assert svc.calls == [10]  # single call, no batching
    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 10
    assert all(set(row.keys()) == {"date", "amount"} for row in assessed)


def test_batches_pad_when_model_underenumerates():
    """Even when a batch's model response omits rows, reconciliation pads to the
    batch's row count so the concatenated result covers every extracted row."""

    class UnderEnumeratingService(FakeAssessmentService):
        def assess_results(self, **kwargs):
            # Model returns only the FIRST row's assessment per batch.
            core = super().assess_results(**kwargs)
            core.enhanced_assessment[self.list_field] = core.enhanced_assessment[
                self.list_field
            ][:1]
            return core

    svc = UnderEnumeratingService("transactions")
    data = {"transactions": _rows(60)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        max_retries=0,  # isolate reconcile/pad behavior from the retry pass
    )

    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 60  # padded back to full extracted count
    # First row of each batch keeps real confidence; the rest are null placeholders.
    assert assessed[0]["date"]["confidence"] == 0.9
    assert assessed[1]["date"]["confidence"] is None


def test_reconcile_aligns_list_length():
    """reconcile_assessment_to_data truncates over-long and pads short lists."""
    data = {"txns": _rows(3)}
    out = reconcile_assessment_to_data({"txns": [{"confidence": 0.9}]}, data)
    assert len(out["txns"]) == 3
    assert out["txns"][0]["date"]["confidence"] == 0.9  # fanned to per-column
    assert out["txns"][2]["date"]["confidence"] is None  # padded placeholder


class DropLastFirstPass(FakeAssessmentService):
    """Drops the last row on the FIRST full-list pass, scores everything it's
    handed on any later (retry) call — a model that under-counts once but scores
    a focused re-ask of just the missing rows."""

    def __init__(self, list_field, full_len):
        super().__init__(list_field)
        self.full_len = full_len

    def assess_results(self, **kw):
        core = super().assess_results(**kw)
        rows = kw["extraction_results"].get(self.list_field, [])
        if len(rows) == self.full_len and len(self.calls) == 1:
            core.enhanced_assessment[self.list_field] = core.enhanced_assessment[
                self.list_field
            ][:-1]
        return core


def test_missing_rows_are_retried_to_full_coverage():
    """A row dropped on the first pass is recovered by the bounded retry → every
    list cell ends with a real (non-null) confidence, no null placeholders."""
    svc = DropLastFirstPass("transactions", full_len=10)
    data = {"transactions": _rows(10)}
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,  # single-call path, then retry
    )
    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 10
    for row in assessed:
        for leaf in row.values():
            assert leaf["confidence"] is not None
    assert len(svc.calls) >= 2  # retry happened


def test_retry_stops_when_no_progress():
    """If the model can never score a row, retry stops after bounded rounds and
    leaves the null placeholder (no infinite loop)."""

    class NeverScoresLastRow(FakeAssessmentService):
        def assess_results(self, **kw):
            core = super().assess_results(**kw)
            rows = kw["extraction_results"].get(self.list_field, [])
            if len(rows) > 1:
                core.enhanced_assessment[self.list_field] = core.enhanced_assessment[
                    self.list_field
                ][:-1]
            else:
                core.enhanced_assessment[self.list_field] = []
            return core

    svc = NeverScoresLastRow("transactions")
    data = {"transactions": _rows(5)}
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
        max_retries=2,
    )
    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 5
    assert assessed[-1]["date"]["confidence"] is None


# --------------------------------------------------------------------------- #
# Truncation hardening: a model that hits its max-output-token ceiling returns
# unparseable JSON (default/placeholder scores) with ``truncated=True``. The
# batcher must SHRINK the row slice and retry — not accept the placeholders —
# and record the activity in ``split_stats`` for visibility.
# --------------------------------------------------------------------------- #
class TruncateOverNRows(FakeAssessmentService):
    """Mimics a small-cap model (e.g. Nova Lite) whose per-row output (incl. LLM
    bounding boxes) overflows the token ceiling once a call carries more than
    ``max_rows`` rows: it returns ``truncated=True`` with only default 0.5
    placeholders. Calls with <= ``max_rows`` rows score cleanly."""

    def __init__(self, list_field: str, max_rows: int):
        super().__init__(list_field)
        self.max_rows = max_rows

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        self.calls.append(len(rows))
        if len(rows) > self.max_rows:
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
        enhanced = {
            self.list_field: [{"amount": {"confidence": 0.9}} for _ in rows],
            "account_holder": {"confidence": 0.95},
        }
        return AssessmentCoreResult(
            enhanced_assessment=enhanced,
            parsing_succeeded=True,
            truncated=False,
            duration_seconds=1.0,
            metering={"Assessment/bedrock/model": {"outputTokens": 50}},
        )


def test_truncated_batch_is_split_until_it_fits():
    """A batch the model truncates is recursively halved until every row is
    scored — no null/default placeholders survive — and split_stats records it."""
    svc = TruncateOverNRows("transactions", max_rows=2)
    data = {"transactions": _rows(5), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=5,  # one batch of 5 -> truncates -> must split down to <=2
    )

    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 5
    # Every row recovered a real confidence via shrinking (no leftover 0.5/null).
    for row in assessed:
        assert row["amount"]["confidence"] == 0.9

    stats = result["split_stats"]
    assert stats["truncated_calls"] >= 1
    assert stats["splits"] >= 1
    assert stats["min_batch_size_used"] <= 2
    assert stats["unrecoverable_rows"] == 0
    # The smallest successful calls carried <= max_rows rows.
    assert any(c <= 2 for c in svc.calls)


def test_truncated_calls_are_counted_in_metering():
    """A truncated call still burns output tokens before we split — its metering
    must be folded in so cost reflects the wasted work, not just the successful
    sub-slices. The fake emits 10000 output tokens per truncated call and 50 per
    clean call; total must include the truncated attempts."""
    svc = TruncateOverNRows("transactions", max_rows=2)
    data = {"transactions": _rows(5), "account_holder": "Jane Doe"}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=5,
    )

    truncated = sum(1 for c in svc.calls if c > 2)
    clean = sum(1 for c in svc.calls if c <= 2)
    assert truncated >= 1  # at least the initial 5-row call truncated
    expected = truncated * 10000 + clean * 50
    got = result["metering"]["Assessment/bedrock/model"]["outputTokens"]
    assert got == expected, (
        f"metering must count truncated calls: expected {expected}, got {got}"
    )


def test_truncation_that_never_fits_is_bounded_and_visible():
    """If even a single row truncates, splitting bottoms out at 1 row without
    infinite recursion, leaves placeholders, and reports unrecoverable rows."""
    svc = TruncateOverNRows("transactions", max_rows=0)  # everything truncates
    data = {"transactions": _rows(3)}

    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=3,
        max_retries=1,
    )

    assessed = result["assessment"]["transactions"]
    assert len(assessed) == 3  # still aligned, just unscored
    stats = result["split_stats"]
    assert stats["truncated_calls"] >= 1
    assert stats["min_batch_size_used"] == 1  # bottomed out at a single row
    assert stats["unrecoverable_rows"] == 3
    assert split_stats_are_notable(stats)


def test_clean_run_reports_no_split_stats():
    """A run with no truncation is not 'notable' — callers omit the metadata."""
    svc = FakeAssessmentService("transactions")
    data = {"transactions": _rows(10), "account_holder": "Jane Doe"}
    result = assess_results_batched(
        svc,
        class_label="bank-statement",
        extraction_results=data,
        document_text="...",
        page_images=[],
        batch_size=25,
    )
    assert split_stats_are_notable(result["split_stats"]) is False
    assert format_split_stats_report(result["split_stats"]) == ""


def test_merge_split_stats_sums_across_shards():
    """Sharded path aggregates per-shard split_stats additively; min takes the
    smaller non-null batch size."""
    a = {
        "truncated_calls": 2,
        "splits": 1,
        "min_batch_size_used": 4,
        "rows_recovered_by_retry": 3,
        "unrecoverable_rows": 0,
    }
    b = {
        "truncated_calls": 1,
        "splits": 2,
        "min_batch_size_used": 2,
        "rows_recovered_by_retry": 5,
        "unrecoverable_rows": 1,
    }
    merged = merge_split_stats(a, b)
    assert merged is not None
    assert merged["truncated_calls"] == 3
    assert merged["splits"] == 3
    assert merged["min_batch_size_used"] == 2
    assert merged["rows_recovered_by_retry"] == 8
    assert merged["unrecoverable_rows"] == 1
    assert merge_split_stats(None, None) is None


class LowConfidenceOnAmount17(FakeAssessmentService):
    """Rows whose amount is 17.00 score low; the core's own alert names the
    row by its position WITHIN THE SLICE it was handed (mimicking the real
    core, which enumerates only the rows it sees)."""

    def assess_results(self, **kw):
        rows = kw["extraction_results"].get(self.list_field, [])
        self.calls.append(len(rows))
        row_assessments = []
        core_alerts = []
        for local_idx, row in enumerate(rows):
            low = row.get("amount") == "17.00"
            conf = 0.5 if low else 0.95
            row_assessments.append(
                {"confidence": conf, "confidence_reason": "x"}
            )
            if low:
                core_alerts.append(
                    {
                        "attribute_name": f"{self.list_field}[{local_idx}]",
                        "confidence": conf,
                        "confidence_threshold": 0.9,
                    }
                )
        return AssessmentCoreResult(
            enhanced_assessment={self.list_field: row_assessments},
            confidence_threshold_alerts=core_alerts,
            metering={},
            parsing_succeeded=True,
            duration_seconds=0.1,
        )


def test_alert_row_indexes_are_global_not_slice_local():
    """A low-confidence row in the SECOND batch must be reported at its
    position in the merged list (transactions[16]), not at its position
    within the slice that assessed it (transactions[6]). Regression test
    for the per-slice index surface (upstream #813)."""
    svc = LowConfidenceOnAmount17("transactions")
    schema = {
        "properties": {
            "transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"date": {"type": "string"}},
                },
            }
        }
    }
    out = assess_results_batched(
        svc,
        class_label="bank_statement",
        extraction_results={"transactions": _rows(25)},
        document_text="doc",
        page_images=[],
        batch_size=10,
        class_schema=schema,
        default_confidence_threshold=0.9,
    )
    paths = [a["attribute_name"] for a in out["alerts"]]
    # Reconciliation reshapes row-level confidence leaves into per-cell leaves
    # (one per data column), so the regenerated surface names cells — the same
    # shape the standalone path's per-sub-field alerts use (``attr[i].sub``).
    # The essential property is the ROW index: 16 (merged list), never 6 (the
    # row's position within the slice that assessed it).
    assert paths == ["transactions[16].date", "transactions[16].amount"], (
        f"expected merged-list cell paths at row 16, got {paths} — an index "
        "of 6 means the surface was accumulated per core (slice-local) "
        "instead of regenerated from the merged assessment"
    )
