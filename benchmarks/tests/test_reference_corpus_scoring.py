# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metric that does not apply must not read as a scorer that produced nothing (#1364).

What was wrong
--------------
``analyze.score_doc`` chooses between two scorers on whether a local truth file exists,
and the two emit **disjoint** accuracy families:

* ``score_synthetic`` — ``cells_compared``, ``cell_accuracy``, ``scalar_accuracy``,
  ``completeness_recall``, scored against the generator's exact per-cell truth;
* ``score_reference`` — ``weighted_accuracy`` and ``class_accuracy``, read from the
  stack's own evaluation of a labelled test set.

``aggregate.score_reference_run`` passes ``truth=None`` for every reference-corpus row,
because a test set's documents have no local ``.pdf.truth.json`` — so a reference row
legitimately carries no exact-key metric. Which scorer had run was recorded **nowhere**
on the row. The result was a row shaped exactly like a synthetic row whose scorer had
failed: four absent keys, four blank CSV cells, ``cells_compared`` read as 0 by any
reader that defaults it, and a mean over no observations read as ``nan``.

That reading was published — "the reference corpora produced cost but no scored
accuracy", "nothing was scored against ground truth" — for a grid in which **all 2,280**
reference rows carried a numeric ``weighted_accuracy`` (``realkie`` 0.796, ``ocr_bench``
0.996 mean), and it left a documentation section unrefreshed whose numbers were in the
artifact the whole time. The symmetry is the proof that the ``nan`` is a property of
reading the wrong column and not of the run: on the same grid the 380 completed
**synthetic** rows produce ``weighted_accuracy`` = nan over n=0 by the identical
mechanism, and nobody has ever reported those as unscored.

The notice ``write_summary`` already prints is the right sentence for this case and
never fired for it: it is gated on ``docs_missing_truth``, which ``run_matrix``
populates only with *synthetic* documents whose local truth file is absent. A control
that exists and is never consulted on the case it describes.

What these tests pin
--------------------
1. Every scored row says which scorer produced it (``scored_against``), on both paths.
2. A reference row additionally says **why** the exact-key family is absent and which
   column carries its accuracy instead (``exact_truth_unavailable``) — and the absent
   keys stay absent rather than becoming present-and-null, because
   presence-and-null is already this harness's signal for "measured, nothing to join".
3. The unread-sections path still names the scorer, while
   ``analyze.unread_sections_row`` itself stays limited to its two bookkeeping keys.
4. All three keys reach ``CSV_COLS`` and a written CSV —
   ``DictWriter(extrasaction="ignore")`` drops an unlisted key without a word, which is
   why ``cells_compared`` (``cell_accuracy``'s denominator) was readable from neither
   view despite ``METHODOLOGY.md`` saying it is printed beside it.
5. ``aggregate.row_scored_against`` classifies a row of **any** vintage, so the 96
   committed summaries become readable without being rewritten.
6. ``cell_stats`` carries the denominator each accuracy ``n`` belongs to, so the 21/120
   split inside a 141-run cell is in the artifact rather than inferred.
7. ``write_summary`` says so on stdout, derived from the rows and not from the runmap.
8. ``aggregate.py --reference-tables`` renders the per-corpus × per-cell table the
   guidance publishes, so it stops being assembled by hand — the step that does not
   happen when the reader has already concluded the corpora were not scored. A metric
   with no observations is *named* there rather than printed as ``0.000``.
9. The committed grids that ran a reference corpus — including the v0.6.11 ``core`` one
   the report was written from — are classified correctly end to end, offline.
"""

from __future__ import annotations

import csv
import json
import os

import pytest
from harness_import import harness_module

lib = harness_module("lib")
analyze = harness_module("analyze")
aggregate = harness_module("aggregate")

BENCHMARKS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(BENCHMARKS, "results")

# The exact-key accuracy family, spelled out HERE rather than read from the module
# under test. Reading the harness's own tuple in these assertions would make every loop
# over it vacuous the moment that tuple were emptied — and a vacuous loop is the shape
# of defect this whole suite exists to catch. One test below asserts the two agree,
# which is where a divergence belongs.
EXACT_KEY_FAMILY = (
    "cells_compared",
    "cell_accuracy",
    "scalar_accuracy",
    "completeness_recall",
)

TRUTH = {
    "seq_ids": ["SEQ00001"],
    "rows_typed": {"SEQ00001": {"Amount": 1.0}},
    "list_key": "Transactions",
    "fields": {"AccountNumber": "1234"},
    "expected_sections": 1,
}


@pytest.fixture
def scored_doc(monkeypatch):
    """``score_doc`` with its three reads stubbed — one knob each."""

    def run(section_read=None, truth=None, evaluation=None):
        monkeypatch.setattr(
            lib,
            "doc_row",
            lambda *a, **k: {"ObjectStatus": "COMPLETED", "PageCount": 2},
        )
        monkeypatch.setattr(
            lib, "read_metering", lambda *a, **k: lib.Reading.present({})
        )
        monkeypatch.setattr(
            lib,
            "read_sections",
            lambda *a, **k: (
                section_read if section_read is not None else lib.SectionRead([])
            ),
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


# --------------------------------------------------------------------------- #
# 1-2. The row says which scorer ran, and why the other family is absent
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEveryRowNamesItsScorer:
    def test_a_reference_row_says_the_stack_evaluation_scored_it(self, scored_doc):
        row = scored_doc(
            truth=None, evaluation={"overall_metrics": {"weighted_overall_score": 0.8}}
        )
        assert row["scored_against"] == analyze.SCORED_STACK_EVALUATION
        assert row["weighted_accuracy"] == 0.8
        # The reason is useless unless it names the column that DOES carry the
        # accuracy — "not applicable" alone is what a reader already concluded, and
        # they concluded it meant unscored.
        reason = row["exact_truth_unavailable"]
        # BOTH substitutes, not just the headline one: a reference row's
        # classification accuracy is `class_accuracy`, and a reason that names only
        # `weighted_accuracy` sends a reader looking for classification quality to
        # the same dead end this fix exists to remove.
        assert reason and "weighted_accuracy" in reason and "class_accuracy" in reason
        for metric in EXACT_KEY_FAMILY:
            assert metric in reason, metric
            # ...and the keys stay ABSENT. Present-and-null is this harness's signal
            # for "measured, nothing to join" and must not be borrowed here.
            assert metric not in row, metric

    def test_a_synthetic_row_says_exact_truth_and_declares_nothing_unavailable(
        self, scored_doc
    ):
        row = scored_doc(truth=TRUTH)
        assert row["scored_against"] == analyze.SCORED_EXACT_TRUTH
        assert row["exact_truth_unavailable"] is None
        assert row["scalar_accuracy"] is not None
        assert "weighted_accuracy" not in row

    def test_the_two_verdicts_are_distinct_values(self):
        """A single constant used for both would satisfy every assertion above."""
        assert analyze.SCORED_EXACT_TRUTH != analyze.SCORED_STACK_EVALUATION

    def test_the_exact_key_family_this_suite_asserts_over_is_the_one_in_use(self):
        """The one place the two spellings of the family are allowed to meet."""
        assert set(aggregate.EXACT_TRUTH_METRICS) == set(EXACT_KEY_FAMILY)


# --------------------------------------------------------------------------- #
# 3. The unread path keeps the scorer and gains nothing else
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheUnreadPathStillNamesTheScorer:
    @pytest.mark.parametrize("truth", [TRUTH, None], ids=["synthetic", "reference"])
    def test_a_row_whose_sections_would_not_read_still_names_its_scorer(
        self, scored_doc, truth
    ):
        unread = lib.SectionRead([], unreadable=2, errors=("1/result.json: KMS",))
        row = scored_doc(section_read=unread, truth=truth)
        assert row["sections_unreadable"] == 2
        expected = (
            analyze.SCORED_EXACT_TRUTH if truth else analyze.SCORED_STACK_EVALUATION
        )
        assert row["scored_against"] == expected
        # Still no metric key — the scorer is known, the measurement is not.
        for metric in ("cell_accuracy", "weighted_accuracy", "conf_coverage"):
            assert metric not in row, metric

    def test_unread_sections_row_itself_is_still_only_its_two_keys(self):
        """The provenance belongs to ``score_doc``, not to the unread row.

        ``test_absence_versus_failure`` derives "an unread row carries no measured
        key" by intersecting this key set with a scorer's. Putting the provenance
        inside either scorer would be the way to break that, so it is pinned here too.
        """
        unread = analyze.unread_sections_row(
            lib.SectionRead([], unreadable=1, errors=("x",))
        )
        assert set(unread) == {"sections_unreadable", "sections_unread"}


# --------------------------------------------------------------------------- #
# 4. The keys reach the CSV, and so does cell_accuracy's denominator
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheCsvCarriesIt:
    @pytest.mark.parametrize(
        "column", ["scored_against", "exact_truth_unavailable", "cells_compared"]
    )
    def test_the_column_is_declared(self, column):
        assert column in aggregate.CSV_COLS

    def test_a_written_csv_distinguishes_the_two_scorers(self, tmp_path):
        rows = [
            {
                "cell": "c",
                "doc": "tiny_form.pdf",
                "sub_doc": None,
                "success": True,
                "cell_accuracy": 1.0,
                "cells_compared": 10,
                "scored_against": analyze.SCORED_EXACT_TRUTH,
                "exact_truth_unavailable": None,
            },
            {
                "cell": "c",
                "doc": "realkie",
                "sub_doc": "a.pdf",
                "success": True,
                "weighted_accuracy": 0.8,
                "scored_against": analyze.SCORED_STACK_EVALUATION,
                "exact_truth_unavailable": analyze.EXACT_TRUTH_UNAVAILABLE_REFERENCE,
            },
        ]
        aggregate.write_summary({}, rows, str(tmp_path))
        with open(tmp_path / "summary.csv", newline="") as fh:
            out = list(csv.DictReader(fh))
        assert [r["scored_against"] for r in out] == [
            analyze.SCORED_EXACT_TRUTH,
            analyze.SCORED_STACK_EVALUATION,
        ]
        # cell_accuracy without its denominator is the "a null column scores 1.000"
        # blind spot, and the denominator was being dropped by extrasaction="ignore".
        assert [r["cells_compared"] for r in out] == ["10", ""]
        assert "weighted_accuracy" in out[1]["exact_truth_unavailable"]
        assert out[0]["exact_truth_unavailable"] == ""


# --------------------------------------------------------------------------- #
# 5. Rows of any vintage, so the committed grids need no rewrite
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRowsWrittenBeforeTheKeyExisted:
    def test_a_recorded_verdict_wins(self):
        row = {"scored_against": analyze.SCORED_EXACT_TRUTH, "sub_doc": "a.pdf"}
        assert aggregate.row_scored_against(row) == analyze.SCORED_EXACT_TRUTH

    @pytest.mark.parametrize("recorded", ["", "unknown", "exact-truth", 1, True])
    def test_a_recorded_value_nothing_recognises_is_not_honoured(self, recorded):
        """``return recorded`` for anything non-null would pass the test above and
        put an unrecognised string — a typo, a value from a future scorer — into
        every roll-up that compares against the two constants. Such a row falls back
        to the shape evidence, and to "unknown" when there is none."""
        assert (
            aggregate.row_scored_against(
                {"doc": "realkie", "sub_doc": "a.pdf", "scored_against": recorded}
            )
            == analyze.SCORED_STACK_EVALUATION
        )
        assert (
            aggregate.row_scored_against(
                {"doc": "x", "status": "NOT_LAUNCHED", "scored_against": recorded}
            )
            is None
        )

    def test_a_truthy_sub_doc_is_a_reference_row(self):
        """``score_reference_run`` is the only writer of ``sub_doc``, and it is
        exactly the path that hands ``score_doc`` no truth."""
        assert (
            aggregate.row_scored_against({"doc": "realkie", "sub_doc": "a.pdf"})
            == analyze.SCORED_STACK_EVALUATION
        )

    @pytest.mark.parametrize("metric", EXACT_KEY_FAMILY)
    def test_sub_doc_outranks_a_stray_exact_key_metric(self, metric):
        """The two pieces of shape evidence can only ever disagree by accident, and
        ``sub_doc`` is the stronger one: only ``score_reference_run`` writes it, while
        an exact-key metric could reach a row from a hand-edited artifact or a scorer
        that gains one. Pinned because the two checks are adjacent and reordering them
        is invisible on a row where only one applies."""
        row = {"doc": "realkie", "sub_doc": "a.pdf", metric: 1.0}
        assert aggregate.row_scored_against(row) == analyze.SCORED_STACK_EVALUATION

    def test_a_present_but_null_sub_doc_is_not_a_reference_row(self):
        """``_key`` sets ``sub_doc: None`` on every synthetic row, so membership
        testing instead of truthiness would classify the whole grid as reference."""
        row = {"doc": "tiny_form.pdf", "sub_doc": None, "cell_accuracy": 1.0}
        assert aggregate.row_scored_against(row) == analyze.SCORED_EXACT_TRUTH

    @pytest.mark.parametrize("metric", EXACT_KEY_FAMILY)
    def test_any_exact_key_metric_identifies_the_exact_truth_scorer(self, metric):
        """Keyed on PRESENCE, not value: on a document that has a list and returned
        none of its rows, ``cell_accuracy`` is null and ``cells_compared`` is ``0``,
        and both of those are falsy, so truthiness would send that row back to
        "unknown". A document with no list at all carries neither key (#1497), which
        is why the fallback is ``any`` over the family rather than all of it."""
        row = {"doc": "tiny_form.pdf", "sub_doc": None, metric: None}
        assert aggregate.row_scored_against(row) == analyze.SCORED_EXACT_TRUTH

    def test_weighted_accuracy_alone_identifies_the_reference_scorer(self):
        row = {"doc": "realkie", "weighted_accuracy": None}
        assert aggregate.row_scored_against(row) == analyze.SCORED_STACK_EVALUATION

    def test_a_row_that_says_nothing_is_unknown_and_not_exact_truth(self):
        """A NOT_LAUNCHED row, or one whose sections would not read. Defaulting this
        to exact truth would put the misreading back on the rows least able to bear
        it."""
        assert aggregate.row_scored_against({"status": "NOT_LAUNCHED"}) is None
        assert (
            aggregate.row_scored_against(
                {"sections_unreadable": 1, "sections_unread": "KMS"}
            )
            is None
        )


# --------------------------------------------------------------------------- #
# 6-7. The roll-up carries the split, and scoring says so out loud
# --------------------------------------------------------------------------- #
def _synthetic(cell="c", acc=1.0):
    return {
        "cell": cell,
        "doc": "tiny_form.pdf",
        "sub_doc": None,
        "success": True,
        "scalar_accuracy": acc,
        "cell_accuracy": acc,
        "cells_compared": 10,
        "scored_against": analyze.SCORED_EXACT_TRUTH,
        "exact_truth_unavailable": None,
    }


def _reference(cell="c", doc="realkie", sub="a.pdf", acc=0.8):
    return {
        "cell": cell,
        "doc": doc,
        "sub_doc": sub,
        "success": True,
        "weighted_accuracy": acc,
        "scored_against": analyze.SCORED_STACK_EVALUATION,
        "exact_truth_unavailable": analyze.EXACT_TRUTH_UNAVAILABLE_REFERENCE,
    }


@pytest.mark.unit
class TestTheRollUpCarriesTheSplit:
    def test_each_accuracy_n_has_a_denominator_in_the_artifact(self):
        rows = [_synthetic() for _ in range(21)] + [
            _reference(sub=f"d{i}.pdf") for i in range(120)
        ]
        s = aggregate.cell_stats(rows)["c"]
        assert s["n_success"] == 141
        assert s["n_scored_exact_truth"] == 21
        assert s["n_scored_stack_evaluation"] == 120
        # The two `n`s each look thinned against n_success; the counts say why.
        assert s["scalar_accuracy"]["n"] == s["n_scored_exact_truth"]
        assert s["weighted_accuracy"]["n"] == s["n_scored_stack_evaluation"]

    def test_the_counts_do_not_double_count_or_absorb_an_unknown_row(self):
        rows = [
            _synthetic(),
            _reference(),
            {"cell": "c", "success": True, "sections_unreadable": 1},
        ]
        s = aggregate.cell_stats(rows)["c"]
        assert (s["n_scored_exact_truth"], s["n_scored_stack_evaluation"]) == (1, 1)
        assert s["n_success"] == 3

    def test_the_counts_are_over_successful_runs_like_the_stats_they_explain(self):
        """These two numbers exist to be the denominator of a `_stats` `n`, and every
        `_stats` block beside them is taken over `succ`. Counting over all runs would
        make them larger than the `n` they explain on any grid with a failure — and
        every fixture above is all-success, so nothing else here separates the two."""
        rows = [
            _synthetic(),
            {**_synthetic(), "success": False, "status": "FAILED"},
            _reference(),
            {**_reference(sub="b.pdf"), "success": False, "status": "FAILED"},
        ]
        s = aggregate.cell_stats(rows)["c"]
        assert s["n_runs"] == 4 and s["n_success"] == 2
        assert (s["n_scored_exact_truth"], s["n_scored_stack_evaluation"]) == (1, 1)

    def test_the_counts_follow_the_recorded_provenance_not_the_sub_doc_shape(self):
        """A synthetic document with no local truth file is scored by the stack's
        evaluation too — that is what ``run_matrix``'s ``docs_missing_truth`` records —
        and it carries no ``sub_doc``. Reading ``sub_doc`` directly gets that row
        wrong, and agrees with the recorded value on every other row, so this is the
        one fixture that separates the two derivations."""
        rows = [
            _synthetic(),
            {
                "cell": "c",
                "doc": "kv_form.pdf",
                "sub_doc": None,
                "success": True,
                "weighted_accuracy": 0.7,
                "scored_against": analyze.SCORED_STACK_EVALUATION,
                "exact_truth_unavailable": "no .pdf.truth.json for this document",
            },
        ]
        s = aggregate.cell_stats(rows)["c"]
        assert (s["n_scored_exact_truth"], s["n_scored_stack_evaluation"]) == (1, 1)


@pytest.mark.unit
class TestScoringSaysSoOutLoud:
    def test_the_notice_fires_for_reference_rows_with_an_empty_runmap(
        self, capsys, tmp_path
    ):
        """Derived from the ROWS. ``docs_missing_truth`` is empty on every grid that
        ran a reference corpus, which is why the existing notice never fired."""
        rows = [
            _synthetic(),
            _reference(doc="realkie"),
            _reference(doc="ocr_bench", sub="b.pdf"),
        ]
        aggregate.write_summary({"docs_missing_truth": []}, rows, str(tmp_path))
        out = capsys.readouterr().out
        assert "2 of 3 rows" in out
        assert "ocr_bench" in out and "realkie" in out
        assert "weighted_accuracy" in out
        assert "scored_against" in out
        # The two CLAIMS, as contiguous phrases rather than as a bag of tokens. A
        # negated notice keeps every substring above and says the opposite thing,
        # which is a worse artifact than no notice at all.
        assert "are scored by the stack's own evaluation" in out
        assert "NOT against exact per-cell truth" in out

    def test_an_all_synthetic_grid_says_nothing_about_it(self, capsys, tmp_path):
        aggregate.write_summary({}, [_synthetic()], str(tmp_path))
        assert "weighted_accuracy" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 7b. The per-corpus table has a command, so it is not assembled by hand
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestTheReferenceTableRenderer:
    def test_it_renders_one_table_per_corpus_with_the_accuracy_that_applies(
        self, capsys, tmp_path
    ):
        summary = {
            "meta": {"suite": "core", "stack_version": "0.6.11", "overrides": []},
            "rows": [
                _synthetic(),
                _reference(doc="realkie", sub="a.pdf", acc=0.80),
                _reference(doc="realkie", sub="b.pdf", acc=0.70),
                _reference(doc="ocr_bench", sub="c.pdf", acc=0.99),
            ],
        }
        path = tmp_path / "summary.json"
        path.write_text(json.dumps(summary))
        tables = aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert set(tables) == {"realkie", "ocr_bench"}
        assert "### realkie (2 documents) — core on 0.6.11" in out
        assert "### ocr_bench (1 documents) — core on 0.6.11" in out
        # The mean of the two realkie documents, and the synthetic row excluded.
        assert "0.750" in out
        assert "tiny_form" not in out

    def test_a_metric_with_no_observations_is_named_rather_than_left_blank(
        self, capsys, tmp_path
    ):
        """`core-tt-simple-off` runs with assessment off, so it has no confidence at
        all. The published table printed `0.000` there, which is the zero-for-absence
        reading this whole change is about."""
        summary = {
            "meta": {"suite": "core", "stack_version": "0.6.11"},
            "rows": [{**_reference(), "mean_confidence": None}],
        }
        path = tmp_path / "summary.json"
        path.write_text(json.dumps(summary))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "not scored" in out
        assert "0.000" not in out

    def test_a_probability_above_one_is_named_rather_than_printed(
        self, capsys, tmp_path
    ):
        """A confidence is a probability, so 7.585 is not a figure to print.

        Four committed grids carry such a ``mean_confidence`` — one grader leaf
        answered on a 0-100 scale and the row mean carries it (#1395) — and this
        renderer used to format it to three decimals like any other number, which is
        how four of them reached the published guidance as confidences above 1.0
        (#1411). The harness now refuses an out-of-range leaf where it is measured, so
        the only way to meet one is to re-render an already-committed grid; a word
        there is what stops that re-publishing an impossible number.
        """
        row = {
            **_reference(),
            "class_accuracy": 1.0,
            "parse_failures": 0,
            "mean_confidence": 7.585,
            "cost": 0.0223,
            "wall_s": 24.0,
        }
        path = tmp_path / "summary.json"
        path.write_text(
            json.dumps(
                {"meta": {"suite": "core", "stack_version": "0.6.8"}, "rows": [row]}
            )
        )
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "7.585" not in out
        # The whole row, so the word lands in the confidence column and nowhere else —
        # and so it is distinguishable from the no-observations case above, which is a
        # different state that a reader acts on differently.
        assert (
            "| `c` | 1 | 1 | 1 | 0.800 | 1.000 | 0 | not measurable | $0.0223 | 24 s |"
            in out
        )
        assert "not scored" not in out

    @pytest.mark.parametrize("mean_confidence", [1.293, 1.622, 1.625, 1.626, 7.585])
    def test_the_bound_is_one_rather_than_some_larger_number(
        self, capsys, tmp_path, mean_confidence
    ):
        """Pinned on the five roll-ups the committed grids actually produce.

        The four smallest are the ones `config-guidance.md` published, and they are
        barely above the bound, so a guard written against the largest alone would
        print all four. Parametrised over the real values rather than one convenient
        one for that reason.
        """
        row = {**_reference(), "mean_confidence": mean_confidence}
        path = tmp_path / "summary.json"
        path.write_text(json.dumps({"meta": {}, "rows": [row]}))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "not measurable" in out
        assert f"{mean_confidence:.3f}" not in out

    def test_a_probability_of_exactly_one_is_still_a_figure(self, capsys, tmp_path):
        """1.000 is the commonest value in the `class accuracy` column and the bound is
        inclusive, so the guard must not reach it — including when a float mean lands a
        few ULPs above it."""
        rows = [
            {**_reference(sub="a.pdf"), "mean_confidence": 1.0, "class_accuracy": 1.0},
            {**_reference(sub="b.pdf"), "mean_confidence": 0.1 * 3 / 0.3},
        ]
        path = tmp_path / "summary.json"
        path.write_text(json.dumps({"meta": {}, "rows": rows}))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "not measurable" not in out
        assert "1.000" in out

    def test_an_unbounded_column_above_one_is_still_a_figure(self, capsys, tmp_path):
        """Cost and wall-time are not probabilities. The guard is over the three
        columns that are, named in ``PROBABILITY_SCALE_METRICS``, rather than over
        every number in the table."""
        rows = [{**_reference(), "cost": 1.5, "wall_s": 42.0}]
        path = tmp_path / "summary.json"
        path.write_text(json.dumps({"meta": {}, "rows": rows}))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "$1.5000" in out
        assert "42 s" in out
        assert "not measurable" not in out

    def test_every_guarded_column_is_one_this_table_renders(self):
        """The guard names metrics; the table names columns. A metric that is not a
        column of this table would be a guard over nothing."""
        rendered = {m for _h, m in aggregate.REFERENCE_TABLE_COLUMNS if m}
        assert aggregate.PROBABILITY_SCALE_METRICS <= rendered
        assert "mean_confidence" in aggregate.PROBABILITY_SCALE_METRICS

    def test_a_synthetic_only_grid_says_why_it_has_no_table(self, capsys, tmp_path):
        path = tmp_path / "summary.json"
        path.write_text(
            json.dumps({"meta": {"suite": "coresynth"}, "rows": [_synthetic()]})
        )
        assert aggregate.reference_tables(str(path)) == {}
        out = capsys.readouterr().out
        assert "no reference-corpus rows" in out
        assert "docs_reference" in out

    def test_runs_and_documents_are_separate_columns(self, capsys, tmp_path):
        """A 3-repeat grid holds 60 runs over 20 documents, and quoting only the first
        is how a corpus comes to be described as 60 documents. Asserted in the corpus
        HEADING as well as in the row, because they are counted independently and a
        fixture with one run per document cannot tell either of them apart."""
        rows = [
            _reference(sub="a.pdf"),
            _reference(sub="a.pdf"),
            _reference(sub="b.pdf"),
        ]
        path = tmp_path / "summary.json"
        path.write_text(json.dumps({"meta": {}, "rows": rows}))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        header, _, body = out.partition("|---")
        assert "runs" in header and "documents" in header
        assert "(2 documents)" in header
        assert "| 3 | 2 | 3 |" in body

    def test_the_means_are_over_completed_runs(self, capsys, tmp_path):
        """A run that failed has a `completed` column of its own and must not drag the
        accuracy column down — the same rule `cell_stats` follows."""
        rows = [
            _reference(sub="a.pdf", acc=0.80),
            {
                **_reference(sub="b.pdf", acc=0.20),
                "success": False,
                "status": "FAILED",
            },
        ]
        path = tmp_path / "summary.json"
        path.write_text(json.dumps({"meta": {}, "rows": rows}))
        aggregate.reference_tables(str(path))
        out = capsys.readouterr().out
        assert "| 2 | 2 | 1 | 0.800 |" in out


# --------------------------------------------------------------------------- #
# 8. The real artifacts the report was written from, read offline
# --------------------------------------------------------------------------- #
# A non-null `sub_doc` is written only by `score_reference_run`, so this substring
# selects exactly the committed grids that ran a reference corpus. Prefiltering on
# text rather than parsing all 121 committed summaries keeps this a cheap scan: four
# of them carry reference rows and only those are parsed.
_REFERENCE_ROW_MARKER = '"sub_doc": "'


def _committed_grids_with_reference_rows():
    """Every committed summary holding reference-corpus rows, newest last.

    Discovered by scanning rather than pinned to a path: ``results/RETENTION.md``
    keeps one set per release and prunes old ones, so a hardcoded ``v0.6.11`` would
    turn this into a vacuous pass one release later. Failing when NO committed grid
    has them is the point — these are the assertions that hold against real data.
    """
    found = []
    for release in sorted(os.listdir(RESULTS)):
        rel = os.path.join(RESULTS, release)
        if not os.path.isdir(rel):
            continue
        for suite in sorted(os.listdir(rel)):
            path = os.path.join(rel, suite, "summary.json")
            if not os.path.isfile(path):
                continue
            with open(path) as fh:
                text = fh.read()
            if _REFERENCE_ROW_MARKER not in text:
                continue
            rows = json.loads(text).get("rows") or []
            if any(r.get("sub_doc") for r in rows):
                found.append((f"{release}/{suite}", rows))
    assert found, "no committed grid carries reference-corpus rows"
    return found


@pytest.fixture(scope="module")
def committed_grids():
    return _committed_grids_with_reference_rows()


@pytest.mark.unit
class TestTheCommittedGridsAreClassifiedCorrectly:
    def test_every_reference_row_is_scored_and_says_which_scorer_did_it(
        self, committed_grids
    ):
        for where, rows in committed_grids:
            reference = [r for r in rows if r.get("sub_doc")]
            synthetic = [
                r
                for r in rows
                if not r.get("sub_doc") and r.get("status") == "COMPLETED"
            ]
            assert reference and synthetic, where
            for row in reference:
                assert (
                    aggregate.row_scored_against(row) == analyze.SCORED_STACK_EVALUATION
                ), where
                if row.get("status") == "COMPLETED":
                    # The claim the report contradicted: these rows ARE scored
                    # against the corpus labels, by the stack's own evaluation.
                    assert isinstance(row.get("weighted_accuracy"), (int, float)), where
                    for metric in EXACT_KEY_FAMILY:
                        assert metric not in row, (where, metric)
            for row in synthetic:
                assert (
                    aggregate.row_scored_against(row) == analyze.SCORED_EXACT_TRUTH
                ), where

    def test_the_absence_is_symmetric_so_nan_is_a_property_of_the_column_read(
        self, committed_grids
    ):
        """The synthetic rows of the same grids have no ``weighted_accuracy`` either.

        Same mechanism, opposite direction, never reported as "unscored" — which is
        what establishes that the ``nan`` was about which column was read and not
        about the runs.
        """
        for where, rows in committed_grids:
            synthetic = [
                r
                for r in rows
                if not r.get("sub_doc") and r.get("status") == "COMPLETED"
            ]
            assert not any("weighted_accuracy" in r for r in synthetic), where
            assert all(
                isinstance(r.get("cell_accuracy"), (int, float)) for r in synthetic
            ), where
