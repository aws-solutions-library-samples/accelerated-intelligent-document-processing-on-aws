# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence-alert dedupe at the batching merge (``idp_common.assessment.batching``).

Every slice is assessed with the SAME scalars/context, so a non-list attribute is
re-assessed once per slice and re-alerts once per slice. The merge already treats
those repeats as redundant for the assessment — it keeps the first slice's scalars
and discards the rest — but the alert list was a plain ``extend``, so the same
finding landed once per slice. Measured on a 512-row workbook: 2,603 alerts over
16 distinct group paths (~163 copies each), the dominant share of a tracking item
that breached DynamoDB's 409,600-byte ceiling. The item write is where a document
is lost, so these tests lock in the collapse AND the properties that make it safe:
no finding is dropped, and indexed paths — whose row indexes are slice-local and
can collide across slices — are never touched.
"""

from __future__ import annotations

import pytest

from idp_common.assessment.batching import dedupe_alerts

pytestmark = pytest.mark.unit


def _alert(name: str, confidence: float, threshold: float = 0.8) -> dict:
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def test_repeats_of_one_scalar_path_collapse_to_one():
    """163 slices re-alerting the same group path is one finding, not 163."""
    alerts = [_alert("totals.total_overhead", 0.4) for _ in range(163)]

    assert dedupe_alerts(alerts) == [_alert("totals.total_overhead", 0.4)]


def test_the_measured_shape_collapses_to_its_distinct_paths():
    """The corpus case: 16 distinct paths x ~163 copies -> 16 alerts."""
    names = [f"totals.field_{i}" for i in range(16)]
    alerts = [_alert(n, 0.5) for _ in range(163) for n in names]

    kept = dedupe_alerts(alerts)

    assert len(kept) == 16
    assert [a["attribute_name"] for a in kept] == names


def test_indexed_paths_are_never_collapsed_even_when_identical():
    """Row indexes in batched-mode alerts are LOCAL to the slice that produced
    them, so two slices can emit the same indexed path for different rows.
    Collapsing them would delete a genuine finding — identical copies included."""
    alerts = [
        _alert("cost_elements[3].unit_cost", 0.6),  # slice 1's row 3
        _alert("cost_elements[3].unit_cost", 0.6),  # slice 2's row 3 - different row
        _alert("cost_elements[3].unit_cost", 0.2),
    ]

    assert dedupe_alerts(alerts) == alerts


def test_per_row_alerts_all_survive_because_their_paths_are_indexed():
    alerts = [_alert(f"cost_elements[{i}].unit_cost", 0.6) for i in range(512)]

    assert dedupe_alerts(alerts) == alerts


def test_disagreeing_copies_keep_the_lowest_confidence():
    """The alert claims 'scored below threshold'; the worst score is the
    strongest true form of that claim, and picking it is order-independent."""
    forward = dedupe_alerts(
        [
            _alert("totals.piece_price", 0.7),
            _alert("totals.piece_price", 0.2),
            _alert("totals.piece_price", 0.5),
        ]
    )
    backward = dedupe_alerts(
        [
            _alert("totals.piece_price", 0.5),
            _alert("totals.piece_price", 0.2),
            _alert("totals.piece_price", 0.7),
        ]
    )

    assert forward == backward == [_alert("totals.piece_price", 0.2)]


def test_a_scoreless_copy_never_displaces_a_scored_one():
    """A missing/non-numeric confidence coerces to 1.0 in the comparison, so an
    alert that carries no score keeps its slot only if it arrived first — and a
    scored copy takes over from it."""
    scoreless_first = dedupe_alerts(
        [{"attribute_name": "totals.piece_price"}, _alert("totals.piece_price", 0.7)]
    )
    assert scoreless_first == [_alert("totals.piece_price", 0.7)]

    scored_first = dedupe_alerts(
        [_alert("totals.piece_price", 0.7), {"attribute_name": "totals.piece_price"}]
    )
    assert scored_first == [_alert("totals.piece_price", 0.7)]


def test_first_appearance_order_is_preserved():
    """A reordering would make the tracking item churn between identical runs."""
    alerts = [
        _alert("part_number", 0.3),
        _alert("totals.total_overhead", 0.4),
        _alert("part_number", 0.1),
        _alert("supplier_name", 0.5),
    ]

    kept = dedupe_alerts(alerts)

    assert [a["attribute_name"] for a in kept] == [
        "part_number",
        "totals.total_overhead",
        "supplier_name",
    ]
    assert kept[0]["confidence"] == 0.1  # lowest copy won, in first slot


def test_unkeyable_entries_pass_through():
    """An alert we cannot key is an alert we must not drop."""
    alerts = [
        "not-a-dict",
        {"confidence": 0.4},  # no attribute_name
        {"attribute_name": None, "confidence": 0.4},
        _alert("totals.piece_price", 0.7),
        _alert("totals.piece_price", 0.2),
    ]

    kept = dedupe_alerts(alerts)

    assert kept[:3] == alerts[:3]
    assert kept[3:] == [_alert("totals.piece_price", 0.2)]


def test_non_list_input_passes_through():
    assert dedupe_alerts(None) is None
    assert dedupe_alerts({"attribute_name": "x"}) == {"attribute_name": "x"}


def test_idempotent():
    alerts = [
        _alert("totals.piece_price", 0.7),
        _alert("totals.piece_price", 0.2),
        _alert("cost_elements[3].unit_cost", 0.6),
        _alert("cost_elements[3].unit_cost", 0.6),
        "not-a-dict",
    ]

    once = dedupe_alerts(alerts)

    assert dedupe_alerts(once) == once
