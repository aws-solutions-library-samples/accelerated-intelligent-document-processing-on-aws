# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The coverage-shortfall rungs, measured against the committed grids (#997).

What this is for
----------------
``assessment_coverage_incomplete`` fires at 5% of extracted list rows unscored for a
warning and at 25% plus 10 absolute unscored rows for an error. Those numbers were
argued from the reconciliation code path — a run whose model scored every row should
land at 0% shortfall — and for a while nothing recorded coverage on real output, so
the rate at which the warning rung fires on a *healthy* document was unknown. It is
now recorded: ``conf_rows_expected`` / ``conf_rows_scored`` are carried by every
committed ``benchmarks/results/*/*/summary.json`` row from v0.6.8 onward, produced by
``idp_common.assessment.batching.confidence_coverage`` — the function the shipping
guard calls.

This suite re-derives the distribution from those artifacts on every test run, so the
decision to leave the fractions where they are rests on a measurement a reader can
re-execute rather than on a paragraph. The numbers themselves are written up under
"Coverage: measured" in ``lib/idp_common_pkg/idp_common/assessment/README.md``.

Two properties of how it measures, both deliberate:

* **The rungs are not restated here.** Each distinct recorded ``(expected,
  unscored)`` pair is turned back into an assessment/extraction pair and run through
  the real :func:`audit_explainability`, so what is counted is the severity the
  product would emit. Re-implementing ``shortfall >= 0.05`` in the test would make it
  agree with the guard for reasons unrelated to the data, which is the exact failure
  #997 is about.
* **It is a ratchet, not a snapshot.** The assertions are one-sided with the measured
  figure quoted in the failure message, so adding a benchmark grid is expected to
  leave them green and a grid that moves the distribution materially is expected to
  fail. If one does, the right response is to re-measure and revisit the rungs — not
  to widen the bound.

What it cannot tell you
-----------------------
``processing_issues`` postdates every committed grid (it is null on all of them), so
these artifacts record the shortfall without the product's own verdict beside it.
``audit_explainability`` suppresses the coverage rung whenever the self-healing
ladder already reported an error for the same section, so the firing shares below are
an **upper bound** on how often the rung actually spoke. The columns added in #1378
answer that directly on the next grid.
"""

from __future__ import annotations

import json
import pathlib

import pytest

RESULTS = pathlib.Path(__file__).resolve().parents[1] / "results"

# Non-vacuity floors. The suite is only worth anything over a corpus, and a bug (or a
# retention change) that stopped the metric being recorded would otherwise turn every
# assertion below into a statement about an empty list. Measured at the time of
# writing: 38 grids, 6,672 coverage-bearing document-runs.
MIN_GRIDS = 30
MIN_RUNS = 6000

# The share of runs at exactly 0 unscored rows. Measured 97.32%. This is the premise
# the rungs were argued from, and it is the one number whose collapse would mean the
# 5% rung is firing on healthy documents after all.
MIN_ZERO_SHORTFALL_SHARE = 0.95

# The share of runs the warning rung fires on. Measured 0.66%. A rung that is noise
# shows up here.
MAX_WARNING_FIRING_SHARE = 0.02

# The largest number of unscored rows recorded on a run the warning rung said nothing
# about. Re-measured on the v0.6.12 grids: **115 of 3,200** (3.6%) on
# `scale_3200.pdf` / `astra-adv-wide`, up from 24 of 800 (3.0%).
#
# ⚠️ **The two measurements together are the finding, not the new number.** The row
# count grew 4.8x while the SHARE barely moved, because the document grew 4x — which
# is what a purely multiplicative rung does. The silent band is proportional to list
# length, so the absolute number of rows that can go unreported has no bound at all;
# it is 115 here only because 3,200 rows is the largest list in the corpus. That is
# the same shape #1361 fixed for `extraction_rows_below_ocr_estimate`, by replacing a
# multiplicative tolerance with a 29-row additive one, and the argument for giving
# this warning rung an absolute ceiling the way the error rung already carries an
# absolute floor. Tracked separately; this pin only stops it growing unnoticed again.
MAX_SILENT_UNSCORED_ROWS = 115


def _rows():
    """Every committed grid row carrying the coverage metric."""
    out = []
    for path in sorted(RESULTS.glob("*/*/summary.json")):
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
        for row in summary.get("rows") or []:
            if row.get("conf_rows_expected") is None:
                continue
            out.append((path.parent.parent.name, path.parent.name, row))
    return out


@pytest.fixture(scope="module")
def corpus():
    rows = _rows()
    assert len({(v, s) for v, s, _ in rows}) >= MIN_GRIDS, (
        f"only {len({(v, s) for v, s, _ in rows})} committed grids carry "
        f"conf_rows_expected; this suite measures a distribution and needs a corpus. "
        "If the metric stopped being recorded, that is the finding."
    )
    assert len(rows) >= MIN_RUNS, (
        f"only {len(rows)} coverage-bearing document-runs found (expected at least "
        f"{MIN_RUNS}); every share below would be computed over too little data to "
        "mean anything."
    )
    return [row for _v, _s, row in rows]


@pytest.fixture(scope="module")
def corpus_located():
    """The same corpus, but each row paired with the grid directory it came from.

    `corpus` drops the location because every share it computes is corpus-wide. A
    carve-out has to name the grid it applies to, or it would shield the same shape
    anywhere — so that one check reads this instead.
    """
    return [(f"{suite}", row) for _v, suite, row in _rows()]


def _defined(corpus):
    """Runs where coverage is defined — the section extracted at least one list row."""
    return [r for r in corpus if r["conf_rows_expected"]]


def _leaf(confidence):
    return {"confidence": confidence, "confidence_reason": "measured"}


def _severity_for(expected: int, unscored: int) -> str | None:
    """The severity the SHIPPING guard emits for this recorded pair, or None.

    Reconstructs the smallest input with that shape and runs the real
    ``audit_explainability`` over it, rather than re-stating the rungs. Called once
    per distinct pair (there are far fewer pairs than runs), which keeps a corpus-wide
    classification cheap.
    """
    from idp_common.assessment.batching import audit_explainability

    scored = expected - unscored
    data = {"rows": [{"amount": f"{i}.00"} for i in range(expected)]}
    assessment = {
        "rows": [
            {"amount": _leaf(0.9 if i < scored else None)} for i in range(expected)
        ]
    }
    _gaps, issues = audit_explainability(
        assessment, data, geometry_mode="off", section_id="measured"
    )
    found = [i for i in issues if i.code == "assessment_coverage_incomplete"]
    return found[0].severity if found else None


@pytest.fixture(scope="module")
def severities(corpus):
    """``(expected, unscored) -> severity|None`` for every shape in the corpus."""
    pairs = {
        (r["conf_rows_expected"], r["conf_rows_unscored"]) for r in _defined(corpus)
    }
    return {pair: _severity_for(*pair) for pair in pairs}


def _share(rows, predicate):
    return sum(1 for r in rows if predicate(r)) / len(rows)


def test_healthy_runs_sit_at_zero_shortfall(corpus):
    """The premise the rungs were reasoned from, measured.

    Reconciliation pads exactly one assessment entry per extracted row, so a run
    whose model scored every row should land at 0% shortfall. It does, on the large
    majority of real runs — and because a document-level 0 implies 0 in every one of
    the document's sections, this share is exact rather than an average that could be
    hiding a bad section.
    """
    defined = _defined(corpus)
    share = _share(defined, lambda r: r["conf_rows_unscored"] == 0)
    assert share >= MIN_ZERO_SHORTFALL_SHARE, (
        f"only {share:.2%} of {len(defined)} coverage-bearing runs are at exactly 0 "
        f"unscored rows (measured 97.32% when this was written). The 5%/25% rungs "
        "rest on healthy runs sitting at 0; if that has stopped being true the rungs "
        "need revisiting, not this bound."
    )


def test_the_warning_rung_is_not_firing_at_a_rate_that_makes_it_noise(
    corpus, severities
):
    """A rung that fires on ordinary documents is noise regardless of its rationale.

    Counted through the shipping guard: every distinct recorded shape is classified by
    ``audit_explainability`` itself. The result is an upper bound — the guard
    suppresses this rung when the ladder already reported an error, and the committed
    grids do not record which runs that happened on.
    """
    defined = _defined(corpus)
    fired = [
        r
        for r in defined
        if severities[(r["conf_rows_expected"], r["conf_rows_unscored"])] is not None
    ]
    share = len(fired) / len(defined)
    assert share <= MAX_WARNING_FIRING_SHARE, (
        f"the coverage rung would fire on {share:.2%} of {len(defined)} runs "
        f"(measured 0.66%). Past a couple of percent it is a document-level alarm on "
        "the healthy path, which is the condition #997 asked about."
    )


def test_no_healthy_short_list_produces_a_partial_shortfall(corpus):
    """The specific concern #997 raised, and the measured answer to it.

    A single unscored row is a shortfall of ``1/N``, so on a section of 20 rows or
    fewer one unscored row crosses the 5% warning rung with no slack at all. Whether
    real runs produce that single unscored row is the whole question, and over the
    committed grids they do not: among single-section runs extracting 20 rows or
    fewer, the number with *some* rows scored and at least one not is zero.

    Restricted to single-section runs on purpose. The harness sums coverage over a
    document's sections while the guard runs per section, so on a multi-section
    document a short unscored section can be diluted by a long scored one — the two
    figures coincide exactly when there is one section.

    A future grid that produces one of these should fail here. That is the point: it
    would be the first observation of the warning rung firing on a partial short-list
    gap, and the rung's 5% would then be a live question rather than a settled one.
    """
    short = [
        r
        for r in _defined(corpus)
        if (r.get("validation_sections") or 1) == 1 and r["conf_rows_expected"] <= 20
    ]
    assert len(short) >= 1000, (
        f"only {len(short)} single-section runs extract 20 rows or fewer; the "
        "short-list claim needs a population to be about."
    )
    partial = [r for r in short if 0 < r["conf_rows_scored"] < r["conf_rows_expected"]]
    assert partial == [], (
        f"{len(partial)} of {len(short)} short-list runs show a PARTIAL coverage "
        "shortfall, where the measurement behind the shipped 5% warning fraction "
        "found none. Re-read 'Coverage: measured' in the assessment README: the "
        "rung's false-positive rate on short lists is no longer zero and the "
        f"fraction is back in question. First few: "
        f"{[(r['doc'], r['cell'], r['conf_rows_scored'], r['conf_rows_expected']) for r in partial[:5]]}"
    )


def test_every_sub_floor_error_shortfall_is_a_total_one(corpus, severities):
    """Why ``scored_rows == 0`` is an error even below the absolute row floor.

    The floor exists so that one unscored row in a four-row list is not rendered as a
    red "Incomplete" section. Measured, nothing in that class is partial: every run
    that crosses the 25% error fraction with fewer than 10 unscored rows has *no*
    scored row at all — a section with no confidence surface anywhere, which is the
    strongest form of the condition the error rung reports. So the floor as originally
    shipped only ever downgraded a total absence of coverage, and the guard now treats
    zero scored rows as an error at any list length.

    If a future grid records a partial shortfall in this class, the floor is doing the
    job it was written for and the exception needs re-examining against that run.
    """
    from idp_common.assessment.batching import (
        _COVERAGE_SHORTFALL_ERROR_FRACTION as ERROR_FRACTION,
    )
    from idp_common.assessment.batching import (
        _COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS as FLOOR,
    )

    sub_floor = [
        r
        for r in _defined(corpus)
        if r["conf_rows_unscored"] / r["conf_rows_expected"] >= ERROR_FRACTION
        and r["conf_rows_unscored"] < FLOOR
    ]
    assert sub_floor, (
        "no committed run crosses the error fraction below the absolute row floor, "
        "so this assertion currently shields nothing and the measurement behind the "
        "zero-scored-rows exception cannot be reproduced from these artifacts."
    )
    partial = [r for r in sub_floor if r["conf_rows_scored"] > 0]
    assert partial == [], (
        f"{len(partial)} of {len(sub_floor)} sub-floor error-fraction runs have some "
        "rows scored. The zero-scored-rows exception rests on every one of them being "
        "a TOTAL coverage failure; a partial one here means the floor is protecting "
        f"the case it was written for. First few: "
        f"{[(r['doc'], r['cell'], r['conf_rows_scored'], r['conf_rows_expected']) for r in partial[:5]]}"
    )
    # And the shipped guard calls every one of them an error, which is the behaviour
    # the measurement above is the evidence for.
    for r in sub_floor:
        pair = (r["conf_rows_expected"], r["conf_rows_unscored"])
        assert severities[pair] == "error", (pair, severities[pair])


def test_the_silent_band_residual_is_pinned(corpus, severities):
    """What the purely multiplicative warning fraction lets through, in absolute rows.

    The 5% rung scales its slack with the list, while one half of its justification
    ("a 1-2 row gap on an 800-row table is ``assessment_incomplete``'s business") names
    an absolute quantity. So the number of unscored rows that emits no coverage issue
    grows with the section: 0 at 20 rows, 19 at 400, 39 at 800. This pins the largest
    one actually observed, so the residual is a recorded figure rather than an
    arithmetic possibility, and so growth in it is visible.
    """
    silent = [
        r
        for r in _defined(corpus)
        if r["conf_rows_unscored"]
        and severities[(r["conf_rows_expected"], r["conf_rows_unscored"])] is None
    ]
    assert silent, (
        "no committed run sits between 0 and the warning fraction, so there is no "
        "observed residual to pin and this assertion shields nothing."
    )
    worst = max(silent, key=lambda r: r["conf_rows_unscored"])
    assert worst["conf_rows_unscored"] <= MAX_SILENT_UNSCORED_ROWS, (
        f"{worst['conf_rows_unscored']} unscored rows of "
        f"{worst['conf_rows_expected']} ({worst['doc']}, {worst['cell']}) emit no "
        f"coverage issue, above the {MAX_SILENT_UNSCORED_ROWS} rows recorded when "
        "this was measured. The residual the multiplicative rung leaves has grown: "
        "re-measure, update 'Coverage: measured' in the assessment README, and weigh "
        "an absolute ceiling on the warning rung against the firing rate it would add."
    )


def test_no_coverage_outlier_is_the_out_of_range_confidence_artefact(corpus):
    """#1395's artefact must not be mistaken for a coverage finding.

    Four committed grids hold rows whose ``mean_confidence`` exceeds 1.0, caused by a
    grader leaf answering on a 0-100 scale (#1395, #1411). Those rows are real runs
    with a broken score, so before any shortfall outlier is read as data it is worth
    knowing none of them is one of these: every out-of-range row in a coverage-bearing
    grid sits at exactly 0 unscored rows.
    """
    out_of_range = [
        r
        for r in corpus
        if isinstance(r.get("mean_confidence"), (int, float))
        and r["mean_confidence"] > 1.0
    ]
    assert out_of_range, (
        "no out-of-range mean_confidence row is present in a coverage-bearing grid, "
        "so this check shields nothing — if those rows were corrected or dropped, "
        "delete it."
    )
    contaminated = [r for r in out_of_range if r["conf_rows_unscored"]]
    assert contaminated == [], (
        f"{len(contaminated)} of {len(out_of_range)} out-of-range-confidence rows "
        "also carry a coverage shortfall, so the two populations now overlap and a "
        "shortfall outlier can no longer be read without establishing which it is."
    )


def test_an_undefined_coverage_run_extracted_nothing(corpus_located):
    """``expected_rows == 0`` is an extraction finding, not a coverage one.

    The guard declines to fire when the section extracted no list row, which would be
    a hole if those runs were producing rows the measurement could not see. They are
    not: every such run with a synthetic row count recorded shows
    ``rows_extracted == 0``, i.e. the model returned no list at all.
    """
    undefined = [(g, r) for g, r in corpus_located if not r["conf_rows_expected"]]
    assert undefined, "no run records undefined coverage; this check shields nothing."
    with_rows = [
        (g, r)
        for g, r in undefined
        if isinstance(r.get("rows_extracted"), (int, float)) and r["rows_extracted"]
    ]

    # ⚠️ The premise above no longer holds universally, and the exception is a KNOWN,
    # FILED defect rather than a reason to relax the check: issue #1494. Four
    # `coresynth__extraction-model-sonnet55` runs extracted 2 rows of 400 on
    # `med_narrow.pdf` and the coverage walk recorded no denominator for any of them,
    # so the coverage rung could not speak about the 398 rows that were lost. They are
    # carved out BY NAME and COUNT-PINNED rather than filtered by shape, so a fifth
    # instance — or the same shape on another model or document — fails here.
    KNOWN_UNDEFINED_WITH_ROWS = {
        ("coresynth__extraction-model-sonnet55", "med_narrow.pdf"),
    }
    EXPECTED_KNOWN = 4

    known = [(g, r) for g, r in with_rows if (g, r["doc"]) in KNOWN_UNDEFINED_WITH_ROWS]
    unknown = [pair for pair in with_rows if pair not in known]

    assert unknown == [], (
        f"{len(unknown)} runs record extracted rows while reporting no coverage "
        "denominator, so the coverage walk is missing rows the extraction produced — "
        "a list nested inside a group attribute is the shape that does this. First "
        f"few: {[(g, r['doc'], r['cell'], r['rows_extracted']) for g, r in unknown[:5]]}"
    )
    assert len(known) == EXPECTED_KNOWN, (
        f"the #1494 carve-out now shields {len(known)} runs, not {EXPECTED_KNOWN}. "
        "If the count grew, a new instance of the coverage-walk hole has appeared and "
        "belongs on that issue; if it shrank to 0 the defect is fixed and this "
        "carve-out should be deleted along with it."
    )
