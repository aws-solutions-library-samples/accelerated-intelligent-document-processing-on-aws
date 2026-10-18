# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Row identity under a digit-perturbing OCR backend (GitHub #1500).

What this is for
----------------
``completeness_recall`` is distinct row identifiers recovered over the truth file's
id count, and for four releases that denominator was read as a row count when it was
a count of *recognisable* identifiers. ``SEQ(\\d{5})`` requires exactly five digits
and the regex engine will happily take the first five of six, so a tag the
``bedrock_llm`` OCR backend returned a digit wider — ``SEQ00042`` as ``SEQ000042`` —
was not missed. It read as ``int("00004") == 4`` and landed on row 4, which was
already present. The distinct count fell by one with no row anywhere absent, and the
recall fell with it.

The artifacts under ``benchmarks/results/`` are what make this testable offline, and
this suite derives everything it asserts from them rather than restating figures:

* the population — every committed row whose ``dups`` is at least its shortfall, the
  shape the issue was found by. ``dups`` is 0 for truncation, because the rows that
  do come back are distinct; it pays for the shortfall one-for-one under identifier
  collapse. 105 rows across 13 distinct ``(truth, extracted, dups, prefix)`` shapes
  carry it today;
* the corroboration — ``conf_rows_expected``, which is
  ``idp_common.assessment.batching.confidence_coverage`` counting the rows of every
  list-valued extracted attribute and knows nothing about ``SEQ``. It reads the full
  truth count on every one of those rows that records it;
* the mechanism — each committed shape is turned back into the perturbation that
  produces it (a duplicated leading zero over a contiguous block of ids, derived
  from the row's own ``truncation_prefix`` and ``dups``), run through the historical
  five-digit expression to confirm it reproduces the artifact exactly, and then
  through the real :func:`analyze.score_synthetic`.

What would have caught it
-------------------------
:func:`test_the_canonicalised_scorer_recovers_every_row` is the one that matters: a
run returning every row must not be reported as having lost any, and the committed
artifacts say which runs those were. :func:`test_the_guard_names_the_shape` covers
the backstop — ``dups`` at or above the shortfall is a shape truncation cannot
produce, and that was recorded on every affected run while nothing consulted it.

What this cannot tell you
-------------------------
Three of the 105 rows are not reproduced by a *contiguous* corrupted block, which is
expected — the corruption is page-scoped, so a run whose corrupted pages are not
adjacent needs a union of blocks. One of those three
(``scale_1600``/``core-tt-adv-sep``) returned 1,698 tags for 1,600 truth rows, so it
carries genuinely duplicated rows as well and its single missing id is not
attributable to collapse from these artifacts alone. The committed figures are left
exactly as scored; this suite is about what the scorer does from now on.
"""

from __future__ import annotations

import collections
import json
import pathlib
import re

import pytest
from harness_import import harness_module

analyze = harness_module("analyze")
lib = harness_module("lib")

RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results"

# The historical expression, spelled out once. This is the thing under
# characterisation, so it is reproduced rather than imported — `lib.SEQ` is the fixed
# pattern now, and a test that read the behaviour off the module it is meant to pin
# would pass whatever the module did.
STRICT_FIVE_DIGIT = re.compile(r"SEQ(\d{5})")

# Non-vacuity floors, measured over the committed grids. One-sided: adding a grid can
# only add rows, and the fix means a new grid should add none. A collapse in either
# figure means the population this suite reasons over is gone, and the assertions
# below would then be statements about an empty list.
MIN_SIGNATURE_ROWS = 105
MIN_SIGNATURE_SHAPES = 13

# How many of those shapes a single contiguous block of leading-zero perturbations
# reproduces exactly — 10 of 13, covering 102 of the 105 rows. Pinned so that a
# change to `SEQ_STRICT`, or a corpus change that moves id rendering, shows up here
# as the mechanism no longer reproducing rather than as a quieter failure elsewhere.
MIN_SHAPES_REPRODUCED = 10
MIN_ROWS_REPRODUCED = 102


def _signature_rows():
    """Every committed row whose shortfall is at least matched by its ``dups``."""
    rows = []
    for path in sorted(RESULTS.glob("*/*/summary.json")):
        grid = str(path.relative_to(RESULTS).parent)
        for row in json.loads(path.read_text()).get("rows", []):
            truth, got, dups = (
                row.get("rows_truth"),
                row.get("rows_extracted"),
                row.get("dups"),
            )
            if truth is None or got is None:
                continue
            shortfall = truth - got
            if 0 < shortfall <= (dups or 0):
                rows.append({"grid": grid, **row})
    return rows


SIGNATURE_ROWS = _signature_rows()


def _shape(row):
    return (
        row["rows_truth"],
        row["rows_extracted"],
        row["dups"],
        row["truncation_prefix"],
    )


SHAPES = collections.Counter(_shape(r) for r in SIGNATURE_ROWS)


def _perturb_leading_zero(tag):
    """``SEQ00042`` as the OCR backend returned it: ``SEQ000042``."""
    return "SEQ0" + tag[3:]


def _tags(n_truth, corrupted):
    """One ``SEQnnnnn`` tag per truth row, perturbed over ``corrupted``."""
    out = []
    for seq in range(n_truth):
        tag = f"SEQ{seq:05d}"
        out.append(_perturb_leading_zero(tag) if seq in corrupted else tag)
    return out


def _score_strictly(n_truth, corrupted):
    """``(rows_extracted, dups, truncation_prefix)`` the five-digit pattern reported."""
    seqs = [
        int(m)
        for tag in _tags(n_truth, corrupted)
        for m in STRICT_FIVE_DIGIT.findall(tag)
    ]
    extracted = set(seqs)
    prefix = 0
    while prefix in extracted:
        prefix += 1
    return len(extracted), len(seqs) - len(extracted), prefix


def _block_for(shape):
    """The contiguous corrupted id block a committed shape implies, or ``None``.

    Derived from the row's own numbers: the shortfall starts where the recovered
    prefix stops, and it is ``dups`` ids long. Returned only when scoring that block
    through the historical expression reproduces the artifact exactly — which is the
    claim, not an assumption.
    """
    truth, got, dups, prefix = shape
    if prefix is None:
        return None
    block = set(range(prefix, min(prefix + dups, truth)))
    if _score_strictly(truth, block) == (got, dups, prefix):
        return block
    return None


REPRODUCED = {s: b for s in SHAPES if (b := _block_for(s)) is not None}


def _score_through_the_harness(n_truth, corrupted, monkeypatch, typed=True):
    """Run the real :func:`analyze.score_synthetic` over one perturbed extraction."""
    rows = [
        {"Description": f"{tag} AnyCompany Store", "Amount": float(seq)}
        for seq, tag in enumerate(_tags(n_truth, corrupted))
    ]
    sections = [
        {"inference_result": {"Transactions": rows}, "explainability_info": [{}]}
    ]
    truth = {
        "seq_ids": [f"SEQ{s:05d}" for s in range(n_truth)],
        "list_key": "Transactions",
    }
    if typed:
        truth["rows_typed"] = {
            f"SEQ{s:05d}": {"Amount": float(s)} for s in range(n_truth)
        }
    monkeypatch.setattr(lib, "read_sections", lambda *a, **k: lib.SectionRead(sections))
    return analyze.score_synthetic("bucket", "prefix/", truth)


# --------------------------------------------------------------------------- #
# The population, and that it is not empty
# --------------------------------------------------------------------------- #
def test_the_committed_grids_carry_the_signature():
    assert len(SIGNATURE_ROWS) >= MIN_SIGNATURE_ROWS, (
        f"{len(SIGNATURE_ROWS)} committed rows carry a shortfall at or below their "
        f"`dups`, fewer than the {MIN_SIGNATURE_ROWS} measured for #1500. Every "
        "assertion in this file is taken over that population, so if it has gone, "
        "this suite is asserting nothing — find out whether the artifacts were "
        "rescored (they must not be) or retention dropped the grids."
    )
    assert len(SHAPES) >= MIN_SIGNATURE_SHAPES, (
        f"{len(SHAPES)} distinct (truth, extracted, dups, prefix) shapes, fewer than "
        f"the {MIN_SIGNATURE_SHAPES} measured."
    )


def test_the_figures_are_identical_across_releases():
    """One shape recurring across releases is what says this is the instrument.

    A model-side completeness problem varies run to run. The dominant shape here is
    recorded byte-identically on dozens of runs spanning several releases, because
    the corpus and the perturbation are both deterministic — and it was read as a
    fresh product regression on four of those releases.
    """
    top, count = SHAPES.most_common(1)[0]
    grids = {r["grid"].split("/")[0] for r in SIGNATURE_ROWS if _shape(r) == top}
    assert count >= 20 and len(grids) >= 3, (
        f"the most common signature shape {top} appears on {count} runs across "
        f"{len(grids)} releases ({sorted(grids)}); #1500 measured a shape recurring "
        "on dozens of runs across several releases, which is the evidence that it is "
        "deterministic instrumentation rather than model behaviour."
    )


# --------------------------------------------------------------------------- #
# No row was lost — established without reading a SEQ tag at all
# --------------------------------------------------------------------------- #
def test_every_signature_row_returned_a_tag_for_every_truth_row():
    """``rows_extracted + dups`` is how many tags came back. It is never short."""
    short = [
        (
            r["grid"],
            r["doc"],
            r["cell"],
            r["rows_truth"],
            r["rows_extracted"] + r["dups"],
        )
        for r in SIGNATURE_ROWS
        if r["rows_extracted"] + r["dups"] < r["rows_truth"]
    ]
    assert short == [], (
        f"{len(short)} signature rows returned fewer tags than the truth has rows, so "
        "their shortfall is not fully explained by identifier collapse and must be "
        f"read individually. First few: {short[:5]}"
    )


def test_the_seq_free_row_count_agrees_that_nothing_was_lost():
    """``conf_rows_expected`` counts data rows and knows nothing about ``SEQ``.

    It is ``confidence_coverage``'s denominator —
    ``sum(len(v) for list-valued v in inference_result)`` — so it is the one figure in
    the artifact that answers "how many rows came back" without going through the
    identifier at all. On every signature row that records it, it reads the full truth
    count while ``rows_extracted`` reads short.
    """
    withcov = [r for r in SIGNATURE_ROWS if r.get("conf_rows_expected")]
    assert len(withcov) >= 70, (
        f"only {len(withcov)} signature rows carry conf_rows_expected; the SEQ-free "
        "corroboration for #1500 rests on that column and 75 rows carried it when "
        "this was written."
    )
    disagree = [
        (r["grid"], r["doc"], r["cell"], r["rows_truth"], r["conf_rows_expected"])
        for r in withcov
        if r["conf_rows_expected"] != r["rows_truth"]
    ]
    assert disagree == [], (
        f"{len(disagree)} signature rows report a data-row count that differs from "
        "the truth row count, so on those runs rows really were missing and the "
        f"collapse reading does not hold. First few: {disagree[:5]}"
    )


# --------------------------------------------------------------------------- #
# The mechanism, reproduced from each committed shape
# --------------------------------------------------------------------------- #
def test_a_contiguous_perturbation_reproduces_the_committed_shapes():
    rows = sum(SHAPES[s] for s in REPRODUCED)
    assert len(REPRODUCED) >= MIN_SHAPES_REPRODUCED and rows >= MIN_ROWS_REPRODUCED, (
        f"a duplicated leading zero over a contiguous id block reproduces "
        f"{len(REPRODUCED)} of {len(SHAPES)} committed shapes ({rows} of "
        f"{len(SIGNATURE_ROWS)} rows), below the {MIN_SHAPES_REPRODUCED} shapes / "
        f"{MIN_ROWS_REPRODUCED} rows measured for #1500. Either the corpus changed "
        "how it renders ids, or SEQ_STRICT is no longer the five-digit pattern the "
        "committed grids were scored with."
    )


@pytest.mark.parametrize("shape", sorted(REPRODUCED), ids=str)
def test_the_five_digit_pattern_reproduces_the_artifact(shape):
    """The historical expression, on a blob holding every row, gives the published
    numbers — which is what says the published numbers were about the pattern."""
    truth, got, dups, prefix = shape
    block = REPRODUCED[shape]
    assert len(_tags(truth, block)) == truth, "every row is present in the blob"
    assert _score_strictly(truth, block) == (got, dups, prefix)


@pytest.mark.parametrize("shape", sorted(REPRODUCED), ids=str)
def test_the_canonicalised_scorer_recovers_every_row(shape, monkeypatch):
    """The test that would have caught #1500.

    Same blob, same perturbation, current scorer: full recall, no duplicate rows, and
    the perturbation itself recorded as ``rows_id_repaired`` — exactly the count that
    used to be subtracted from the row total.
    """
    truth, got, dups, _prefix = shape
    scored = _score_through_the_harness(truth, REPRODUCED[shape], monkeypatch)
    assert scored["rows_truth"] == truth
    assert scored["rows_tagged"] == truth
    assert scored["rows_extracted"] == truth, (
        f"a run returning all {truth} rows is reported as recovering "
        f"{scored['rows_extracted']}; the committed artifact for this shape said "
        f"{got}, and that is #1500 back."
    )
    assert scored["completeness_recall"] == 1.0
    assert scored["dups"] == 0
    assert scored["n_gaps"] == 0
    assert scored["rows_id_repaired"] == dups
    assert scored["rows_id_unresolved"] == 0
    assert scored["unverified_row_shortfall"] is None


@pytest.mark.parametrize("shape", sorted(REPRODUCED), ids=str)
def test_a_perturbed_row_is_joined_to_its_own_cell_truth(shape, monkeypatch):
    """``cell_accuracy``'s join is the direction where a wrong id is worse than none.

    The row count merely under-reports. ``score_cells`` keys by the same identifier,
    so a tag read as another row's id pairs this row's values with that row's truth
    and reports value errors that are artefacts of the join — and ``cells_compared``
    hides it, because the denominator shrinks with the join.
    """
    truth = shape[0]
    scored = _score_through_the_harness(truth, REPRODUCED[shape], monkeypatch)
    assert scored["cells_compared"] == truth
    assert scored["cell_accuracy"] == 1.0, (
        "every row carries its own correct Amount, so any miss here is a row joined "
        "to another row's truth"
    )


def test_the_cell_join_canonicalises_against_the_document_not_the_instrument():
    """A partial ``rows_typed`` must not make a wrong candidate unique (#1500).

    ``rows_typed`` declares the rows carrying cell truth; ``seq_ids`` declares every
    row in the document. They coincide today and have diverged before. What a
    divergence causes is not a missed repair but a CONFIDENT wrong one, and the
    mechanism is worth stating exactly: ``SEQ000042`` reads as 42 outright, and the
    plain integer reading is accepted whenever it names a row of the join set. Narrow
    that set to row 4 and 42 is no longer in it, so the reading falls through to the
    one-edit candidates ``{42, 2, 4}``, of which exactly one — 4 — survives the
    intersection. The repair then succeeds and pairs row 42's values with row 4's
    truth.

    So the join target is the document's set. Below, row 4 is the only row declaring
    cell truth (``9.00``) while the document holds rows 4 and 42, and row 42 came
    back perturbed carrying ``42.00``. Canonicalised against the document it is row
    42 and is left out of the comparison for want of truth; canonicalised against
    ``rows_typed`` it would be row 4 and score as a value error.
    """
    rows_typed = {"SEQ00004": {"Amount": "9.00"}}
    seq_ids = [f"SEQ{s:05d}" for s in (4, 42)]
    sections = [
        {
            "inference_result": {
                "Transactions": [{"Description": "SEQ000042 Any", "Amount": "42.00"}]
            },
            "explainability_info": [
                {"Transactions": [{"Amount": {"confidence": 0.9}}]}
            ],
        }
    ]
    # Against the instrument's own keys the one-edit repair is unique and wrong.
    assert lib.canonical_seq("000042", {4}) == 4
    # Against the document's set the integer reading names a real row, so no repair
    # is attempted at all.
    assert lib.canonical_seq("000042", {4, 42}) == 42

    hits, total, matched = analyze.score_cells(
        sections, rows_typed, "Transactions", seq_ids
    )
    # `matched` is `len(by_seq)`, i.e. one row recovered either way; `total` is the
    # discriminator — 0 cells compared, because row 42 declares no cell truth.
    assert (hits, total, matched) == (0, 0, 1), (
        "the perturbed row 42 was joined to row 4's cell truth, which reports a "
        "value error that is an artefact of the join"
    )
    # And the per-cell confidence join, which pairs on the same identifier — the
    # observation would be (0.9, incorrect) against another row's expected value.
    assert (
        analyze.confidence_observations(sections, rows_typed, "Transactions", seq_ids)
        == []
    )
    # The narrow join target is what produces the artefact, so the test is about a
    # difference rather than about an absence.
    assert analyze.score_cells(sections, rows_typed, "Transactions") == (0, 1, 1), (
        "joining against `rows_typed` alone no longer mispairs, so this test no "
        "longer distinguishes the two join targets"
    )


# --------------------------------------------------------------------------- #
# The fix must not launder real row loss
# --------------------------------------------------------------------------- #
def test_a_genuine_truncation_is_still_reported_as_row_loss(monkeypatch):
    kept = 60
    rows = [
        {"Description": f"SEQ{s:05d} AnyCompany Store", "Amount": float(s)}
        for s in range(kept)
    ]
    sections = [
        {"inference_result": {"Transactions": rows}, "explainability_info": [{}]}
    ]
    monkeypatch.setattr(lib, "read_sections", lambda *a, **k: lib.SectionRead(sections))
    scored = analyze.score_synthetic(
        "b",
        "p/",
        {"seq_ids": [f"SEQ{s:05d}" for s in range(100)], "list_key": "Transactions"},
    )
    assert scored["rows_extracted"] == kept
    assert scored["rows_tagged"] == kept
    assert scored["completeness_recall"] == 0.6
    assert scored["truncation_prefix"] == kept
    assert scored["dups"] == 0
    assert scored["rows_id_repaired"] == 0
    assert scored["unverified_row_shortfall"] is None


def test_a_clean_run_records_no_identifier_repair(monkeypatch):
    scored = _score_through_the_harness(100, set(), monkeypatch)
    assert scored["completeness_recall"] == 1.0
    assert scored["rows_tagged"] == 100
    assert scored["rows_id_repaired"] == 0
    assert scored["rows_id_unresolved"] == 0
    assert scored["unverified_row_shortfall"] is None


def test_an_ambiguous_identifier_is_counted_not_guessed(monkeypatch):
    """A digit inserted mid-string can be undone several ways. None is chosen.

    ``SEQ00042`` returned as ``SEQ000492`` could be 42, 492, 49 or 2 with one deletion
    undone. Attributing it would pair the row with another row's cell truth, so it is
    reported as an unresolved identifier and the shortfall is flagged as unverified
    rather than published as row loss.
    """
    rows = []
    for seq in range(100):
        tag = f"SEQ{seq:05d}"
        if 50 <= seq < 60:
            tag = tag[:6] + "9" + tag[6:]
        rows.append({"Description": f"{tag} AnyCompany Store", "Amount": float(seq)})
    sections = [
        {"inference_result": {"Transactions": rows}, "explainability_info": [{}]}
    ]
    monkeypatch.setattr(lib, "read_sections", lambda *a, **k: lib.SectionRead(sections))
    scored = analyze.score_synthetic(
        "b",
        "p/",
        {"seq_ids": [f"SEQ{s:05d}" for s in range(100)], "list_key": "Transactions"},
    )
    assert scored["rows_tagged"] == 100
    assert scored["rows_id_unresolved"] == 10
    assert scored["rows_extracted"] == 90
    assert scored["unverified_row_shortfall"] == "unresolved_row_ids"


# --------------------------------------------------------------------------- #
# The backstop: a shortfall `dups` pays for is not row loss
# --------------------------------------------------------------------------- #
def test_the_guard_names_the_shape_truncation_cannot_produce():
    """A WIRING check over the committed numbers, and it is worth being clear that
    that is all the first assertion is.

    ``_signature_rows`` selects on ``0 < shortfall <= dups``, which is the condition
    the guard's first branch tests, so running the guard over that selection can only
    confirm that the shipped function implements the predicate the selection names —
    on real recorded inputs rather than invented ones, which is the part worth
    having, but not evidence about the population.

    The second assertion is the evidence. It uses a field the selection does not
    read: on every one of these rows ``completeness_recall`` is exactly
    ``rows_extracted / rows_truth`` and strictly below 1.0, so the recorded recall on
    all of them is the understated figure #1500 is about, and the guard's verdict and
    the published metric are about the same runs.
    """
    silent = [
        (
            r["grid"],
            r["doc"],
            r["cell"],
            r["rows_truth"],
            r["rows_extracted"],
            r["dups"],
        )
        for r in SIGNATURE_ROWS
        if lib.unverified_row_shortfall(r["rows_truth"], r["rows_extracted"], r["dups"])
        != "dups_at_or_above_shortfall"
    ]
    assert silent == [], (
        f"the guard is silent on {len(silent)} of {len(SIGNATURE_ROWS)} committed "
        "rows whose shortfall its own `dups` pays for, which is the combination "
        f"truncation cannot produce. First few: {silent[:5]}"
    )

    unflagged = [
        (r["grid"], r["doc"], r["cell"], r["completeness_recall"])
        for r in SIGNATURE_ROWS
        if r.get("completeness_recall")
        != round(r["rows_extracted"] / r["rows_truth"], 4)
        or r["completeness_recall"] >= 1.0
    ]
    assert unflagged == [], (
        f"{len(unflagged)} of {len(SIGNATURE_ROWS)} rows the guard speaks about do "
        "not carry an understated `completeness_recall`, so the guard's verdict and "
        "the metric a release A/B publishes are no longer about the same runs — "
        f"which is what makes the annotation worth printing. First few: "
        f"{unflagged[:5]}"
    )


def test_the_guard_is_silent_on_truncation_and_on_a_complete_run():
    """Both directions, from the shape of the committed truncations.

    Taken from the real ones: a truncated run records ``dups: 0`` and a
    ``truncation_prefix`` near its kept count. The guard must say nothing about those,
    or it is noise and will be ignored — which is the state `dups` itself was in.
    """
    truncations = [
        r
        for path in sorted(RESULTS.glob("*/*/summary.json"))
        for r in json.loads(path.read_text()).get("rows", [])
        if r.get("rows_truth")
        and r.get("rows_extracted") is not None
        and r["rows_truth"] - r["rows_extracted"] > 0
        and not r.get("dups")
    ]
    assert len(truncations) >= 100, (
        f"{len(truncations)} committed rows record a shortfall with no dups; this "
        "check compares the guard against real truncations and 523 carried that "
        "shape when it was written."
    )
    fired = [
        (r.get("doc"), r.get("cell"), r["rows_truth"], r["rows_extracted"])
        for r in truncations
        if lib.unverified_row_shortfall(r["rows_truth"], r["rows_extracted"], r["dups"])
    ]
    assert fired == [], (
        f"the guard fires on {len(fired)} genuine truncations, where the shortfall IS "
        f"row loss. First few: {fired[:5]}"
    )
    assert lib.unverified_row_shortfall(400, 400, 0) is None
    assert lib.unverified_row_shortfall(0, 0, 0) is None
    assert lib.unverified_row_shortfall(None, None, None) is None


def test_each_excuse_is_bounded_by_the_shortfall_it_can_account_for():
    """One unresolved tag excuses one row, not an arbitrary shortfall.

    An unresolved tag contributes nothing to ``rows_extracted``, so it can account
    for exactly one unit of shortfall. Unbounded, a single hallucinated identifier
    would annotate a 40-row completeness regression as not-row-loss — the reading
    this guard exists to prevent, in the mirror direction. The two counts are summed
    because each pays for one unit, and the label says which one carries it.
    """
    # 40 rows short, one unattributable tag: 39 units are unexplained.
    assert lib.unverified_row_shortfall(400, 360, 0, 1) is None
    # Exactly accounted for, by each count alone and by the two together.
    assert lib.unverified_row_shortfall(400, 360, 0, 40) == "unresolved_row_ids"
    assert lib.unverified_row_shortfall(400, 360, 40, 0) == "dups_at_or_above_shortfall"
    assert lib.unverified_row_shortfall(400, 360, 25, 15) == "unresolved_row_ids"
    # One unit short of covering it.
    assert lib.unverified_row_shortfall(400, 360, 25, 14) is None
    # `dups` alone covering it keeps the `dups` label even with unresolved tags too.
    assert lib.unverified_row_shortfall(400, 360, 40, 5) == "dups_at_or_above_shortfall"


# --------------------------------------------------------------------------- #
# The canonicaliser itself
# --------------------------------------------------------------------------- #
class TestCanonicalSeq:
    TRUTH = frozenset(range(400))

    @pytest.mark.parametrize("seq", [0, 1, 42, 99, 100, 399])
    def test_a_well_formed_tag_reads_as_itself(self, seq):
        assert lib.canonical_seq(f"{seq:05d}", self.TRUTH) == seq

    @pytest.mark.parametrize("seq", [0, 1, 42, 99, 100, 399])
    def test_a_duplicated_leading_zero_reads_as_the_row_it_came_from(self, seq):
        assert lib.canonical_seq(f"0{seq:05d}", self.TRUTH) == seq

    def test_the_five_digit_pattern_is_what_produced_the_wrong_answer(self):
        """The defect, stated as a comparison rather than as prose."""
        assert int(STRICT_FIVE_DIGIT.match("SEQ000042").group(1)) == 4
        assert lib.canonical_seq("000042", self.TRUTH) == 42

    def test_an_id_outside_the_truth_set_is_not_attributed(self):
        assert lib.canonical_seq("00912", frozenset(range(100))) is None

    def test_an_ambiguous_single_deletion_is_not_attributed(self):
        # "000492" -> 492 is outside; deleting one digit gives 42, 492, 49 and 2,
        # three of which are truth rows. Nothing is chosen.
        assert lib.canonical_seq("000492", self.TRUTH) is None

    def test_a_narrow_run_is_not_read_as_the_row_its_integer_names(self):
        """The direction that would have traded this defect for a quieter one.

        A tag that came back a digit SHORT reads as a plausible other row of the same
        corpus — `SEQ0042` is `int("0042") == 42` leaving the regex and row 4 entering
        it — so the plain integer reading is refused below `SEQ_WIDTH` and the run is
        reconciled like any other perturbation. Here it is ambiguous, so it is
        counted rather than attributed.
        """
        assert lib.canonical_seq("0042", self.TRUTH) is None

    def test_a_narrow_run_is_repaired_when_only_one_reading_is_a_truth_row(self):
        assert lib.canonical_seq("0042", frozenset({42})) == 42

    def test_a_substituted_digit_is_not_guessed(self):
        """A run of the right width naming no truth row had a digit CHANGED, and a
        substitution has ten readings per position with nothing to choose between."""
        assert lib.canonical_seq("00942", frozenset(range(100))) is None

    def test_an_unambiguous_single_deletion_is_repaired(self):
        # Only row 300 is declared, so "003004" has exactly one reading.
        assert lib.canonical_seq("003004", frozenset({300})) == 300

    def test_with_no_truth_to_join_against_the_integer_reading_is_returned(self):
        assert lib.canonical_seq("00042", None) == 42
        assert lib.canonical_seq("000042", frozenset()) == 42


# --------------------------------------------------------------------------- #
# The aggregate is where the wrong conclusion was drawn, so it has to carry it
# --------------------------------------------------------------------------- #
class TestTheAggregateCarriesTheFinding:
    """A correct row metric nobody reads is the defect one layer up.

    The published claim was a *cell* mean — "recall 1.000 -> 0.958, the only cell to
    move" — taken over a cell in which one run of nine had an unreadable row
    identity. So the count has to reach `cell_stats` and the `--compare` output, not
    only the rows.
    """

    @staticmethod
    def _row(**kw):
        row = {
            "cell": "core-llm-simple-sep",
            "doc": "small_narrow.pdf",
            "repeat": 0,
            "success": True,
            "status": "COMPLETED",
            "cost": 1.0,
            "completeness_recall": 1.0,
            "scalar_accuracy": 1.0,
            "weighted_accuracy": 1.0,
        }
        row.update(kw)
        return row

    def test_cell_stats_counts_the_unverified_runs(self):
        aggregate = harness_module("aggregate")
        rows = [
            self._row(repeat=0),
            self._row(
                repeat=1,
                completeness_recall=0.62,
                unverified_row_shortfall="dups_at_or_above_shortfall",
                rows_id_repaired=38,
            ),
        ]
        stats = aggregate.cell_stats(rows)["core-llm-simple-sep"]
        assert stats["n_unverified_row_shortfall"] == 1
        assert stats["n_rows_id_repaired"] == 1
        assert stats["n_success"] == 2

    def test_a_recall_move_over_an_unverified_run_says_so(self, tmp_path):
        aggregate = harness_module("aggregate")
        base = [self._row(repeat=i) for i in range(3)]
        cur = [
            self._row(
                repeat=i,
                completeness_recall=0.62,
                unverified_row_shortfall="dups_at_or_above_shortfall",
                rows_id_repaired=38,
            )
            for i in range(3)
        ]
        (tmp_path / "cur.json").write_text(json.dumps({"rows": cur, "cell_stats": {}}))
        (tmp_path / "base.json").write_text(
            json.dumps({"rows": base, "cell_stats": {}})
        )
        reg, _imp, _weak = aggregate.compare_cells(
            str(tmp_path / "cur.json"), str(tmp_path / "base.json")
        )
        hits = [t for _cell, t in reg if "recall" in t]
        assert hits, "a uniform -0.38 recall move must still be reported"
        assert "NOT ROW LOSS" in hits[0], (
            "the recall finding does not say that every run behind it has an "
            f"unreadable row identity, which is #1500's whole subject: {hits[0]}"
        )

    def test_the_baseline_side_is_read_from_the_rows_not_the_stored_block(
        self, tmp_path
    ):
        """The shape every committed artifact in this repository actually has (#1500).

        `_cells` prefers a STORED `cell_stats` block, and all 120 committed blocks
        were written before `n_unverified_row_shortfall` existed. A comparison that
        asked the block would therefore answer 0 on the baseline side of every
        release A/B available here — the side that carries the defect — and the next
        release's recall move would be published as a plain improvement with nothing
        saying the older figure was an instrument artifact.

        So the baseline below is built the way a pre-fix grid was: its rows carry the
        shape in `rows_truth`/`rows_extracted`/`dups` and nothing else, and its stored
        block is a real `cell_stats` with the two keys this change added stripped out.
        The current grid is the post-fix one at recall 1.000, so the move is an
        improvement — and it must still carry the annotation.
        """
        aggregate = harness_module("aggregate")
        base_rows = [
            self._row(
                repeat=i,
                completeness_recall=0.62,
                rows_truth=100,
                rows_extracted=62,
                dups=38,
            )
            for i in range(3)
        ]
        cur_rows = [
            self._row(repeat=i, rows_truth=100, rows_extracted=100, dups=0)
            for i in range(3)
        ]
        stale = aggregate.cell_stats(base_rows)
        for cell in stale.values():
            cell.pop("n_unverified_row_shortfall", None)
            cell.pop("n_rows_id_repaired", None)
        assert "n_unverified_row_shortfall" not in next(iter(stale.values())), (
            "this fixture has to reproduce a block written before the counter "
            "existed, or it is not the committed shape"
        )
        (tmp_path / "cur.json").write_text(
            json.dumps({"rows": cur_rows, "cell_stats": aggregate.cell_stats(cur_rows)})
        )
        (tmp_path / "base.json").write_text(
            json.dumps({"rows": base_rows, "cell_stats": stale})
        )
        _reg, imp, _weak = aggregate.compare_cells(
            str(tmp_path / "cur.json"), str(tmp_path / "base.json")
        )
        hits = [t for _cell, t in imp if "recall" in t]
        assert hits, "a uniform +0.38 recall move must still be reported"
        assert "NOT ROW LOSS on baseline 3/3" in hits[0], (
            "the baseline's unreadable row identity is invisible because the stored "
            "`cell_stats` block was consulted instead of the rows, which is the "
            f"state of every committed artifact: {hits[0]}"
        )


class TestReadSeqIds:
    def test_it_counts_tags_repairs_and_unresolved_separately(self):
        truth = frozenset(range(5))
        blob = json.dumps(
            [
                "SEQ00000 a",  # clean
                "SEQ000001 b",  # perturbed -> 1, repaired
                "SEQ000002 c",  # perturbed -> 2, repaired
                "SEQ09999 d",  # names no truth row
            ]
        )
        reading = lib.read_seq_ids(blob, truth)
        assert reading.ids == (0, 1, 2)
        assert reading.tagged == 4
        assert reading.repaired == 2
        assert reading.unresolved == ("09999",)

    def test_a_repeated_row_still_counts_as_a_duplicate(self):
        reading = lib.read_seq_ids("SEQ00007 a SEQ00007 b", frozenset({7}))
        assert reading.ids == (7, 7)
        assert len(reading.ids) - len(set(reading.ids)) == 1
        assert reading.repaired == 0
