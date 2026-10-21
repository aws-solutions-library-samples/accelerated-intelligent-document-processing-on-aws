# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Section accuracy split into a boundary half and a label half (#1329).

``split_accuracy_without_order`` requires a predicted section to match a
ground-truth one on **both** its page set and its class, so a wrong boundary and
a wrong label are reported as the same failure. A report reading
``split_accuracy_without_order: 0.5`` cannot say whether the model is splitting
the packet wrongly or naming the pieces wrongly, and those two call for
different fixes — a different segmentation prompt, or a different labeling
model.

The decomposition is exact rather than an approximation, which is the property
worth pinning:

    split_accuracy_without_order = boundary_accuracy x label_accuracy

``label_accuracy`` is ``None``, not ``0.0``, when no section's boundaries
matched. Reporting zero would present "we never got far enough to judge the
labels" as "every label was wrong" — the exact pair of readings this
decomposition exists to keep apart.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from idp_common.evaluation.stickler_backend import compute_boundary_and_label_accuracy


def _section(
    pages: List[int], doc_class: Optional[str], section_id: str = "1"
) -> Dict[str, Any]:
    return {
        "section_id": section_id,
        "document_class": doc_class,
        "page_indices": pages,
    }


@pytest.mark.unit
class TestBoundaryAndLabelAccuracy:
    def test_everything_right_scores_one_on_both_halves(self):
        gt = [_section([0, 1], "invoice"), _section([2], "receipt", "2")]
        result = compute_boundary_and_label_accuracy(gt, list(gt))
        assert result["boundary_accuracy"] == 1.0
        assert result["label_accuracy"] == 1.0

    def test_right_boundaries_and_a_wrong_label_separate_cleanly(self):
        """The reading the conflated metric cannot give: the packet was split
        perfectly and one piece was named wrongly."""
        gt = [_section([0, 1], "invoice"), _section([2], "receipt", "2")]
        pred = [_section([0, 1], "invoice"), _section([2], "letter", "2")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 1.0
        assert result["label_accuracy"] == 0.5

    def test_wrong_boundaries_with_the_right_labels_separate_the_other_way(self):
        gt = [_section([0, 1], "invoice"), _section([2], "invoice", "2")]
        pred = [_section([0, 1, 2], "invoice")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 0.0
        # Nothing matched on boundaries, so no label was scored at all.
        assert result["label_accuracy"] is None

    def test_no_boundary_match_reports_not_scored_rather_than_zero(self):
        """⚠️ The distinction the whole decomposition exists for. 0.0 would read
        as 'every label was wrong', which is a different fact."""
        gt = [_section([0, 1], "invoice")]
        pred = [_section([0], "invoice")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["label_accuracy"] is None
        assert result["label_matched_sections"] == 0

    def test_the_product_equals_the_conflated_metric(self):
        """split_accuracy_without_order counts sections matching on page set AND
        class; boundary counts page set; label is the fraction of those that
        also match on class. So the product is the conflated number exactly."""
        gt = [
            _section([0, 1], "invoice", "1"),
            _section([2], "receipt", "2"),
            _section([3, 4], "letter", "3"),
            _section([5], "invoice", "4"),
        ]
        pred = [
            _section([0, 1], "invoice", "1"),  # both right
            _section([2], "letter", "2"),  # boundary right, label wrong
            _section([3], "letter", "3"),  # boundary wrong
            _section([5], "invoice", "4"),  # both right
        ]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 0.75  # 3 of 4
        assert result["label_accuracy"] == pytest.approx(2 / 3, abs=1e-4)
        # Both-right is 2 of 4, which is what the conflated metric reports.
        assert result["boundary_accuracy"] * result["label_accuracy"] == pytest.approx(
            0.5, abs=1e-3
        )

    def test_order_does_not_matter(self):
        """Matches the 'without_order' metric this decomposes: a section is
        matched by its page set, not by its position in the list."""
        gt = [_section([0], "invoice", "1"), _section([1], "receipt", "2")]
        pred = [_section([1], "receipt", "2"), _section([0], "invoice", "1")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 1.0
        assert result["label_accuracy"] == 1.0

    def test_a_duplicated_prediction_cannot_satisfy_two_sections(self):
        """A model that emits the same range twice must not score it twice."""
        gt = [_section([0], "invoice", "1"), _section([0], "invoice", "2")]
        pred = [_section([0], "invoice", "1")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_matched_sections"] == 1
        assert result["boundary_accuracy"] == 0.5

    def test_an_unlabeled_prediction_fails_the_label_half_only(self):
        """The shape two-stage Stage A produces, and the shape a segment whose
        Stage B call failed keeps: boundaries good, no class."""
        gt = [_section([0, 1], "invoice")]
        pred = [_section([0, 1], None)]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 1.0
        assert result["label_accuracy"] == 0.0

    def test_no_ground_truth_sections_scores_nothing(self):
        result = compute_boundary_and_label_accuracy([], [_section([0], "invoice")])
        assert result["boundary_accuracy"] is None
        assert result["label_accuracy"] is None
        assert result["total_sections"] == 0

    def test_no_predictions_scores_zero_boundaries(self):
        result = compute_boundary_and_label_accuracy([_section([0], "invoice")], [])
        assert result["boundary_accuracy"] == 0.0
        assert result["label_accuracy"] is None

    def test_a_ground_truth_section_with_no_pages_is_skipped(self):
        gt = [_section([], "invoice", "1"), _section([0], "receipt", "2")]
        pred = [_section([0], "receipt", "2")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        # Still divided by the total GT count, so a malformed section is a miss
        # rather than being quietly removed from the denominator.
        assert result["total_sections"] == 2
        assert result["boundary_accuracy"] == 0.5

    def test_string_page_indices_are_accepted(self):
        gt = [_section(["0", "1"], "invoice")]  # type: ignore[list-item]
        pred = [_section([0, 1], "invoice")]
        result = compute_boundary_and_label_accuracy(gt, pred)
        assert result["boundary_accuracy"] == 1.0


@pytest.mark.unit
class TestDocSplitMetricsReporting:
    def test_the_new_metrics_appear_in_the_serialized_result(self):
        from idp_common.evaluation.models import DocSplitMetrics

        metrics = DocSplitMetrics(
            page_level_accuracy=0.9,
            split_accuracy_without_order=0.5,
            split_accuracy_with_order=0.5,
            total_pages=10,
            total_splits=2,
            correctly_classified_pages=9,
            correctly_split_without_order=1,
            correctly_split_with_order=1,
            boundary_accuracy=1.0,
            label_accuracy=0.5,
            boundary_matched_sections=2,
            label_matched_sections=1,
        )
        as_dict = metrics.to_dict()
        assert as_dict["boundary_accuracy"] == 1.0
        assert as_dict["label_accuracy"] == 0.5
        assert as_dict["boundary_matched_sections"] == 2
        assert as_dict["label_matched_sections"] == 1

    def test_the_defaults_keep_an_older_result_loadable(self):
        """An older results.json carries neither field, so both must default."""
        from idp_common.evaluation.models import DocSplitMetrics

        metrics = DocSplitMetrics(
            page_level_accuracy=0.9,
            split_accuracy_without_order=0.5,
            split_accuracy_with_order=0.5,
            total_pages=10,
            total_splits=2,
            correctly_classified_pages=9,
            correctly_split_without_order=1,
            correctly_split_with_order=1,
        )
        assert metrics.boundary_accuracy is None
        assert metrics.label_accuracy is None

    def test_an_unscored_label_half_is_reported_as_unscored_in_the_report(self):
        from idp_common.evaluation.models import (
            DocSplitMetrics,
            DocumentEvaluationResult,
        )

        result = DocumentEvaluationResult(
            document_id="d",
            section_results=[],
            doc_split_metrics=DocSplitMetrics(
                page_level_accuracy=0.5,
                split_accuracy_without_order=0.0,
                split_accuracy_with_order=0.0,
                total_pages=2,
                total_splits=1,
                correctly_classified_pages=1,
                correctly_split_without_order=0,
                correctly_split_with_order=0,
                boundary_accuracy=0.0,
                label_accuracy=None,
            ),
        )
        report = result.to_markdown()
        assert "boundary_accuracy" in report
        assert "not scored" in report
