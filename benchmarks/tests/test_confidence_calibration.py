# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence calibration against exact per-cell ground truth (issue #935).

What was missing
----------------
The repository owned both halves of this measurement and never connected them.
``idp_common.evaluation.confidence_curve`` implements ECE, binned AUROC, Bayesian
shrinkage and the two reliability gates the shipped review-effort estimator acts on;
``benchmarks/`` owns a synthetic corpus with exact per-cell truth keyed by unique
``SEQnnnnn`` row tags. The harness never imported the first, and could not have used
it: ``lib.walk_confidence`` appended the bare confidence scalar to a list and threw
the field path away, so a score could not be joined to the cell it describes.

``score_synthetic`` therefore reported ``mean_confidence``, ``pct_conf_below_0.9``
and ``n_conf_leaves`` — three descriptions of the confidence distribution's SHAPE,
none of which says whether it is true — and no calibration metric at all.

What these tests pin
--------------------
1. ``walk_confidence`` returns field PATHS, and they are the same paths
   ``flatten_values`` gives the extracted values, so the join is by position within
   a row rather than by bare field name.
2. ``confidence_values`` yields the multiset the old list-appending walk did, so
   ``mean_confidence`` / ``pct_conf_below_0.9`` / ``n_conf_leaves`` are unchanged and
   the committed baselines stay comparable.
3. The join is by SEQ tag, not by position, and a row returned out of order still
   scores against its own truth.
4. ECE, AUROC and the reliability verdict come from the SHIPPED engine, not a
   lookalike — the point of the exercise is to measure the thresholds that ship.
5. The two ECE estimators differ in the documented direction, so neither is quoted
   as the other.
6. The failure this whole measurement exists to catch is detectable: confident,
   well-calibrated, and wrong at the top of the range.
7. Pooling is exact rather than an average of per-document averages.
8. The gate fires on a magnitude move AND on a threshold crossing, and stays silent
   on a sample too thin to read.
9. The keys reach ``CSV_COLS`` — a row key absent from that list is dropped by
   ``DictWriter(extrasaction="ignore")`` in silence.

Note on where this runs: ``benchmarks/tests`` is in ``scripts/run_all_tests.py``'s
``RUN_ROOTS``, so ``make test`` covers it. Neither CI target
(``test-cicd``/``test-packages-cicd``) runs this directory — that is pre-existing and
true of the other suites here too.
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "benchmarks/harness")

analyze = pytest.importorskip("analyze")
aggregate = pytest.importorskip("aggregate")
lib = pytest.importorskip("lib")


LIST_KEY = "Transactions"


def _leaf(confidence, threshold=0.8):
    return {
        "confidence": confidence,
        "confidence_threshold": threshold,
        "geometry": [{"boundingBox": {"left": 0.1}, "page": 1}],
    }


def _section(rows, *, wrap=True, list_key=LIST_KEY, prefix_instances=False):
    """One section ``result.json`` as the harness reads it off S3.

    ``rows`` is a list of ``(seq, amount, confidence)``. ``amount`` is what the
    extraction returned; the truth built by :func:`_truth` always expects the seq's
    own index as a float, so passing anything else makes that cell wrong.
    """
    extracted = [
        {"Description": f"SEQ{seq:05d} AnyCompany Store", "Amount": amount}
        for seq, amount, _c in rows
    ]
    assessed = [
        {"Description": _leaf(1.0), "Amount": _leaf(conf)} for _s, _a, conf in rows
    ]
    ir = {"Account Number": "000123456789", list_key: extracted}
    ei = {"Account Number": _leaf(0.99), list_key: assessed}
    if prefix_instances:
        ir = {"instances": [ir]}
        ei = {"instances": [ei]}
    return {
        "inference_result": ir,
        "explainability_info": [ei] if wrap else ei,
    }


def _truth(seqs):
    return {
        "list_key": LIST_KEY,
        "seq_ids": [f"SEQ{s:05d}" for s in seqs],
        "rows_typed": {f"SEQ{s:05d}": {"Amount": float(s)} for s in seqs},
    }


def _rows(n, *, confidence=1.0, wrong=()):
    """``n`` rows, correct unless their seq is in ``wrong``."""
    return [
        (
            s,
            (-1.0 if s in wrong else float(s)),
            confidence(s) if callable(confidence) else confidence,
        )
        for s in range(n)
    ]


# --------------------------------------------------------------------- the path join


def test_walk_confidence_returns_paths_that_index_the_extracted_values():
    """The whole mechanical blocker in #935. Without the path, a confidence cannot be
    attributed to a cell, so no amount of statistics downstream can help."""
    from idp_common.evaluation import flatten_values

    section = _section(_rows(3))
    confidences = lib.walk_confidence(section["explainability_info"])
    values = flatten_values(section["inference_result"])
    assert "Transactions[1].Amount" in confidences
    # Every confidence path names a real extracted cell, and nothing is matched by
    # bare field name: one Amount cell per row, distinguished by its index.
    assert set(confidences) <= set(values)
    assert sum(1 for p in confidences if p.endswith(".Amount")) == 3


def test_paths_survive_the_multi_instance_wrapper():
    """A multi-instance class nests the record under ``instances``, which shifts every
    path one level. Getting that wrong yields zero observations, not wrong ones — the
    quiet failure mode."""
    section = _section(_rows(4), prefix_instances=True)
    confidences = lib.walk_confidence(section["explainability_info"])
    assert "instances[0].Transactions[2].Amount" in confidences
    observations = analyze.confidence_observations(
        [section], _truth(range(4))["rows_typed"], LIST_KEY
    )
    assert len(observations) == 4


def test_confidence_values_preserves_the_old_multiset():
    """``mean_confidence`` / ``pct_conf_below_0.9`` / ``n_conf_leaves`` are computed
    from this and are read straight out of committed baselines, so the multiset —
    not just the set — has to be unchanged."""
    section = _section([(0, 0.0, 0.4), (1, 1.0, 0.4), (2, 2.0, 1.0)])
    values = lib.confidence_values(section["explainability_info"])
    # 3 Amount leaves + 3 Description leaves + 1 Account Number leaf. Counted, not
    # de-duplicated: the two 0.4s must both survive or the mean shifts.
    assert sorted(values) == [0.4, 0.4, 0.99, 1.0, 1.0, 1.0, 1.0]


def test_the_explainability_wrapper_list_is_unwrapped():
    """``explainability_info`` is written as ``[assessment_dict]``. A walk that adds a
    level for it misaligns every path from ``inference_result`` and silently produces
    no observations at all."""
    wrapped = analyze.confidence_observations(
        [_section(_rows(5), wrap=True)], _truth(range(5))["rows_typed"], LIST_KEY
    )
    bare = analyze.confidence_observations(
        [_section(_rows(5), wrap=False)], _truth(range(5))["rows_typed"], LIST_KEY
    )
    assert wrapped == bare
    assert len(wrapped) == 5


# ------------------------------------------------------------------ the SEQ-tag join


def test_the_join_is_by_seq_tag_and_not_by_position():
    """The exactness of this corpus rests on the tag. A model that returns the rows in
    a different order is not wrong, and scoring row 3 against truth row 3 would say it
    was."""
    rows = _rows(4, wrong={2})
    shuffled = [rows[3], rows[0], rows[2], rows[1]]
    observations = analyze.confidence_observations(
        [_section(shuffled)], _truth(range(4))["rows_typed"], LIST_KEY
    )
    assert len(observations) == 4
    assert sum(1 for _c, ok in observations if not ok) == 1


def test_a_cell_without_a_confidence_leaf_is_not_an_observation():
    """A missing confidence is ``score_confidence_coverage``'s subject (#997). Counting
    it here — at any assumed value — would mix a coverage defect into a calibration
    number."""
    section = _section(_rows(3))
    del section["explainability_info"][0][LIST_KEY][1]["Amount"]
    observations = analyze.confidence_observations(
        [section], _truth(range(3))["rows_typed"], LIST_KEY
    )
    assert len(observations) == 2


def test_only_cells_the_truth_declares_are_compared():
    """``Description`` carries the SEQ tag and has no declared truth value, so it has
    no verdict — it must not contribute a fabricated "correct"."""
    observations = analyze.confidence_observations(
        [_section(_rows(6))], _truth(range(6))["rows_typed"], LIST_KEY
    )
    assert len(observations) == 6
    assert all(ok for _c, ok in observations)


def test_a_row_recovered_in_two_sections_contributes_once():
    """Matching ``score_cells``. Double-counting would inflate the observation count
    that every shipped reliability floor is thresholded on."""
    observations = analyze.confidence_observations(
        [_section(_rows(3)), _section(_rows(3))],
        _truth(range(3))["rows_typed"],
        LIST_KEY,
    )
    assert len(observations) == 3


def test_no_rows_typed_means_no_observations():
    """A reference corpus has no per-cell truth, so there is nothing to join. Zero
    observations, not zero error."""
    assert analyze.confidence_observations([_section(_rows(3))], None, LIST_KEY) == []


# --------------------------------------------------------- reuse of the shipped engine


def test_ece_and_the_verdict_come_from_the_shipped_curve():
    """#935's point is that the measurement must be about the gates that ship. Asserted
    by recomputing through ``ConfidenceCurve`` directly and requiring equality."""
    from idp_common.evaluation import ConfidenceCurve

    rows = _rows(
        120,
        confidence=lambda s: 0.55 if s % 3 == 0 else 0.95,
        wrong=set(range(0, 120, 3)),
    )
    section = _section(rows)
    truth = _truth(range(120))
    observations = analyze.confidence_observations(
        [section], truth["rows_typed"], LIST_KEY
    )
    curve = ConfidenceCurve()
    curve.add_observations(observations, source="scoring")
    health = curve.calibration_health()

    scored = analyze.score_calibration([section], truth["rows_typed"], LIST_KEY)
    assert scored["calibration_observations"] == 120
    assert scored["calibration_ece"] == round(health.ece, 4)
    assert scored["calibration_auroc"] == round(health.auroc, 4)
    assert scored["calibration_bin_coverage"] == health.bin_coverage
    assert scored["calibration_reliable"] is health.reliable


def test_the_curve_payload_records_a_scoring_observation_not_a_review_one():
    """A benchmark measures the whole confidence range, including the high-confidence
    zone worst-first review never reaches. Recording these as review observations
    would keep the curve PARTIALLY_MEASURED forever."""
    scored = analyze.score_calibration(
        [_section(_rows(40))], _truth(range(40))["rows_typed"], LIST_KEY
    )
    payload = scored["calibration_curve"]
    assert payload["scoringObservations"] == 40
    assert payload["reviewObservations"] == 0


def test_the_two_ece_estimators_differ_in_the_documented_direction():
    """A set of cells all scored 1.00 and all correct is perfectly calibrated. The
    shipped gate compares against the bin MIDPOINT, so it reports 0.05 — inside the
    0.15 bar, so the gate does not misfire, but not a number to quote as the grader's
    calibration error."""
    scored = analyze.score_calibration(
        [_section(_rows(50, confidence=1.0))], _truth(range(50))["rows_typed"], LIST_KEY
    )
    assert scored["calibration_ece"] == 0.05
    assert scored["calibration_ece_mean_conf"] == 0.0
    assert scored["calibration_brier"] == 0.0


def test_the_mean_confidence_ece_agrees_with_sticklers_estimator():
    """``calibration_ece_mean_conf`` is documented as the estimator Stickler's
    ``ECEMetric`` uses, and the study page quotes it as the grader's calibration error.
    That claim is only worth making if the two agree, and they can disagree on bin-edge
    convention alone — Stickler bisects upper edges where ``bin_index`` divides — so it
    is asserted rather than stated.
    """
    from stickler.structured_object_evaluator.models.confidence import ConfidencePair

    from idp_common.evaluation.stickler_backend.confidence import ECEMetric

    n = 300

    def confidence(seq):
        return [0.15, 0.45, 0.75, 1.0][seq % 4]

    wrong = set(range(0, n, 3))
    rows = _rows(n, confidence=confidence, wrong=wrong)
    section = _section(rows)
    truth = _truth(range(n))
    observations = analyze.confidence_observations(
        [section], truth["rows_typed"], LIST_KEY
    )
    stickler = ECEMetric(n_bins=10).compute(
        [
            ConfidencePair(
                is_match=bool(ok), confidence=float(c), similarity=1.0 if ok else 0.0
            )
            for c, ok in observations
        ]
    )["value"]
    scored = analyze.score_calibration([section], truth["rows_typed"], LIST_KEY)
    assert scored["calibration_ece_mean_conf"] == round(stickler, 4)
    # And the gate's midpoint estimator is a different number on the same data, which
    # is the reason both are recorded.
    assert scored["calibration_ece"] != scored["calibration_ece_mean_conf"]


def test_auroc_is_undefined_rather_than_perfect_when_nothing_is_wrong():
    """Not 1.0 and not 0.5. With no incorrect cell there are no pairs to rank, so
    ranking power is unmeasured — and a corpus mean that read it as 1.0 would be
    reporting the share of flawless documents."""
    scored = analyze.score_calibration(
        [_section(_rows(60))], _truth(range(60))["rows_typed"], LIST_KEY
    )
    assert scored["calibration_auroc"] is None
    assert scored["calibration_auroc_unbinned"] is None
    assert scored["calibration_observations"] == 60


def test_a_document_with_no_joinable_cell_reports_none_not_zero():
    scored = analyze.score_calibration([_section(_rows(3))], None, LIST_KEY)
    assert scored["calibration_observations"] == 0
    assert scored["calibration_ece"] is None
    assert scored["calibration_reliable"] is None
    assert scored["calibration_curve"] is None


# ------------------------------------------------- the failure the study is looking for


def test_confident_and_wrong_is_detected_as_undiscriminating():
    """The finding the repository already recorded once and could not re-measure:
    passing calibration with chance-level ranking, every error in the top bin, so a
    worst-first review queue reaches none of them.

    Here every cell scores 1.00 and a tenth of them are wrong. ECE stays modest, and
    AUROC is exactly chance because there is nothing in the ordering to separate the
    wrong cells from the right ones.
    """
    from idp_common.evaluation.confidence_curve import (
        AUROC_UNRELIABLE_THRESHOLD,
        MIN_OBSERVATIONS_FOR_AUROC,
    )

    n = MIN_OBSERVATIONS_FOR_AUROC * 2
    rows = _rows(n, confidence=1.0, wrong=set(range(0, n, 10)))
    scored = analyze.score_calibration(
        [_section(rows)], _truth(range(n))["rows_typed"], LIST_KEY
    )
    assert scored["calibration_observations"] == n
    assert scored["calibration_auroc"] == 0.5
    assert scored["calibration_auroc"] <= AUROC_UNRELIABLE_THRESHOLD
    # Calibration alone would not have raised this: the error is well under the bar.
    assert scored["calibration_ece"] < 0.15
    assert scored["calibration_reliable"] is False


def test_confidence_that_does_rank_errors_scores_above_chance():
    """The control for the test above. Same error rate, but the wrong cells carry the
    low scores, so ranking power is real and the verdict is reliable.

    The correct cells are deliberately split across two bins. ``MIN_BINS_FOR_SIGNAL``
    is 3, and a curve occupying fewer than that is ``degenerate`` however well it
    ranks — confidence that takes two values cannot order a review queue beyond
    "these, then those". So a perfect AUROC on a two-bin curve is still reported
    unreliable, which is the shipped behaviour and not a quirk of this harness.
    """
    n = 200
    wrong = set(range(0, n, 10))

    def confidence(seq):
        if seq in wrong:
            return 0.25
        return 0.65 if seq % 2 else 0.95

    rows = _rows(n, confidence=confidence, wrong=wrong)
    scored = analyze.score_calibration(
        [_section(rows)], _truth(range(n))["rows_typed"], LIST_KEY
    )
    assert scored["calibration_auroc"] == 1.0
    assert scored["calibration_auroc_unbinned"] == 1.0
    assert scored["calibration_reliable"] is True


# ------------------------------------------------------------------------- pooling


def test_pooling_is_exact_and_not_an_average_of_averages():
    """A 5-row form and a 400-row statement must not carry equal weight. Asserted by
    requiring the pooled figures to equal those of a single scoring over the union."""
    small = _section(_rows(5, confidence=0.65, wrong={0}), list_key=LIST_KEY)
    big = _section(
        [(s + 100, float(s + 100), 0.95) for s in range(400)], list_key=LIST_KEY
    )
    truth_small = _truth(range(5))["rows_typed"]
    truth_big = _truth(range(100, 500))["rows_typed"]

    parts = [
        analyze.score_calibration([small], truth_small, LIST_KEY)["calibration_curve"],
        analyze.score_calibration([big], truth_big, LIST_KEY)["calibration_curve"],
    ]
    pooled = analyze.pool_calibration(parts)

    union = analyze.score_calibration(
        [small, big], {**truth_small, **truth_big}, LIST_KEY
    )
    assert pooled["observations"] == 405
    assert pooled["ece"] == union["calibration_ece"]
    assert pooled["ece_mean_conf"] == union["calibration_ece_mean_conf"]
    assert pooled["brier"] == union["calibration_brier"]
    assert pooled["auroc"] == union["calibration_auroc"]


def test_pooling_reports_the_unbinned_auroc_only_when_given_it():
    """It cannot be recovered from bin counts. A pooled report that invented one from
    the binned value would be quoting the gate's low-biased estimate as the metric."""
    part = analyze.score_calibration(
        [_section(_rows(50, confidence=0.75, wrong={1, 2}))],
        _truth(range(50))["rows_typed"],
        LIST_KEY,
    )["calibration_curve"]
    assert analyze.pool_calibration([part])["auroc_unbinned"] is None
    assert (
        analyze.pool_calibration([part], unbinned_auroc=0.61)["auroc_unbinned"] == 0.61
    )


def test_pooling_nothing_is_none():
    assert analyze.pool_calibration([]) is None
    assert analyze.pool_calibration([None, None]) is None


def test_the_cell_rollup_pools_rather_than_averaging():
    rows = []
    for repeat in range(2):
        scored = analyze.score_calibration(
            [_section(_rows(60, confidence=0.85, wrong={0, 1}))],
            _truth(range(60))["rows_typed"],
            LIST_KEY,
        )
        rows.append(
            {"cell": "c", "doc": "d.pdf", "repeat": repeat, "success": True, **scored}
        )
    stats = aggregate.cell_stats(rows)["c"]
    assert stats["calibration"]["observations"] == 120
    assert stats["calibration"]["correct"] == 116


# ---------------------------------------------------------------------- the gate


def _cell(observations, ece, auroc):
    return {"calibration": {"observations": observations, "ece": ece, "auroc": auroc}}


def test_the_gate_flags_a_calibration_error_that_grows():
    findings = aggregate.calibration_findings(
        _cell(500, 0.09, 0.80), _cell(500, 0.02, 0.80), regression=True
    )
    assert len(findings) == 1
    assert "calibration ECE +0.070" in findings[0]


def test_the_gate_flags_ranking_power_that_falls():
    findings = aggregate.calibration_findings(
        _cell(500, 0.02, 0.70), _cell(500, 0.02, 0.80), regression=True
    )
    assert len(findings) == 1
    assert "confidence AUROC -0.100" in findings[0]


def test_a_tiny_move_that_crosses_a_shipped_bar_is_still_a_regression():
    """The move the magnitude thresholds would miss, and the one that matters: on the
    far side of ``AUROC_UNRELIABLE_THRESHOLD`` the estimator stops recommending a
    worst-first review subset at all, so the product's behaviour has changed."""
    from idp_common.evaluation.confidence_curve import AUROC_UNRELIABLE_THRESHOLD

    findings = aggregate.calibration_findings(
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD),
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD + 0.005),
        regression=True,
    )
    assert len(findings) == 1
    assert "CROSSED" in findings[0]


def test_a_tiny_ece_move_across_the_unreliable_bar_is_a_regression():
    from idp_common.evaluation.confidence_curve import ECE_UNRELIABLE_THRESHOLD

    findings = aggregate.calibration_findings(
        _cell(500, ECE_UNRELIABLE_THRESHOLD + 0.001, 0.80),
        _cell(500, ECE_UNRELIABLE_THRESHOLD, 0.80),
        regression=True,
    )
    assert len(findings) == 1
    assert "CROSSED" in findings[0]


def test_a_sample_too_thin_to_read_is_not_reported_in_either_direction():
    """Below the shipped floors a move is noise, and stability is not reassurance."""
    from idp_common.evaluation.confidence_curve import (
        MIN_OBSERVATIONS_FOR_AUROC,
        MIN_OBSERVATIONS_FOR_MEASURED,
    )

    thin = MIN_OBSERVATIONS_FOR_MEASURED - 1
    assert (
        aggregate.calibration_findings(
            _cell(thin, 0.40, 0.20), _cell(thin, 0.01, 0.95), regression=True
        )
        == []
    )
    # Thick enough for calibration error, too thin for ranking power: the ECE finding
    # is reported and the AUROC one is not.
    between = MIN_OBSERVATIONS_FOR_AUROC - 1
    findings = aggregate.calibration_findings(
        _cell(between, 0.40, 0.20), _cell(between, 0.01, 0.95), regression=True
    )
    assert len(findings) == 1
    assert "ECE" in findings[0]


def test_a_baseline_without_calibration_is_skipped_not_reported_as_a_change():
    """Every summary scored before #935 is in this state. Reporting "None -> 0.02" as
    an improvement on every cell of the first run would bury the real findings."""
    assert aggregate.calibration_findings(_cell(500, 0.02, 0.9), {}) == []
    assert aggregate.calibration_findings({}, _cell(500, 0.02, 0.9)) == []


def test_an_undefined_auroc_on_one_side_is_not_compared():
    """ "No wrong cells to rank" is not a change in ranking power."""
    assert (
        aggregate.calibration_findings(
            _cell(500, 0.02, None), _cell(500, 0.02, 0.95), regression=True
        )
        == []
    )


def test_improvements_are_reported_too():
    findings = aggregate.calibration_findings(
        _cell(500, 0.02, 0.90), _cell(500, 0.12, 0.60), regression=False
    )
    assert len(findings) == 2


# ------------------------------------------------------------------------ plumbing


def test_the_calibration_keys_reach_the_csv():
    """A row key absent from ``CSV_COLS`` is dropped by
    ``DictWriter(extrasaction="ignore")`` without a word — which is how
    ``n_conf_leaves`` came to be computed by the scorer and readable from nothing."""
    for key in (
        "calibration_observations",
        "calibration_ece",
        "calibration_ece_mean_conf",
        "calibration_auroc",
        "calibration_auroc_unbinned",
        "calibration_brier",
        "calibration_bin_coverage",
    ):
        assert key in aggregate.CSV_COLS, key


def test_score_synthetic_declares_every_calibration_key_even_with_no_observations():
    """The keys must be present and None rather than absent: a missing key and a key
    holding None read identically in a summary, but only the second survives a
    ``cell_stats`` roll-up that looks the metric up by name."""
    empty = analyze.score_calibration([], None, None)
    populated = analyze.score_calibration(
        [_section(_rows(5))], _truth(range(5))["rows_typed"], LIST_KEY
    )
    assert set(empty) == set(populated)
