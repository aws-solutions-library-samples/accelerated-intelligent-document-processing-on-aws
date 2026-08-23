# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for accuracy confidence intervals."""

import pytest

from idp_common.evaluation.intervals import (
    accuracy_interval,
    accuracy_interval_from_confusion_matrix,
    wilson_interval,
)


@pytest.mark.unit
class TestWilsonInterval:
    def test_never_leaves_zero_to_one(self):
        """The reason this is Wilson and not the normal approximation.

        At n=20, p=0.90 the normal interval puts the upper bound at 103.2% — an
        impossible accuracy, printed in exactly the low-evidence case this reporting
        exists to flag. A reader shown 103% discounts the number instead of the
        sample size.
        """
        low, high = wilson_interval(18, 20)
        assert 0.0 <= low <= high <= 1.0
        assert high < 1.0, "an 18/20 result must not read as certainly perfect"

    def test_a_perfect_small_sample_is_not_reported_as_certain(self):
        """3 for 3 is not evidence of 100% accuracy."""
        low, high = wilson_interval(3, 3)
        assert high == pytest.approx(1.0)
        assert low < 0.5, f"3/3 should admit a lot of doubt, got low={low}"

    def test_a_perfect_large_sample_is_tight(self):
        low, high = wilson_interval(300, 300)
        assert low > 0.98

    def test_zero_successes_stays_in_range(self):
        low, high = wilson_interval(0, 10)
        assert low == 0.0
        assert 0.0 < high < 0.5

    def test_no_observations_is_uninformative_not_an_error(self):
        """A field no document contained is a normal state, not a failure."""
        assert wilson_interval(0, 0) == (0.0, 1.0)
        assert wilson_interval(5, -1) == (0.0, 1.0)

    @pytest.mark.parametrize(
        "n,expected_margin_pts",
        # The margins that motivate the feature: a field at 90% needs a few hundred
        # observations before its accuracy is pinned to a couple of points. Wilson is
        # slightly asymmetric, so these are approximate to a tenth of a point.
        [(20, 13.6), (100, 5.9), (300, 3.4), (500, 2.6)],
    )
    def test_margin_shrinks_with_sample_size(self, n, expected_margin_pts):
        interval = accuracy_interval(round(0.9 * n), n)
        assert interval is not None
        assert interval.margin * 100 == pytest.approx(expected_margin_pts, abs=0.15)

    def test_tighter_samples_are_strictly_tighter(self):
        widths = [
            accuracy_interval(round(0.9 * n), n).margin for n in (20, 100, 300, 500)
        ]
        assert widths == sorted(widths, reverse=True)


@pytest.mark.unit
class TestAccuracyInterval:
    def test_reports_the_point_estimate_and_bounds(self):
        interval = accuracy_interval(90, 100)
        assert interval.point == pytest.approx(0.90)
        assert interval.low < 0.90 < interval.high
        assert interval.observations == 100

    def test_nothing_measured_is_none_not_zero(self):
        """Zero would render as "always wrong" for a field simply never seen."""
        assert accuracy_interval(0, 0) is None

    def test_margin_is_half_the_reported_width(self):
        interval = accuracy_interval(45, 50)
        assert interval.margin == pytest.approx((interval.high - interval.low) / 2)


@pytest.mark.unit
class TestFromConfusionMatrix:
    def test_denominator_matches_how_accuracy_is_derived(self):
        """Must describe the number displayed next to it.

        Accuracy is (tp + tn) / (tp + fp + tn + fn), so the interval has to use the
        same denominator or it would qualify a different statistic.
        """
        metrics = {"tp": 80, "tn": 10, "fp": 5, "fn": 5}
        interval = accuracy_interval_from_confusion_matrix(metrics)
        assert interval.observations == 100
        assert interval.point == pytest.approx(0.90)

    def test_missing_counts_are_treated_as_zero(self):
        interval = accuracy_interval_from_confusion_matrix({"tp": 9, "fn": 1})
        assert interval.observations == 10
        assert interval.point == pytest.approx(0.9)

    def test_an_all_zero_matrix_yields_no_interval(self):
        assert accuracy_interval_from_confusion_matrix({"tp": 0, "fn": 0}) is None

    def test_non_numeric_and_bool_counts_are_ignored(self):
        """Booleans are ints in Python; counting True as 1 would invent evidence."""
        interval = accuracy_interval_from_confusion_matrix(
            {"tp": 9, "fn": 1, "fp": True, "tn": "3"}
        )
        assert interval.observations == 10

    def test_a_non_dict_is_rejected_rather_than_raising(self):
        assert accuracy_interval_from_confusion_matrix(None) is None
        assert accuracy_interval_from_confusion_matrix([1, 2]) is None
