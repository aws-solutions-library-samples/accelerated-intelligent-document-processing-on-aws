# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The coverage-shortfall rungs, measured against the committed grids (#997).

What this is for
----------------
``assessment_coverage_incomplete`` fires at 5% of extracted list rows unscored **or**
10 unscored rows in absolute terms for a warning, and at 25% plus 10 absolute unscored
rows for an error. Those numbers were
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

# The share of runs the rung fires on, at either severity. Measured 0.70% over the
# grids committed here and 0.64% over those plus the v0.6.12 grids of PR #1487, both
# with the absolute ceiling of #1495 live. A rung that is noise shows up here.
MAX_WARNING_FIRING_SHARE = 0.02

# The largest number of unscored rows recorded on a run the warning rung says nothing
# about: 6 of 400 (1.5%). Re-measured for #1495, when the warning rung gained the
# absolute ceiling the error rung's absolute floor always had. The figure it replaces
# was 24 of 800 (3.0%) over the grids up to v0.6.11 and 115 of 3,200 (3.6%) once a
# 3,200-row document was benchmarked — the count grew 4.8x on a 4x larger document at
# a near-identical share, which is what a purely multiplicative rung does and why the
# ceiling exists.
#
# This figure is now a measurement UNDER a structural bound rather than the only thing
# standing between the band and the longest list anyone benchmarks: the ceiling caps it
# at ``_COVERAGE_SHORTFALL_WARNING_MIN_UNSCORED_ROWS - 1`` whatever the document's
# size, which ``test_the_silent_band_residual_is_pinned`` asserts separately from this
# pin. A grid that lands between 7 and 9 unscored rows therefore fails this pin while
# the structural bound still holds; re-pin it in that case. A grid at or above the
# ceiling means the ceiling is not being enforced, which is a defect in the guard.
MAX_SILENT_UNSCORED_ROWS = 6


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


def _severity_without_the_ceiling(expected: int, unscored: int) -> str | None:
    """The same, with the warning rung's absolute ceiling lifted.

    "Reported by the ceiling alone" is a statement about the guard, so it is read off
    the guard with that one instrument disabled rather than by restating the
    fraction's comparand here — which would make the subset agree with the guard for
    a reason unrelated to the guard, and stop meaning what it says the moment either
    comparand moved. ``audit_explainability`` reads the constant from its module at
    call time, so raising it out of reach is enough; it is restored unconditionally.
    """
    from idp_common.assessment import batching

    name = "_COVERAGE_SHORTFALL_WARNING_MIN_UNSCORED_ROWS"
    saved = getattr(batching, name)
    setattr(batching, name, 10**9)
    try:
        return _severity_for(expected, unscored)
    finally:
        setattr(batching, name, saved)


def _pairs(corpus):
    return {
        (r["conf_rows_expected"], r["conf_rows_unscored"]) for r in _defined(corpus)
    }


@pytest.fixture(scope="module")
def severities(corpus):
    """``(expected, unscored) -> severity|None`` for every shape in the corpus."""
    return {pair: _severity_for(*pair) for pair in _pairs(corpus)}


@pytest.fixture(scope="module")
def severities_without_the_ceiling(corpus):
    """The same mapping as the fraction alone would produce (#1495)."""
    return {pair: _severity_without_the_ceiling(*pair) for pair in _pairs(corpus)}


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
        f"(measured 0.70%). Past a couple of percent it is a document-level alarm on "
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
    """What the warning rung lets through, in absolute rows — bound, then measurement.

    The 5% fraction scales its slack with the list, while one half of its
    justification ("a 1-2 row gap on an 800-row table is ``assessment_incomplete``'s
    business") names an absolute quantity. A fraction alone therefore leaves a silent
    band that grows with the section — 19 rows at 400, 39 at 800, 159 at 3,200 — and
    observing it grow is exactly what happened: 24 unscored rows of 800 over the grids
    up to v0.6.11, then 115 of 3,200 at a near-identical share once a 4x larger
    document was benchmarked (#1495).

    Two assertions, in order of strength. The first is the **structural** bound the
    absolute ceiling gives: no run can be silent at or above
    ``_COVERAGE_SHORTFALL_WARNING_MIN_UNSCORED_ROWS`` unscored rows, whatever the list
    length, and that is a property of the guard rather than of the corpus — it is read
    off the shipped constant so re-tuning the constant moves it rather than breaking
    it. The second is the **measurement** under that bound, which keeps the observed
    residual a recorded figure rather than an arithmetic possibility.
    """
    from idp_common.assessment.batching import (
        _COVERAGE_SHORTFALL_WARNING_MIN_UNSCORED_ROWS as CEILING,
    )

    silent = [
        r
        for r in _defined(corpus)
        if r["conf_rows_unscored"]
        and severities[(r["conf_rows_expected"], r["conf_rows_unscored"])] is None
    ]
    assert silent, (
        "no committed run sits between 0 and the warning rung, so there is no "
        "observed residual to pin and this assertion shields nothing."
    )
    worst = max(silent, key=lambda r: r["conf_rows_unscored"])
    assert worst["conf_rows_unscored"] < CEILING, (
        f"{worst['conf_rows_unscored']} unscored rows of "
        f"{worst['conf_rows_expected']} ({worst['doc']}, {worst['cell']}) emit no "
        f"coverage issue, at or above the {CEILING}-row absolute ceiling the warning "
        "rung carries. The ceiling is not being enforced — that is a defect in "
        "``audit_explainability``, not a figure to re-pin."
    )
    assert worst["conf_rows_unscored"] <= MAX_SILENT_UNSCORED_ROWS, (
        f"{worst['conf_rows_unscored']} unscored rows of "
        f"{worst['conf_rows_expected']} ({worst['doc']}, {worst['cell']}) emit no "
        f"coverage issue, above the {MAX_SILENT_UNSCORED_ROWS} rows recorded when "
        "this was measured. The ceiling still holds (the assertion above passed), so "
        "this is a new observation inside the band the fraction deliberately leaves "
        "to ``assessment_incomplete``: re-measure, record the new figure in "
        "'Coverage: measured' in the assessment README, and re-pin."
    )


def test_nothing_the_absolute_ceiling_newly_reports_is_an_error(
    corpus, severities, severities_without_the_ceiling
):
    """The ceiling reports; it never escalates (#1495).

    A run that crosses the absolute ceiling without crossing the 5% fraction is below
    the 25% error fraction by arithmetic, so the ceiling cannot reach error severity —
    and ``extraction.row_shortfall_action`` does not govern this code, so no
    deployment can turn it into a failure either. Asserted over the corpus because it
    is the property that decides whether this change can break a document that
    completes today: every run the ceiling newly speaks about must be a warning.

    The subset is "the guard speaks, and would not with the ceiling lifted" — read
    from ``audit_explainability`` twice rather than from a restated comparand, so it
    keeps meaning "reported by the ceiling alone" whatever the rungs are expressed in.
    """
    ceiling_only = [
        r
        for r in _defined(corpus)
        if severities[(r["conf_rows_expected"], r["conf_rows_unscored"])] is not None
        and severities_without_the_ceiling[
            (r["conf_rows_expected"], r["conf_rows_unscored"])
        ]
        is None
    ]
    assert ceiling_only, (
        "no committed run is reported by the absolute ceiling alone, so the ceiling "
        "currently shields nothing on this corpus and this assertion is vacuous — "
        "which would also mean the firing-rate figures in 'Coverage: measured' can no "
        "longer be reproduced from these artifacts."
    )
    offenders = [
        (r["doc"], r["cell"], r["conf_rows_unscored"], r["conf_rows_expected"])
        for r in ceiling_only
        if severities[(r["conf_rows_expected"], r["conf_rows_unscored"])] != "warning"
    ]
    assert offenders == [], (
        f"{len(offenders)} of {len(ceiling_only)} runs reported by the absolute "
        "ceiling alone are not at warning severity. The ceiling exists to make a "
        "shortfall visible, not to fail a document that completes today. First few: "
        f"{offenders[:5]}"
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


def test_an_undefined_coverage_run_extracted_nothing(corpus):
    """``expected_rows == 0`` is an extraction finding, not a coverage one.

    The guard declines to fire when the section extracted no list row, which would be
    a hole if those runs were producing rows the measurement could not see. They are
    not: every such run with a synthetic row count recorded shows
    ``rows_extracted == 0``, i.e. the model returned no list at all.
    """
    undefined = [r for r in corpus if not r["conf_rows_expected"]]
    assert undefined, "no run records undefined coverage; this check shields nothing."
    with_rows = [
        r
        for r in undefined
        if isinstance(r.get("rows_extracted"), (int, float)) and r["rows_extracted"]
    ]
    assert with_rows == [], (
        f"{len(with_rows)} runs record extracted rows while reporting no coverage "
        "denominator, so the coverage walk is missing rows the extraction produced — "
        "a list nested inside a group attribute is the shape that does this. First "
        f"few: {[(r['doc'], r['cell'], r['rows_extracted']) for r in with_rows[:5]]}"
    )
