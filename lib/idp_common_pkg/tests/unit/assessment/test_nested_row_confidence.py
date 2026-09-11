# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A list row containing a nested group or an inner list is not "unscored".

``_row_confidence_missing`` used to look exactly one level down::

    leaves = [v for v in row_assess.values() if isinstance(v, dict)]
    return any(leaf.get("confidence") is None for leaf in leaves)

so a nested GROUP inside a row was mistaken for a confidence leaf — a group has
no ``confidence`` key, so ``.get("confidence")`` was None and the row was
reported unscored *however well the model had scored it*. An inner LIST was
skipped by the ``isinstance(v, dict)`` filter entirely.

Measured live (stack IDPMulti, 2026-09-03) on a 3-record pay statement whose rows
carry an ``Employee`` group and an ``Earnings`` list: every leaf came back at
0.99–1.0 with OCR geometry and ``truncated_calls: 0``, and the section still
reported ``assessment_incomplete`` (**error** — Incomplete in the UI) for all 3
rows after burning a ``claude-sonnet-5:1m`` escalation call that recovered 0,
because a stronger model reproduces the identical shape. The ladder had no way
out.

Pre-existing for any list-of-object attribute whose rows contain a group or an
inner list; multi-instance sections (GitHub #715) make every record a row, so it
became universal there.
"""

from __future__ import annotations

import pytest

from idp_common.assessment.batching import (
    _missing_row_indices,
    _row_confidence_missing,
    build_assessment_issues,
)

pytestmark = pytest.mark.unit


def _leaf(confidence=0.99):
    return {"confidence": confidence, "confidence_threshold": 0.8}


# The exact shape captured from the live run.
LIVE_SCORED_ROW = {
    "CheckNumber": _leaf(0.99),
    "PayDate": _leaf(1.0),
    "NetPay": _leaf(0.99),
    "Employee": {"Name": _leaf(0.99), "EmployeeId": _leaf(0.99)},
    "Earnings": [
        {"Description": _leaf(0.99), "Current": _leaf(0.99)},
        {"Description": _leaf(0.99), "Current": _leaf(0.99)},
    ],
}


def test_a_fully_scored_nested_row_is_not_missing():
    assert _row_confidence_missing(LIVE_SCORED_ROW) is False


def test_a_row_whose_only_unscored_leaf_is_nested_is_still_missing():
    """The retry/escalation ladder must still fire for a genuinely partial row —
    the fix must not turn the check into "anything goes"."""
    row = {
        **LIVE_SCORED_ROW,
        "Employee": {"Name": _leaf(None), "EmployeeId": _leaf(0.9)},
    }
    assert _row_confidence_missing(row) is True


def test_an_unscored_leaf_inside_an_inner_list_row_is_detected():
    row = {
        **LIVE_SCORED_ROW,
        "Earnings": [{"Description": _leaf(0.9), "Current": _leaf(None)}],
    }
    assert _row_confidence_missing(row) is True


def test_a_scored_flat_row_is_not_missing():
    assert _row_confidence_missing({"a": _leaf(0.9), "b": _leaf(0.8)}) is False


def test_a_partially_scored_flat_row_is_missing():
    assert _row_confidence_missing({"a": _leaf(0.9), "b": _leaf(None)}) is True


def test_the_reconciliation_placeholder_is_missing():
    """`reconcile_assessment_to_data` pads with a null-confidence leaf; that is
    the case this predicate exists to find."""
    assert (
        _row_confidence_missing(
            {"confidence": None, "confidence_reason": "Not individually assessed."}
        )
        is True
    )
    assert _row_confidence_missing({"date": _leaf(None), "amount": _leaf(None)}) is True


def test_a_per_row_scalar_confidence_is_honoured():
    assert _row_confidence_missing({"confidence": 0.9}) is False


@pytest.mark.parametrize("row", [{}, None, "text", 3, [], [{"confidence": 0.9}]])
def test_rows_with_no_confidence_leaf_at_all_are_missing(row):
    assert _row_confidence_missing(row) is True


def test_missing_row_indices_over_the_live_payload():
    """The end-to-end symptom: 3 fully-scored records reported as 3 unscored
    rows."""
    assessment = [LIVE_SCORED_ROW, LIVE_SCORED_ROW, LIVE_SCORED_ROW]
    data = [{"CheckNumber": "1"}, {"CheckNumber": "2"}, {"CheckNumber": "3"}]
    assert _missing_row_indices(assessment, data) == []


def test_no_false_assessment_incomplete_error_for_a_scored_nested_section():
    """What the user actually saw: an ERROR-severity issue, which renders the
    section Incomplete in the UI, on a section whose confidence was perfect."""
    stats = {
        "truncated_calls": 0,
        "splits": 0,
        "rows_recovered_by_retry": 0,
        "rows_recovered_by_escalation": 0,
        "unrecoverable_rows": 0,
        "escalation_model": "us.anthropic.claude-sonnet-5:1m",
        "schema_mismatch_fields": [],
    }
    assert build_assessment_issues(stats, section_id="1") == []


def test_a_genuinely_incomplete_section_still_errors():
    """Contrast with the test above — the guard must not be disarmed."""
    stats = {
        "truncated_calls": 2,
        "splits": 1,
        "rows_recovered_by_retry": 0,
        "rows_recovered_by_escalation": 0,
        "unrecoverable_rows": 3,
        "escalation_model": "us.anthropic.claude-sonnet-5:1m",
        "schema_mismatch_fields": [],
    }
    issues = build_assessment_issues(stats, section_id="1")
    assert [i.code for i in issues] == ["assessment_incomplete"]
    assert issues[0].severity == "error"
