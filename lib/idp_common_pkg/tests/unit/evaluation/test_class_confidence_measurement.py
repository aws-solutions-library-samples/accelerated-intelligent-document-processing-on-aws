# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Measuring classification confidence, not just reporting it (GitHub #673).

A classification confidence that nobody checks is worse than none: a model asked
"how sure are you?" answers ~0.95 on right and wrong pages alike, and acting on
that escalates noise. Two pieces make it checkable:

- ``attach_page_confidence`` puts the classifier's own confidence on the same
  per-page row that already says whether the class was ``correct``.
- the benchmark harness's ``score_classification`` turns those rows into
  ``class_calibration_separation`` = mean(conf | right) − mean(conf | wrong),
  which is the number that decides whether the score carries information.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from idp_common.evaluation.stickler_backend import attach_page_confidence
from idp_common.models import Document, Page

_ANALYZE_PATH = os.path.join(
    os.path.dirname(__file__),
    "../../../../../benchmarks/harness/analyze.py",
)


def _load_score_classification():
    """Load the benchmark harness's scorer without importing the whole harness."""
    spec = importlib.util.spec_from_file_location("bench_analyze", _ANALYZE_PATH)
    if spec is None or spec.loader is None:
        pytest.skip(f"benchmark harness not present at {_ANALYZE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.score_classification


def _document(confidences, first_page_id=1):
    doc = Document(id="pkg.pdf", input_key="pkg.pdf")
    for offset, confidence in enumerate(confidences):
        page_id = str(first_page_id + offset)
        doc.pages[page_id] = Page(
            page_id=page_id, classification="w2", confidence=confidence
        )
    return doc


@pytest.mark.unit
class TestAttachPageConfidence:
    def test_annotates_each_row_with_the_predicted_confidence(self):
        rows = [
            {"page_index": 0, "correct": True},
            {"page_index": 1, "correct": False},
        ]
        attach_page_confidence(rows, _document([0.9, 0.4]))
        assert rows[0]["predicted_confidence"] == 0.9
        assert rows[1]["predicted_confidence"] == 0.4

    def test_unscored_pages_annotate_as_none(self):
        """Not scored must stay distinguishable from scored 0.0."""
        rows = [{"page_index": 0, "correct": True}]
        attach_page_confidence(rows, _document([None]))
        assert rows[0]["predicted_confidence"] is None

    def test_page_index_offset_comes_from_the_document(self):
        """`page_index` is 0-based from the document's FIRST page, not from 1.

        Section page_indices are written relative to the global minimum page id,
        so a document whose pages start at 5 must not be off by four.
        """
        rows = [{"page_index": 0}, {"page_index": 1}]
        attach_page_confidence(rows, _document([0.7, 0.3], first_page_id=5))
        assert [r["predicted_confidence"] for r in rows] == [0.7, 0.3]

    def test_row_without_a_matching_page_is_left_alone(self):
        """Better unannotated than annotated with another page's number."""
        rows = [{"page_index": 99, "correct": False}]
        attach_page_confidence(rows, _document([0.9]))
        assert "predicted_confidence" not in rows[0]

    def test_document_without_pages_is_a_no_op(self):
        rows = [{"page_index": 0}]
        assert attach_page_confidence(rows, Document(id="d", input_key="d")) == rows


@pytest.mark.unit
class TestScoreClassification:
    @staticmethod
    def _ev(page_details, accuracy=0.5):
        return {
            "doc_split_metrics": {
                "page_level_accuracy": accuracy,
                "page_details": page_details,
            }
        }

    def test_separation_is_right_minus_wrong(self):
        score = _load_score_classification()(
            self._ev(
                [
                    {"correct": True, "predicted_confidence": 0.9},
                    {"correct": True, "predicted_confidence": 0.8},
                    {"correct": False, "predicted_confidence": 0.4},
                ]
            )
        )
        # mean(right)=0.85, mean(wrong)=0.4
        assert score["class_calibration_separation"] == pytest.approx(0.45)
        assert score["n_class_scored_pages"] == 3
        assert score["class_mean_confidence"] == pytest.approx(0.7)

    def test_a_uniformly_overconfident_model_scores_near_zero(self):
        """The failure this metric exists to catch: ~0.95 whether right or wrong."""
        score = _load_score_classification()(
            self._ev(
                [
                    {"correct": True, "predicted_confidence": 0.95},
                    {"correct": False, "predicted_confidence": 0.94},
                ]
            )
        )
        assert score["class_calibration_separation"] == pytest.approx(0.01)

    def test_separation_is_undefined_without_both_populations(self):
        """A perfect run says nothing about calibration — it must not score 0."""
        score = _load_score_classification()(
            self._ev([{"correct": True, "predicted_confidence": 0.9}])
        )
        assert score["class_calibration_separation"] is None
        assert score["n_class_scored_pages"] == 1

    def test_unscored_run_reports_none_not_zero(self):
        score = _load_score_classification()(
            self._ev([{"correct": True}, {"correct": False}])
        )
        assert score["class_calibration_separation"] is None
        assert score["class_mean_confidence"] is None
        assert score["n_class_scored_pages"] == 0
        # Accuracy is still measured — it needs no confidence.
        assert score["class_accuracy"] == 0.5

    def test_missing_evaluation_report(self):
        score = _load_score_classification()(None)
        assert score["class_accuracy"] is None
        assert score["n_class_scored_pages"] == 0
