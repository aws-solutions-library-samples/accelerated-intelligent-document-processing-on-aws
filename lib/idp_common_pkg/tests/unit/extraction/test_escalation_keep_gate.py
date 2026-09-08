# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The escalation keep/discard gate (#791).

``fail_action: escalate`` re-extracts the failing fields with a stronger model and
then has to decide whether the new result should REPLACE the original. The old
gate was ``esc.valid or len(esc.errors) < len(full.errors)`` — a comparison of
total error counts. Per-row errors scale with the row count while a whole-field
error is always one, so that comparison systematically favours the result with
LESS data. Measured: 100 rows with one unreadable cell each produce 100
``required`` errors; the same field returned as ``null`` produces 1. The old gate
kept the null and logged an improvement from 100 to 1.

These tests pin the replacement: never keep a result that loses populated data,
and compare errors per field rather than in total.
"""

from __future__ import annotations

import pytest

from idp_common.extraction.validation import (
    escalation_data_loss,
    escalation_outcome,
    validate_extraction,
)

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "AccountNumber": {"type": "string"},
        "Transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Date": {"type": "string", "format": "date"},
                    "Description": {"type": "string"},
                    "Amount": {"type": "number"},
                },
                "required": ["Date", "Description", "Amount"],
            },
        },
    },
    "required": ["AccountNumber", "Transactions"],
}


def _rows(n: int, amount=12.5) -> list[dict]:
    return [
        {
            "Date": f"2024-01-{i % 28 + 1:02d}",
            "Description": f"SEQ{i:05d}",
            "Amount": amount,
        }
        for i in range(n)
    ]


def _doc(rows, account="123") -> dict:
    return {"AccountNumber": account, "Transactions": rows}


def _decide(original, escalated):
    before = validate_extraction(original, SCHEMA)
    after = validate_extraction(escalated, SCHEMA)
    return escalation_outcome(original, escalated, before, after), before, after


# ---------------------------------------------------------------------------
# The two hazards from #791, exactly as reproduced
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_nulling_a_100_row_list_is_rejected_even_though_total_errors_drop_100_to_1():
    original = _doc(_rows(100, amount=None))  # 100 abstained cells
    escalated = _doc(None)  # the stronger model nulled the whole list

    (keep, reason), before, after = _decide(original, escalated)

    # This is the arithmetic that fooled the old gate.
    assert len(before.errors) == 100
    assert len(after.errors) == 1
    assert keep is False
    assert "lost populated data" in reason
    assert "had 100 rows" in reason


@pytest.mark.unit
def test_a_valid_but_shorter_list_is_rejected():
    """``after.valid`` alone used to be sufficient. A clean 50 rows where the
    original had 100 is a truncation, not a fix."""
    original = _doc(_rows(100, amount=None))
    escalated = _doc(_rows(50))

    (keep, reason), _, after = _decide(original, escalated)
    assert after.valid is True
    assert keep is False
    assert "had 100 rows, escalation returned 50" in reason


# ---------------------------------------------------------------------------
# What a good escalation looks like
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filling_in_the_abstained_cells_is_kept():
    original = _doc(_rows(100, amount=None))
    escalated = _doc(_rows(100))
    (keep, reason), _, after = _decide(original, escalated)
    assert after.valid is True
    assert keep is True


@pytest.mark.unit
def test_a_partial_fix_with_the_same_row_count_is_kept():
    """40 of 100 cells still unreadable: fewer errors on the same field, no rows
    lost — a genuine improvement."""
    original = _doc(_rows(100, amount=None))
    fixed = _rows(100)
    for row in fixed[:40]:
        row["Amount"] = None
    escalated = _doc(fixed)

    (keep, reason), before, after = _decide(original, escalated)
    assert (len(before.errors), len(after.errors)) == (100, 40)
    assert keep is True
    assert "improved: Transactions" in reason


@pytest.mark.unit
def test_a_longer_list_is_not_data_loss():
    """The stronger model finding rows the weaker one missed is the point."""
    original = _doc(_rows(90, amount=None))
    escalated = _doc(_rows(100))
    (keep, _), _, _ = _decide(original, escalated)
    assert keep is True


# ---------------------------------------------------------------------------
# Per-field comparison
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fixing_one_field_while_breaking_another_is_rejected():
    """Totals can hide this: -3 on one field and +1 on another nets -2 and the old
    gate would have kept it."""
    original = {"AccountNumber": "123", "Transactions": _rows(3, amount=None)}
    escalated = {"AccountNumber": None, "Transactions": _rows(3)}
    (keep, reason), before, after = _decide(original, escalated)
    assert len(after.errors) < len(before.errors)  # the total DID drop
    assert keep is False
    # Caught by the data-loss rule first: a populated scalar came back null.
    assert "AccountNumber" in reason


@pytest.mark.unit
def test_no_change_visible_to_validation_is_rejected():
    original = _doc(_rows(5, amount=None))
    (keep, reason), _, _ = _decide(original, dict(original))
    assert keep is False
    assert "changed nothing" in reason


@pytest.mark.unit
def test_worse_on_a_field_without_data_loss_is_rejected():
    """Same rows, but the escalation broke the date format on every row while
    fixing the amounts — more errors on Transactions than before."""
    original = _doc(_rows(3, amount=None))
    broken = _rows(3)
    for row in broken:
        row["Date"] = "not-a-date"
        row["Description"] = None  # adds a required error per row
    escalated = _doc(broken)
    (keep, reason), before, after = _decide(original, escalated)
    assert (
        after.errors_by_field()["Transactions"]
        > before.errors_by_field()["Transactions"]
    )
    assert keep is False
    assert "worse" in reason


# ---------------------------------------------------------------------------
# escalation_data_loss in isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("before", "after", "lost"),
    [
        ({"L": [1, 2, 3]}, {"L": None}, True),
        ({"L": [1, 2, 3]}, {}, True),
        ({"L": [1, 2, 3]}, {"L": [1, 2]}, True),
        ({"L": [1, 2, 3]}, {"L": [1, 2, 3]}, False),
        ({"L": [1, 2, 3]}, {"L": [1, 2, 3, 4]}, False),
        ({"L": []}, {"L": None}, False),  # nothing to lose
        ({"L": None}, {"L": None}, False),
        ({"S": "x"}, {"S": None}, True),
        ({"S": "x"}, {}, True),
        ({"S": "x"}, {"S": "y"}, False),  # a correction is not a loss
        ({"S": None}, {"S": None}, False),
        ({"G": {"a": 1}}, {"G": None}, True),  # a group is a value too
        ({"G": {"a": 1}}, {"G": {"a": 2}}, False),
    ],
)
def test_escalation_data_loss_cases(before, after, lost):
    assert bool(escalation_data_loss(before, after)) is lost


@pytest.mark.unit
def test_data_loss_is_reported_per_field():
    lost = escalation_data_loss(
        {"A": [1, 2], "B": "x", "C": "kept"}, {"A": None, "B": None, "C": "kept"}
    )
    assert len(lost) == 2
    assert any(entry.startswith("A:") for entry in lost)
    assert any(entry.startswith("B:") for entry in lost)


# ---------------------------------------------------------------------------
# errors_by_field / FieldError.field
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_errors_are_attributed_to_their_top_level_field():
    report = validate_extraction(
        {"AccountNumber": None, "Transactions": _rows(2, amount=None)}, SCHEMA
    )
    assert report.errors_by_field() == {"AccountNumber": 1, "Transactions": 2}
    assert all(e.field in {"AccountNumber", "Transactions"} for e in report.errors)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
