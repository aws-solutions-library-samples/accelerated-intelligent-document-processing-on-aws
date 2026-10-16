# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A confidence is a probability, and nothing in the harness checked that (#1395).

Four releases' committed grids carry a ``mean_confidence`` above 1.0 — 21 cells over
``v0.6.8/core``, ``v0.6.8/core__extraction-model-sonnet5``, ``v0.6.9/core`` and
``v0.6.11/core``, the largest 14.1851 — and the published configuration guidance quotes
the per-cell roll-up of those rows. The arithmetic says what happened: every one of the
21 rows reports ``n_conf_leaves: 15`` and ``pct_conf_below_0.9: 0.0``, so all fifteen
leaves were at or above 0.9, and a mean of 7.585 over fifteen such leaves requires one
of them to be 100 — a percentage averaged with fourteen fractions.
:func:`test_the_1395_row_shape_is_reproduced_exactly` rebuilds that multiset and gets
7.585 / 0.0 / 15 back, which is the triple the artifact carries.

What this suite pins is the control that was missing, not the symptom:

1. The range is checked at the ONE place every harness confidence passes through
   (``lib.walk_confidence``), so a statistic added later inherits it rather than having
   to remember.
2. It RAISES. A clamp is what would have hidden this — 100 clamped to 1.0 produces a
   plausible 0.99 that no scan can find — so the check must refuse to produce a figure
   rather than produce a tidier one.
3. It is checked BEFORE the mean, the percentage and the bin index are computed, which
   is observable on the two inputs that survive the arithmetic unflagged: ``NaN``
   (``round(nan, 4)`` is ``nan``, so a check placed after the write would publish it)
   and a non-numeric leaf (which would raise ``TypeError`` from ``sum`` instead of
   saying what is wrong).
4. A raise costs the grid ONE row, not the run: ``aggregate.score_all`` and
   ``aggregate.score_reference_run`` already convert a per-document exception into a
   ``SCORE_ERROR`` row carrying the message.

The committed grids are deliberately NOT asserted over here. Whether a historical
artifact is corrected is a separate decision from whether the harness can produce the
defect again, and a suite that asserted over them would fail on data this change does
not touch.
"""

from __future__ import annotations

import json
import os

import pytest
from harness_import import harness_module

lib = harness_module("lib")
analyze = harness_module("analyze")
aggregate = harness_module("aggregate")


# The exact multiset behind six of the 21 committed cells: fourteen leaves in [0.9, 1.0]
# summing to 13.775, plus one leaf of 100.
ROW_1395_LEAVES = [0.9839] * 12 + [0.9841] * 2 + [100.0]


def _leaf(confidence):
    return {"confidence": confidence, "confidence_threshold": 0.8}


def _section(confidences, *, field="Amount"):
    """One section ``result.json`` as the harness reads it off S3."""
    return {
        "inference_result": {
            "Transactions": [
                {"Description": f"SEQ{i:05d} AnyCompany Store", field: float(i)}
                for i, _c in enumerate(confidences)
            ]
        },
        "explainability_info": [
            {"Transactions": [{field: _leaf(c)} for c in confidences]}
        ],
    }


# --------------------------------------------------------------------------- #
# The reproduction
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheDefectIsReproduced:
    def test_the_1395_row_shape_is_reproduced_exactly(self):
        """One percentage among fourteen fractions, and the three published figures.

        Not an approximation of the artifact: ``mean_confidence`` 7.585,
        ``pct_conf_below_0.9`` 0.0 and ``n_conf_leaves`` 15 are the exact triple on
        six of the 21 committed cells. The percentage reads 0.0 because 100 is not
        below 0.9 either — the companion statistic agrees with the impossible mean,
        which is why neither looked wrong on its own.
        """
        assert len(ROW_1395_LEAVES) == 15
        assert round(sum(ROW_1395_LEAVES) / len(ROW_1395_LEAVES), 4) == 7.585
        assert sum(1 for c in ROW_1395_LEAVES if c < 0.9) == 0

    def test_a_single_percentage_leaf_is_what_it_takes(self):
        """The inflation is one leaf, not a systemic rescaling.

        14.1851 over fifteen leaves needs two of them at 100; 7.585 needs one. Both
        land far outside [0, 1], which is why they were visible at all — the same
        mismatch on a 2,000-leaf row dilutes to below 1.0 and is not.
        """
        one = [0.9839] * 12 + [0.9841] * 2 + [100.0]
        two = [0.9828] * 13 + [100.0, 100.0]
        assert round(sum(one) / 15, 4) == 7.585
        assert round(sum(two) / 15, 4) == 14.1851
        diluted = [0.95] * 1999 + [100.0]
        assert round(sum(diluted) / 2000, 4) <= 1.0


# --------------------------------------------------------------------------- #
# The assertion itself
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheRangeAssertion:
    @pytest.mark.parametrize(
        "value",
        [
            100.0,  # the value the grader actually emitted
            14.1851,  # the largest committed mean
            7.585,  # the most frequent committed mean
            1.0001,  # a bound widened by the smallest amount that admits anything
            2,  # an int, not just a float
            -0.01,
            -1.0,
        ],
    )
    def test_a_value_outside_the_unit_interval_raises(self, value):
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range({"Transactions[0].Amount": value})

    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 0, 1])
    def test_the_closed_unit_interval_is_accepted(self, value):
        """Both endpoints are legitimate: a grader that is certain says 1.0, and the
        committed grids contain both 1.0 and 0.1 leaves."""
        lib.assert_confidences_in_range({"Transactions[0].Amount": value})

    def test_nan_is_refused_rather_than_averaged(self):
        """``NaN`` passes no comparison, so it is out of range — and it is the input
        that distinguishes a check before the arithmetic from one after it:
        ``round(nan, 4)`` is ``nan``, so a check placed after the mean is computed
        publishes a ``NaN`` mean_confidence and reports nothing."""
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range({"x": float("nan")})

    def test_a_non_numeric_leaf_is_named_rather_than_crashing_the_sum(self):
        """The other input that distinguishes before from after: ``sum`` over a list
        holding a string raises ``TypeError``, which says nothing about confidence."""
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range({"x": "0.95"})
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range({"x": None})

    @pytest.mark.parametrize("value", [True, False])
    def test_a_boolean_leaf_is_refused_rather_than_read_as_1_or_0(self, value):
        """``True`` is numerically 1.0 in Python and ``False`` is 0.0, so both sit
        inside the range without being confidences. The product's walk already drops
        them, which is exactly why a check that let them through here would never be
        contradicted by a real payload."""
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range({"x": value})

    def test_an_empty_mapping_passes_and_a_non_empty_bad_one_does_not(self):
        """A check that only ever runs on an empty sequence never fires. Nothing is
        out of range in an empty mapping, so it passes; the paired case is what says
        the check is reachable at all."""
        lib.assert_confidences_in_range({})
        lib.assert_confidences_in_range([])
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range([0.99, 100.0])

    def test_the_message_names_the_field_and_the_value(self):
        """A count alone does not say whether one grader leaf or a whole column is
        mis-scaled, and that is the difference between a row to drop and a grid to
        rerun."""
        with pytest.raises(lib.ConfidenceOutOfRange) as excinfo:
            lib.assert_confidences_in_range(
                {"Transactions[7].Amount": 100.0, "AccountNumber": 0.99},
                "s3://bucket/doc/sections/1/result.json",
            )
        message = str(excinfo.value)
        assert "Transactions[7].Amount=100.0" in message
        assert "1 of 2" in message
        assert "s3://bucket/doc/sections/1/result.json" in message
        # The in-range leaf is not named: a reader triaging this needs the offenders.
        assert "AccountNumber" not in message

    def test_the_message_caps_the_list_but_not_the_count(self):
        """A mis-scaled column on a 400-row table is thousands of leaves. Printing
        them all buries the one number that sizes the problem.

        The cap is spelled out here rather than read from the module under test: an
        assertion computed from the constant it is checking holds for every value
        that constant could take.
        """
        with pytest.raises(lib.ConfidenceOutOfRange) as excinfo:
            lib.assert_confidences_in_range({f"T[{i}].A": 100.0 for i in range(50)})
        message = str(excinfo.value)
        assert "50 of 50" in message
        assert "(40 more)" in message
        assert message.count("=100.0") == 10
        assert lib.MAX_OUT_OF_RANGE_REPORTED == 10

    def test_it_raises_rather_than_clamping(self):
        """The control, stated as a property. A clamp is what let four releases ship
        this: 100 clamped to 1.0 yields a mean of 0.985 on the #1395 row, which sits
        inside the range every neighbouring cell reports (0.959-0.981) and which no
        scan over the committed grids could have found."""
        clamped = [min(1.0, c) for c in ROW_1395_LEAVES]
        assert round(sum(clamped) / len(clamped), 4) == 0.985
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.assert_confidences_in_range(ROW_1395_LEAVES)


# --------------------------------------------------------------------------- #
# The choke point: every harness confidence passes through the walk
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheWalkIsTheCheckPoint:
    def test_walk_confidence_refuses_a_payload_holding_a_percentage(self):
        section = _section(ROW_1395_LEAVES)
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.walk_confidence(section["explainability_info"])

    def test_confidence_values_inherits_it(self):
        """``confidence_values`` delegates to the walk, so the distribution-only
        statistics cannot be fed an out-of-range leaf by a route of their own."""
        section = _section(ROW_1395_LEAVES)
        with pytest.raises(lib.ConfidenceOutOfRange):
            lib.confidence_values(section["explainability_info"])

    def test_a_healthy_payload_is_unchanged(self):
        """The check adds a refusal, not a transformation: the multiset the committed
        baselines were computed from has to come back identical."""
        section = _section([0.99, 0.4, 1.0])
        assert sorted(lib.confidence_values(section["explainability_info"])) == [
            0.4,
            0.99,
            1.0,
        ]

    def test_the_calibration_join_is_covered_by_the_same_check(self):
        """``confidence_observations`` reads the walk, and a 100 would land in the top
        bin of every reliability figure the confidence study publishes."""
        section = _section(ROW_1395_LEAVES)
        truth = {
            f"SEQ{i:05d}": {"Amount": float(i)} for i in range(len(ROW_1395_LEAVES))
        }
        with pytest.raises(lib.ConfidenceOutOfRange):
            analyze.confidence_observations([section], truth, "Transactions")


# --------------------------------------------------------------------------- #
# The aggregation the defect was published through
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheDistributionHelper:
    def test_it_refuses_the_1395_multiset(self):
        with pytest.raises(lib.ConfidenceOutOfRange):
            analyze.confidence_distribution(ROW_1395_LEAVES)

    def test_it_computes_the_three_figures_it_always_did(self):
        """One definition now serves both scorers, and it has to agree with the
        expression the committed baselines were produced by.

        The multiset is deliberately lopsided — one leaf below 0.9 out of five, not
        a half-and-half split — because a 50/50 input reads the same whether
        ``pct_conf_below_0.9`` counts the leaves below the threshold or the ones
        above it.
        """
        confs = [0.95, 0.85, 1.0, 0.92, 0.98]
        assert analyze.confidence_distribution(confs) == {
            "mean_confidence": 0.94,
            "pct_conf_below_0.9": 20.0,
            "n_conf_leaves": 5,
        }

    @pytest.mark.parametrize("value", [float("nan"), "0.95", None, True])
    def test_the_helper_refuses_before_it_computes_anything(self, value):
        """The property, asserted on the helper and not only on the check it calls.

        These four inputs are the ones that distinguish a check placed before the
        arithmetic from one placed after it and still inside the function: ``NaN``
        survives ``round`` and would be returned as the mean, a string or ``None``
        would raise ``TypeError`` out of ``sum``, and a boolean would be averaged as
        1.0. Every one of them has to come back as a named confidence refusal.
        """
        with pytest.raises(lib.ConfidenceOutOfRange):
            analyze.confidence_distribution([0.95, value])

    def test_no_leaves_is_null_and_not_zero(self):
        """ "The grader scored nothing here" and "it scored 0.0" are different
        readings, and the second is the one a mean over no observations invites."""
        assert analyze.confidence_distribution([]) == {
            "mean_confidence": None,
            "pct_conf_below_0.9": None,
            "n_conf_leaves": 0,
        }

    def test_both_scorers_use_it(self):
        """The two sites held the same copy-pasted expression, and the range went
        unchecked in both. Asserting they share one definition is what stops a third
        copy appearing beside them."""
        source = open(
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "harness",
                "analyze.py",
            )
        ).read()
        assert source.count("**confidence_distribution(confs") == 2
        # The expression itself exists once, inside the helper.
        assert source.count("sum(confs) / len(confs)") == 1


# --------------------------------------------------------------------------- #
# End to end, and what a raise costs
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEndToEnd:
    @pytest.fixture
    def scored_doc(self, monkeypatch):
        def run(sections, truth=None, evaluation=None):
            monkeypatch.setattr(
                lib,
                "doc_row",
                lambda *a, **k: {"ObjectStatus": "COMPLETED", "PageCount": 1},
            )
            monkeypatch.setattr(
                lib, "read_metering", lambda *a, **k: lib.Reading.present({})
            )
            monkeypatch.setattr(
                lib, "read_sections", lambda *a, **k: lib.SectionRead(sections)
            )
            monkeypatch.setattr(
                lib,
                "read_json",
                lambda *a, **k: (
                    lib.Reading.present(evaluation)
                    if evaluation is not None
                    else lib.Reading.absent("no such object")
                ),
            )
            return analyze.score_doc("bucket", "table", "run", "doc", truth)

        return run

    def test_the_synthetic_scorer_refuses(self, scored_doc):
        with pytest.raises(lib.ConfidenceOutOfRange):
            scored_doc(
                [_section(ROW_1395_LEAVES)],
                truth={
                    "seq_ids": [f"SEQ{i:05d}" for i in range(len(ROW_1395_LEAVES))],
                    "list_key": "Transactions",
                },
            )

    def test_the_reference_scorer_refuses(self, scored_doc):
        """The scorer the 21 committed cells came from: every one is a reference row,
        scored by the stack's own evaluation rather than against exact truth."""
        with pytest.raises(lib.ConfidenceOutOfRange):
            scored_doc(
                [_section(ROW_1395_LEAVES)],
                evaluation={"overall_metrics": {"weighted_overall_score": 0.99}},
            )

    def test_a_healthy_reference_document_still_scores(self, scored_doc):
        row = scored_doc(
            [_section([0.95, 0.85, 1.0])],
            evaluation={"overall_metrics": {"weighted_overall_score": 0.99}},
        )
        assert row["mean_confidence"] == 0.9333
        assert row["n_conf_leaves"] == 3

    def test_a_refusal_costs_one_row_and_not_the_grid(self):
        """``score_reference_run`` already converts a per-document exception into one
        ``SCORE_ERROR`` row carrying the message, which is why raising is affordable
        on a 2,000-row grid that cost real money to produce."""
        calls = {"n": 0}

        def score(_bucket, _table, _run, name, _truth):
            calls["n"] += 1
            if name == "BAD.png":
                raise lib.ConfidenceOutOfRange("1 of 15 confidence scores … 100.0")
            return {"status": "COMPLETED", "success": True, "mean_confidence": 0.96}

        rows = aggregate.score_reference_run(
            {"output_bucket": "b", "tracking_table": "t"},
            {"cell": "c", "doc": "ocr_bench", "run_id": "r", "n_docs": 3},
            list_prefixes=lambda *a: ["r/GOOD1.png/", "r/BAD.png/", "r/GOOD2.png/"],
            score=score,
        )
        assert calls["n"] == 3
        by_doc = {r["sub_doc"]: r for r in rows}
        assert by_doc["BAD.png"]["status"] == "SCORE_ERROR"
        assert "100.0" in by_doc["BAD.png"]["error"]
        # No figure is carried on the refused row — not a clamped one, not a zero.
        assert "mean_confidence" not in by_doc["BAD.png"]
        # The other two documents are scored normally.
        assert [by_doc["GOOD1.png"]["success"], by_doc["GOOD2.png"]["success"]] == [
            True,
            True,
        ]

    def test_score_all_contains_it_the_same_way(self, tmp_path, monkeypatch):
        """The synthetic path's own container, asserted rather than assumed — it is a
        second ``try``/``except`` in a second function."""
        runmap = {
            "resources": {"output_bucket": "b", "tracking_table": "t"},
            "runs": [
                {
                    "cell": "c",
                    "doc": "d.pdf",
                    "doc_name": "d.pdf",
                    "run_id": "r1",
                    "repeat": 0,
                },
                {
                    "cell": "c",
                    "doc": "e.pdf",
                    "doc_name": "e.pdf",
                    "run_id": "r2",
                    "repeat": 0,
                },
            ],
        }
        (tmp_path / "runmap.json").write_text(json.dumps(runmap))

        def score(_bucket, _table, run_id, _doc, _truth):
            if run_id == "r1":
                raise lib.ConfidenceOutOfRange("1 of 15 confidence scores … 100.0")
            return {"status": "COMPLETED", "success": True}

        monkeypatch.setattr(aggregate.analyze, "score_doc", score)
        _rm, rows = aggregate.score_all(str(tmp_path))
        assert [r["status"] for r in rows] == ["SCORE_ERROR", "COMPLETED"]
        assert "100.0" in rows[0]["error"]
