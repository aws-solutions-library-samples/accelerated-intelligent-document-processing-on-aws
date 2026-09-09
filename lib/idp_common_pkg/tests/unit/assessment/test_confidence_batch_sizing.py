# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Token-aware confidence list-batch sizing.

These tests exist because a wrong value here has already cost a document: a
simple-mode run extracted all 800 rows of a transaction list, then the Assessment
step truncated on a 25-row batch, the recovery ladder bisected, and Step Functions
retried the 900-second Lambda five times (~6,200s) before the document ended
ABORTED.

The central property under test is that there is NO correct constant. The batch
size that fits one confidence call is a function of three inputs — the confidence
model's output cap, the row's column count, and whether the geometry mode adds a
per-cell bounding box — so any fixed number is right for one shape and wrong for
the rest. ``extraction.confidence.list_batch_size`` is therefore a CEILING on the
derivation, never a target.

Deliberately NOT changed here: the shipped default stays 25. Defaulting it to 0
would be written into ``Config#default`` on every stack update, and every release
up to and including v0.6.7 rejects 0 (``gt=0``) — which wedges a rollback the same
way an empty pricing block did. Adopting a derived-by-default ceiling needs its own
change, with a live upgrade test.
"""

from __future__ import annotations

import pytest

from idp_common.assessment.batching import (
    compute_token_aware_batch_size,
    count_row_columns,
)
from idp_common.bedrock.sizing import (
    _ABS_MAX_LIST_BATCH,
    confidence_per_row_tokens,
    confidence_rows_per_call,
)

NOVA_LITE = "us.amazon.nova-lite-v1:0"  # 10,000-token output cap
SONNET5_1M = "us.anthropic.claude-sonnet-5:1m"  # 128,000-token output cap
CLAUDE3 = "us.anthropic.claude-3-haiku-20240307-v1:0"  # 8,192-token output cap


def _row(n_cols: int) -> dict[str, str]:
    return {f"Column{i}": "value" for i in range(n_cols)}


# ---------------------------------------------------------------------------
# The arithmetic: floor(cap * 0.5 / (cols * 40 * bbox_mult)), capped at 50
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cap", "cols", "geometry", "expected"),
    [
        # Nova Lite (10,000). Bounding boxes triple the per-cell cost.
        (10_000, 1, "llm_grounded", 41),
        (10_000, 3, "llm_grounded", 13),
        (10_000, 6, "llm_grounded", 6),
        (10_000, 8, "llm_grounded", 5),
        # Same model without bounding boxes: three times as many rows fit.
        (10_000, 3, "ocr_only", 41),
        (10_000, 8, "ocr_only", 15),
        # A smaller cap shrinks everything.
        (8_192, 3, "llm_grounded", 11),
        # A large cap hits the reliability ceiling, not the token math.
        (128_000, 3, "llm_grounded", _ABS_MAX_LIST_BATCH),
    ],
)
def test_rows_per_call_arithmetic(cap, cols, geometry, expected):
    """Pins the derived value for each (cap, columns, geometry) combination.

    The spread in this table IS the point: 41 rows and 5 rows are both correct
    answers for the same model, so neither the historical pinned default of 25 nor
    the historical derived value of 50 can be right for both.
    """
    assert confidence_rows_per_call(cap, cols, geometry) == expected


@pytest.mark.unit
def test_no_single_constant_satisfies_the_table():
    """Guards the reasoning, not just the numbers: assert that the correct value
    genuinely varies, so a future 'simplification' back to a constant fails."""
    values = {
        confidence_rows_per_call(10_000, cols, "llm_grounded") for cols in (1, 3, 6, 8)
    }
    assert len(values) > 1


# ---------------------------------------------------------------------------
# Column counting across rows, not off row 0
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_column_count_uses_the_widest_row_not_the_first():
    """Extraction does not guarantee uniform rows. Measuring only ``rows[0]``
    under-counts the width, which INFLATES the batch — the direction that
    truncates. A narrow first row must not shrink the measured width."""
    rows = [{"Date": "2024-01-05"}] + [_row(3) for _ in range(9)]
    assert count_row_columns(rows) == 3
    assert compute_token_aware_batch_size(NOVA_LITE, rows, "llm_grounded", 25) == 13


@pytest.mark.unit
def test_null_values_still_count_as_columns():
    """A null cell still emits a confidence leaf, so it must not reduce the count."""
    assert count_row_columns([{"A": None, "B": None, "C": None}]) == 3


@pytest.mark.unit
def test_nested_values_are_not_counted_as_scalar_columns():
    assert count_row_columns([{"A": "x", "B": {"nested": 1}, "C": [1, 2]}]) == 1


@pytest.mark.unit
def test_bare_dict_is_accepted_as_a_single_row():
    """Back-compat: callers used to pass one row."""
    assert count_row_columns(_row(4)) == 4
    assert compute_token_aware_batch_size(
        NOVA_LITE, _row(3), "llm_grounded", 25
    ) == compute_token_aware_batch_size(NOVA_LITE, [_row(3)], "llm_grounded", 25)


@pytest.mark.unit
def test_opaque_scalar_rows_are_floored_at_one_confidence_leaf():
    """For non-dict rows the width cannot be counted, so the estimate falls back to
    value length x8. ``json.dumps("Robert Smith")`` is ~3 tokens, predicting ~24
    output tokens for a row that really emits 40-120 — about 5x optimistic, and
    optimism here means a batch that truncates. The estimate is floored at the cost
    of one confidence leaf so a list of short strings cannot reach the ceiling."""
    short = ["Robert Smith"] * 200
    bbox = compute_token_aware_batch_size(CLAUDE3, short, "llm_grounded", 25)
    plain = compute_token_aware_batch_size(CLAUDE3, short, "ocr_only", 25)
    # Floored at 1 column: 8,192 x 0.5 / (1 x 40 x 3) = 34 -> ceiling 25 binds;
    # without bbox 8,192 x 0.5 / 40 = 102 -> ceiling 25 binds. The floor's job is to
    # stop the x8 estimate producing something ABOVE these.
    assert bbox <= 25 and plain <= 25
    assert bbox <= plain
    # Monotonic: a longer scalar must not yield a LARGER batch than a short one.
    longer = ["Robert Smith " * 40] * 200
    assert compute_token_aware_batch_size(
        CLAUDE3, longer, "llm_grounded", 25
    ) <= compute_token_aware_batch_size(CLAUDE3, short, "llm_grounded", 25)


@pytest.mark.unit
def test_column_count_scans_every_row_not_a_window():
    """There is no sampling window. A window would move the row-0 bug to the first N
    rows: an 800-row statement whose first 25 rows are narrow would still be sized as
    if the whole list were narrow."""
    rows = [{"Date": "x"}] * 25 + [_row(10)] * 775
    assert count_row_columns(rows) == 10
    # Sized for 10 columns (12), not for 1 column (which would reach the ceiling).
    assert compute_token_aware_batch_size(NOVA_LITE, rows, "ocr_only", 25) == 12


@pytest.mark.unit
def test_non_dict_rows_report_no_countable_columns():
    assert count_row_columns(["a", "b"]) is None
    assert count_row_columns(None) is None
    assert count_row_columns([]) is None


# ---------------------------------------------------------------------------
# The configured value is a ceiling, and 0 means "no ceiling"
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_configured_value_is_a_ceiling_never_a_target():
    """A ceiling BELOW the derived size wins; the derivation never grows past it."""
    rows = [_row(3) for _ in range(40)]
    # ocr_only 3-column derives 41, which is above the shipped ceiling of 25, so the
    # ceiling binds and the batch stays at 25 rather than growing.
    assert confidence_rows_per_call(10_000, 3, "ocr_only") == 41
    assert compute_token_aware_batch_size(NOVA_LITE, rows, "ocr_only", 25) == 25
    # With bounding boxes the derivation (13) is below the ceiling and wins.
    assert compute_token_aware_batch_size(NOVA_LITE, rows, "llm_grounded", 25) == 13


@pytest.mark.unit
def test_explicit_ceiling_above_the_reliability_cap_is_honoured():
    """``_ABS_MAX_LIST_BATCH`` bounds what the SYSTEM picks when auto-deriving, not
    what an operator asked for. Clamping a deliberate pin of 75 down to 50 would be
    a silent regression against the behaviour earlier releases shipped."""
    rows = [_row(1) for _ in range(200)]
    assert compute_token_aware_batch_size(SONNET5_1M, rows, "ocr_only", 75) == 75


@pytest.mark.unit
def test_no_ceiling_falls_back_to_the_reliability_cap():
    """With no ceiling supplied the reliability cap is the only bound."""
    rows = [_row(1) for _ in range(200)]
    assert (
        compute_token_aware_batch_size(SONNET5_1M, rows, "ocr_only", 0)
        == _ABS_MAX_LIST_BATCH
    )


@pytest.mark.unit
@pytest.mark.parametrize("ceiling", [0, 1, 25, 50, 1000])
def test_result_is_always_at_least_one(ceiling):
    """A zero batch size would make the caller's row slicing step by zero."""
    rows = [_row(30) for _ in range(5)]
    assert compute_token_aware_batch_size(NOVA_LITE, rows, "llm_grounded", ceiling) >= 1


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cols", "geometry"),
    [
        (130, "ocr_only"),  # 130 x 40 = 5,200 > 10,000 x 0.5
        (42, "llm_grounded"),  # 42 x 40 x 3 = 5,040 > 10,000 x 0.5
        (400, "ocr_only"),
    ],
)
def test_floor_engages_when_a_single_row_exceeds_the_output_budget(cols, geometry):
    """The ``max(_MIN_LIST_BATCH, ...)`` floor is only reachable when ONE row costs
    more than the whole output budget, which needs ~42 columns with bounding boxes or
    ~126 without. Narrower rows derive >= 1 anyway, so a test using them cannot fail
    if the floor is deleted — verified by mutation. Extracted rows this wide exist
    (wide forms, multi-instance records), so this is the case that pins it."""
    rows = [_row(cols) for _ in range(4)]
    raw = int(10_000 * 0.5 // confidence_per_row_tokens(cols, geometry))
    assert raw == 0, "test input no longer exercises the floor"
    assert compute_token_aware_batch_size(NOVA_LITE, rows, geometry, 25) == 1


@pytest.mark.unit
def test_ceiling_of_one_is_respected():
    assert compute_token_aware_batch_size(SONNET5_1M, [_row(2)], "ocr_only", 1) == 1


# ---------------------------------------------------------------------------
# Unknown model / unmeasurable rows must be conservative, not permissive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unknown_model_sizes_conservatively_rather_than_trusting_the_ceiling():
    """Previously an unresolvable output cap returned ``configured_batch_size``
    unchanged, which is how a permissive default reached a small-cap model. It must
    now derive from the conservative fallback cap instead."""
    rows = [_row(3) for _ in range(40)]
    result = compute_token_aware_batch_size(
        "some.unknown.model-v9:0", rows, "llm_grounded", 25
    )
    assert result < 25
    assert result >= 1


@pytest.mark.unit
def test_missing_model_id_sizes_conservatively():
    rows = [_row(3) for _ in range(40)]
    assert compute_token_aware_batch_size(None, rows, "llm_grounded", 25) < 25
    assert compute_token_aware_batch_size("", rows, "llm_grounded", 25) < 25


@pytest.mark.unit
def test_no_rows_still_yields_a_usable_size():
    """``assess_results_batched`` sizes before it knows there are rows; a 0 here
    would make the row slicing step by zero."""
    assert compute_token_aware_batch_size(NOVA_LITE, None, "llm_grounded", 0) >= 1


# ---------------------------------------------------------------------------
# Model comparison
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bigger_output_cap_allows_a_bigger_batch():
    rows = [_row(8) for _ in range(40)]
    small = compute_token_aware_batch_size(CLAUDE3, rows, "llm_grounded", 0)
    large = compute_token_aware_batch_size(SONNET5_1M, rows, "llm_grounded", 0)
    assert large > small


@pytest.mark.unit
def test_reliability_ceiling_binds_on_a_huge_output_model():
    """The absolute ceiling is retained deliberately: models under-enumerate very
    long lists regardless of whether the tokens fit."""
    rows = [_row(2) for _ in range(200)]
    assert (
        compute_token_aware_batch_size(SONNET5_1M, rows, "ocr_only", 0)
        == _ABS_MAX_LIST_BATCH
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
