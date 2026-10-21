# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metric the guidance paper publishes must not be able to go dark (#1490, #1497).

What was wrong
--------------
``typed_accuracy`` and ``typed_fields`` were emitted on every scored synthetic row and
were ``None`` on 7,434 of the 7,458 committed rows that carry the key — every row of
every suite the published tables are built from. The metric itself works: the 24
remaining rows, in the four committed ``kv_form`` sets, carry real values. What was
missing was the one document in the corpus whose truth declares ``fields_typed``, which
no standard suite runs.

The damage was done by the *encoding*, not by the gap. A key that is present and null
already means something specific in this harness — "measured, and there was nothing to
join" (see ``calibration_curve``, and the note above ``SCORED_EXACT_TRUTH``) — so a
metric that was never applicable claiming that state is indistinguishable from one that
was measured and came back empty. Downstream, a consumer that averages it gets an empty
series rather than an error and a table built from it renders blank cells rather than
failing, which is why a column the release procedure asks for by name was quietly
absent from the configuration guidance for several releases before anyone noticed.

What these tests pin
--------------------
1. **Every metric the published page names is live** — non-null on at least one row of
   the committed results tree. This is the issue's own suggested guard: a metric that
   stops being produced anywhere fails a test instead of a review. The metric set is
   parsed out of ``docs/benchmarking/index.md``'s instrument table rather than restated
   here, so a metric cannot be added to the published page without being covered, and
   the evidence is the committed artifacts rather than a figure in a docstring.

   ⚠️ That makes this one check a function of the **retention policy** as well as of
   the harness, and for one metric the policy had to be changed to carry it.
   ``typed_accuracy`` is defined only for a document whose truth declares
   ``fields_typed`` — the ``kv_form`` generator alone today — and ``corefast`` is
   ``[tiny_form, small_narrow, longdesc_100]``, all ``bank_statement``, so **no release
   grid can carry it**. Its only non-null evidence is 24 rows across four one-off
   ``kv_form`` slices, which the pruning rule permits removing once their finding is
   written into a ``docs/benchmarking/`` page — i.e. a policy-compliant prune could turn
   this red with the harness working perfectly.
   ``RETENTION.md`` therefore gives one of those slices an explicit Keep row, and
   ``test_the_kept_slice_backing_typed_accuracy_is_still_policy_protected`` below pins
   that, so the coupling fails as a named condition rather than as a mystery. The
   assertion message still names the prune as a distinct cause with its own remedy,
   because the other two remedies do not apply to it. The contract tests further down
   are the ones that are **not** coupled to the tree: they re-score the corpus's
   generators, so they answer "does the harness still produce it" on its own.
2. **The typed-scalar pair is absent, never present-and-null, on a document that has no
   typed scalar truth** — and present and *measuring* on one that does. Closed over the
   corpus's generators, derived from the generator directory and
   ``matrices/doc_matrix.yaml``, so a generator added tomorrow is covered without an
   edit here. Both branches are required to be non-empty, so neither can go vacuous.
3. **The reason column reaches the CSV**, beside the two columns it explains. A row key
   absent from ``aggregate.CSV_COLS`` is dropped by ``DictWriter(extrasaction="ignore")``
   without a word, which is the state ``typed_fields`` was in: written by the scorer and
   readable from nothing, so the CSV carried a ratio and not the count it was taken
   over.
4. **The same contract for the per-cell pair, on the other axis** (#1497), where there
   are THREE states rather than two and two of them used to encode identically: no list
   attribute (both keys absent), a list that joined nothing (``cell_accuracy`` null and
   ``cells_compared`` the literal 0 — a measurement, so it keeps the present-and-null
   encoding), and a list with cells joined. The three are asserted **pairwise distinct**
   as triples, which is the contract rather than any one branch of it. The reference
   path, where the whole exact-key family is absent and ``exact_truth_unavailable``
   explains it, is a fourth shape this change must leave untouched, and is pinned too.
   ``cell_accuracy`` is the column the configuration guidance leans on hardest, and an
   accuracy figure whose denominator reads as a blank is the specific error that hides
   row loss: a model returning 10 rows of 400 scores 1.000 over the 20 cells it
   returned.
"""

from __future__ import annotations

import glob
import importlib.util
import inspect
import json
import os
import re

import pytest
from harness_import import BENCHMARKS, REPO, harness_module

analyze = harness_module("analyze")
aggregate = harness_module("aggregate")

RESULTS = os.path.join(BENCHMARKS, "results")
GENERATORS = os.path.join(BENCHMARKS, "corpus", "generators")
DOC_MATRIX = os.path.join(BENCHMARKS, "matrices", "doc_matrix.yaml")
PUBLISHED_PAGE = os.path.join(REPO, "docs", "benchmarking", "index.md")


# --------------------------------------------------------------------------- #
# The published metric set, read off the published page
# --------------------------------------------------------------------------- #
def _metric_table_starts(lines) -> list[int]:
    """Line indices of every ``| Metric | Reads |`` header on the published page."""
    return [
        i
        for i, ln in enumerate(lines)
        if re.match(r"^\|\s*Metric\s*\|\s*Reads\s*\|", ln)
    ]


def published_metric_rows() -> dict[str, str]:
    """``{metric: the whole table row}`` from ``docs/benchmarking/index.md``.

    Derived rather than restated, for the usual reason: a list here would be a second
    copy of the published set, and the copy that goes stale is the one nothing reads.
    The table is the page's own "Three of these have more than one instrument, and
    picking the wrong one has produced wrong conclusions here before" block, whose
    first column is a single backticked row key.

    It is **tolerant** on purpose and asserts nothing: it is called at collection time
    by ``@pytest.mark.parametrize``, so a raise here is a collection error, and a
    collection error takes out ``test_the_published_set_was_actually_parsed`` — the
    test written to diagnose exactly this condition — along with everything else in
    the module. A drift that renames the header or adds a second such table therefore
    has to surface as an empty parse that the guard test reports, not as an exception.
    The strict "exactly one table" shape lives in that guard.
    """
    with open(PUBLISHED_PAGE) as fh:
        lines = fh.read().splitlines()
    starts = _metric_table_starts(lines)
    if len(starts) != 1:
        return {}
    rows = {}
    for ln in lines[starts[0] + 2 :]:
        if not ln.startswith("|"):
            break
        cells = ln.split("|")
        match = re.fullmatch(r"`([A-Za-z0-9_.]+)`", cells[1].strip())
        if match:
            rows[match.group(1)] = ln
    return rows


def published_metrics() -> list[str]:
    """Just the metric names, in page order."""
    return list(published_metric_rows())


def committed_rows(root: str = RESULTS):
    """Every scored row in the committed results tree, or in one subtree of it.

    Every committed JSON carrying a ``rows`` list, selected by that shape rather than
    by filename. Most are ``summary.json``, but not all — ``boundary_prompt-*.json``
    and ``prefix_survey_32_classes.json`` are scored grids under another name — and a
    future artifact of the same shape should be read too.

    ``root`` is parameterised so a check about **one** slice reads it the same way the
    whole-tree liveness check does; a second, slightly different reader is how the two
    would come to disagree about whether a metric is present.
    """
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.json"), recursive=True)):
        try:
            with open(path) as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(blob, dict) and isinstance(blob.get("rows"), list):
            rows += [r for r in blob["rows"] if isinstance(r, dict)]
    return rows


@pytest.fixture(scope="module")
def rows():
    return committed_rows()


@pytest.mark.unit
class TestEveryPublishedMetricIsLive:
    def test_the_published_set_was_actually_parsed(self):
        """Guard the guard: an empty set would make every assertion below vacuous.

        This is also where the parser's strict shape is asserted, rather than inside
        ``published_metric_rows``: that runs at collection time, so an assertion there
        raises a collection error, which suppresses this test too and leaves the drift
        undiagnosed.
        """
        with open(PUBLISHED_PAGE) as fh:
            starts = _metric_table_starts(fh.read().splitlines())
        assert len(starts) == 1, (
            f"expected exactly one '| Metric | Reads |' table in {PUBLISHED_PAGE}, "
            f"found {len(starts)} — this parser has to be retargeted, and until it is, "
            "every liveness assertion in this module is parametrized over an empty set"
        )
        names = published_metrics()
        assert len(names) >= 5, names
        # The metric this file exists for must be in it, or the parse has drifted off
        # the table it is aimed at.
        assert "typed_accuracy" in names

    def test_the_evidence_tree_is_not_empty(self, rows):
        """And guard the other input: no committed rows means no liveness evidence."""
        assert len(rows) > 1000, (
            f"only {len(rows)} committed rows found under {RESULTS}; the liveness "
            "check below cannot mean anything against an empty results tree"
        )

    @pytest.mark.parametrize("metric", published_metrics())
    def test_the_metric_is_non_null_on_at_least_one_committed_row(self, metric, rows):
        """A published metric no committed run ever produced is a dark column.

        Null rather than absent is what made #1490 survive: a reader of the artifact
        cannot tell a metric that went dark from one that was measured and had nothing
        to join, and a table built from it renders blanks either way.
        """
        live = sum(1 for r in rows if r.get(metric) is not None)
        assert live, (
            f"`{metric}` is published in docs/benchmarking/index.md and is non-null on "
            f"0 of the {len(rows)} committed scored rows. THREE causes, with different "
            "remedies, so establish which one before fixing it: (1) the harness stopped "
            "producing it — the scorer contract tests in this module answer that, since "
            "they re-score the corpus's generators and do not read the results tree at "
            "all; (2) no committed suite runs a document it applies to, in which case "
            "the page must say so rather than letting the column render blank (#1490); "
            "or (3) THE EVIDENCE WAS PRUNED. The Keep table in "
            "benchmarks/results/RETENTION.md is what a metric's evidence has to be "
            "named in to survive, and only one one-off slice is named there — so a "
            "metric whose evidence sits in any other one-off slice is removable by a "
            "policy-compliant prune with the harness working perfectly. Remedy for (3) "
            "is to restore the slice (`git checkout <SHA> -- benchmarks/results/<dir>/`) "
            "and give it a Keep row in RETENTION.md, or to run a slice that produces the "
            "metric into the release grid."
        )

    def test_the_kept_slice_backing_typed_accuracy_is_still_policy_protected(self):
        """The one metric with no release-grid evidence needs a named Keep row.

        ``typed_accuracy`` is measurable only on a document whose truth declares
        ``fields_typed``, and no document in ``corefast`` does, so the liveness check
        above rests on a one-off slice that the pruning rule would otherwise permit
        removing. This asserts the two halves of that protection together — the slice
        is on disk **and** RETENTION.md names it — because either half alone is the
        state the check was already in: present but unprotected, or protected on paper
        with the bytes gone.
        """
        policy = os.path.join(RESULTS, "RETENTION.md")
        with open(policy) as fh:
            text = fh.read()
        keep_rows = [
            ln
            for ln in text.splitlines()
            if ln.startswith("|") and "v0.6.8/enforcement-kvform/" in ln
        ]
        slice_dir = os.path.join(RESULTS, "v0.6.8", "enforcement-kvform")
        on_disk = os.path.isdir(slice_dir)

        assert keep_rows and on_disk, (
            "`typed_accuracy` is the one published metric no release grid can carry "
            "(it needs a document whose truth declares `fields_typed`; corefast has "
            "none), so its liveness evidence is a one-off kv_form slice and must be "
            "protected explicitly. State found: RETENTION.md Keep row for "
            f"v0.6.8/enforcement-kvform/ = {bool(keep_rows)}, directory present = "
            f"{on_disk}. If the slice was deliberately replaced, point BOTH this test "
            "and the Keep row at the replacement in the same change; if a release grid "
            "now runs a typed-truth document, delete the Keep row and this test "
            "together."
        )

        live = [
            r for r in committed_rows(slice_dir) if r.get("typed_accuracy") is not None
        ]
        assert live, (
            f"{slice_dir} is kept and present but carries 0 rows with a non-null "
            "`typed_accuracy`, so the Keep row protects nothing and the liveness check "
            "above is passing on some other slice that is NOT protected. Find which "
            "slice is carrying it and move the Keep row there."
        )


# --------------------------------------------------------------------------- #
# The typed-scalar pair, closed over the corpus's document generators
# --------------------------------------------------------------------------- #
def generator_names() -> list[str]:
    """The corpus's generators, read off the directory."""
    return sorted(
        os.path.splitext(os.path.basename(p))[0]
        for p in glob.glob(os.path.join(GENERATORS, "*.py"))
        if not os.path.basename(p).startswith("_")
    )


def _load_generator(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(GENERATORS, name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _smallest_matrix_entry(gen, docm):
    """The cheapest ``doc_matrix`` entry using ``gen``, as build kwargs.

    Taking the parameters from the matrix rather than writing them here is what keeps
    this closed: a generator is reachable only through a matrix entry, so a new
    generator arrives with its own parameters and needs no edit in this file. A
    generator with no entry at all is a finding in its own right — nothing in the
    corpus can run it — and is reported as one below.
    """
    entries = [d for d in docm.get("synthetic", []) if d.get("gen") == gen]
    assert entries, (
        f"generator `{gen}` has no entry in {DOC_MATRIX}, so no suite can run it; "
        "add one, or remove the generator"
    )
    build = _load_generator(gen).build
    accepted = set(inspect.signature(build).parameters)
    entry = min(entries, key=lambda d: (d.get("rows", 0), d.get("fields", 0)))
    kwargs = {k: v for k, v in entry.items() if k in accepted}
    # Shrink whatever size knobs this generator has: the contract under test is which
    # keys a scored row carries, and nothing about it needs a big document.
    for knob, cap in (("rows", 2), ("fields", 5)):
        if knob in accepted:
            kwargs[knob] = min(kwargs.get(knob, cap), cap)
    return entry["id"], kwargs


def _perfect_sections(truth):
    """One section holding a correct extraction of ``truth``.

    Correct on purpose rather than empty: an empty inference result would give a
    populated ``typed_accuracy`` of 0.0 and satisfy a presence check while proving
    nothing about whether the metric measures anything.
    """
    result = dict(truth.get("fields") or {})
    result.update(truth.get("fields_typed") or {})
    rows_typed = truth.get("rows_typed") or {}
    if rows_typed:
        result[truth.get("list_key") or "Transactions"] = [
            {"Description": f"{seq} x", **cells} for seq, cells in rows_typed.items()
        ]
    return [{"inference_result": result}]


def _score(truth, sections=None):
    """``score_synthetic`` over ``sections``, defaulting to a correct extraction.

    The override is what reaches the third per-cell state (#1497): a document that HAS
    a list attribute and returned not one of its rows. That is a real measurement over
    an empty join and must stay distinguishable from a document that has no list at
    all, so the suite has to be able to produce both.
    """
    if sections is None:
        sections = _perfect_sections(truth)
    original = analyze.lib.read_sections
    analyze.lib.read_sections = lambda *a, **k: analyze.lib.SectionRead(sections)
    try:
        return analyze.score_synthetic("bucket", "prefix/", truth)
    finally:
        analyze.lib.read_sections = original


@pytest.fixture(scope="module")
def scored_by_generator(tmp_path_factory):
    """``{generator: (truth, scored row)}`` for every generator in the corpus."""
    yaml = pytest.importorskip("yaml")
    with open(DOC_MATRIX) as fh:
        docm = yaml.safe_load(fh)
    out = {}
    base = tmp_path_factory.mktemp("corpus")
    for gen in generator_names():
        doc_id, kwargs = _smallest_matrix_entry(gen, docm)
        path = str(base / f"{doc_id}.pdf")
        _load_generator(gen).build(out=path, **kwargs)
        with open(path + ".truth.json") as fh:
            truth = json.load(fh)
        out[gen] = (truth, _score(truth))
    return out


@pytest.fixture(scope="module")
def truth_by_generator(scored_by_generator):
    """``{generator: truth}``, so a test can re-score the same document differently."""
    return {gen: truth for gen, (truth, _row) in scored_by_generator.items()}


def _assert_named_generators(text, where, expected, declaration, *, optional=False):
    """``text`` must name exactly the generators in ``expected``, by backticked name.

    The lever for the two texts in this contract that cannot be derived from the
    corpus — a module-level reason string in ``analyze``, and prose on the published
    page. ``optional`` is for a text that describes the condition by PROPERTY instead
    ("a document that has a list attribute"), which needs no pin and does not acquire
    one by naming nothing; it still has to be right if it does name a generator.
    """
    named = {gen for gen in generator_names() if f"`{gen}`" in text}
    if optional and not named:
        return
    assert named == expected, (
        f"{where} names generators {sorted(named)} as the ones carrying this truth, "
        f"but the generators that actually declare {declaration} are "
        f"{sorted(expected)}. The text claims to be exhaustive, so the claim is wrong "
        "rather than merely incomplete — update it."
    )


TYPED_KEYS = ("typed_accuracy", "typed_fields")
REASON_KEY = "typed_truth_unavailable"


@pytest.mark.unit
class TestTypedScalarKeysAreAbsentRatherThanNull:
    def test_every_generator_in_the_corpus_was_scored(self, scored_by_generator):
        """Closure: the universe is the generator directory, not a list here."""
        assert set(scored_by_generator) == set(generator_names())
        assert scored_by_generator, "no generators found — this suite proves nothing"

    def test_both_branches_are_exercised(self, scored_by_generator):
        """One generator must declare typed scalars and one must not.

        If every generator fell on the same side, one of the two assertions below
        would be testing nothing while still reporting green — which is the shape of
        failure this file exists to make impossible.
        """
        declares = {
            gen
            for gen, (truth, _) in scored_by_generator.items()
            if truth.get("fields_typed")
        }
        assert declares, "no generator declares `fields_typed`; typed_accuracy is dead"
        assert set(scored_by_generator) - declares, (
            "every generator declares `fields_typed`, so the absence contract below "
            "is untested"
        )

    def test_a_generator_with_typed_scalars_populates_the_pair(
        self, scored_by_generator
    ):
        for gen, (truth, row) in scored_by_generator.items():
            if not truth.get("fields_typed"):
                continue
            assert row["typed_accuracy"] == 1.0, (gen, row["typed_accuracy"])
            assert row["typed_fields"] == len(truth["fields_typed"]), gen
            # The reason column is present and null here, which is this harness's
            # established way of saying "the family applies and nothing is missing".
            assert row[REASON_KEY] is None, gen

    def test_a_generator_without_typed_scalars_carries_neither_key(
        self, scored_by_generator
    ):
        """The #1490 regression, stated directly.

        Before this, both keys were present and null on every row of every
        bank_statement document — which is every row of `corefast`, `coresynth`,
        `simplegrid` and `scaling`, and every synthetic row of `core`. `core` runs
        `core_docs`, which also names the `realkie` and `ocr_bench` reference corpora;
        those go through `score_reference` and carry no typed key at all, so they were
        never part of this defect and are not part of its fix.
        """
        for gen, (truth, row) in scored_by_generator.items():
            if truth.get("fields_typed"):
                continue
            for key in TYPED_KEYS:
                assert key not in row, (
                    f"{gen}: `{key}` is present (value {row.get(key)!r}) on a document "
                    "with no typed scalar truth. Present-and-null here is "
                    "indistinguishable from a measurement that came back empty "
                    "(#1490); the key must be absent and the reason recorded."
                )
            reason = row[REASON_KEY]
            assert reason, gen
            # The reason has to name what is missing and what to read instead, or it
            # is a flag rather than an explanation.
            for word in TYPED_KEYS + ("fields_typed", "cell_accuracy"):
                assert word in reason, (gen, word, reason)

    def test_the_reason_is_a_statement_about_this_document_not_a_failure(
        self, scored_by_generator
    ):
        """It must not read as an error: nothing went wrong on these rows."""
        for _gen, (truth, row) in scored_by_generator.items():
            if truth.get("fields_typed"):
                continue
            assert "absent, not zero" in row[REASON_KEY]
            # The rest of the row is a complete, successful measurement.
            assert row["scalar_accuracy"] is not None
            assert row["completeness_recall"] is not None

    def test_the_hardcoded_generator_names_match_the_generator_set(
        self, scored_by_generator
    ):
        """The two places in this contract that are NOT derived, pinned to the set.

        Everything else here is closed over the generator directory, so a generator
        added tomorrow is covered without an edit. Two texts cannot be: the reason
        string is a module-level constant in ``analyze`` and cannot import the corpus,
        and the published page is prose. Both name ``kv_form`` outright and both say
        "and nothing else". A second generator emitting ``fields_typed`` would satisfy
        every assertion above — the closure is real — and leave those two texts
        telling a reader the opposite of what the harness does, with the suite green.
        This is the assertion that fails instead, and it fails on each text separately
        so the message says which one to edit.
        """
        declares = {
            gen
            for gen, (truth, _) in scored_by_generator.items()
            if truth.get("fields_typed")
        }
        page_row = published_metric_rows().get("typed_accuracy", "")
        assert page_row, (
            f"no `typed_accuracy` row in the instrument table of {PUBLISHED_PAGE}"
        )
        for text, where in (
            (
                analyze.TYPED_TRUTH_UNAVAILABLE_NO_TYPED_SCALARS,
                "analyze.TYPED_TRUTH_UNAVAILABLE_NO_TYPED_SCALARS",
            ),
            (page_row, f"the `typed_accuracy` row of {PUBLISHED_PAGE}"),
        ):
            _assert_named_generators(text, where, declares, "`fields_typed`")

    @pytest.mark.parametrize("column", TYPED_KEYS + (REASON_KEY,))
    def test_the_typed_columns_reach_the_csv(self, column):
        """``DictWriter(extrasaction="ignore")`` drops an unlisted key in silence.

        ``typed_fields`` was in exactly that state — written by the scorer on every
        kv_form row and readable from nothing — so the CSV carried `typed_accuracy` as
        a ratio without the count it was taken over, the same blind spot
        ``cells_compared`` was added to close.
        """
        assert column in aggregate.CSV_COLS


# --------------------------------------------------------------------------- #
# The per-cell pair, on the other axis — and its THIRD state
# --------------------------------------------------------------------------- #
# `cell_accuracy` had the identical defect to the typed pair above, and it matters
# more, because it is the column the configuration guidance leans on hardest: every
# per-model and per-cell table in its §5 quotes it. On a `kv_form` row — a document
# with no list attribute — it was present and null for structurally the same reason:
# there were no cells to compare, not zero correct cells.
#
# What makes this one not a copy of the typed case is that there are THREE states here,
# not two, and two of them used to encode identically:
#
#   1. no list attribute        -> both keys ABSENT, `cell_truth_unavailable` says why
#   2. a list, nothing joined   -> `cell_accuracy` null (0/0), `cells_compared` == 0
#   3. a list, cells joined     -> the ratio and its denominator
#
# State 2 is a real measurement and keeps the present-and-null encoding, which is what
# that encoding means in this harness. It also used to write `cells_compared: None` via
# `cell_tot or None`, so the honest 0 was folded onto the absent one and a reader of
# either the row or the CSV got a blank for both. An accuracy figure whose denominator
# reads as a blank is the specific error that hides row loss: a model returning 10 rows
# of 400 scores 1.000 over the 20 cells it returned (#1497).
CELL_KEYS = ("cell_accuracy", "cells_compared")
CELL_REASON_KEY = "cell_truth_unavailable"


def _empty_sections():
    """One section that extracted nothing — reaches state 2 on a document with a list."""
    return [{"inference_result": {}}]


@pytest.mark.unit
class TestCellKeysAreAbsentRatherThanNull:
    def test_both_cell_branches_are_exercised(self, scored_by_generator):
        """One generator must declare per-row truth and one must not.

        Same closure requirement as the typed pair: if every generator fell on the
        same side of this, one of the assertions below would report green while
        testing nothing.
        """
        declares = {
            gen
            for gen, (truth, _) in scored_by_generator.items()
            if truth.get("rows_typed")
        }
        assert declares, "no generator declares `rows_typed`; cell_accuracy is dead"
        assert set(scored_by_generator) - declares, (
            "every generator declares `rows_typed`, so the absence contract below "
            "is untested"
        )

    def test_a_generator_with_a_list_populates_the_pair(self, scored_by_generator):
        """State 3, and MEASURING — not merely present."""
        for gen, (truth, row) in scored_by_generator.items():
            if not truth.get("rows_typed"):
                continue
            assert row["cell_accuracy"] == 1.0, (gen, row["cell_accuracy"])
            expected = sum(len(cells or {}) for cells in truth["rows_typed"].values())
            assert row["cells_compared"] == expected, gen
            assert row[CELL_REASON_KEY] is None, gen

    def test_a_generator_without_a_list_carries_neither_key(self, scored_by_generator):
        """State 1 — the #1497 regression, stated directly."""
        for gen, (truth, row) in scored_by_generator.items():
            if truth.get("rows_typed"):
                continue
            for key in CELL_KEYS:
                assert key not in row, (
                    f"{gen}: `{key}` is present (value {row.get(key)!r}) on a document "
                    "with no list attribute. There were no cells to compare, not zero "
                    "correct cells — and present-and-null here is indistinguishable "
                    "from a list that joined nothing, which IS a measurement (#1497)."
                )
            reason = row[CELL_REASON_KEY]
            assert reason, gen
            # The reason must name what is missing and what to read instead, or it is
            # a flag rather than an explanation.
            for word in CELL_KEYS + ("rows_typed", "scalar_accuracy", "typed_accuracy"):
                assert word in reason, (gen, word, reason)
            assert "absent, not zero" in reason, gen
            # Nothing went wrong on this row: the rest of it is a complete measurement.
            assert row["scalar_accuracy"] is not None, gen

    def test_a_list_that_joined_nothing_is_measured_not_absent(
        self, truth_by_generator
    ):
        """State 2, which must NOT be absent — it is a measurement over an empty join.

        This is the half of the fix that is easy to get wrong in the other direction.
        A run that returned none of a 400-row table has been measured: the right
        encoding is `cell_accuracy` null, because 0/0 is undefined and a 1.0 there
        would be the "a null column scores 1.000" blind spot in its purest form, with
        `cells_compared` the literal 0 it compared.
        """
        measured = 0
        for gen, truth in truth_by_generator.items():
            if not truth.get("rows_typed"):
                continue
            row = _score(truth, _empty_sections())
            measured += 1
            for key in CELL_KEYS:
                assert key in row, (
                    f"{gen}: `{key}` is absent on a document that HAS a list and "
                    "returned none of it. That is a measurement, not an inapplicable "
                    "metric, and absence is reserved for the latter (#1497)."
                )
            assert row["cell_accuracy"] is None, (gen, row["cell_accuracy"])
            # The denominator is the honest 0, not a blank a reader fills in by
            # guessing correctly. `cell_tot or None` used to fold it onto state 1.
            assert row["cells_compared"] == 0, (gen, row["cells_compared"])
            assert row[CELL_REASON_KEY] is None, gen
        assert measured, "no generator reached state 2; this test proved nothing"

    def test_the_three_states_are_pairwise_distinguishable(self, truth_by_generator):
        """The contract itself, rather than any one of its three branches.

        Read on the three keys together, which is what a consumer of the row, the CSV
        or a table built from either actually does. Before #1497 states 1 and 2 were
        the same triple — `(absent-as-None, absent-as-None, no reason)` — which is how
        "this document has no list" came to render in a published table as a blank
        cell indistinguishable from "this run returned nothing".
        """
        with_list = [t for t in truth_by_generator.values() if t.get("rows_typed")]
        without = [t for t in truth_by_generator.values() if not t.get("rows_typed")]
        assert with_list and without

        def signature(row):
            return tuple(
                (key, key in row, row.get(key))
                for key in CELL_KEYS + (CELL_REASON_KEY,)
            )

        states = {
            "no list attribute": signature(_score(without[0])),
            "list, nothing joined": signature(_score(with_list[0], _empty_sections())),
            "list, cells joined": signature(_score(with_list[0])),
        }
        assert len(set(states.values())) == 3, states

    def test_the_reference_path_keeps_its_own_handling(self):
        """A fourth row shape, which this change must leave exactly as it was.

        A reference corpus is scored by the stack's own evaluation against the corpus
        labels, so the whole exact-key family is absent there and has been since
        #1364 — explained by `exact_truth_unavailable`, not by this change's reason
        column. Pinned because the two absences look identical on a row and the
        remedies are different: one wants a document with a list, the other cannot
        have one at all.
        """
        keys = set(analyze.scored_against_keys(None))
        assert keys == {"scored_against", "exact_truth_unavailable"}
        assert (
            analyze.scored_against_keys(None)["exact_truth_unavailable"]
            == analyze.EXACT_TRUTH_UNAVAILABLE_REFERENCE
        )
        # It names the cell columns, so a reader of a reference row is sent to
        # `weighted_accuracy` rather than to this change's reason column.
        for word in CELL_KEYS + ("weighted_accuracy",):
            assert word in analyze.EXACT_TRUTH_UNAVAILABLE_REFERENCE, word
        # And the two reasons are distinct strings, so a row carrying one is never
        # mistaken for a row carrying the other.
        assert (
            analyze.CELL_TRUTH_UNAVAILABLE_NO_LIST
            != analyze.EXACT_TRUTH_UNAVAILABLE_REFERENCE
        )

    def test_a_list_free_synthetic_row_is_still_identified_as_exact_truth(
        self, scored_by_generator
    ):
        """`row_scored_against`'s shape fallback must survive the two keys going away.

        It falls back to `any(k in row for k in EXACT_TRUTH_METRICS)`, and two of
        those four are now conditional on the document — `cells_compared` and
        `cell_accuracy` here, `typed_accuracy` in #1490. A `kv_form` row is left
        carrying `scalar_accuracy` and `completeness_recall`, which `score_synthetic`
        writes unconditionally; if that ever stopped being true, a list-free synthetic
        row would be classified as "unknown" and drop out of `n_scored_exact_truth`.
        """
        for gen, (truth, row) in scored_by_generator.items():
            if truth.get("rows_typed"):
                continue
            shape_only = {k: v for k, v in row.items() if k != "scored_against"}
            shape_only["sub_doc"] = None
            assert (
                aggregate.row_scored_against(shape_only) == analyze.SCORED_EXACT_TRUTH
            ), gen

    def test_the_hardcoded_generator_names_match_the_generator_set(
        self, scored_by_generator
    ):
        """The same pin as on the typed side, for the texts that name a generator.

        ``CELL_TRUTH_UNAVAILABLE_NO_LIST`` names ``bank_statement`` outright and says
        "only", so it is wrong rather than incomplete if a second generator starts
        emitting ``rows_typed``. The published table row is pinned only *if* it names
        one: it states the condition by property — "a document that has a list
        attribute" — which is the better form here and needs no maintenance, but a
        generator name added to it later must still be the right one.
        """
        declares = {
            gen
            for gen, (truth, _) in scored_by_generator.items()
            if truth.get("rows_typed")
        }
        page_row = published_metric_rows().get("cell_accuracy", "")
        assert page_row, (
            f"no `cell_accuracy` row in the instrument table of {PUBLISHED_PAGE}"
        )
        _assert_named_generators(
            analyze.CELL_TRUTH_UNAVAILABLE_NO_LIST,
            "analyze.CELL_TRUTH_UNAVAILABLE_NO_LIST",
            declares,
            "`rows_typed`",
        )
        _assert_named_generators(
            page_row,
            f"the `cell_accuracy` row of {PUBLISHED_PAGE}",
            declares,
            "`rows_typed`",
            optional=True,
        )

    def test_neither_reason_string_is_emitted_without_the_other_applying(
        self, scored_by_generator
    ):
        """The bound on the two reasons cross-referencing each other's metrics.

        ``CELL_TRUTH_UNAVAILABLE_NO_LIST`` sends the reader to `typed_accuracy` and
        ``TYPED_TRUTH_UNAVAILABLE_NO_TYPED_SCALARS`` sends them to `cell_accuracy`, so
        a generator declaring NEITHER kind of truth would emit both reasons, each
        naming a column that is also absent on that row. Not reachable today, and this
        is what fails if it becomes reachable: such a generator needs a reason string
        of its own rather than these two together.
        """
        for gen, (truth, row) in scored_by_generator.items():
            assert truth.get("rows_typed") or truth.get("fields_typed"), (
                f"{gen} declares neither `rows_typed` nor `fields_typed`, so its rows "
                f"carry both reason strings and each refers the reader to a column "
                f"that is absent too: {row.get(CELL_REASON_KEY)!r} and "
                f"{row.get(REASON_KEY)!r}. Give it a reason of its own."
            )

    @pytest.mark.parametrize("column", CELL_KEYS + (CELL_REASON_KEY,))
    def test_the_cell_columns_reach_the_csv(self, column):
        """``DictWriter(extrasaction="ignore")`` drops an unlisted key in silence."""
        assert column in aggregate.CSV_COLS
