# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Typed scalar + per-cell scoring — the metrics that make value handling visible.

Why this exists
---------------
The benchmark's ground truth records the text a document RENDERS (``"$685.50"``),
and ``scalar_accuracy`` compares it with ``str()``. That is correct for the
question it answers, but it means a pipeline that correctly returns the *number*
``685.5`` is scored WRONG — so any value-normalization behaviour is measured as a
regression, exactly backwards.

Compounding it, list scoring was completeness-only (SEQ tags), so per-row cell
values were invisible to every metric. An enforcement A/B therefore recorded 81
value repairs and a delta of exactly zero on every dimension, and the run looked
uninformative rather than mis-instrumented.

Two metrics fix that, and both are ADDITIVE — ``scalar_accuracy`` keeps its old
meaning so the committed baseline stays comparable:

* ``typed_accuracy`` — scalar fields vs a schema-TYPED expectation
  (``fields_typed``).
* ``cell_accuracy``  — list cells vs typed per-row truth (``rows_typed``),
  matched to the extraction by SEQ tag.
"""

import sys

import pytest

sys.path.insert(0, "benchmarks/harness")

analyze = pytest.importorskip("analyze")


# --------------------------------------------------------------------------- #
# typed_match — type fidelity is the point, not just value equality
# --------------------------------------------------------------------------- #
class TestTypedMatch:
    @pytest.mark.parametrize(
        ("expected", "got"),
        [
            (685.5, 685.5),
            (685.5, 685.50),
            (600.0, 600),  # JSON does not distinguish int from float
            (0.0, 0),
            (-13.7, -13.7),
            (70000.0, 70000),
        ],
    )
    def test_numbers_compare_by_value(self, expected, got):
        assert analyze.typed_match(expected, got) is True

    @pytest.mark.parametrize(
        "got",
        [
            "685.5",  # right value, wrong TYPE -- the failure under test
            "$685.50",  # the rendered form, un-normalized
            "685,50",
            None,
            True,
            {"G1": 685.5},  # a 1S-TopK candidate object that escaped the split
            [685.5],
        ],
    )
    def test_a_non_number_in_a_number_field_is_a_miss(self, got):
        assert analyze.typed_match(685.5, got) is False

    def test_wrong_number_is_a_miss(self):
        assert analyze.typed_match(685.5, 685.6) is False

    @pytest.mark.parametrize(("expected", "got"), [(True, True), (False, False)])
    def test_booleans_must_be_real_booleans(self, expected, got):
        assert analyze.typed_match(expected, got) is True

    @pytest.mark.parametrize("got", ["true", "Yes", "yes", "True", 1, 0, 1.0])
    def test_boolean_ish_strings_and_ints_are_misses(self, got):
        # A `boolean` field that comes back as "Yes" -- or as the int 1 -- is
        # precisely the defect a typed metric exists to catch, so none of these
        # may score as correct.
        assert analyze.typed_match(True, got) is False

    def test_bool_is_checked_before_the_numeric_branch(self):
        """`isinstance(True, int)` is True in Python, so order matters."""
        assert analyze.typed_match(1.0, True) is False
        assert analyze.typed_match(True, 1) is False

    @pytest.mark.parametrize(
        ("expected", "got", "want"),
        [
            ("1985-05-14", "1985-05-14", True),
            ("1985-05-14", "  1985-05-14  ", True),  # whitespace tolerated
            ("1985-05-14", "05/14/1985", False),  # un-normalized -> miss
            ("1985-05-14", None, False),
            ("1985-05-14", 19850514, False),
        ],
    )
    def test_dates_are_strings_compared_exactly(self, expected, got, want):
        assert analyze.typed_match(expected, got) is want

    def test_none_expectation_requires_none(self):
        assert analyze.typed_match(None, None) is True
        assert analyze.typed_match(None, "") is False


# --------------------------------------------------------------------------- #
# score_cells — per-row value fidelity, matched by SEQ
# --------------------------------------------------------------------------- #
def _section(rows, key="Transactions"):
    return {"inference_result": {key: rows}}


class TestScoreCells:
    ROWS_TYPED = {
        "SEQ00000": {"Date": "2024-01-13", "Amount": 0.0},
        "SEQ00001": {"Date": "2024-02-14", "Amount": -13.7},
    }

    def test_all_cells_correct(self):
        secs = [
            _section(
                [
                    {"Description": "SEQ00000 x", "Date": "2024-01-13", "Amount": 0.0},
                    {
                        "Description": "SEQ00001 y",
                        "Date": "2024-02-14",
                        "Amount": -13.7,
                    },
                ]
            )
        ]
        hits, total, matched = analyze.score_cells(
            secs, self.ROWS_TYPED, "Transactions"
        )
        assert (hits, total, matched) == (4, 4, 2)

    def test_un_normalized_values_score_as_misses(self):
        """The whole point: rendered text in a typed field must not pass."""
        secs = [
            _section(
                [
                    {
                        "Description": "SEQ00000 x",
                        "Date": "01/13/2024",
                        "Amount": "$0.00",
                    },
                    {
                        "Description": "SEQ00001 y",
                        "Date": "14 Feb 2024",
                        "Amount": "(13.70)",
                    },
                ]
            )
        ]
        hits, total, _ = analyze.score_cells(secs, self.ROWS_TYPED, "Transactions")
        assert (hits, total) == (0, 4)

    def test_a_row_that_never_came_back_is_not_counted(self):
        """Missing rows are recall's business; counting them here double-penalizes.

        cell_accuracy answers "were the recovered values right", so a truncated
        run must not also show as a value problem.
        """
        secs = [
            _section(
                [{"Description": "SEQ00000 x", "Date": "2024-01-13", "Amount": 0.0}]
            )
        ]
        hits, total, matched = analyze.score_cells(
            secs, self.ROWS_TYPED, "Transactions"
        )
        assert (hits, total, matched) == (2, 2, 1)

    def test_absent_cell_counts_as_a_miss_not_a_skip(self):
        secs = [_section([{"Description": "SEQ00000 x", "Date": "2024-01-13"}])]
        hits, total, _ = analyze.score_cells(secs, self.ROWS_TYPED, "Transactions")
        assert (hits, total) == (1, 2)  # Date hit, Amount absent -> miss

    def test_field_names_match_case_insensitively(self):
        secs = [
            _section(
                [{"description": "SEQ00000 x", "date": "2024-01-13", "amount": 0.0}]
            )
        ]
        hits, total, _ = analyze.score_cells(secs, self.ROWS_TYPED, "Transactions")
        assert (hits, total) == (2, 2)

    def test_rows_are_found_across_multiple_sections(self):
        secs = [
            _section(
                [{"Description": "SEQ00000 x", "Date": "2024-01-13", "Amount": 0.0}]
            ),
            _section(
                [{"Description": "SEQ00001 y", "Date": "2024-02-14", "Amount": -13.7}]
            ),
        ]
        hits, total, matched = analyze.score_cells(
            secs, self.ROWS_TYPED, "Transactions"
        )
        assert (hits, total, matched) == (4, 4, 2)

    def test_no_typed_truth_means_no_metric(self):
        """Absent `rows_typed` must yield None, not 0 -- 0 would read as a failure."""
        assert analyze.score_cells([_section([])], None, "Transactions") == (
            None,
            None,
            None,
        )
        assert analyze.score_cells([_section([])], {}, "Transactions") == (
            None,
            None,
            None,
        )

    def test_a_row_with_no_seq_tag_is_ignored_rather_than_mismatched(self):
        secs = [_section([{"Date": "2024-01-13", "Amount": 0.0}])]
        hits, total, matched = analyze.score_cells(
            secs, self.ROWS_TYPED, "Transactions"
        )
        assert matched == 0 and (hits, total) == (0, 0)

    def test_a_non_list_value_under_the_list_key_does_not_raise(self):
        secs = [_section("not a list")]
        assert analyze.score_cells(secs, self.ROWS_TYPED, "Transactions") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# Backward compatibility — the committed baseline must stay comparable
# --------------------------------------------------------------------------- #
class TestExistingScoringIsUnchanged:
    def test_string_truth_still_matches_the_rendered_text(self):
        """Every pre-existing truth field is a rendered string. Do not break it."""
        assert analyze.typed_match("$685.50", "$685.50") is True
        assert analyze.typed_match("000123456789", "000123456789") is True

    def test_new_metrics_are_none_when_the_truth_does_not_declare_them(self):
        """A truth file without `fields_typed`/`rows_typed` reports None, not 0.

        `aggregate.py` averages these; a 0 would drag every historical cell down
        and read as a regression that never happened.
        """
        hits, total, matched = analyze.score_cells([], None, None)
        assert hits is total is matched is None


# --------------------------------------------------------------------------- #
# The generators actually produce what the scorer expects
# --------------------------------------------------------------------------- #
class TestGeneratorTruthContract:
    @staticmethod
    def _gen(name):
        import importlib.util
        import os

        path = os.path.join("benchmarks", "corpus", "generators", name + ".py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_kv_form_typed_expectations_are_the_converted_rendered_values(self):
        kv = self._gen("kv_form")
        assert kv._typed("Premium", "$685.50") == 685.5
        assert kv._typed("Interest Rate", "3.90%") == 3.9  # NOT divided by 100
        assert kv._typed("Date of Birth", "05/14/1985") == "1985-05-14"
        # Fields a typed schema would NOT convert stay strings.
        for label in ("Policy Number", "ZIP Code", "Tax ID", "Phone", "Full Name"):
            assert kv._typed(label, "whatever") is None, label

    def test_value_noise_dates_are_never_ambiguous(self):
        """Day is forced to 13..28 so no rendering can be misread as M/D vs D/M.

        A benchmark that scored a day<=12 numeric date would be rewarding a guess;
        the correct pipeline behaviour on those is to REFUSE.
        """
        bs = self._gen("bank_statement")
        for seq in range(200):
            rendered, iso = bs._date_pair(seq)
            day = int(iso.split("-")[2])
            assert 13 <= day <= 28, (seq, rendered, iso)

    def test_value_noise_amount_renderings_all_decode_to_the_typed_value(self):
        """Every rendering style must be recoverable -- otherwise the metric is
        measuring the generator's ambiguity rather than the pipeline."""
        from idp_common.extraction.coercion import coerce_extraction

        bs = self._gen("bank_statement")
        schema = {"type": "object", "properties": {"Amount": {"type": "number"}}}
        for seq in range(24):
            _cells, typed = bs._row(seq, 3, "short", 0.0, value_noise=True)
            rendered = _cells[2]
            report = coerce_extraction({"Amount": rendered}, schema)
            got = report.data["Amount"]
            assert got == pytest.approx(typed["Amount"]), (
                f"seq={seq} rendered={rendered!r} -> {got!r}, "
                f"expected {typed['Amount']!r}"
            )

    def test_value_noise_off_leaves_the_row_and_truth_untouched(self):
        bs = self._gen("bank_statement")
        cells, typed = bs._row(7, 3, "short", 0.0, value_noise=False)
        assert typed is None
        assert cells[0] == "08/08/2024"  # the original renderer
        assert cells[2] == "-95.90"


# --------------------------------------------------------------------------- #
# Schema <-> truth consistency
#
# `typed_accuracy` only means anything if the class schema the document is
# extracted under actually declares the types the truth expects. The corpus had
# no class for kv_form at all -- doc_matrix excluded it as "future work" and
# make_configs pointed it at an unrelated forms schema, which yields a meaningless
# number rather than a visible error. Both are now derived from one source; this
# is the guard that keeps them that way.
# --------------------------------------------------------------------------- #
class TestKvFormSchemaMatchesTruth:
    @staticmethod
    def _artifacts():
        import json
        import os

        import yaml

        base = os.path.join("benchmarks", "corpus", "docs", "kv_form.pdf")
        if not (
            os.path.exists(base + ".truth.json")
            and os.path.exists(base + ".classes.yaml")
        ):
            pytest.skip("run benchmarks/harness/gen_corpus.py --only kv_form first")
        truth = json.load(open(base + ".truth.json"))
        classes = yaml.safe_load(open(base + ".classes.yaml"))
        return truth, classes["classes"][0]["properties"]

    def test_every_field_is_declared(self):
        truth, props = self._artifacts()
        assert set(props) == set(truth["fields"]), (
            "schema and truth disagree about which fields exist"
        )

    def test_every_typed_expectation_has_a_matching_declared_type(self):
        truth, props = self._artifacts()
        for label, expected in truth["fields_typed"].items():
            decl = props[label]
            if isinstance(expected, float):
                assert decl["type"] == "number", (label, decl)
                assert decl.get("format") is None, (label, decl)
            else:
                assert decl["type"] == "string" and decl.get("format") == "date", (
                    label,
                    decl,
                )

    def test_untyped_fields_are_declared_as_plain_strings(self):
        """A ZIP code is not a number. Coercing one would be a bug, so the schema
        must not invite it."""
        truth, props = self._artifacts()
        for label in set(truth["fields"]) - set(truth["fields_typed"]):
            decl = props[label]
            assert decl["type"] == "string", (label, decl)
            assert decl.get("format") is None, (label, decl)

    def test_the_typed_set_is_not_empty(self):
        """Guard the guard: a generator change that stopped emitting typed
        expectations would make every assertion above vacuous."""
        truth, _ = self._artifacts()
        assert len(truth["fields_typed"]) >= 5


# --------------------------------------------------------------------------- #
# run_matrix per-class doc routing
#
# A suite may name documents of several classes, but configs are built per class.
# run_matrix used to run EVERY named doc under whatever --class was passed --
# despite a comment claiming "only synthetic docs handled by this class run here"
# -- so a flat form scored under a transaction-list schema produced a meaningless
# number that looked real. These pin the routing.
# --------------------------------------------------------------------------- #
class TestDocsForClass:
    DOCM = {
        "synthetic": [
            {"id": "valuenoise_100", "gen": "bank_statement"},
            {"id": "small_narrow", "gen": "bank_statement"},
            {"id": "kv_form", "gen": "kv_form"},
        ],
        "reference": [{"id": "realkie", "config": "realkie-fcc-verified"}],
    }

    @staticmethod
    def _fn():
        rm = pytest.importorskip("run_matrix")
        return rm._docs_for_class

    def test_only_the_requested_class_is_kept(self):
        keep, other = self._fn()(
            ["valuenoise_100", "small_narrow", "kv_form"], self.DOCM, "bank_statement"
        )
        assert keep == ["valuenoise_100", "small_narrow"]
        assert other == ["kv_form"]

    def test_the_other_direction_too(self):
        keep, other = self._fn()(["valuenoise_100", "kv_form"], self.DOCM, "kv_form")
        assert keep == ["kv_form"]
        assert other == ["valuenoise_100"]

    def test_reference_docs_route_on_their_declared_config(self):
        keep, other = self._fn()(["realkie"], self.DOCM, "realkie-fcc-verified")
        assert keep == ["realkie"] and other == []
        keep, other = self._fn()(["realkie"], self.DOCM, "bank_statement")
        assert keep == [] and other == ["realkie"]

    def test_an_unknown_doc_is_kept_not_silently_dropped(self):
        """Better a loud missing-PDF error than a doc that vanishes from the grid."""
        keep, other = self._fn()(["not_in_matrix"], self.DOCM, "bank_statement")
        assert keep == ["not_in_matrix"] and other == []

    def test_single_class_suites_are_unaffected(self):
        """The common case must behave exactly as before this change."""
        docs = ["valuenoise_100", "small_narrow"]
        keep, other = self._fn()(docs, self.DOCM, "bank_statement")
        assert keep == docs and other == []


# --------------------------------------------------------------------------- #
# sections_correct -- the only metric that can see a boundary-detection failure
#
# A document split into 3 sections instead of 1 still reports
# completeness_recall 1.0 and status COMPLETED, because every row came back --
# just distributed across sections that should not exist (#653/#726). Reported as
# 1.0/0.0 so the MEAN over repeats is the pass rate, which is the only meaningful
# reading of a non-deterministic failure.
# --------------------------------------------------------------------------- #
class TestSectionCountScoring:
    @staticmethod
    def _gen():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "bs", "benchmarks/corpus/generators/bank_statement.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_generator_declares_the_expected_section_count(self, tmp_path):
        bs = self._gen()
        import json

        for docs in (1, 2, 3):
            out = str(tmp_path / f"d{docs}.pdf")
            bs.build(rows=10, documents=docs, out=out)
            truth = json.load(open(out + ".truth.json"))
            assert truth["expected_sections"] == docs

    def test_rows_are_per_document_not_a_shared_budget(self, tmp_path):
        """The first version consumed the whole row budget on copy 1, leaving the
        second statement empty — which would have made the over-merge test
        meaningless (an empty second document cannot be merged wrongly)."""
        bs = self._gen()
        import json

        out = str(tmp_path / "two.pdf")
        bs.build(rows=20, documents=2, out=out)
        truth = json.load(open(out + ".truth.json"))
        assert len(truth["seq_ids"]) == 40
        assert len(set(truth["seq_ids"])) == 40, "SEQ tags must stay globally unique"

    def test_default_is_a_single_document(self, tmp_path):
        """Every existing corpus doc must be unaffected."""
        bs = self._gen()
        import json

        out = str(tmp_path / "one.pdf")
        bs.build(rows=10, out=out)
        assert json.load(open(out + ".truth.json"))["expected_sections"] == 1

    def test_absent_expectation_yields_none_not_a_failure(self):
        """A truth file that does not declare it must report None, not 0.0 —
        0.0 would read as a boundary failure on every historical document."""
        assert (
            analyze.score_synthetic.__doc__ is not None
        )  # sanity: the function exists
        # exercised through the real scorer with a stubbed section reader
        import types

        secs = [{"inference_result": {}}]
        orig = analyze.lib.iter_section_results
        analyze.lib.iter_section_results = lambda *a, **k: iter(secs)
        try:
            out = analyze.score_synthetic("b", "p/", {"fields": {}, "seq_ids": []})
            assert out["sections_correct"] is None
            assert out["sections_expected"] is None
            assert out["sections"] == 1
        finally:
            analyze.lib.iter_section_results = orig
        assert isinstance(types, types.ModuleType)

    @pytest.mark.parametrize(
        ("expected", "actual", "want"),
        [(1, 1, 1.0), (1, 3, 0.0), (2, 2, 1.0), (2, 1, 0.0), (2, 3, 0.0)],
    )
    def test_scored_one_or_zero_so_the_mean_is_a_pass_rate(
        self, expected, actual, want
    ):
        secs = [{"inference_result": {}} for _ in range(actual)]
        orig = analyze.lib.iter_section_results
        analyze.lib.iter_section_results = lambda *a, **k: iter(secs)
        try:
            out = analyze.score_synthetic(
                "b", "p/", {"fields": {}, "seq_ids": [], "expected_sections": expected}
            )
        finally:
            analyze.lib.iter_section_results = orig
        assert out["sections_correct"] == want
        assert out["sections"] == actual
