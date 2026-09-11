# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Variance-aware quality comparison in aggregate.py's --compare.

This code decides what a release report *claims*, so a bug here becomes a wrong
published number. It did: at v0.6.5 the quality metrics were compared on the raw
mean shift alone, so a cell whose recall reads 0.10 or 1.00 on the SAME document
depending on the run was reported as a headline "CELL-LEVEL IMPROVEMENT: recall
0.700 -> 1.000" — in the direction that flattered the release. A 4x repeat later
showed the cell is simply bimodal.

The test is PAIRED on (cell, doc, repeat) because both releases run the identical
document set; that removes document heterogeneity, which matters because the doc
set spans 5-row and 100-row documents.
"""

import importlib.util
import os
import sys

import pytest

_HARNESS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness"
)


@pytest.fixture(scope="module")
def agg():
    """Import aggregate.py without its AWS-touching siblings running anything."""
    sys.path.insert(0, _HARNESS)
    spec = importlib.util.spec_from_file_location(
        "bench_aggregate_under_test", os.path.join(_HARNESS, "aggregate.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_aggregate_under_test"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("bench_aggregate_under_test", None)


def _summary(rows):
    return {"rows": rows, "cell_stats": {}}


def _row(cell, doc, recall, repeat=0):
    return {
        "cell": cell,
        "doc": doc,
        "repeat": repeat,
        "completeness_recall": recall,
        "scalar_accuracy": 1.0,
        "weighted_accuracy": 1.0,
        "success": True,
        "cost": 1.0,
    }


@pytest.mark.unit
class TestPairedDeltas:
    def test_pairs_on_cell_doc_and_repeat(self, agg):
        cur = _summary([_row("c", "a.pdf", 1.0), _row("c", "b.pdf", 0.5)])
        base = _summary([_row("c", "a.pdf", 0.9), _row("c", "b.pdf", 0.5)])
        deltas = agg._paired_quality_deltas(cur, base)
        assert sorted(deltas[("c", "completeness_recall")]) == [0.0, pytest.approx(0.1)]

    def test_unmatched_documents_are_ignored(self, agg):
        cur = _summary([_row("c", "a.pdf", 1.0), _row("c", "only_new.pdf", 0.0)])
        base = _summary([_row("c", "a.pdf", 1.0)])
        assert (
            len(agg._paired_quality_deltas(cur, base)[("c", "completeness_recall")])
            == 1
        )

    def test_non_numeric_values_are_skipped(self, agg):
        cur = _summary([_row("c", "a.pdf", None)])
        base = _summary([_row("c", "a.pdf", 1.0)])
        assert ("c", "completeness_recall") not in agg._paired_quality_deltas(cur, base)


@pytest.mark.unit
class TestDeltaSpread:
    def test_floor_applies_below_two_samples(self, agg):
        assert agg._delta_spread([]) == agg.QUALITY_SPREAD_FLOOR
        assert agg._delta_spread([0.5]) == agg.QUALITY_SPREAD_FLOOR

    def test_identical_deltas_fall_back_to_the_floor(self, agg):
        """A uniform shift has zero spread — the floor must not vanish."""
        assert agg._delta_spread([0.1, 0.1, 0.1]) == agg.QUALITY_SPREAD_FLOOR

    def test_spread_reflects_disagreement_between_documents(self, agg):
        assert agg._delta_spread([0.0, 0.0, 0.9]) > 0.4


@pytest.mark.unit
class TestCompareVerdicts:
    def _run(self, agg, tmp_path, cur_rows, base_rows):
        import json

        cur, base = tmp_path / "cur.json", tmp_path / "base.json"
        cur.write_text(json.dumps(_summary(cur_rows)))
        base.write_text(json.dumps(_summary(base_rows)))
        return agg.compare_cells(str(cur), str(base))

    def test_bimodal_cell_is_inconclusive_not_an_improvement(self, agg, tmp_path):
        """The v0.6.5 regression: one document swinging 0.1 -> 1.0 drove the verdict."""
        base = [
            _row("c", "a.pdf", 0.1),
            _row("c", "b.pdf", 1.0),
            _row("c", "d.pdf", 1.0),
        ]
        cur = [
            _row("c", "a.pdf", 1.0),
            _row("c", "b.pdf", 1.0),
            _row("c", "d.pdf", 1.0),
        ]
        reg, imp, weak = self._run(agg, tmp_path, cur, base)
        assert not [w for w in imp if "recall" in w[1]]
        assert [w for w in weak if "recall" in w[1]]

    def test_uniform_shift_across_documents_is_still_reported(self, agg, tmp_path):
        """A real, consistent regression must NOT be swallowed by the new test.

        Every document moves the same way, so the paired spread is ~0 and the
        floor decides — which is exactly why the spread is computed on the deltas
        rather than on each side's own (large) across-document stdev.
        """
        base = [
            _row("c", "a.pdf", 1.0),
            _row("c", "b.pdf", 0.5),
            _row("c", "d.pdf", 0.2),
        ]
        cur = [
            _row("c", "a.pdf", 0.9),
            _row("c", "b.pdf", 0.4),
            _row("c", "d.pdf", 0.1),
        ]
        reg, imp, weak = self._run(agg, tmp_path, cur, base)
        assert [w for w in reg if "recall" in w[1]], (
            "a uniform -0.10 regression on every document must be reported"
        )

    def test_shift_below_the_threshold_is_not_reported_at_all(self, agg, tmp_path):
        base = [_row("c", "a.pdf", 1.0), _row("c", "b.pdf", 1.0)]
        cur = [_row("c", "a.pdf", 0.995), _row("c", "b.pdf", 0.995)]
        reg, imp, weak = self._run(agg, tmp_path, cur, base)
        assert not any("recall" in w[1] for w in reg + imp + weak)

    def test_new_failures_are_always_reported(self, agg, tmp_path):
        base = [_row("c", "a.pdf", 1.0)]
        cur = [{**_row("c", "a.pdf", 1.0), "success": False}]
        reg, _, _ = self._run(agg, tmp_path, cur, base)
        assert any("NEW FAILURES" in w[1] for w in reg)
