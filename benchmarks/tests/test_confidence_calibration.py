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
4. The binned ECE, AUROC and the reliability verdict come from the SHIPPED engine, not
   a lookalike — the point of the exercise is to measure the thresholds that ship. The
   two figures computed locally instead, the unbinned AUROC and the Brier score, are
   each asserted EQUAL to the metric class the evaluation service uses, since computing
   them locally is what keeps the ``[evaluation]`` extra off the scoring path.
5. The two estimators of each metric differ in the documented direction, so neither is
   quoted as the other, and the gate reads the mobile one for magnitude and the
   product's own for a threshold crossing — on BOTH metrics.
6. The failure this whole measurement exists to catch is detectable: confident,
   well-calibrated, and wrong at the top of the range.
7. Pooling is exact rather than an average of per-document averages.
8. The gate fires on a magnitude move AND on a threshold crossing, and stays silent
   on a sample too thin to read.
9. Both regression THRESHOLDS are pinned, and a sub-threshold move measured on real
   release-to-release data is asserted SILENT. Without that, the values were free to be
   lowered arbitrarily with every test still passing.
10. A comparison is reported as unread when EITHER side lacks the metric, not only when
    the baseline does.
11. The keys reach ``CSV_COLS`` — a row key absent from that list is dropped by
    ``DictWriter(extrasaction="ignore")`` in silence — and the artifact's numeric
    payloads round-trip through the one-line writer unchanged.

**Note on where this runs.** ``benchmarks/tests`` is in ``scripts/run_all_tests.py``'s
``RUN_ROOTS``, so ``make test`` covers it, and it is **run by both CIs** — ``make
test-packages-cicd`` invokes it (``.github/workflows/developer-tests.yml`` and
``.gitlab-ci.yml`` both call that target), and
``scripts/tests/test_src_lambda_tests_in_ci.py`` derives the universe of directories
holding a tracked ``test_*.py`` and fails if one reaches neither CI, so this directory
cannot silently fall out of coverage again.

⚠️ **Two things still bound what a green run here means, and neither is fixed by that.**

The ``sys.path.insert`` below is RELATIVE to the working directory, so running this file
from anywhere but the repository root collapses the whole suite to ``1 skipped`` with no
warning and a green exit. CI invokes it from the repository root so CI is unaffected; a
developer running it from ``benchmarks/`` is not. Same absence-versus-failure shape as
[#1079](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1079).

And whether a red gate can actually stop a merge is a repository setting rather than
anything in this tree: neither ``develop`` nor ``main`` currently carries branch
protection, so every gate here is advisory in the sense that a pull request can be merged
over it. That is an accepted residual with a recorded decision, not open work. Do not take
the state from this docstring — ``make check-branch-protection`` reads it live, one branch
per invocation.

⚠️ **The three Stickler equality assertions are vacuous without ``stickler`` installed.**
``test_the_mean_confidence_ece_agrees_with_sticklers_estimator``,
``test_the_brier_score_agrees_with_sticklers_metric`` and
``test_the_unbinned_auroc_matches_sticklers_over_raw_pairs`` are each
``importorskip``-guarded, which is deliberate — the whole point of the local
implementations is that scoring must not require the ``[evaluation]`` extra — but it means
a bare local run without that extra reports green while three cross-checks never
executed. CI installs the ``test`` extra, so they are live there. If you are relying on
them to sign off a change to ``unbinned_auroc``, ``mean_conf_ece`` or the Brier
arithmetic, confirm they did not skip (``-rs``).

A note on counting passes from this directory: four tests in ``test_typed_scoring.py``
skip unless ``benchmarks/harness/gen_corpus.py --only kv_form`` has been run, so the same
tree reports 238 passed with the corpus generated and 234 passed / 4 skipped without it.
Quote the split, not the total.
"""

from __future__ import annotations

import json
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


def test_a_length_mismatch_between_assessment_and_data_rows_fails_loudly():
    """The one condition under which the join MISPAIRS instead of under-reporting.

    The confidence for row i is the i-th element of the assessment's row list, so if
    that list were shorter than the data's, row i's confidence would describe some
    other transaction and every calibration number would be a plausible-looking
    statement about the wrong rows. ``reconcile_assessment_to_data`` guarantees equal
    lengths in the pipeline; this asserts the harness notices if that guarantee is ever
    removed, rather than scoring the mispaired data.
    """
    section = _section(_rows(10))
    del section["explainability_info"][0][LIST_KEY][4]
    with pytest.raises(AssertionError, match="misalignment"):
        analyze.confidence_observations(
            [section], _truth(range(10))["rows_typed"], LIST_KEY
        )


def test_an_equal_length_assessment_list_passes_the_alignment_check():
    """The control: reconciliation's actual output, including rows padded with a null
    confidence, must not trip the assertion."""
    section = _section(_rows(10))
    section["explainability_info"][0][LIST_KEY][4] = {
        "Description": _leaf(None),
        "Amount": _leaf(None),
    }
    observations = analyze.confidence_observations(
        [section], _truth(range(10))["rows_typed"], LIST_KEY
    )
    assert len(observations) == 9


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
    # Both are Optional on CalibrationHealth, and they become None for *different*
    # reasons — worth stating, because a fixture edit that trips one would otherwise
    # fail against a comment blaming the other. `ece` is None only with no
    # observations at all. `auroc` is None when either class is empty
    # (`n_correct <= 0 or n_wrong <= 0`): it is a ranking statistic, so it is
    # undefined without both a correct and an incorrect case, however many
    # observations there are. This fixture supplies 120 of which every third is
    # wrong, so both are defined; asserting that is cheaper than rounding None.
    assert health.ece is not None
    assert health.auroc is not None
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


def _stickler_pairs(observations):
    """``observations`` as Stickler ``ConfidencePair``s. Import-guarded by the caller."""
    from stickler.structured_object_evaluator.models.confidence import ConfidencePair

    return [
        ConfidencePair(
            is_match=bool(ok), confidence=float(c), similarity=1.0 if ok else 0.0
        )
        for c, ok in observations
    ]


def test_the_mean_confidence_ece_agrees_with_sticklers_estimator():
    """``calibration_ece_mean_conf`` is documented as the estimator Stickler's
    ``ECEMetric`` uses, and the study page quotes it as the grader's calibration error.
    That claim is only worth making if the two agree, and they can disagree on bin-edge
    convention alone — Stickler bisects upper edges where ``bin_index`` divides — so it
    is asserted rather than stated.

    Guarded by ``importorskip`` like every other Stickler comparison in this file:
    computing these statistics without the ``[evaluation]`` extra is the point of the
    local implementations, so a check ON that extra must not be the thing that makes the
    suite require it.
    """
    pytest.importorskip("stickler")
    from idp_common.evaluation.stickler_backend.confidence import ECEMetric

    n = 300

    def confidence(seq):
        # Deliberately UNEQUAL per-bin counts. With 75 observations in each of four
        # bins, a mutant computing an unweighted average of the per-bin gaps returns
        # the identical number and the test passes while the weighting is wrong.
        return [0.15, 0.45, 0.45, 0.75, 0.75, 0.75, 1.0, 1.0, 1.0, 1.0][seq % 10]

    wrong = set(range(0, n, 3))
    rows = _rows(n, confidence=confidence, wrong=wrong)
    section = _section(rows)
    truth = _truth(range(n))
    observations = analyze.confidence_observations(
        [section], truth["rows_typed"], LIST_KEY
    )
    counts = {}
    for c, _ok in observations:
        counts[c] = counts.get(c, 0) + 1
    assert len(set(counts.values())) > 1, "per-bin counts must differ, see above"
    stickler = ECEMetric(n_bins=10).compute(_stickler_pairs(observations))["value"]
    scored = analyze.score_calibration([section], truth["rows_typed"], LIST_KEY)
    assert scored["calibration_ece_mean_conf"] == round(stickler, 4)
    # And the gate's midpoint estimator is a different number on the same data, which
    # is the reason both are recorded.
    assert scored["calibration_ece"] != scored["calibration_ece_mean_conf"]


def test_the_brier_score_agrees_with_sticklers_metric():
    """``calibration_brier`` is `sse / n` over the stored `brierSse`, not a call into
    Stickler's ``BrierScoreMetric`` — same trade as the unbinned AUROC, and asserted for
    the same reason. The study page publishes this column, so "it is obviously the same
    formula" is not enough: the two could diverge on which observations they see (a cell
    the join skipped, a duplicate row counted twice) even with identical arithmetic, and
    that difference would be invisible in the number itself.
    """
    pytest.importorskip("stickler")
    from idp_common.evaluation.stickler_backend.confidence import BrierScoreMetric

    def confidence(seq):
        return [0.05, 0.3, 0.62, 0.62, 0.9, 1.0, 1.0, 0.45, 0.78, 0.99][seq % 10]

    n = 240
    for wrong in ({}, set(range(0, n, 4)), set(range(n)) - {5}, set(range(0, n, 7))):
        section = _section(_rows(n, confidence=confidence, wrong=wrong))
        truth = _truth(range(n))
        observations = analyze.confidence_observations(
            [section], truth["rows_typed"], LIST_KEY
        )
        theirs = BrierScoreMetric().compute(_stickler_pairs(observations))["value"]
        scored = analyze.score_calibration([section], truth["rows_typed"], LIST_KEY)
        assert scored["calibration_brier"] == round(theirs, 4), (wrong and len(wrong),)
        # And pooling the stored sufficient statistic reaches the same value, which is
        # what the published per-arm Brier column is.
        pooled = analyze.pool_calibration([scored["calibration_curve"]])
        assert pooled["brier"] == round(theirs, 4)


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
    # The unbinned AUROC too, which bin counts alone cannot recover — the value tally
    # is what makes it exact rather than absent.
    assert pooled["auroc_unbinned"] == union["calibration_auroc_unbinned"]


def test_the_stored_payload_is_a_sufficient_statistic_for_every_published_figure():
    """The committed summary has to be enough on its own.

    The S3 artifacts these figures come from live exactly as long as nobody deletes
    the stack, and three release stacks are already gone. So every number the study
    page quotes must be recoverable from `calibration_curve` with no bucket — which is
    the same property that lets `--calibration` read a committed summary offline.
    """
    scored = analyze.score_calibration(
        [
            _section(
                _rows(
                    160,
                    confidence=lambda s: 0.15 + 0.2 * (s % 5),
                    wrong=set(range(0, 160, 7)),
                )
            )
        ],
        _truth(range(160))["rows_typed"],
        LIST_KEY,
    )
    from_payload = analyze.pool_calibration([scored["calibration_curve"]])
    for pooled_key, scored_key in (
        ("observations", "calibration_observations"),
        ("correct", "calibration_correct"),
        ("ece", "calibration_ece"),
        ("ece_mean_conf", "calibration_ece_mean_conf"),
        ("auroc", "calibration_auroc"),
        ("auroc_unbinned", "calibration_auroc_unbinned"),
        ("brier", "calibration_brier"),
        ("bin_coverage", "calibration_bin_coverage"),
    ):
        assert from_payload[pooled_key] == scored[scored_key], pooled_key


def test_a_payload_without_a_value_tally_makes_the_pooled_unbinned_auroc_none():
    """Every summary scored before the tally existed is in this state. A pooled figure
    computed from the payloads that DO have one would be a real number over the wrong
    population, which is worse than an absent one."""
    scored = analyze.score_calibration(
        [_section(_rows(120, confidence=0.75, wrong={1, 2}))],
        _truth(range(120))["rows_typed"],
        LIST_KEY,
    )
    with_tally = scored["calibration_curve"]
    without = {k: v for k, v in with_tally.items() if k != "valueTally"}
    assert analyze.pool_calibration([with_tally])["auroc_unbinned"] is not None
    assert analyze.pool_calibration([with_tally, without])["auroc_unbinned"] is None
    # Everything the bin counts DO support is still reported.
    assert analyze.pool_calibration([with_tally, without])["ece"] is not None


def test_the_unbinned_auroc_matches_sticklers_over_raw_pairs():
    """The tally-based computation replaced a call into Stickler, which removed the
    harness's only dependency on the ``[evaluation]`` extra from the default scoring
    path. The equivalence is the reason that was safe, so it is asserted rather than
    assumed — under heavy ties, at both extremes of ranking, and over many distinct
    values.

    Not on a single class: AUROC is undefined there, so there is no value to be equal
    to. That case is
    :func:`test_the_unbinned_auroc_is_none_when_a_class_is_absent_or_the_tally_is_dropped`,
    which asserts ``None``.
    """
    pytest.importorskip("stickler")
    from idp_common.evaluation.stickler_backend.confidence import AUROCMetric

    def stickler_auroc(observations):
        return AUROCMetric().compute(_stickler_pairs(observations)).get("value")

    cases = [
        # Heavy ties at one value — the shipped default's actual shape.
        [(1.0, i % 20 != 0) for i in range(400)],
        # Wrong cells at the bottom: a perfect ranker.
        [(0.2 if i % 10 == 0 else 0.9, i % 10 != 0) for i in range(300)],
        # Wrong cells at the TOP: worse than chance, and the sign must survive.
        [(0.95 if i % 10 == 0 else 0.3, i % 10 != 0) for i in range(300)],
        # Many distinct values with ties inside some of them.
        [(round(0.05 * (i % 19), 4), (i * 7) % 11 != 0) for i in range(500)],
    ]
    for observations in cases:
        mine = analyze.unbinned_auroc(analyze.value_tally(observations))
        theirs = stickler_auroc(observations)
        assert mine is not None and theirs is not None
        assert abs(mine - theirs) < 1e-12, (mine, theirs)


def test_the_unbinned_auroc_is_none_when_a_class_is_absent_or_the_tally_is_dropped():
    assert analyze.unbinned_auroc(analyze.value_tally([(0.9, True)] * 50)) is None
    assert analyze.unbinned_auroc(analyze.value_tally([(0.9, False)] * 50)) is None
    assert analyze.unbinned_auroc(None) is None
    # Past the cap the tally is dropped whole rather than truncated: half a tally
    # would yield a confident AUROC over part of the data. Derived from the constant
    # ON PURPOSE here — this assertion is about the drop-whole BEHAVIOUR at whatever
    # the cap is. The cap's VALUE is pinned separately, below, because a case derived
    # from the constant cannot pin it.
    many = [
        (i / (analyze.MAX_TALLY_VALUES * 2), i % 3 != 0)
        for i in range(analyze.MAX_TALLY_VALUES * 2)
    ]
    assert analyze.value_tally(many) is None


def test_the_tally_cap_is_pinned_at_a_literal_and_brackets_the_real_distribution():
    """``MAX_TALLY_VALUES`` is free to move unless a literal says otherwise.

    Its only other test builds ``MAX_TALLY_VALUES * 2`` distinct values, so it passes
    for *every* value of the constant: measured, raising it 390x to 100,000 and lowering
    it to 64 both left the whole suite green. That is the same free-to-move-constant
    shape the two regression thresholds were ratcheted against, in the same file, and
    ``COMPACT_PAYLOAD_KEYS`` a few hundred lines below already applies the opposite
    discipline for the same reason.

    The cap has two jobs and the literals below pin both. It must sit ABOVE the real
    distribution, or a legitimate document silently loses its unbinned AUROC: the
    published corpus emits 1 to 8 distinct confidence values per document-run, and the
    widest single document sampled reaches 6. And it must sit far enough BELOW
    per-cell-distinct to actually bound memory on a grader that emits one value per
    cell, where a 1,600-row document would otherwise tally 3,200 entries.
    """
    assert analyze.MAX_TALLY_VALUES == 256

    # Above the distribution: 8 distinct values is the widest arm measured on the
    # published grid (Sonnet 5, `separate`), and 64 is comfortable headroom over it.
    kept = analyze.value_tally([(i / 64, i % 3 != 0) for i in range(64)])
    assert kept is not None and len(kept) == 64

    # At the cap, kept; one past it, dropped whole. Written as literals derived from
    # nothing, so a change to the constant fails here rather than silently re-deriving.
    assert len(analyze.value_tally([(i / 256, i % 3 != 0) for i in range(256)])) == 256
    assert analyze.value_tally([(i / 257, i % 3 != 0) for i in range(257)]) is None

    # Below per-cell-distinct on the largest corpus document, which is what the cap is
    # for: 3,200 cells on `xl_narrow` would tally far past it.
    assert analyze.MAX_TALLY_VALUES < 3200


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


#: "not given", as distinct from an explicit ``None`` — which is a meaningful value
#: here, because an undefined AUROC is exactly one of the states under test.
_DEFAULT = object()


def _cell(observations, ece, auroc, ece_mean_conf=_DEFAULT, auroc_unbinned=_DEFAULT):
    """A minimal pooled-calibration block.

    ``ece_mean_conf`` and ``auroc_unbinned`` default to their binned/midpoint
    counterparts so the simple cases below describe a cell whose two estimators of each
    metric agree. The cases that matter are the ones where they do NOT, because that is
    the whole reason the gate reads different ones for the magnitude and the crossing.
    """
    return {
        "calibration": {
            "observations": observations,
            "ece": ece,
            "ece_mean_conf": ece if ece_mean_conf is _DEFAULT else ece_mean_conf,
            "auroc": auroc,
            "auroc_unbinned": auroc if auroc_unbinned is _DEFAULT else auroc_unbinned,
        }
    }


def test_the_gate_flags_a_calibration_error_that_grows():
    findings = aggregate.calibration_findings(
        _cell(500, 0.09, 0.80), _cell(500, 0.02, 0.80), regression=True
    )
    assert len(findings) == 1
    assert "calibration ECE +0.070" in findings[0]


def test_the_magnitude_check_reads_the_estimator_that_can_actually_move():
    """The midpoint ECE is nearly immobile, so a magnitude gate reading it is blind.

    Confidence drifts 0.97 -> 0.995 with accuracy held at 0.97 and all mass in the
    top bin — the exact shape the study shows the shipped default produces. Real
    calibration error worsens by 0.025; the midpoint estimator does not move at all,
    because both confidences are in the same bin and it compares against that bin's
    MIDPOINT. The accuracy gate is silent too, since accuracy did not change.
    """
    base = _cell(500, 0.02, 0.80, ece_mean_conf=0.000)
    cur = _cell(500, 0.02, 0.80, ece_mean_conf=0.025)
    # The midpoint estimator is identical on both sides: a gate reading it sees zero.
    assert base["calibration"]["ece"] == cur["calibration"]["ece"]
    findings = aggregate.calibration_findings(cur, base, regression=True)
    assert len(findings) == 1
    assert "calibration ECE +0.025" in findings[0]


def test_a_worsening_calibration_error_is_never_reported_as_an_improvement():
    """The inverted case. Confidence is held at 0.995 and accuracy falls 0.999 ->
    0.95, so the real error worsens by 0.041 while the midpoint estimator moves
    -0.049 — which a gate reading it calls an improvement, in the direction that
    flatters the release."""
    base = _cell(500, 0.049, 0.80, ece_mean_conf=0.004)
    cur = _cell(500, 0.000, 0.80, ece_mean_conf=0.045)
    regressions = aggregate.calibration_findings(cur, base, regression=True)
    improvements = aggregate.calibration_findings(cur, base, regression=False)
    assert len(regressions) == 1
    assert "calibration ECE +0.041" in regressions[0]
    assert improvements == []


def test_a_genuine_calibration_improvement_is_still_reported():
    """The control for the test above — the direction has to survive both ways."""
    improvements = aggregate.calibration_findings(
        _cell(500, 0.02, 0.80, ece_mean_conf=0.004),
        _cell(500, 0.02, 0.80, ece_mean_conf=0.045),
        regression=False,
    )
    assert len(improvements) == 1
    assert "calibration ECE -0.041" in improvements[0]


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


def test_the_auroc_magnitude_check_reads_the_estimator_that_can_actually_move():
    """The AUROC arm of the same defect the ECE arm was fixed for.

    The curve's BINNED AUROC is what the shipped gate reads, and on this corpus it is
    almost immobile: across the 216 committed cells carrying a calibration block it is
    undefined on 183 and exactly 0.5000 on 29 of the 33 where it is defined, because a
    single-bin curve makes every pair a tie. Here both sides sit at that 0.5, while the
    unbinned value falls 0.62 -> 0.54. A magnitude gate reading the binned number sees
    nothing at all.
    """
    base = _cell(500, 0.02, 0.5, auroc_unbinned=0.62)
    cur = _cell(500, 0.02, 0.5, auroc_unbinned=0.54)
    assert base["calibration"]["auroc"] == cur["calibration"]["auroc"]
    findings = aggregate.calibration_findings(cur, base, regression=True)
    assert len(findings) == 1
    assert "confidence AUROC -0.080" in findings[0]
    # The crossing arm is silent, and cannot do otherwise: 0.5 is below the 0.55 bar on
    # both sides, so there is nothing to cross. That is exactly why the magnitude arm
    # must not read the same value.
    assert "CROSSED" not in findings[0]


def test_the_auroc_crossing_check_still_reads_the_gates_own_binned_value():
    """``AUROC_UNRELIABLE_THRESHOLD`` is applied by the shipped product to
    ``CalibrationHealth.auroc``, so a crossing computed from the unbinned value would
    not predict what the review-effort estimator does. The unbinned value is held
    constant here, so the finding can only have come from the crossing."""
    from idp_common.evaluation.confidence_curve import AUROC_UNRELIABLE_THRESHOLD

    findings = aggregate.calibration_findings(
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD, auroc_unbinned=0.80),
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD + 0.005, auroc_unbinned=0.80),
        regression=True,
    )
    assert len(findings) == 1
    assert "CROSSED" in findings[0]
    assert "binned estimator" in findings[0]


def test_an_auroc_undefined_on_one_side_still_reports_a_crossing():
    """An arm can lose its unbinned value (no wrong cells to rank) while the binned one
    still crosses the bar. Reporting nothing there would hide a product-behaviour change
    behind a missing statistic."""
    from idp_common.evaluation.confidence_curve import AUROC_UNRELIABLE_THRESHOLD

    findings = aggregate.calibration_findings(
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD, auroc_unbinned=None),
        _cell(500, 0.02, AUROC_UNRELIABLE_THRESHOLD + 0.01, auroc_unbinned=0.80),
        regression=True,
    )
    assert len(findings) == 1
    assert "confidence AUROC unmeasured" in findings[0]
    assert "CROSSED" in findings[0]


def test_a_tiny_ece_move_across_the_unreliable_bar_is_a_regression():
    """And the crossing check reads the GATE's estimator, not the mean-confidence one.

    ``ECE_UNRELIABLE_THRESHOLD`` is applied by the shipped product to
    ``CalibrationHealth.ece``, so a crossing computed from any other estimator would
    not predict what the review-effort estimator does. Here the mean-confidence value
    is held constant on both sides, so the magnitude check contributes nothing and the
    finding can only have come from the crossing.
    """
    from idp_common.evaluation.confidence_curve import ECE_UNRELIABLE_THRESHOLD

    findings = aggregate.calibration_findings(
        _cell(500, ECE_UNRELIABLE_THRESHOLD + 0.001, 0.80, ece_mean_conf=0.01),
        _cell(500, ECE_UNRELIABLE_THRESHOLD, 0.80, ece_mean_conf=0.01),
        regression=True,
    )
    assert len(findings) == 1
    assert "CROSSED" in findings[0]
    assert f"{ECE_UNRELIABLE_THRESHOLD}" in findings[0]


def test_the_crossing_check_fires_on_a_grossly_overconfident_cell():
    """The end-to-end case: a cell whose accuracy collapses to 0.70 while confidence
    stays near 1.0 crosses the unreliable bar on the gate's own estimator, and the
    magnitude check agrees. Built from real observations rather than hand-written
    numbers, so it exercises the whole path from the join to the report line."""
    from idp_common.evaluation.confidence_curve import ECE_UNRELIABLE_THRESHOLD

    n = 400
    healthy = analyze.score_calibration(
        [_section(_rows(n, confidence=1.0))], _truth(range(n))["rows_typed"], LIST_KEY
    )
    broken = analyze.score_calibration(
        [
            _section(
                _rows(
                    n, confidence=1.0, wrong=set(range(0, n, 10)) | set(range(1, n, 5))
                )
            )
        ],
        _truth(range(n))["rows_typed"],
        LIST_KEY,
    )
    base = {"calibration": analyze.pool_calibration([healthy["calibration_curve"]])}
    cur = {"calibration": analyze.pool_calibration([broken["calibration_curve"]])}
    assert cur["calibration"]["accuracy"] < 0.75
    assert cur["calibration"]["ece"] > ECE_UNRELIABLE_THRESHOLD
    assert base["calibration"]["ece"] <= ECE_UNRELIABLE_THRESHOLD
    findings = aggregate.calibration_findings(cur, base, regression=True)
    assert any("CROSSED" in f for f in findings)


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


def test_a_baseline_that_predates_the_metric_is_reported_as_unread_not_as_silence(
    capsys, tmp_path
):
    """Skipping is right; being indistinguishable from a passing gate is not.

    A baseline with no `calibration` block makes every comparison vacuous, and with no
    output at all a reader sees the same empty regression list they would see from a
    clean run. That is the "control that exists but is never consulted" defect, so
    `compare_cells` names the metric, the count of affected cells, and the remedy.
    Covers `conf_coverage` (#997) in the same state for the same reason.
    """
    scored = analyze.score_calibration(
        [_section(_rows(60, confidence=0.85, wrong={0, 1}))],
        _truth(range(60))["rows_typed"],
        LIST_KEY,
    )
    coverage = analyze.score_confidence_coverage([_section(_rows(60))])
    row = {
        "cell": "c",
        "doc": "d.pdf",
        "sub_doc": None,
        "repeat": 0,
        "success": True,
        "cost": 0.01,
        "scalar_accuracy": 1.0,
        **coverage,
        **scored,
    }
    baseline_row = {
        k: v
        for k, v in row.items()
        if not k.startswith("calibration") and not k.startswith("conf_")
    }
    cur_path = tmp_path / "cur.json"
    base_path = tmp_path / "base.json"
    cur_path.write_text(json.dumps({"meta": {}, "rows": [row]}))
    base_path.write_text(json.dumps({"meta": {}, "rows": [baseline_row]}))

    aggregate.compare_cells(str(cur_path), str(base_path))
    out = capsys.readouterr().out
    assert "NOT COMPARED" in out
    assert "#935" in out and "#997" in out
    assert "INERT" in out


def test_nothing_is_reported_as_unread_when_both_sides_have_the_metric():
    """The control. A metric present on both sides is compared, so naming it as unread
    would be noise on every future run."""
    scored = analyze.score_calibration(
        [_section(_rows(60, confidence=0.85, wrong={0, 1}))],
        _truth(range(60))["rows_typed"],
        LIST_KEY,
    )
    cell = {
        "calibration": analyze.pool_calibration([scored["calibration_curve"]]),
        "conf_coverage": aggregate._stats([1.0, 0.98]),
    }
    assert aggregate._missing_metric_notes(cell, cell) == []
    # And absent on BOTH sides is not a baseline problem either.
    assert aggregate._missing_metric_notes({}, {}) == []


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


def test_a_grid_that_never_recorded_the_metric_is_reported_too(capsys, tmp_path):
    """The REVERSE direction, and the one that is common.

    Reporting only "current has it, baseline does not" left the mirror case silent, and
    the mirror case is the state of most committed grids: only a handful of the 96
    committed summaries carry a calibration statistic. Comparing the release procedure's
    own named pair — a `corefast` grid against the backfilled `baseline.json` — therefore
    produced zero findings and no note, output indistinguishable from a clean run, and
    for the `IDPUpg068to069` grid it cannot be fixed at all: that stack's KMS key is
    pending deletion, so no re-score can reach its objects.
    """
    scored = analyze.score_calibration(
        [_section(_rows(60, confidence=0.85, wrong={0, 1}))],
        _truth(range(60))["rows_typed"],
        LIST_KEY,
    )
    coverage = analyze.score_confidence_coverage([_section(_rows(60))])
    base_row = {
        "cell": "c",
        "doc": "d.pdf",
        "sub_doc": None,
        "repeat": 0,
        "success": True,
        "cost": 0.01,
        "scalar_accuracy": 1.0,
        **coverage,
        **scored,
    }
    # The CURRENT side is the one missing the metric this time.
    cur_row = {
        k: v
        for k, v in base_row.items()
        if not k.startswith("calibration") and not k.startswith("conf_")
    }
    cur_path, base_path = tmp_path / "cur.json", tmp_path / "base.json"
    cur_path.write_text(json.dumps({"meta": {}, "rows": [cur_row]}))
    base_path.write_text(json.dumps({"meta": {}, "rows": [base_row]}))

    aggregate.compare_cells(str(cur_path), str(base_path))
    out = capsys.readouterr().out
    assert "NOT COMPARED" in out
    assert "THIS RUN does not" in out
    assert "#935" in out and "#997" in out
    # And the direction is distinguishable in the returned structure, not only in prose.
    cur_cell = aggregate._cells(json.loads(cur_path.read_text()))["c"]
    base_cell = aggregate._cells(json.loads(base_path.read_text()))["c"]
    assert all(
        side == "current"
        for _label, side in aggregate._missing_metric_notes(cur_cell, base_cell)
    )
    assert all(
        side == "baseline"
        for _label, side in aggregate._missing_metric_notes(base_cell, cur_cell)
    )


# ------------------------------------------------------- the thresholds are ratcheted


def test_the_ece_threshold_is_not_free_to_be_lowered():
    """The threshold's own value is pinned, in both directions.

    Reverting the ESTIMATOR fails three tests above and reverting the threshold to the
    old 0.03 fails one, but nothing stopped the value being lowered: dropping it 100x to
    0.0001 left every test in this file passing, because no test read
    ``CALIBRATION_ECE_REGRESSION`` and nothing asserted that a sub-threshold move stays
    SILENT. A gate that fires on everything is as uninformative as one that fires on
    nothing, and it would fire here — the drift measured below is what an UNCHANGED
    configuration does between releases.
    """
    assert aggregate.CALIBRATION_ECE_REGRESSION == 0.01

    # The largest mean-confidence ECE drift measured for a configuration-matched arm
    # across the two published releases: `integrated`/`nova_lite`/`sonnet5`, 0.0036 ->
    # 0.0017 on v0.6.8 -> v0.6.9. Real data, unchanged configuration, and therefore the
    # floor the threshold has to sit above.
    observed_arm_drift = 0.0019
    assert aggregate.CALIBRATION_ECE_REGRESSION > observed_arm_drift

    for direction in (+1, -1):
        base = _cell(5000, 0.02, 0.80, ece_mean_conf=0.0036)
        cur = _cell(5000, 0.02, 0.80, ece_mean_conf=0.0036 + direction * 0.0019)
        assert aggregate.calibration_findings(cur, base, regression=True) == []
        assert aggregate.calibration_findings(cur, base, regression=False) == []

    # The largest WORSENING measured at CELL granularity, which is the granularity the
    # gate fires at: +0.0088, on a cell whose pooled sample collapsed 4,410 -> 410. It
    # must also stay silent, and the margin is only 1.14x — see the constant's comment.
    base = _cell(4410, 0.02, 0.80, ece_mean_conf=0.0010)
    cur = _cell(410, 0.02, 0.80, ece_mean_conf=0.0098)
    assert aggregate.calibration_findings(cur, base, regression=True) == []


def test_the_auroc_threshold_is_not_free_to_be_lowered():
    """Same ratchet on the ranking-power side. The largest unbinned-AUROC drift for a
    configuration-matched arm across the two releases is 0.0461, which is 92% of the
    0.05 threshold — so this one has very little margin and a reduction would start
    reporting unchanged configurations as regressions immediately."""
    assert aggregate.CALIBRATION_AUROC_REGRESSION == 0.05

    observed_arm_drift = 0.0461
    assert aggregate.CALIBRATION_AUROC_REGRESSION > observed_arm_drift

    for direction in (+1, -1):
        base = _cell(5000, 0.02, 0.50, auroc_unbinned=0.3817)
        cur = _cell(5000, 0.02, 0.50, auroc_unbinned=0.3817 + direction * 0.0461)
        assert aggregate.calibration_findings(cur, base, regression=True) == []
        assert aggregate.calibration_findings(cur, base, regression=False) == []


# ------------------------------------------- the skipped buckets, and the artifact shape


def test_a_run_with_no_confidence_is_counted_apart_from_one_with_no_joinable_cell():
    """The two halves of what used to be a single ``no_observations`` bucket.

    They mean opposite things — the first is a configuration (`confidence.mode: off`),
    the second is an extraction that returned nothing joinable — and pooling them
    produced a count whose only escalation rule ("chase it when it exceeds the
    off-cells") fired on the published grid's own output, where 55 are the first and 34
    the second.
    """
    assert aggregate._empty_curve_reason(0) == "no_confidence"
    assert aggregate._empty_curve_reason(None) == "no_confidence"
    assert aggregate._empty_curve_reason(1) == "no_joinable_cell"
    assert aggregate._empty_curve_reason(148) == "no_joinable_cell"


def test_the_numeric_payloads_are_written_on_one_line(tmp_path):
    """Compaction is a formatting choice with a measured justification, so the shape is
    pinned: expanded, the calibration payloads were ~104,700 of the ~130,800 lines the
    #935 backfill added to the committed summaries, over 80,000 of them holding one
    number each. The `_stats` siblings stay expanded — see ``COMPACT_PAYLOAD_KEYS``.
    """
    scored = analyze.score_calibration(
        [_section(_rows(60, confidence=0.85, wrong={0, 1}))],
        _truth(range(60))["rows_typed"],
        LIST_KEY,
    )
    row = {"cell": "c", "doc": "d.pdf", "repeat": 0, "success": True, **scored}
    summary = {
        "meta": {"stack": "S"},
        "rows": [row],
        "cell_stats": aggregate.cell_stats([row]),
    }
    path = tmp_path / "summary.json"
    aggregate.dump_summary(summary, str(path))
    text = path.read_text()

    # Round-trips exactly: compaction must not be able to change a value.
    assert json.loads(text) == json.loads(json.dumps(summary))

    # Named explicitly, not read from the constant: iterating the constant would make
    # this test vacuous the moment someone emptied it.
    assert set(aggregate.COMPACT_PAYLOAD_KEYS) == {
        "calibration_curve",
        "calibration",
        "conf_coverage",
    }

    lines = text.splitlines()
    for key in ("calibration_curve", "calibration", "conf_coverage"):
        holding = [ln for ln in lines if f'"{key}": {{' in ln]
        assert holding, key
        for ln in holding:
            assert ln.rstrip().rstrip(",").endswith("}"), (key, ln[:120])
    # A `_stats` sibling that predates this change is NOT compacted, so the deleted-lines
    # -are-punctuation property of the backfill diff holds.
    assert any(ln.rstrip().endswith('"cost": {') for ln in lines)
