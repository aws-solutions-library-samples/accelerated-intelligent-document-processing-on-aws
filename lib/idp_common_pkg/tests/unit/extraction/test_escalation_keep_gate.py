# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The escalation keep/discard gate (#791).

``fail_action: escalate`` re-extracts the failing fields with a stronger model and
then has to decide whether the new result should REPLACE the original. The old
gate was ``esc.valid or len(esc.errors) < len(full.errors)`` — a comparison of
total error counts. Per-row errors scale with the row count while a whole-field
error is always one, so that comparison systematically favours the result with
LESS data: 100 rows with one unreadable cell each produce 100 ``required`` errors;
the same field returned as ``null`` produces 1. The old gate kept the null and
logged an improvement from 100 to 1. And ``valid`` alone kept a clean 50-row
result where the original had 100.

The replacement: each field is accepted on its own merits — it must not lose
populated data (unless the original value was itself present-but-invalid, so that
removal IS the fix) and must have fewer errors than before.
"""

from __future__ import annotations

import pytest

from idp_common.extraction.validation import (
    escalation_data_loss,
    escalation_outcome,
    select_escalated_fields,
    validate_extraction,
)

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "AccountNumber": {"type": "string"},
        "Status": {"type": "string", "enum": ["open", "closed"]},
        "Payments": {"type": "array", "maxItems": 12, "items": {"type": "number"}},
        "Tags": {"type": "array", "uniqueItems": True, "items": {"type": "string"}},
        "Borrower": {
            "type": "object",
            "properties": {
                "Name": {"type": "string"},
                "Accounts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["Name"],
        },
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


def _doc(rows, **extra) -> dict:
    d = {"AccountNumber": "123", "Transactions": rows}
    d.update(extra)
    return d


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
    assert (len(before.errors), len(after.errors)) == (
        100,
        1,
    )  # what fooled the old gate
    assert keep is False
    assert "had 100 populated rows" in reason


@pytest.mark.unit
def test_a_valid_but_shorter_list_is_rejected():
    """``after.valid`` alone used to be sufficient. 50 clean rows where the original
    had 100 is a truncation, not a fix — the original's errors were all `required`
    (data MISSING), and removing more data never fixes missing data."""
    original = _doc(_rows(100, amount=None))
    escalated = _doc(_rows(50))
    (keep, reason), _, after = _decide(original, escalated)
    assert after.valid is True
    assert keep is False
    assert "had 100 populated rows, escalation returned 50" in reason


# ---------------------------------------------------------------------------
# Reductions that ARE the fix must be allowed (review of the first draft found
# these were all vetoed, and the first is a regression against the old gate)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retracting_a_hallucinated_enum_value_to_null_is_kept():
    """The commonest scalar failure: the model invented a value the document does
    not contain. The correct answer is null. The original was PRESENT BUT WRONG
    (an `enum` error, not `required`), so the retraction is a correction."""
    original = _doc(_rows(1), Status="pending")  # not in the enum
    escalated = _doc(_rows(1), Status=None)
    (keep, reason), before, after = _decide(original, escalated)
    assert before.errors_by_field()["Status"] == 1 and after.valid
    assert keep is True
    assert "Status accepted" in reason


@pytest.mark.unit
def test_trimming_to_maxitems_is_kept():
    original = _doc(_rows(1), Payments=[1.0] * 15)
    escalated = _doc(_rows(1), Payments=[1.0] * 12)
    (keep, reason), _, after = _decide(original, escalated)
    assert after.valid and keep is True


@pytest.mark.unit
def test_deduplicating_for_uniqueitems_is_kept():
    original = _doc(_rows(1), Tags=["a", "a", "b"])
    escalated = _doc(_rows(1), Tags=["a", "b"])
    (keep, _), _, after = _decide(original, escalated)
    assert after.valid and keep is True


@pytest.mark.unit
def test_collapsing_all_null_placeholder_rows_is_kept():
    """Five empty rows carry nothing; replacing them with two real rows loses no
    populated data even though the row count falls."""
    original = _doc([{"Date": None, "Description": None, "Amount": None}] * 5)
    escalated = _doc(_rows(2))
    (keep, _), before, after = _decide(original, escalated)
    assert len(before.errors) == 15 and after.valid
    assert keep is True


@pytest.mark.unit
def test_shortening_a_list_whose_only_errors_were_required_is_still_rejected():
    """The exception is narrow: only `maxItems`/`uniqueItems` justify dropping
    rows. A field with only `required` errors may not shrink."""
    original = _doc(_rows(10, amount=None))
    escalated = _doc(_rows(9))
    (keep, _), _, _ = _decide(original, escalated)
    assert keep is False


# ---------------------------------------------------------------------------
# What a good escalation looks like
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_filling_in_the_abstained_cells_is_kept():
    (keep, _), _, after = _decide(_doc(_rows(100, amount=None)), _doc(_rows(100)))
    assert after.valid and keep is True


@pytest.mark.unit
def test_a_partial_fix_with_the_same_row_count_is_kept():
    original = _doc(_rows(100, amount=None))
    fixed = _rows(100)
    for row in fixed[:40]:
        row["Amount"] = None
    (keep, reason), before, after = _decide(original, _doc(fixed))
    assert (len(before.errors), len(after.errors)) == (100, 40)
    assert keep is True
    assert "Transactions accepted: errors fell 100 -> 40" in reason


@pytest.mark.unit
def test_a_longer_list_is_not_data_loss():
    (keep, _), _, _ = _decide(_doc(_rows(90, amount=None)), _doc(_rows(100)))
    assert keep is True


# ---------------------------------------------------------------------------
# Per-field acceptance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_one_bad_field_does_not_sink_the_good_ones():
    """Five failing fields; the stronger model fixes four and nulls a fifth that
    had a value. All-or-nothing would discard everything. Per-field keeps four."""
    original = _doc(
        _rows(3, amount=None), Status="bogus", Payments=[1.0] * 15, Tags=["x", "x"]
    )
    original["Borrower"] = {"Name": None, "Accounts": ["A1", "A2"]}
    escalated = _doc(_rows(3), Status="open", Payments=[1.0] * 12, Tags=["x"])
    escalated["Borrower"] = {"Name": "Jon", "Accounts": None}  # nested list destroyed

    before = validate_extraction(original, SCHEMA)
    after = validate_extraction(escalated, SCHEMA)
    merged, decisions = select_escalated_fields(original, escalated, before, after)

    accepted = {k for k, v in decisions.items() if v.startswith("accepted")}
    assert accepted == {"Transactions", "Status", "Payments", "Tags"}
    assert decisions["Borrower"].startswith("rejected")
    assert merged["Borrower"] == original["Borrower"]  # kept the original
    assert merged["Transactions"] == escalated["Transactions"]
    assert validate_extraction(merged, SCHEMA).errors_by_field() == {"Borrower": 1}


@pytest.mark.unit
def test_fixing_one_field_while_nulling_a_clean_one_keeps_only_the_fix():
    """Totals can hide this (-3 and +1 nets -2). The clean field that came back
    null is rejected on data loss; the fix is accepted."""
    original = _doc(_rows(3, amount=None))
    escalated = {"AccountNumber": None, "Transactions": _rows(3)}
    before = validate_extraction(original, SCHEMA)
    after = validate_extraction(escalated, SCHEMA)
    merged, decisions = select_escalated_fields(original, escalated, before, after)
    assert decisions["AccountNumber"].startswith(
        "rejected: AccountNumber: had 1 populated"
    )
    assert decisions["Transactions"].startswith("accepted")
    assert merged["AccountNumber"] == "123"
    assert validate_extraction(merged, SCHEMA).valid


@pytest.mark.unit
def test_nested_loss_inside_a_group_is_detected():
    """Leaf counting recurses even though only top-level fields are compared."""
    original = _doc(_rows(1))
    original["Borrower"] = {"Name": None, "Accounts": ["A1", "A2", "A3"]}
    escalated = _doc(_rows(1))
    escalated["Borrower"] = {"Name": "Jon", "Accounts": None}
    (keep, reason), _, after = _decide(original, escalated)
    assert after.valid  # the old `valid` gate would have kept this
    assert keep is False and "Borrower" in reason


@pytest.mark.unit
def test_emptying_every_row_while_keeping_the_row_count_is_loss():
    original = _doc(_rows(100, amount=None))
    escalated = _doc([{"Date": None, "Description": None, "Amount": None}] * 100)
    (keep, _), _, _ = _decide(original, escalated)
    assert keep is False


@pytest.mark.unit
def test_no_change_visible_to_validation_is_rejected():
    original = _doc(_rows(5, amount=None))
    (keep, reason), _, _ = _decide(original, dict(original))
    assert keep is False and "changed nothing" in reason


@pytest.mark.unit
def test_worse_on_a_field_is_rejected():
    original = _doc(_rows(3, amount=None))
    broken = _rows(3)
    for row in broken:
        row["Date"] = "not-a-date"
        row["Description"] = None
    (keep, reason), _, _ = _decide(original, _doc(broken))
    assert keep is False and "errors rose" in reason


@pytest.mark.unit
def test_a_new_root_level_error_kind_rejects_everything():
    """Root errors are unattributable, so a NEW kind at root — a hallucinated key
    under additionalProperties: false — must not hide behind a root error that
    went away."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"Owner's Name": {"type": "string"}, "Amt": {"type": "number"}},
        "required": ["Owner's Name", "Amt"],
    }
    original = {"Owner's Name": None, "Amt": None}
    escalated = {"Owner's Name": "Jon", "Amt": 5.0, "Hallucinated": "oops"}
    before = validate_extraction(original, schema)
    after = validate_extraction(escalated, schema)
    _, decisions = select_escalated_fields(original, escalated, before, after)
    assert all(
        v.startswith("rejected: escalation introduced root-level")
        for v in decisions.values()
    )


@pytest.mark.unit
def test_required_property_names_with_apostrophes_are_attributed():
    """jsonschema formats the name with repr(), which switches to double quotes
    when the name contains an apostrophe. The attribution regex must cope, or the
    error lands in the root bucket and per-field logic misjudges."""
    schema = {
        "type": "object",
        "properties": {"Owner's Name": {"type": "string"}},
        "required": ["Owner's Name"],
    }
    report = validate_extraction({}, schema)
    assert report.errors[0].field == "Owner's Name"
    assert report.failed_top_level_fields == {"Owner's Name"}


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
        ({"L": []}, {"L": None}, False),
        ({"L": [None, None]}, {"L": None}, False),  # placeholders carry nothing
        ({"L": None}, {"L": None}, False),
        ({"S": "x"}, {"S": None}, True),
        ({"S": "x"}, {}, True),
        ({"S": "x"}, {"S": "y"}, False),  # a correction is not a loss
        ({"S": "  "}, {"S": None}, False),  # blank is already unpopulated
        ({"G": {"a": 1}}, {"G": None}, True),
        ({"G": {"a": 1}}, {"G": {"a": 2}}, False),
        ({"G": {"a": 1, "b": [1, 2]}}, {"G": {"a": 1, "b": None}}, True),  # nested
    ],
)
def test_escalation_data_loss_cases(before, after, lost):
    assert bool(escalation_data_loss(before, after)) is lost


@pytest.mark.unit
def test_metadata_carries_the_per_field_counts_the_decision_used():
    report = validate_extraction(
        {"AccountNumber": None, "Transactions": _rows(2, amount=None)}, SCHEMA
    )
    assert report.to_metadata()["errors_by_field"] == {
        "AccountNumber": 1,
        "Transactions": 2,
    }


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
