# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-run comparison (aggregate.py --compare) must aggregate over repeats.

`compare()` paired rows on `cell|doc|repeat`, which is wrong twice over:

1. The repeat INDEX carries no identity. Repeat 2 of the new run is not "the same
   run" as repeat 2 of the baseline — they are independent samples of the same
   (cell, doc). Pairing by index discards exactly what repeats are for.
2. A single sample decided the verdict. `if b.success and not c.success` reported
   a NEW FAILURE from one draw, and a >=0.02 mean shift reported an accuracy
   regression from one draw.

Both misfired for real during the v0.6.5 fix verification: a repeats=1 grid
reported a new FAILURE (`tt-simple-int` on the 800-row doc) and a -0.143 accuracy
drop, and NEITHER reproduced. That is why `corefast` — the release-gate A/B grid —
now runs `repeats: 3`, and why this comparison reasons about failure RATES and
mean-vs-spread.

`compare_cells` (the cell-level roll-up) was already variance-aware; see
test_compare_variance.py. This is the per-(cell, doc) view beside it.
"""

import importlib.util
import json
import os
import sys

import pytest

_HARNESS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness"
)


@pytest.fixture(scope="module")
def agg():
    sys.path.insert(0, _HARNESS)
    spec = importlib.util.spec_from_file_location(
        "bench_aggregate_repeats_under_test", os.path.join(_HARNESS, "aggregate.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_aggregate_repeats_under_test"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("bench_aggregate_repeats_under_test", None)


def _row(recall=1.0, repeat=0, success=True, cost=1.0, cell="c", doc="a.pdf", **kw):
    row = {
        "cell": cell,
        "doc": doc,
        "repeat": repeat,
        "completeness_recall": recall,
        "scalar_accuracy": 1.0,
        "weighted_accuracy": 1.0,
        "success": success,
        "status": "COMPLETED" if success else "FAILED",
        "cost": cost,
    }
    row.update(kw)
    return row


def _reps(values, **kw):
    """One row per value, with distinct repeat indices."""
    return [_row(recall=v, repeat=i, **kw) for i, v in enumerate(values)]


def _run(agg, tmp_path, cur_rows, base_rows):
    cur, base = tmp_path / "cur.json", tmp_path / "base.json"
    cur.write_text(json.dumps({"rows": cur_rows, "cell_stats": {}}))
    base.write_text(json.dumps({"rows": base_rows, "cell_stats": {}}))
    return agg.compare(str(cur), str(base))


pytestmark = pytest.mark.unit


class TestGroupingIgnoresRepeatIndex:
    def test_repeats_collapse_into_one_key(self, agg):
        groups = agg._by_cell_doc(_reps([1.0, 0.5, 0.0]))
        assert list(groups) == ["c|a.pdf"]
        assert len(groups["c|a.pdf"]) == 3

    def test_distinct_docs_stay_separate(self, agg):
        rows = _reps([1.0, 1.0]) + _reps([1.0], doc="b.pdf")
        assert sorted(agg._by_cell_doc(rows)) == ["c|a.pdf", "c|b.pdf"]


class TestFailureRates:
    def test_a_flake_on_one_of_three_repeats_is_not_a_regression(self, agg, tmp_path):
        """The exact false positive that cost a verification cycle: the baseline
        also flakes, so 1/3 vs 1/3 is the same behaviour, not a new failure."""
        base = [_row(repeat=0), _row(repeat=1, success=False), _row(repeat=2)]
        cur = [_row(repeat=0, success=False), _row(repeat=1), _row(repeat=2)]
        reg, _ = _run(agg, tmp_path, cur, base)
        assert not [r for r in reg if "FAILURE" in r[1]]

    def test_a_real_increase_in_failure_rate_is_reported(self, agg, tmp_path):
        base = [_row(repeat=i) for i in range(3)]
        cur = [_row(repeat=i, success=False) for i in range(3)]
        reg, _ = _run(agg, tmp_path, cur, base)
        hits = [r for r in reg if "FAILURE RATE" in r[1]]
        assert hits and "0/3 -> 3/3" in hits[0][1]

    def test_a_partial_increase_is_reported_but_flagged_as_needing_confirmation(
        self, agg, tmp_path
    ):
        """1/3 -> 2/3 is a rate increase worth reporting, but both sides fail
        sometimes, so it must not read as a settled verdict."""
        base = [_row(repeat=0, success=False), _row(repeat=1), _row(repeat=2)]
        cur = [
            _row(repeat=0, success=False),
            _row(repeat=1, success=False),
            _row(repeat=2),
        ]
        reg, _ = _run(agg, tmp_path, cur, base)
        hits = [r for r in reg if "FAILURE RATE" in r[1]]
        assert hits
        assert "confirm before believing" in hits[0][1]

    def test_a_fixed_failure_is_an_improvement(self, agg, tmp_path):
        base = [_row(repeat=i, success=False) for i in range(3)]
        cur = [_row(repeat=i) for i in range(3)]
        _, imp = _run(agg, tmp_path, cur, base)
        assert [i for i in imp if "FAILURE RATE" in i[1]]

    def test_single_repeat_failure_still_reported(self, agg, tmp_path):
        """repeats=1 behaviour is unchanged — 0/1 -> 1/1 is still a rate increase."""
        reg, _ = _run(agg, tmp_path, [_row(success=False)], [_row()])
        assert [r for r in reg if "FAILURE RATE" in r[1]]


class TestQualityAgainstSpread:
    def test_bimodal_cell_is_not_a_regression(self, agg, tmp_path):
        """Recall swinging 0.1..1.0 on the SAME document across repeats: the mean
        moves, but the move is inside the noise the data itself shows."""
        base = _reps([1.0, 1.0, 1.0])
        cur = _reps([1.0, 0.1, 1.0])
        reg, imp = _run(agg, tmp_path, cur, base)
        assert not [r for r in reg if "completeness_recall" in r[1]]
        assert not [i for i in imp if "completeness_recall" in i[1]]

    def test_a_consistent_drop_across_every_repeat_is_reported(self, agg, tmp_path):
        """A real regression must survive: all three repeats agree, so spread is
        ~0 and the delta clears it."""
        base = _reps([1.0, 1.0, 1.0])
        cur = _reps([0.5, 0.5, 0.5])
        reg, _ = _run(agg, tmp_path, cur, base)
        hits = [r for r in reg if "completeness_recall" in r[1]]
        assert hits, "a uniform -0.5 across all repeats must be reported"
        assert "n=3->3" in hits[0][1]

    def test_a_consistent_gain_is_an_improvement(self, agg, tmp_path):
        base = _reps([0.5, 0.5, 0.5])
        cur = _reps([1.0, 1.0, 1.0])
        _, imp = _run(agg, tmp_path, cur, base)
        assert [i for i in imp if "completeness_recall" in i[1]]

    def test_sub_threshold_change_is_silent(self, agg, tmp_path):
        reg, imp = _run(agg, tmp_path, _reps([0.995]), _reps([1.0]))
        assert not reg and not imp

    def test_single_repeat_regression_still_reported(self, agg, tmp_path):
        """With one sample the spread is 0, so repeats=1 behaves as it always did."""
        reg, _ = _run(agg, tmp_path, [_row(recall=0.5)], [_row(recall=1.0)])
        assert [r for r in reg if "completeness_recall" in r[1]]

    def test_failed_runs_do_not_pollute_the_quality_mean(self, agg, tmp_path):
        """A failed run has no meaningful metric; averaging its 0.0 in would
        manufacture an accuracy regression out of a failure already reported."""
        base = _reps([1.0, 1.0, 1.0])
        cur = [
            _row(recall=1.0),
            _row(recall=0.0, repeat=1, success=False),
            _row(recall=1.0, repeat=2),
        ]
        reg, _ = _run(agg, tmp_path, cur, base)
        assert not [r for r in reg if "completeness_recall" in r[1]]


class TestCost:
    def test_cost_within_run_to_run_spread_is_inconclusive(self, agg, tmp_path):
        """Agentic cost spreads ~4x run-to-run; a mean shift smaller than the
        observed spread cannot be a finding."""
        base = [_row(repeat=i, cost=c) for i, c in enumerate([1.0, 4.0, 1.0])]
        cur = [_row(repeat=i, cost=c) for i, c in enumerate([2.0, 4.0, 2.0])]
        reg, _ = _run(agg, tmp_path, cur, base)
        assert not [r for r in reg if "cost" in r[1]]

    def test_a_consistent_cost_increase_is_reported(self, agg, tmp_path):
        base = [_row(repeat=i, cost=1.0) for i in range(3)]
        cur = [_row(repeat=i, cost=2.0) for i in range(3)]
        reg, _ = _run(agg, tmp_path, cur, base)
        assert [r for r in reg if "cost" in r[1]]

    def test_cost_decrease_is_never_a_regression(self, agg, tmp_path):
        base = [_row(repeat=i, cost=2.0) for i in range(3)]
        cur = [_row(repeat=i, cost=1.0) for i in range(3)]
        reg, _ = _run(agg, tmp_path, cur, base)
        assert not [r for r in reg if "cost" in r[1]]


class TestUnmatched:
    def test_a_cell_doc_absent_from_the_baseline_is_skipped(self, agg, tmp_path):
        reg, imp = _run(agg, tmp_path, _reps([0.0], doc="new.pdf"), _reps([1.0]))
        assert not reg and not imp
