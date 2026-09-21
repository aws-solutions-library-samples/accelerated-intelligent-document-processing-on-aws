#!/usr/bin/env python3
"""Score ONE benchmark run on all seven dimensions vs ground truth.

Synthetic docs -> exact completeness + field/cell accuracy from <id>.truth.json.
Reference docs -> stack evaluation weighted_overall_score + parse-failure rate.

Usage:
  AWS_PROFILE=default python3 analyze.py --bucket <out> --tracking <tbl> \
      --run <runId> --doc <docName> [--truth <truth.json>] [--label L]
Prints a JSON score object.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402


def score_confidence_coverage(sections):
    """Confidence coverage over every section of one document: scored/extracted rows.

    Issue #997. ``n_gaps`` next door is an EXTRACTION metric — truth ids the model
    never produced — and says nothing about whether the rows it *did* produce carry
    a confidence. That second quantity is what the ``assessment_coverage_incomplete``
    guard fires on, and until this existed nothing in the repository recorded it: the
    guard's 5%/25% rungs rested on the expectation that a healthy run sits at 0%
    shortfall, read off the reconciliation code path and never measured.

    The rule is not reimplemented here. ``idp_common.assessment.batching``'s
    ``confidence_coverage`` is the same function the shipping guard calls, so this
    measures the predicate that actually fires rather than a lookalike — a
    reimplementation would disagree with the guard for reasons unrelated to the data,
    which is the whole failure mode #997 is about.

    Returns per-document totals plus the per-field breakdown, and ``None`` for the
    coverage ratio when the document has no list-valued attribute (coverage is
    undefined there, not perfect — a corpus mean that counted those as 100% would be
    reporting the share of list-free documents).

    Requires ``idp_common`` on ``PYTHONPATH`` (see benchmarks/matrices/METHODOLOGY.md,
    which pins it). Deliberately NOT wrapped in a try/except that records ``None``: a
    silent skip here reproduces exactly the defect this measurement exists to close.
    """
    from idp_common.assessment.batching import confidence_coverage

    expected = scored = 0
    by_field = {}
    for sec in sections:
        info = sec.get("explainability_info")
        # `explainability_info` is written as a one-element list wrapping the
        # per-field assessment dict (assessment/service.py); tolerate the bare dict.
        assessment = info[0] if isinstance(info, list) and info else info
        cov = confidence_coverage(assessment, sec.get("inference_result") or {})
        expected += cov["expected_rows"]
        scored += cov["scored_rows"]
        for field, n in cov["unscored_rows_by_field"].items():
            by_field[field] = by_field.get(field, 0) + n
    return {
        "conf_rows_expected": expected,
        "conf_rows_scored": scored,
        "conf_rows_unscored": expected - scored,
        "conf_coverage": round(scored / expected, 4) if expected else None,
        "conf_unscored_by_field": by_field or None,
    }


def records_with_path(ir):
    """``(field-path prefix, record)`` for each dict a truth file compares against.

    Normally just the ``inference_result`` itself, at the empty prefix. For a class
    flagged ``x-aws-idp-multi-instance`` (GitHub #715) the result is
    ``{"instances": [ …record… ]}``, and every user property lives one level down —
    so reading only top-level keys finds NOTHING and scores every scalar field
    wrong. Measured: the `mi-wrapped` cell reported ``scalar_accuracy = 0.0`` on all
    six runs while ``rows_extracted`` showed the data was complete (40/40 and
    100/100). That was this scorer, not the pipeline.

    The generator's ``fields`` records the FIRST document's identity block, and the
    caller uses first-wins merging, so yielding instances in order compares against
    the right record.

    The prefix is the piece ``scalar_bearing_records`` does not need and the
    confidence join cannot do without: it has to name a cell the same way
    ``idp_common.evaluation.flatten_confidences`` does, and that walk indexes every
    list by its position in the RAW list. So the index here is the raw one — a
    non-dict entry ahead of a record shifts the path but not this enumeration.
    """
    if not isinstance(ir, dict):
        return []
    instances = ir.get("instances")
    if isinstance(instances, list):
        records = [
            (f"instances[{i}]", r)
            for i, r in enumerate(instances)
            if isinstance(r, dict)
        ]
        if records:
            return records
    return [("", ir)]


def scalar_bearing_records(ir):
    """The dicts a truth file's flat ``fields`` should be compared against."""
    return [record for _prefix, record in records_with_path(ir)]


def _cell_path(prefix, field, index, cell):
    """Name one list cell the way ``flatten_confidences`` names it.

    ``flatten_values`` and ``flatten_confidences`` build the identical key for the
    identical position, which is what makes the join exact rather than
    by-field-name: one empty ``Description`` cell in a 400-row table must not be
    confused with any other row's.
    """
    root = f"{prefix}.{field}" if prefix else field
    return f"{root}[{index}].{cell}"


def _wall(row):
    st, ct = row.get("WorkflowStartTime"), row.get("CompletionTime")
    if not st or not ct:
        return None
    from datetime import datetime

    def _parse(s):
        return datetime.fromisoformat(str(s).replace("Z", ""))

    try:
        return (_parse(ct) - _parse(st)).total_seconds()
    except Exception:
        return None


def typed_match(expected, got):
    """Does ``got`` match a SCHEMA-TYPED expectation, in both type and value?

    This is deliberately stricter than ``scalar_accuracy``'s string compare, and
    the difference is the whole point of the metric. ``fields`` in a truth file
    records the text the document RENDERS (``"$685.50"``); ``fields_typed``
    records what a correctly-typed extraction must produce (``685.5``). Comparing
    a typed field by ``str()`` scores the *rendered* form as correct, so a
    pipeline that correctly returns the number looks WRONG — which is exactly
    backwards, and is how a value-normalization feature would be measured as a
    regression.

    So: a string in a ``number`` field is a MISS, because that is the failure
    under test. Numbers compare by value (``1234`` == ``1234.0``) since JSON does
    not distinguish them; booleans must be real booleans, not ``"Yes"``/``"true"``.
    """
    if isinstance(expected, bool):
        # Checked before the numeric branch: bool is a subclass of int.
        return isinstance(got, bool) and got == expected
    if isinstance(expected, (int, float)):
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        return float(got) == float(expected)
    if expected is None:
        return got is None
    return isinstance(got, str) and got.strip() == str(expected).strip()


def _seq_of(row):
    """The SEQnnnnn tag embedded in any string cell of an extracted row."""
    if not isinstance(row, dict):
        return None
    for v in row.values():
        if isinstance(v, str):
            m = lib.SEQ.search(v)
            if m:
                return int(m.group(1))
    return None


def score_cells(sections, rows_typed, list_key):
    """Per-CELL accuracy over list rows, matched to truth by SEQ tag.

    Completeness (``completeness_recall``) answers "did every row come back";
    this answers "did every cell come back with the right typed value". They are
    independent: a run can recover 100% of rows and still return every ``Amount``
    as the string ``"$1,234.00"``. Without this metric, per-row value handling is
    invisible to the whole benchmark — which is why an earlier enforcement A/B
    measured 81 value repairs and a delta of exactly zero.

    Returns ``(hits, total, rows_matched)``. ``total`` counts only cells the
    truth declares AND whose row was recovered, so this metric is about VALUE
    fidelity and does not double-count the truncation that recall already reports.
    """
    if not rows_typed:
        return None, None, None
    by_seq = {}
    for sec in sections:
        ir = sec.get("inference_result") or {}
        if not isinstance(ir, dict):
            continue
        # Multi-instance: the record lists live inside each instance.
        for record in scalar_bearing_records(ir):
            for key, val in record.items():
                if list_key and key.lower() != str(list_key).lower():
                    continue
                if not isinstance(val, list):
                    continue
                for row in val:
                    seq = _seq_of(row)
                    if seq is not None:
                        by_seq.setdefault(seq, row)
    hits = total = 0
    for seq_tag, cells in rows_typed.items():
        seq = int(str(seq_tag)[3:]) if str(seq_tag).startswith("SEQ") else int(seq_tag)
        row = by_seq.get(seq)
        if row is None:
            continue  # not recovered at all -> recall's business, not ours
        for cell, exp in (cells or {}).items():
            total += 1
            got = next((v for k, v in row.items() if k.lower() == cell.lower()), None)
            if typed_match(exp, got):
                hits += 1
    return hits, total, len(by_seq)


def _assessed_rows(explainability_info, prefix, field):
    """The assessment's row list for one data field, or None if it has none.

    Navigates ``explainability_info`` by the same prefix ``records_with_path``
    produced, so a multi-instance section resolves to the right record.
    """
    node = explainability_info
    if isinstance(node, list):
        node = node[0] if len(node) == 1 else None
    if not isinstance(node, dict):
        return None
    if prefix:
        # The only prefix shape this harness produces is ``instances[j]``.
        instances = node.get("instances")
        try:
            index = int(prefix[prefix.index("[") + 1 : prefix.index("]")])
        except (ValueError, IndexError):
            return None
        if not isinstance(instances, list) or index >= len(instances):
            return None
        node = instances[index]
        if not isinstance(node, dict):
            return None
    value = node.get(field)
    return value if isinstance(value, list) else None


def _assert_row_alignment(section, prefix, field, data_rows):
    """Fail loudly if the assessment's row list is not the same length as the data's.

    This is the one condition under which the confidence join mispairs rather than
    under-reports, so it is checked rather than assumed — see
    ``confidence_observations``. ``reconcile_assessment_to_data`` guarantees it in
    the pipeline; if that guarantee is ever removed, every calibration number in
    this harness silently becomes a statement about the wrong rows.

    Deliberately an exception and not a recorded ``None``: a scoring run that stops
    is recoverable, and a published reliability diagram built from mispaired cells
    is not.
    """
    assessed = _assessed_rows(section.get("explainability_info"), prefix, field)
    if assessed is None or len(assessed) == len(data_rows):
        return
    where = f"{prefix + '.' if prefix else ''}{field}"
    raise AssertionError(
        f"confidence/data row misalignment on {where}: {len(assessed)} assessment "
        f"rows vs {len(data_rows)} extracted rows. The confidence join is positional, "
        "so this would attribute each confidence to the wrong row. "
        "reconcile_assessment_to_data is supposed to make these equal — check it "
        "before trusting any calibration figure from this grid."
    )


def confidence_observations(sections, rows_typed, list_key):
    """``(confidence, correct)`` per list CELL, joined to exact truth by SEQ tag.

    This is the join #935 is about. A confidence score is only a calibration
    observation once it is paired with whether the value it scores was right, and
    the two halves live in different places: the score in ``explainability_info``,
    the value in ``inference_result``, the answer in the truth file. The corpus's
    unique ``SEQnnnnn`` row tag is what makes the pairing exact rather than
    positional — the row the model returned third may be the truth's fifth.

    Both sides are keyed by ``flatten_*`` field path, so a cell is identified by
    position within its own row and nothing is matched by bare field name. The
    correctness verdict is ``typed_match``, the SAME predicate ``cell_accuracy``
    uses, so a cell cannot be correct for one metric and wrong for the other.

    A row recovered in two sections contributes once (first section wins),
    matching ``score_cells``; counting it twice would inflate the observation count
    that every downstream reliability gate is thresholded on. Cells the truth does
    not declare (``Description``, which carries the tag) and cells with no
    confidence leaf are skipped — a missing confidence is ``score_confidence_coverage``'s
    subject, not evidence about calibration.

    ⚠️ **The invariant this join rests on is index alignment**, and the failure mode
    if it breaks is silent MISPAIRING rather than a missing observation. The row's
    truth is found by SEQ tag, but its confidence is found by POSITION: path
    ``F[i].Cell`` is the i-th element of ``explainability_info[0][F]``, and the join
    assumes that element describes the i-th element of ``inference_result[F]``. That
    holds because ``idp_common.assessment.batching.reconcile_assessment_to_data``
    truncates an over-long assessment list and pads a short one so the two lengths
    are equal — an assessment that returned 44 rows for a 120-row table would
    otherwise attribute row 44's confidence to row 44 of the data, which is some
    other transaction. That function's docstring names the same hazard for HITL and
    the UI, which index the pair the same way. ``_assert_row_alignment`` below
    re-checks it per section, so a change to reconciliation fails here loudly
    instead of quietly producing plausible calibration numbers.

    Two residuals are known, **unfiled**, and not reachable on this corpus. They have no
    issue number on purpose: neither is triggerable by any class in
    ``benchmarks/corpus/``, so neither has an observable symptom to file against, and a
    tracking issue would read as a known product defect rather than as a bound on this
    harness. If the corpus grows a class that reaches either, file it then.

    1. A top-level ``explainability_info`` list with more than one element collapses
       every element onto the same (empty) path prefix, last-write-wins. The wrapper is
       written with exactly one element and all sampled sections have one.
    2. A nested group carrying a group-level ``confidence`` alongside its per-field
       leaves would contribute one spurious observation per group. No class here does.

    Distinct from #1066 and #1067, which are filed, are about code outside this file, and
    are described in the pull request rather than here.
    """
    if not rows_typed:
        return []
    truth = {}
    for tag, cells in rows_typed.items():
        text = str(tag)
        seq = int(text[3:]) if text.startswith("SEQ") else int(text)
        truth[seq] = {str(c).lower(): e for c, e in (cells or {}).items()}

    observations = []
    seen = set()
    for sec in sections:
        ir = sec.get("inference_result") or {}
        confidences = lib.walk_confidence(sec.get("explainability_info"))
        if not confidences:
            continue
        for prefix, record in records_with_path(ir):
            for field, value in record.items():
                if list_key and field.lower() != str(list_key).lower():
                    continue
                if not isinstance(value, list):
                    continue
                _assert_row_alignment(sec, prefix, field, value)
                for index, row in enumerate(value):
                    seq = _seq_of(row)
                    cells = truth.get(seq) if seq is not None else None
                    if not cells or seq in seen:
                        continue
                    seen.add(seq)
                    for cell, got in row.items():
                        key = str(cell).lower()
                        if key not in cells:
                            continue
                        score = confidences.get(_cell_path(prefix, field, index, cell))
                        if not isinstance(score, (int, float)) or isinstance(
                            score, bool
                        ):
                            continue
                        observations.append(
                            (float(score), typed_match(cells[key], got))
                        )
    return observations


def score_calibration(sections, rows_typed, list_key, observations=None):
    """Is this run's confidence CALIBRATED, and does it RANK errors? (#935)

    ``mean_confidence`` and ``pct_conf_below_0.9`` next door describe the shape of
    the confidence distribution and say nothing about whether it is true. Two
    separate questions have to be answered before a confidence score can route
    human review, and they are independent:

    * **Calibration** — when the grader says 0.9, is it right 90% of the time?
      Expected Calibration Error.
    * **Discrimination** — are the wrong cells the low-scoring ones? AUROC. This is
      the only property worst-first review actually needs.

    A run can pass the first and fail the second completely, and the repository has
    already measured exactly that once: ECE 0.032 over 7 bins with AUROC 0.480, all
    77 errors in the top bin, so a worst-first queue reached none of them.

    Neither statistic is implemented here. ``ConfidenceCurve`` is the engine behind
    the shipped review-effort estimator, and its ``ECE_UNRELIABLE_THRESHOLD`` /
    ``AUROC_UNRELIABLE_THRESHOLD`` are the bars the product itself refuses to
    recommend a review subset under — so measuring through it makes the benchmark a
    statement about the thresholds that ship rather than about a lookalike.

    Two of each statistic are reported deliberately, and the pairs do not agree.

    ``calibration_auroc`` is the curve's binned estimate, which is what the gate
    reads and is biased LOW by design (it can call a good ranker mediocre; it will
    not call a chance-level ranker good). ``calibration_auroc_unbinned`` is the value
    to quote as a metric, computed from the value tally by ``unbinned_auroc`` and
    asserted equal to Stickler's ``AUROCMetric`` over the raw pairs.

    ``calibration_ece`` is likewise the gate's: it compares each bin's accuracy to
    the bin MIDPOINT, because the curve stores counts and not the confidences
    themselves. That puts a floor under it — a set of cells all scored 1.00 and all
    correct is perfectly calibrated and still reports 0.05, the distance from 1.00
    to the top bin's midpoint of 0.95. Well inside the 0.15 gate, so the gate does
    not misfire, but it is not a number to quote as the grader's calibration error.
    ``calibration_ece_mean_conf`` compares against the mean confidence observed in
    each bin, which is the standard estimator and what a reliability diagram plots.

    Reporting only one of each would either understate the grader or describe a
    gate nobody runs.

    ``calibration_curve`` carries the raw bin counts, which is what makes a
    per-cell or per-release roll-up EXACT: the curve composes additively, so
    pooling documents is adding two ten-element arrays rather than re-reading S3.
    ``brierSse``, ``confSum`` and ``valueTally`` ride along for the same reason —
    Brier is a mean of squared errors, the mean-confidence ECE needs a per-bin mean,
    and the unbinned AUROC needs the score ORDER, none of which a mean preserves but
    all of which a sum or a tally does. Together they are an exact sufficient
    statistic for every figure this study publishes, which is why they are committed
    into `summary.json`: the S3 artifacts they were derived from outlive their stack
    by exactly as long as nobody deletes it, and three release stacks are already
    gone.

    Everything is ``None`` with ``calibration_observations: 0`` when the document
    has no joinable cell, which is the honest reading for a reference-corpus run or
    a class with no list attribute. It is not 1.0 and it is not a failure.

    ``observations`` lets a caller that already holds the pairs — the release-level
    pass, which retains them to compute the unbinned AUROC over a whole arm — pass
    them in rather than have the join run twice over the same sections.
    """
    if observations is None:
        observations = confidence_observations(sections, rows_typed, list_key)
    if not observations:
        return {
            "calibration_observations": 0,
            "calibration_correct": None,
            "calibration_ece": None,
            "calibration_ece_mean_conf": None,
            "calibration_auroc": None,
            "calibration_auroc_unbinned": None,
            "calibration_brier": None,
            "calibration_bin_coverage": None,
            "calibration_reliable": None,
            "calibration_curve": None,
        }

    from idp_common.evaluation import ConfidenceCurve

    curve = ConfidenceCurve()
    # ``source="scoring"``, not the default "review": a benchmark run measures the
    # WHOLE confidence range, including the high-confidence zone worst-first review
    # never reaches. That is the distinction that lets the shipped estimator call a
    # curve MEASURED rather than PARTIALLY_MEASURED, so recording it wrongly here
    # would understate a curve this corpus genuinely does measure.
    curve.add_observations(observations, source="scoring")
    health = curve.calibration_health()

    from idp_common.evaluation.confidence_curve import BIN_COUNT, bin_index

    total = len(observations)
    correct = sum(1 for _score, ok in observations if ok)
    sse = sum((score - (1.0 if ok else 0.0)) ** 2 for score, ok in observations)
    conf_sum = [0.0] * BIN_COUNT
    for score, _ok in observations:
        conf_sum[bin_index(score)] += score
    ece_mean_conf = mean_conf_ece(curve.correct, curve.total, conf_sum)
    tally = value_tally(observations)
    unbinned = unbinned_auroc(tally)
    return {
        "calibration_observations": total,
        "calibration_correct": correct,
        "calibration_ece": round(health.ece, 4) if health.ece is not None else None,
        "calibration_ece_mean_conf": (
            round(ece_mean_conf, 4) if ece_mean_conf is not None else None
        ),
        "calibration_auroc": (
            round(health.auroc, 4) if health.auroc is not None else None
        ),
        "calibration_auroc_unbinned": (
            round(unbinned, 4) if unbinned is not None else None
        ),
        "calibration_brier": round(sse / total, 4),
        "calibration_bin_coverage": health.bin_coverage,
        "calibration_reliable": health.reliable,
        "calibration_curve": {
            **curve.to_dict(),
            "brierSse": sse,
            "confSum": conf_sum,
            "valueTally": tally,
        },
    }


# A confidence grader emits few distinct values — measured 2 to 10 per document on
# this corpus, because `_expand_row_to_per_column` fans ONE per-row score across the
# row's columns — so a tally keyed on the value itself is a handful of entries and is
# an exact sufficient statistic for the unbinned AUROC. A grader that emitted a
# distinct value per cell would make it as large as the document, so it is capped:
# past the cap the tally is dropped rather than truncated, because half a tally would
# yield a confidently wrong AUROC where an absent one yields None.
MAX_TALLY_VALUES = 256


def value_tally(observations):
    """``{confidence: [n, n_correct]}`` — the sufficient statistic for exact AUROC.

    The bin counts in ``calibration_curve`` pool exactly for ECE, Brier and the
    BINNED AUROC, but they discard within-bin ordering and so cannot recover the
    unbinned AUROC — which is the value worth quoting as the grader's ranking power.
    This tally can, because AUROC depends on the scores only through their order and
    their ties. Keys are strings because JSON has no float keys.

    ``None`` past ``MAX_TALLY_VALUES`` distinct values.
    """
    tally = {}
    for score, ok in observations:
        key = repr(float(score))
        entry = tally.get(key)
        if entry is None:
            if len(tally) >= MAX_TALLY_VALUES:
                return None
            entry = tally[key] = [0, 0]
        entry[0] += 1
        if ok:
            entry[1] += 1
    return tally


def unbinned_auroc(tally):
    """AUROC over the raw confidence values, from a ``value_tally``.

    ``P(a wrong cell scores lower than a correct one)``, with ties counted as half —
    the Mann-Whitney U statistic with mid-ranks, which is what Stickler's
    ``AUROCMetric`` computes over raw pairs and what
    ``tests/test_confidence_calibration.py`` asserts equality against.

    Computed here rather than by calling Stickler so that the benchmark harness keeps
    no dependency on the ``[evaluation]`` extra: it is on the default scoring path for
    every synthetic run, and a scoring run that dies mid-grid because a metric library
    is missing is a worse outcome than computing 15 lines of arithmetic.

    ``ConfidenceCurve.auroc`` remains the value the reliability GATE reads; that one
    is deliberately biased low by binning. Returns ``None`` when one class is absent,
    since ranking is then undefined rather than perfect, and when the tally was
    dropped for having too many distinct values.
    """
    if not tally:
        return None
    graded = sorted((float(value), n, k) for value, (n, k) in tally.items())
    n_correct = sum(k for _v, _n, k in graded)
    n_wrong = sum(n - k for _v, n, k in graded)
    if not n_correct or not n_wrong:
        return None
    concordant = 0.0
    wrong_below = 0
    for _value, n, k in graded:
        wrong_here = n - k
        # Correct cells at this value beat every wrong cell strictly below, and tie
        # with the wrong cells sharing the value.
        concordant += k * (wrong_below + 0.5 * wrong_here)
        wrong_below += wrong_here
    return concordant / (n_correct * n_wrong)


def mean_conf_ece(correct, total, conf_sum):
    """ECE against each bin's MEAN CONFIDENCE rather than its midpoint.

    The estimator Stickler's ``ECEMetric`` uses, and the one a reliability diagram
    plots. Computed here from the same three per-bin sums the summary stores, so a
    pooled release figure is exact rather than an average of per-document ECEs
    (which would weight a 5-row document like a 400-row one).
    """
    n = sum(total)
    if not n:
        return None
    error = 0.0
    for index, count in enumerate(total):
        if count <= 0:
            continue
        error += (count / n) * abs(correct[index] / count - conf_sum[index] / count)
    return error


def pool_calibration(payloads):
    """Fold per-document ``calibration_curve`` payloads into ONE curve.

    Pooling is what makes these numbers readable at all on this corpus. A single
    document contributes a few hundred cells of which nearly all are correct, so its
    own AUROC is usually undefined (one class absent) and its ECE is dominated by
    how accurate that one document happened to be. The shipped reliability gates say
    so explicitly: ``MIN_OBSERVATIONS_FOR_MEASURED`` is 30 and
    ``MIN_OBSERVATIONS_FOR_AUROC`` is 100.

    Exact, not an average of averages, on **every** statistic including the unbinned
    AUROC. The curve composes additively; the non-additive ones travel as sums
    (``brierSse``, ``confSum``) or as a tally (``valueTally``) for exactly that
    reason. Averaging per-document ECEs would weight a 5-row form the same as a
    400-row statement, and there is no per-document AUROC worth averaging at all.

    A payload whose ``valueTally`` is absent — a summary scored before it existed, or
    a grader that emitted more than ``MAX_TALLY_VALUES`` distinct values — makes the
    pooled unbinned AUROC ``None`` rather than a figure computed from the rest. A
    partial pool would be a real number over the wrong population, which is worse
    than an absent one.
    """
    from idp_common.evaluation import ConfidenceCurve, wilson_interval
    from idp_common.evaluation.confidence_curve import BIN_COUNT

    pooled = ConfidenceCurve()
    conf_sum = [0.0] * BIN_COUNT
    sse = 0.0
    tally = {}
    tally_complete = True
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        part = ConfidenceCurve.from_dict(payload)
        for index in range(BIN_COUNT):
            pooled.correct[index] += part.correct[index]
            pooled.total[index] += part.total[index]
        pooled.scoring_observations += part.scoring_observations
        sse += float(payload.get("brierSse") or 0.0)
        for index, value in enumerate((payload.get("confSum") or [])[:BIN_COUNT]):
            conf_sum[index] += float(value)
        part_tally = payload.get("valueTally")
        if not isinstance(part_tally, dict):
            tally_complete = False
            continue
        for key, (n, k) in part_tally.items():
            entry = tally.setdefault(key, [0, 0])
            entry[0] += int(n)
            entry[1] += int(k)

    total = int(pooled.total_observations)
    if not total:
        return None
    correct = int(sum(pooled.correct))
    health = pooled.calibration_health()
    low, high = wilson_interval(correct, total)
    table = []
    for row, bin_total, bin_conf in zip(
        pooled.reliability_table(), pooled.total, conf_sum
    ):
        n = int(bin_total)
        acc_low, acc_high = (
            wilson_interval(int(round(row["observedAccuracy"] * n)), n)
            if n
            else (None, None)
        )
        table.append(
            {
                **{k: row[k] for k in ("binStart", "binEnd", "observations")},
                "observedAccuracy": row["observedAccuracy"],
                "observedAccuracyLow": acc_low,
                "observedAccuracyHigh": acc_high,
                "meanConfidence": (bin_conf / n) if n else None,
            }
        )
    pooled_ece_mean_conf = mean_conf_ece(pooled.correct, pooled.total, conf_sum)
    pooled_unbinned = unbinned_auroc(tally) if tally_complete else None
    return {
        "observations": total,
        "correct": correct,
        "accuracy": round(correct / total, 4),
        "accuracy_low": round(low, 4),
        "accuracy_high": round(high, 4),
        "ece": round(health.ece, 4) if health.ece is not None else None,
        "ece_mean_conf": (
            round(pooled_ece_mean_conf, 4) if pooled_ece_mean_conf is not None else None
        ),
        "auroc": round(health.auroc, 4) if health.auroc is not None else None,
        "auroc_unbinned": (
            round(pooled_unbinned, 4) if pooled_unbinned is not None else None
        ),
        "brier": round(sse / total, 4),
        "bin_coverage": health.bin_coverage,
        # The three ways the shipped estimator refuses to recommend worst-first
        # review, kept apart because they mean different things and only one of them
        # is about the grader being wrong.
        "degenerate": health.degenerate,
        "overconfident": health.overconfident,
        "undiscriminating": health.undiscriminating,
        "reliable": health.reliable,
        "estimate_confidence": pooled.assess_estimate_confidence().value,
        "bins": table,
    }


def score_audit_metadata(sections):
    """What the extraction stage RECORDED about itself, aggregated per document.

    The extraction service writes an audit block into each section's
    ``result.json`` under ``metadata`` — ``forced_tool`` (WS-05), ``validation``
    (schema enforcement) and ``coercion`` (deterministic value repair). Nothing in
    this harness read it, which left three shipped features with no instrument:

    * A forcing A/B could not tell "forcing changed nothing" from "forcing never
      ran" — a model that answers in prose, or a route that cannot carry a
      toolConfig, both fall back silently and score identically to the off arm.
    * An enforcement A/B could not report how often validation or coercion
      actually FIRED, so a delta of zero was uninterpretable: no effect, or no
      opportunity? (That exact ambiguity wasted a whole enforcement run.)

    Returned keys are ``None`` when the feature left no record, so every existing
    baseline stays comparable — a document that ran with forcing off gets
    ``forced_tool_attempted: None``, not ``0``.
    """
    forced_attempted = forced_honored = 0
    forced_skips: dict[str, int] = {}
    val_seen = val_valid = val_errors = 0
    coercions = refusals = 0
    coercion_seen = False
    for sec in sections:
        md = sec.get("metadata")
        if not isinstance(md, dict):
            continue
        ft = md.get("forced_tool")
        if isinstance(ft, dict):
            # `skipped` names a route that cannot carry a toolConfig at all (a
            # Lambda hook, the GPT-5.x route, a class with no properties). That is
            # NOT an unhonored force and must not dilute the honored rate — it
            # means the arm never ran on this section.
            skipped = ft.get("skipped")
            if skipped:
                forced_skips[str(skipped)] = forced_skips.get(str(skipped), 0) + 1
            elif ft.get("requested"):
                forced_attempted += 1
                if ft.get("honored"):
                    forced_honored += 1
        val = md.get("validation")
        if isinstance(val, dict):
            val_seen += 1
            if val.get("valid"):
                val_valid += 1
            val_errors += int(val.get("error_count") or 0)
        co = md.get("coercion")
        if isinstance(co, dict):
            coercion_seen = True
            coercions += int(co.get("coercion_count") or 0)
            # Refusals are the interesting half: a value coercion DECLINED to
            # rewrite (an ambiguous date, a leading zero) is a field the model got
            # wrong that enforcement deliberately did not touch.
            refusals += int(co.get("refusal_count") or 0)
    return {
        "forced_tool_attempted": forced_attempted or None,
        "forced_tool_honored": forced_honored if forced_attempted else None,
        # THE number a forcing A/B is judged on: forcing that is not honored is
        # not being tested.
        "forced_tool_honored_rate": round(forced_honored / forced_attempted, 4)
        if forced_attempted
        else None,
        "forced_tool_skips": forced_skips or None,
        "validation_sections": val_seen or None,
        "validation_valid_rate": round(val_valid / val_seen, 4) if val_seen else None,
        "validation_errors": val_errors if val_seen else None,
        "coercions": coercions if coercion_seen else None,
        "coercion_refusals": refusals if coercion_seen else None,
    }


def score_synthetic(bucket, doc_prefix, truth):
    """Exact completeness + accuracy from SEQ tags and known field values."""
    seqs, confs = [], []
    scalar_hits = scalar_tot = 0
    typed_hits = typed_tot = 0
    fields = truth.get("fields") or {}
    fields_typed = truth.get("fields_typed") or {}
    got_fields = {}
    sections = list(lib.iter_section_results(bucket, doc_prefix))
    for sec in sections:
        ir = sec.get("inference_result", {}) or {}
        blob = json.dumps(ir)
        seqs += [int(m) for m in lib.SEQ.findall(blob)]
        confs += lib.confidence_values(sec.get("explainability_info"))
        # capture scalar fields (top-level, case-insensitive)
        # Unwrap a multi-instance result so its records' fields are visible; a
        # single-record result yields itself, so nothing changes for it.
        for record in scalar_bearing_records(ir):
            for k, v in record.items():
                got_fields.setdefault(k.lower(), v)
    truth_ids = set(int(s[3:]) for s in truth.get("seq_ids", []))
    extracted = set(seqs)
    n_truth = len(truth_ids)
    recall = len(extracted & truth_ids) / n_truth if n_truth else None
    prefix = 0
    while prefix in extracted:
        prefix += 1
    # scalar field accuracy (exact, normalized). Compares against the RENDERED
    # text, so it is unchanged for every existing truth file — the committed
    # baseline stays comparable.
    for label, exp in fields.items():
        scalar_tot += 1
        got = got_fields.get(label.lower())
        if got is not None and str(got).strip() == str(exp).strip():
            scalar_hits += 1
    # Typed accuracy: a SEPARATE metric, not a redefinition of the one above.
    # Only populated for truth files that declare `fields_typed`.
    for label, exp in fields_typed.items():
        typed_tot += 1
        if typed_match(exp, got_fields.get(label.lower())):
            typed_hits += 1
    cell_hits, cell_tot, _rows_matched = score_cells(
        sections, truth.get("rows_typed"), truth.get("list_key")
    )
    cell_accuracy = (
        round(cell_hits / cell_tot, 4) if cell_hits is not None and cell_tot else None
    )
    # Section count vs the truth's expectation. This is the metric boundary
    # detection is judged on, and nothing else can see it: a document split into
    # 3 sections instead of 1 still reports completeness_recall 1.0 and status
    # COMPLETED, because every row came back — just distributed across sections
    # that should not exist (#653/#726). Reported as a 1.0/0.0 so the mean over
    # repeats IS the pass rate, which is what a non-deterministic failure needs.
    audit = score_audit_metadata(sections)
    expected_sections = truth.get("expected_sections")
    sections_correct = (
        (1.0 if len(sections) == int(expected_sections) else 0.0)
        if expected_sections is not None
        else None
    )
    return {
        **audit,
        "sections": len(sections),
        "sections_expected": expected_sections,
        "sections_correct": sections_correct,
        "rows_truth": n_truth,
        "rows_extracted": len(extracted),
        "completeness_recall": round(recall, 4) if recall is not None else None,
        "truncation_prefix": prefix if n_truth else None,
        "dups": len(seqs) - len(extracted),
        "n_gaps": len(truth_ids - extracted),
        "scalar_accuracy": round(scalar_hits / scalar_tot, 4) if scalar_tot else None,
        "typed_accuracy": round(typed_hits / typed_tot, 4) if typed_tot else None,
        "typed_fields": typed_tot or None,
        "cell_accuracy": cell_accuracy,
        "cells_compared": cell_tot or None,
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in confs if c < 0.9) / len(confs), 1
        )
        if confs
        else None,
        "n_conf_leaves": len(confs),
        **score_confidence_coverage(sections),
        # Calibration and discrimination against the exact per-cell truth (#935).
        # Only the synthetic corpus can carry these: a reference corpus is scored by
        # the stack's own evaluation, which does not expose a per-cell verdict to
        # join a per-cell confidence to.
        **score_calibration(sections, truth.get("rows_typed"), truth.get("list_key")),
    }


def score_classification(ev):
    """CLASSIFICATION accuracy + confidence calibration from the stack eval.

    The evaluation report's `doc_split_metrics.page_details` carries, per page,
    the ground-truth class, the predicted class, whether it was `correct`, and
    (since GitHub #673) the classifier's own `predicted_confidence`. That last
    pairing is the only thing that says whether a reported classification
    confidence means anything:

      class_calibration_separation = mean(conf | correct) - mean(conf | wrong)

    Near 0 (or negative) means the model is equally confident when it is right
    and when it is wrong — the score carries no information and must not drive
    escalation, no matter how plausible the individual numbers look. This mirrors
    `calibration_separation`, which does the same for extracted FIELDS.

    Returns Nones when classification was not scored (the default) or when the
    run has no ground-truth classes, so an unscored run reports honestly instead
    of scoring 0.
    """
    out = {
        "class_accuracy": None,
        "class_calibration_separation": None,
        "class_mean_confidence": None,
        "n_class_scored_pages": 0,
    }
    if not ev:
        return out
    ds = ev.get("doc_split_metrics") or {}
    acc = ds.get("page_level_accuracy")
    if isinstance(acc, (int, float)):
        out["class_accuracy"] = round(acc, 4)
    right, wrong = [], []
    for row in ds.get("page_details") or []:
        c = row.get("predicted_confidence")
        if isinstance(c, (int, float)):
            (right if row.get("correct") else wrong).append(c)
    scored = right + wrong
    out["n_class_scored_pages"] = len(scored)
    if scored:
        out["class_mean_confidence"] = round(sum(scored) / len(scored), 4)
    # Needs both populations: separation is undefined when every page was right
    # (a perfect run says nothing about calibration) or every page was wrong.
    if right and wrong:
        out["class_calibration_separation"] = round(
            sum(right) / len(right) - sum(wrong) / len(wrong), 4
        )
    return out


def score_reference(bucket, doc_prefix):
    """Weighted accuracy + parse failures + calibration from the stack eval."""
    ev = lib.get_json(bucket, doc_prefix + "evaluation/results.json")
    acc = pf = None
    sep = None
    if ev:
        acc = ev.get("overall_metrics", {}).get("weighted_overall_score")
        pf = 0
        corr_conf, wrong_conf = [], []
        for sec in ev.get("section_results") or ev.get("sections") or []:
            for a in sec.get("attributes") or []:
                if "fail" in str(a.get("failure_type") or "").lower():
                    pf += 1
                c = a.get("confidence")
                if isinstance(c, (int, float)):
                    (corr_conf if a.get("matched") else wrong_conf).append(c)
        if corr_conf and wrong_conf:
            sep = round(
                sum(corr_conf) / len(corr_conf) - sum(wrong_conf) / len(wrong_conf), 4
            )
    confs = []
    sections = list(lib.iter_section_results(bucket, doc_prefix))
    for sec in sections:
        confs += lib.confidence_values(sec.get("explainability_info"))
    return {
        **score_audit_metadata(sections),
        "weighted_accuracy": acc,
        "parse_failures": pf,
        "calibration_separation": sep,
        **score_classification(ev),
        "mean_confidence": round(sum(confs) / len(confs), 4) if confs else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in confs if c < 0.9) / len(confs), 1
        )
        if confs
        else None,
        "n_conf_leaves": len(confs),
        **score_confidence_coverage(sections),
    }


def score_doc(bucket, tracking, run_id, doc_name, truth=None):
    doc_prefix = f"{run_id}/{doc_name}/"
    row = lib.doc_row(tracking, run_id, run_id and doc_name)
    status = row.get("ObjectStatus", "?")
    metering = lib.doc_metering(tracking, run_id, doc_name)
    cost, by = lib.price_metering(metering)
    by_phase = {}
    for k, units in (metering or {}).items():
        phase = k.split("/")[0]
        c, _ = lib.price_metering({k: units})
        by_phase[phase] = round(by_phase.get(phase, 0.0) + c, 5)
    # Token counts, both summed across the whole document and split by phase.
    #
    # The doc-level sum mixes every model the pipeline called (Sonnet for
    # extraction, Nova for classification/confidence), so it cannot answer "which
    # call got bigger" — a +31% doc-level inputTokens can be entirely Nova, which
    # moves cost by a rounding error. `tokens_by_phase` and `cost_by_key` split
    # it: the phase says WHERE, the pricing key (model id) says WHICH MODEL.
    # Both were added while localizing the v0.6.6 -> v0.6.7 advanced-mode cost
    # rise, which `cost_by_phase` alone could only narrow to "Extraction".
    _TOK_UNITS = (
        "inputTokens",
        "outputTokens",
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
    )
    tok = {}
    tok_by_phase = {}
    for k, units in (metering or {}).items():
        if isinstance(units, dict):
            phase = k.split("/")[0]
            for u in _TOK_UNITS:
                if u in units:
                    tok[u] = tok.get(u, 0) + int(units[u])
                    ph = tok_by_phase.setdefault(phase, {})
                    ph[u] = ph.get(u, 0) + int(units[u])
    out = {
        "doc": doc_name,
        "status": status,
        "success": status == "COMPLETED",
        "page_count": row.get("PageCount"),
        "wall_s": _wall(row),
        "cost": round(cost, 4),
        "cost_by_phase": by_phase,
        "cost_by_key": {k: round(v, 5) for k, v in (by or {}).items()},
        "tokens": tok,
        "tokens_by_phase": tok_by_phase,
    }
    if truth:
        out.update(score_synthetic(bucket, doc_prefix, truth))
    else:
        out.update(score_reference(bucket, doc_prefix))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--tracking", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--doc", required=True)
    ap.add_argument("--truth", default=None)
    ap.add_argument("--label", default=None)
    a = ap.parse_args()
    truth = json.load(open(a.truth)) if a.truth else None
    res = score_doc(a.bucket, a.tracking, a.run, a.doc, truth)
    if a.label:
        res["label"] = a.label
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
