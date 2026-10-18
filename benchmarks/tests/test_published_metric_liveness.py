# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A metric the guidance paper publishes must not be able to go dark (#1490).

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
def published_metrics() -> list[str]:
    """Metric names from the instrument table in ``docs/benchmarking/index.md``.

    Derived rather than restated, for the usual reason: a list here would be a second
    copy of the published set, and the copy that goes stale is the one nothing reads.
    The table is the page's own "Three of these have more than one instrument, and
    picking the wrong one has produced wrong conclusions here before" block, whose
    first column is a single backticked row key.
    """
    with open(PUBLISHED_PAGE) as fh:
        lines = fh.read().splitlines()
    starts = [
        i
        for i, ln in enumerate(lines)
        if re.match(r"^\|\s*Metric\s*\|\s*Reads\s*\|", ln)
    ]
    assert len(starts) == 1, (
        f"expected exactly one '| Metric | Reads |' table in {PUBLISHED_PAGE}, "
        f"found {len(starts)} — this parser has to be retargeted"
    )
    names = []
    for ln in lines[starts[0] + 2 :]:
        if not ln.startswith("|"):
            break
        cell = ln.split("|")[1].strip()
        match = re.fullmatch(r"`([A-Za-z0-9_.]+)`", cell)
        if match:
            names.append(match.group(1))
    return names


def committed_rows():
    """Every scored row in the committed results tree, as read from ``summary*.json``."""
    rows = []
    for path in sorted(
        glob.glob(os.path.join(RESULTS, "**", "*.json"), recursive=True)
    ):
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
        """Guard the guard: an empty set would make every assertion below vacuous."""
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
            f"0 of the {len(rows)} committed scored rows. Either the harness stopped "
            "producing it, or no committed suite runs a document it applies to — in "
            "which case the page must say so rather than letting the column render "
            "blank (#1490)."
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


def _score(truth):
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
        bank_statement document — which is every row of `core`, `corefast`,
        `coresynth`, `simplegrid` and `scaling`.
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

    @pytest.mark.parametrize("column", TYPED_KEYS + (REASON_KEY,))
    def test_the_typed_columns_reach_the_csv(self, column):
        """``DictWriter(extrasaction="ignore")`` drops an unlisted key in silence.

        ``typed_fields`` was in exactly that state — written by the scorer on every
        kv_form row and readable from nothing — so the CSV carried `typed_accuracy` as
        a ratio without the count it was taken over, the same blind spot
        ``cells_compared`` was added to close.
        """
        assert column in aggregate.CSV_COLS
