# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the confidence→accuracy curve and the review-effort estimator.

These focus less on arithmetic than on the properties that make a partial-review
recommendation safe: the curve is monotonic where it should be, a thin curve defers
to the prior instead of swinging on noise, and the failure modes that would let the
estimator certify an inaccurate set are detected rather than assumed away.
"""

import random

import pytest

from idp_common.evaluation import confidence_curve as cc
from idp_common.evaluation.confidence_curve import (
    ConfidenceCurve,
    EstimateConfidence,
    estimate_for_target,
)

pytestmark = pytest.mark.unit


def _calibrated_curve(n=600, seed=7):
    """A well-calibrated curve: P(correct) tracks confidence.

    Beta(9, 1.2) concentrates confidence near 1.0, matching real extraction output; a
    uniform distribution would imply ~50% of fields are wrong.
    """
    rng = random.Random(seed)
    observations = []
    for _ in range(n):
        confidence = min(1.0, max(0.0, rng.betavariate(9, 1.2)))
        observations.append((confidence, rng.random() < confidence))
    curve = ConfidenceCurve.from_observations(observations)
    # Mark as scoring-derived: only a scoring run measures the high-confidence
    # zone, and without that the curve can't claim to be fully measured.
    curve.scoring_observations = n
    return curve


def _doc_confidences(n=120, seed=11):
    rng = random.Random(seed)
    return [min(1.0, max(0.0, rng.betavariate(9, 1.2))) for _ in range(n)]


class TestBinning:
    def test_bin_index_covers_the_whole_range(self):
        assert cc.bin_index(0.0) == 0
        assert cc.bin_index(0.05) == 0
        assert cc.bin_index(0.5) == 5
        # 1.0 belongs in the top bin, not one past the end.
        assert cc.bin_index(1.0) == cc.BIN_COUNT - 1
        assert cc.bin_index(0.999) == cc.BIN_COUNT - 1

    def test_out_of_range_confidence_is_dropped_not_clamped(self):
        """A NaN or 5.0 confidence is an upstream bug; binning it would hide it."""
        curve = ConfidenceCurve()
        accepted = curve.add_observations(
            [
                (0.5, True),
                (float("nan"), True),
                (float("inf"), True),
                (5.0, True),
                (-0.2, True),
                (None, True),
                ("junk", True),
            ]
        )
        assert accepted == 1
        assert curve.total_observations == 1


class TestPriorBlending:
    def test_thin_evidence_defers_to_the_prior(self):
        """Two observations must not swing the curve away from the prior."""
        prior = ConfidenceCurve.from_observations([(0.85, True)] * 200)
        # One unlucky bin: 2 observations, both wrong.
        curve = ConfidenceCurve.from_observations([(0.85, False), (0.85, False)])
        blended = curve.accuracy_at(0.85, prior)
        # Prior says ~1.0, raw observation says 0.0; blending must stay high.
        assert blended > 0.85, blended

    def test_ample_evidence_displaces_the_prior(self):
        prior = ConfidenceCurve.from_observations([(0.85, True)] * 200)
        curve = ConfidenceCurve.from_observations([(0.85, False)] * 200)
        blended = curve.accuracy_at(0.85, prior)
        assert blended < 0.15, blended

    def test_no_prior_falls_back_to_taking_confidence_at_face_value(self):
        """With nothing measured, assume the model's confidence is honest.

        That's the neutral assumption — what a perfectly calibrated model would
        give — rather than inventing an optimistic or pessimistic default.
        """
        empty = ConfidenceCurve()
        assert empty.accuracy_at(0.25) == pytest.approx(0.25, abs=0.05)
        assert empty.accuracy_at(0.95) == pytest.approx(0.95, abs=0.05)


class TestCalibrationHealth:
    def test_overconfidence_is_flagged(self):
        """Confident and wrong — errors hide where review never looks.

        This is the failure mode that lets a set be certified accurate while
        being wrong, so it must never read as reliable.
        """
        curve = ConfidenceCurve.from_observations([(0.95, False)] * 60)
        health = curve.calibration_health()
        assert health.overconfident
        assert not health.reliable
        assert curve.assess_estimate_confidence() is EstimateConfidence.UNRELIABLE

    def test_degenerate_confidence_is_flagged_even_though_ece_looks_perfect(self):
        """The case ECE alone cannot catch.

        When every field scores 0.85 and ~85% are right, ECE is ~0 — a single
        bin is trivially well-calibrated — but there is no signal to rank by, so
        worst-first ordering is arbitrary. Bin coverage is what catches it.
        """
        curve = ConfidenceCurve.from_observations(
            [(0.85, True)] * 51 + [(0.85, False)] * 9
        )
        health = curve.calibration_health()
        assert health.ece is not None and health.ece < 0.05, health.ece
        assert health.bin_coverage == 1
        assert health.degenerate
        assert not health.reliable

    def test_undiscriminating_confidence_is_flagged_when_ece_looks_fine(self):
        """The case ECE *and* bin coverage both miss.

        A grader can spread scores across many bins with a low ECE — passing both
        of those gates — and still rank correctness worse than chance. Here every
        error hides in the >=0.9 bin while the flagged low-confidence fields are all
        actually correct, so worst-first review reaches none of them.
        """
        curve = ConfidenceCurve()
        for index, (n, p_correct) in {
            2: (1, 1.0),
            4: (1, 1.0),
            5: (1, 1.0),
            6: (4, 1.0),
            7: (7, 1.0),
            8: (22, 1.0),
            9: (3972, 0.9806),
        }.items():
            curve.total[index] = n
            curve.correct[index] = round(n * p_correct)

        health = curve.calibration_health()
        assert health.ece is not None and health.ece < 0.05, health.ece
        assert health.bin_coverage == 7  # passes the degenerate gate
        assert not health.degenerate
        assert not health.overconfident
        assert health.auroc is not None and health.auroc <= 0.55, health.auroc
        assert health.undiscriminating
        assert not health.reliable

    def test_good_ranker_is_reliable_despite_worse_ece(self):
        """The converse case: worse ECE, far better ranking.

        A higher ECE than the undiscriminating curve above, but most errors are
        reachable worst-first. Ranking power is what the estimate depends on, so this
        must read as reliable.
        """
        curve = ConfidenceCurve()
        for index, (n, p_correct) in {
            4: (5, 0.4),
            5: (41, 0.9756),
            6: (40, 0.95),
            7: (203, 0.9113),
            8: (99, 0.8687),
            9: (3652, 0.9926),
        }.items():
            curve.total[index] = n
            curve.correct[index] = round(n * p_correct)

        health = curve.calibration_health()
        assert health.auroc is not None and health.auroc > 0.55, health.auroc
        assert not health.undiscriminating
        assert health.reliable

    def test_auroc_is_chance_for_a_single_bin(self):
        """A one-bin curve cannot rank anything, so it must not look perfect."""
        curve = ConfidenceCurve.from_observations(
            [(0.95, True)] * 980 + [(0.95, False)] * 20
        )
        assert curve.calibration_health().auroc == 0.5

    def test_auroc_is_none_without_both_classes(self):
        """With no errors (or nothing correct) ranking is undefined, not perfect."""
        assert ConfidenceCurve.from_observations([(0.9, True)] * 50).auroc() is None
        assert ConfidenceCurve.from_observations([(0.9, False)] * 50).auroc() is None

    def test_auroc_gate_does_not_fire_on_thin_samples(self):
        """Below the observation floor AUROC is noise, so the gate must hold off."""
        curve = ConfidenceCurve.from_observations(
            [(0.95, False)] * 5 + [(0.1, True)] * 5
        )
        health = curve.calibration_health()
        assert health.auroc is not None and health.auroc < 0.55
        assert not health.undiscriminating

    def test_well_calibrated_curve_is_reliable(self):
        health = _calibrated_curve().calibration_health()
        assert health.reliable
        assert not health.degenerate
        assert not health.overconfident

    def test_no_observations_is_not_treated_as_a_problem(self):
        """An unmeasured curve is unknown, not miscalibrated."""
        health = ConfidenceCurve().calibration_health()
        assert health.reliable
        assert health.ece is None


class TestEstimateConfidenceStates:
    def test_cold_start_reports_prior(self):
        assert (
            ConfidenceCurve().assess_estimate_confidence() is EstimateConfidence.PRIOR
        )

    def test_review_only_observations_stay_partially_measured(self):
        """Worst-first review never populates the high-confidence end.

        So even a lot of review observations cannot claim to have measured the
        zone the estimator auto-accepts — only a scoring run can.
        """
        rng = random.Random(5)
        # Correctness must track confidence, or the curve is undiscriminating and
        # correctly reports UNRELIABLE for that reason instead — which would test
        # the ranking gate rather than this test's subject (provenance).
        curve = ConfidenceCurve.from_observations(
            [
                (c, rng.random() < c + 0.4)
                for c in (rng.uniform(0.0, 0.5) for _ in range(200))
            ]
        )
        assert curve.scoring_observations == 0
        assert (
            curve.assess_estimate_confidence() is EstimateConfidence.PARTIALLY_MEASURED
        )

    def test_scoring_run_reaches_measured(self):
        assert (
            _calibrated_curve().assess_estimate_confidence()
            is EstimateConfidence.MEASURED
        )


class TestSticklerIngestion:
    def test_ece_bins_fold_in_and_produce_a_monotonic_curve(self):
        """Stickler's ECEMetric output is the curve — ingest it, don't refit it."""
        from stickler.structured_object_evaluator.models.confidence import (
            ConfidencePair,
            ECEMetric,
        )

        rng = random.Random(3)
        pairs = [
            ConfidencePair(is_match=rng.random() < c, confidence=c, similarity=1.0)
            for c in (rng.random() for _ in range(400))
        ]
        bins = ECEMetric().compute(pairs)["bins"]

        curve = ConfidenceCurve()
        accepted = curve.add_ece_bins(bins)

        assert accepted == 400
        assert curve.scoring_observations == 400
        # Low confidence should predict much lower accuracy than high confidence.
        assert curve.accuracy_at(0.15) < 0.4
        assert curve.accuracy_at(0.95) > 0.8

    def test_empty_or_malformed_bins_are_ignored(self):
        curve = ConfidenceCurve()
        assert curve.add_ece_bins([]) == 0
        assert curve.add_ece_bins(None) == 0
        assert (
            curve.add_ece_bins(
                [
                    {"count": 0, "accuracy": 1.0, "range": [0.0, 0.1]},
                    {"count": 5, "accuracy": None, "range": [0.1, 0.2]},
                    {"count": 5, "accuracy": 1.0, "range": [0.2]},  # bad range
                ]
            )
            == 0
        )


class TestPersistence:
    def test_round_trips(self):
        curve = _calibrated_curve(n=100)
        curve.test_set_id = "ts1"
        curve.config_version = "v3"
        restored = ConfidenceCurve.from_dict(curve.to_dict())
        assert restored.total == curve.total
        assert restored.correct == curve.correct
        assert restored.test_set_id == "ts1"
        assert restored.config_version == "v3"
        assert restored.scoring_observations == curve.scoring_observations

    def test_from_dict_tolerates_a_different_bin_count(self):
        """A stored curve from another bin layout must not crash the resolver."""
        short = ConfidenceCurve.from_dict({"correct": [1.0], "total": [2.0]})
        assert len(short.total) == cc.BIN_COUNT
        long = ConfidenceCurve.from_dict({"correct": [1.0] * 50, "total": [2.0] * 50})
        assert len(long.total) == cc.BIN_COUNT

    def test_from_dict_handles_missing_data(self):
        assert ConfidenceCurve.from_dict(None).total_observations == 0
        assert ConfidenceCurve.from_dict({}).total_observations == 0


class TestAuditSample:
    def test_sized_for_detection_power_not_review_depth(self):
        """The least-reviewed case needs the *most* auditing, not the least.

        Scaling the audit sample with review depth would shrink it to nothing
        exactly when almost everything was auto-accepted — the situation where
        confident errors are most likely to reach a published set.
        """
        barely_reviewed = cc._audit_sample_size(reviewed=5, auto_accepted=1000)
        heavily_reviewed = cc._audit_sample_size(reviewed=500, auto_accepted=1000)
        assert barely_reviewed == heavily_reviewed == cc.AUDIT_SAMPLE_TARGET

    def test_no_audit_when_everything_was_reviewed(self):
        assert cc._audit_sample_size(reviewed=120, auto_accepted=0) == 0

    def test_capped_for_small_pools(self):
        assert cc._audit_sample_size(0, 8) <= 8
        assert cc._audit_sample_size(0, 100) <= cc.AUDIT_SAMPLE_TARGET


class TestEstimateForTarget:
    def test_tighter_targets_require_more_review(self):
        curve = _calibrated_curve()
        docs = _doc_confidences(120)
        results = [
            estimate_for_target(curve, target, 120, docs).docs_to_review
            for target in (90.0, 95.0, 99.0, 99.5)
        ]
        assert results == sorted(results), results
        assert results[0] < results[-1]

    def test_residual_error_meets_the_target(self):
        curve = _calibrated_curve()
        docs = _doc_confidences(120)
        estimate = estimate_for_target(curve, 99.0, 120, docs)
        # Residual error at the recommended depth must actually satisfy the ask.
        assert estimate.residual_error <= 0.01 + 1e-9

    def test_cutoff_is_the_boundary_of_the_reviewed_set(self):
        curve = _calibrated_curve()
        docs = _doc_confidences(60)
        estimate = estimate_for_target(curve, 99.0, 60, docs)
        if estimate.docs_to_review:
            ordered = sorted(docs)
            assert estimate.implied_cutoff == pytest.approx(
                round(ordered[estimate.docs_to_review - 1], 4)
            )

    def test_effort_includes_the_audit_sample(self):
        """The audit is real work; excluding it would understate the cost."""
        curve = _calibrated_curve()
        docs = _doc_confidences(120)
        estimate = estimate_for_target(curve, 95.0, 120, docs)
        per_doc_seconds = (
            cc.DEFAULT_SECONDS_PER_DOC
            + cc.DEFAULT_FIELDS_PER_DOC
            * cc.DEFAULT_ALERT_RATE
            * cc.DEFAULT_SECONDS_PER_ALERT
            + cc.DEFAULT_PAGES_PER_DOC * cc.DEFAULT_SECONDS_PER_PAGE
        )
        expected = (
            (estimate.docs_to_review + estimate.audit_sample_size)
            * per_doc_seconds
            / 60.0
        )
        assert estimate.effort_minutes == pytest.approx(expected)
        assert estimate.audit_sample_size > 0

    def test_effort_scales_with_alerts_not_total_fields(self):
        """Regression: effort was charged per field, so a wide document with almost
        nothing flagged cost as much to review as one full of suspect values.

        A 200-field document with 3 alerts is a few checks plus a skim, not 200
        verifications. Charging per field made it ~47 minutes and made effort
        independent of how much was actually wrong.
        """
        curve = _calibrated_curve()
        docs = _doc_confidences(40)

        wide_but_clean = estimate_for_target(
            curve, 95.0, 40, docs, fields_per_doc=200.0, alerts_per_doc=3.0
        )
        narrow_but_messy = estimate_for_target(
            curve, 95.0, 40, docs, fields_per_doc=12.0, alerts_per_doc=10.0
        )

        per_reviewed = wide_but_clean.effort_minutes / max(
            wide_but_clean.docs_to_review + wide_but_clean.audit_sample_size, 1
        )
        assert per_reviewed < 5.0, f"{per_reviewed:.1f} min for 3 flagged fields"
        # Alerts drive the cost, so more flags outweighs more fields.
        assert narrow_but_messy.effort_minutes > wide_but_clean.effort_minutes

    def test_a_document_with_no_alerts_still_costs_something(self):
        """Confirming labels unchanged is real work: open, skim, mark reviewed."""
        curve = _calibrated_curve()
        docs = _doc_confidences(20)
        estimate = estimate_for_target(curve, 95.0, 20, docs, alerts_per_doc=0.0)
        assert estimate.effort_minutes > 0.0

    def test_prior_driven_estimate_returns_a_wide_range(self):
        """Cold start must not imply precision it doesn't have."""
        docs = _doc_confidences(120)
        cold = estimate_for_target(ConfidenceCurve(), 99.0, 120, docs)
        warm = estimate_for_target(_calibrated_curve(), 99.0, 120, docs)
        cold_width = cold.docs_to_review_high - cold.docs_to_review_low
        warm_width = warm.docs_to_review_high - warm.docs_to_review_low
        assert cold_width > warm_width
        assert cold.estimate_confidence is EstimateConfidence.PRIOR

    def test_unreliable_confidence_recommends_reviewing_everything(self):
        """A small worst-first sample is not defensible on a miscalibrated set."""
        overconfident = ConfidenceCurve.from_observations([(0.95, False)] * 80)
        estimate = estimate_for_target(overconfident, 99.0, 80, [0.95] * 80)
        assert estimate.recommend_review_all
        assert estimate.estimate_confidence is EstimateConfidence.UNRELIABLE

    def test_documents_without_confidence_sort_first(self):
        """An unlabeled document is the least trustworthy, not the most."""
        curve = _calibrated_curve()
        docs = [0.99, None, 0.98]
        estimate = estimate_for_target(curve, 99.9, 3, docs)
        # The None document must be inside the reviewed prefix.
        assert estimate.docs_to_review >= 1

    def test_empty_test_set_is_handled(self):
        estimate = estimate_for_target(_calibrated_curve(), 99.0, 0, [])
        assert estimate.docs_to_review == 0
        assert estimate.effort_minutes == 0.0
        assert estimate.burndown == []

    def test_burndown_is_monotonically_decreasing(self):
        """Each additional reviewed document can only remove error."""
        curve = _calibrated_curve()
        estimate = estimate_for_target(curve, 99.0, 60, _doc_confidences(60))
        residuals = [point["residualErrorPct"] for point in estimate.burndown]
        assert residuals == sorted(residuals, reverse=True)
        assert residuals[-1] == pytest.approx(0.0, abs=1e-6)

    def test_worst_first_beats_random_order(self):
        """The core premise: reviewing the worst documents removes more error than
        reviewing at random. Without it a partial review is not defensible."""
        curve = _calibrated_curve()
        docs = _doc_confidences(120)
        target = 95.0
        worst_first = estimate_for_target(curve, target, 120, docs).docs_to_review
        # Approximate random order by giving every document the mean confidence,
        # so ordering carries no information.
        mean_confidence = sum(docs) / len(docs)
        no_signal = estimate_for_target(
            curve, target, 120, [mean_confidence] * 120
        ).docs_to_review
        assert worst_first < no_signal, (worst_first, no_signal)

    def test_serializes_for_the_api(self):
        estimate = estimate_for_target(
            _calibrated_curve(), 99.0, 60, _doc_confidences(60)
        )
        payload = estimate.to_dict()
        for key in (
            "docsToReview",
            "impliedCutoff",
            "residualError",
            "effortMinutes",
            "estimateConfidence",
            "auditSampleSize",
            "recommendReviewAll",
            "calibration",
            "burndown",
        ):
            assert key in payload, key
        assert payload["calibration"]["reliable"] in (True, False)


class TestReliabilityTable:
    def test_exposes_observed_and_blended_accuracy_per_bin(self):
        curve = _calibrated_curve()
        table = curve.reliability_table()
        assert len(table) == cc.BIN_COUNT
        populated = [row for row in table if row["observations"] > 0]
        assert populated
        for row in populated:
            assert 0.0 <= row["observedAccuracy"] <= 1.0
            assert 0.0 <= row["blendedAccuracy"] <= 1.0

    def test_empty_bins_report_no_observed_accuracy(self):
        curve = ConfidenceCurve.from_observations([(0.95, True)] * 10)
        table = curve.reliability_table()
        low_bin = table[0]
        assert low_bin["observations"] == 0
        assert low_bin["observedAccuracy"] is None
        # Blended still returns something usable so the estimator never divides
        # by an empty bin.
        assert low_bin["blendedAccuracy"] is not None


@pytest.mark.unit
class TestQualityTier:
    """A tier is a claim about label accuracy, so it must be earned from
    measurement rather than asserted — these pin what earns and loses one."""

    @staticmethod
    def _health(**kw):
        d = dict(
            ece=0.03,
            bin_coverage=6,
            total_observations=4000,
            degenerate=False,
            overconfident=False,
            auroc=0.88,
            undiscriminating=False,
        )
        d.update(kw)
        return cc.CalibrationHealth(**d)

    def test_measured_and_accurate_earns_gold(self):
        tier = cc.quality_tier(0.005, EstimateConfidence.MEASURED, self._health())
        assert tier is cc.QualityTier.GOLD

    def test_a_prior_driven_number_cannot_earn_gold_or_silver(self):
        """99.5% computed from a cross-set prior says nothing about THESE labels.

        The accuracy figure alone is not evidence, so a cold-start set stays
        Bronze however good the extrapolated number looks.
        """
        tier = cc.quality_tier(0.005, EstimateConfidence.PRIOR, self._health())
        assert tier is cc.QualityTier.BRONZE

    def test_partially_measured_can_reach_silver(self):
        tier = cc.quality_tier(
            0.005, EstimateConfidence.PARTIALLY_MEASURED, self._health()
        )
        assert tier is cc.QualityTier.SILVER

    def test_accuracy_below_the_gold_bar_is_silver(self):
        tier = cc.quality_tier(0.03, EstimateConfidence.MEASURED, self._health())
        assert tier is cc.QualityTier.SILVER

    def test_unreliable_confidence_is_unrated_not_a_low_tier(self):
        """The problem is that the estimate means nothing, not that labels are bad.

        Ranking worse than chance, degenerate confidence, or overconfidence each
        make a subset review indefensible — so no accuracy claim is available.
        Reporting Bronze would dress that up as a mere quality difference.
        """
        for kw in (
            {"undiscriminating": True},
            {"degenerate": True},
            {"overconfident": True},
        ):
            tier = cc.quality_tier(
                0.005, EstimateConfidence.MEASURED, self._health(**kw)
            )
            assert tier is cc.QualityTier.UNRATED, kw

    def test_every_tier_has_a_plain_language_reason(self):
        """The badge is meaningless without one, so none may be missing."""
        for tier in cc.QualityTier:
            reason = cc.TIER_EXPLANATIONS[tier]
            assert reason and len(reason) > 20, tier

    def test_estimate_exposes_its_tier_and_reason(self):
        curve = _calibrated_curve()
        estimate = estimate_for_target(
            curve,
            target_accuracy=0.99,
            total_docs=50,
            doc_confidences=_doc_confidences(50),
        )
        payload = estimate.to_dict()
        assert payload["qualityTier"] in {t.value for t in cc.QualityTier}
        assert payload["qualityTierReason"]
