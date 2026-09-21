#!/usr/bin/env python3
"""Score every run in a runmap, roll into summary tables, compare to a baseline.

Usage:
  AWS_PROFILE=default python3 aggregate.py --run results/run-XXXX --out results/<release>/<suite>
  python3 aggregate.py --compare results/<release>/<suite>/summary.json --baseline results/baseline.json
  python3 aggregate.py --figures results/<release>/<suite>/summary.json   # emit charts
  AWS_PROFILE=default python3 aggregate.py --calibration results/<release>/*/summary.json \
      --calibration-group assessment,confidence_model   # ECE/Brier/AUROC per arm

Scored output goes in a <suite>/ subdirectory of the release dir; results/ keeps one
complete set per release (see results/RETENTION.md).

Writes summary.json (per (cell,doc) full scores) + summary.csv (+ meta.json).
Regression thresholds: accuracy -0.02, cost +15%, any new failure, calibration
separation -0.03 (field-level and class-level alike), pooled mean-confidence ECE
+0.01, pooled binned AUROC -0.05, or either crossing its shipped unreliable bar.

--calibration pools confidence against the synthetic corpus's exact per-cell truth,
per configuration arm. It reads the `calibration_curve` sufficient statistic stored
in each summary and needs no AWS; --calibration-from-s3 re-reads the extraction
output from the bucket instead, and --augment backfills the statistic into a grid
scored before it existed. Scoring is retroactive either way, so nothing here costs
inference (#935).
"""

# ruff: noqa: E402  (local sibling imports require the sys.path bootstrap first)
import argparse
import csv
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze

import lib

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Floor for the run-to-run spread of a QUALITY metric (accuracy / recall) when a
# cell has n<2 and therefore no measured stdev. In accuracy points, matching the
# 0.02 regression threshold: at n=1 a shift larger than ~0.028 (the combined
# floor for both sides) is still reported, but a smaller one is called
# inconclusive rather than a finding. Chosen because quality metrics on
# non-deterministic cells were observed swinging 0.10 <-> 1.00 on one document.
QUALITY_SPREAD_FLOOR = 0.02

# How far a cell's POOLED calibration may move between releases before it is a
# regression (#935).
#
# The ECE threshold is set from the metric's MEASURED scale, not by analogy with the
# older `calibration_separation` threshold. Across the fourteen arms of the published
# study the mean-confidence ECE spans 0.0005 to 0.3097, and the healthy arms cluster
# below 0.005; more to the point, the same configuration measured on two releases and
# two stacks drifts by at most 0.0007 (0.0037 -> 0.0030, 0.0211 -> 0.0210,
# 0.0033 -> 0.0030, 0.0005 -> 0.0004). 0.01 is roughly fourteen times that observed
# drift and still small enough to catch a healthy arm doubling several times over.
#
# A threshold chosen by analogy instead was 0.03 — larger than the entire range the
# metric occupies on this corpus, so the magnitude arm of the gate could not fire on
# any real movement.
#
# AUROC keeps a coarser threshold: the gate's estimator is binned, so it moves in
# steps set by how mass redistributes across bins rather than continuously.
CALIBRATION_ECE_REGRESSION = 0.01
CALIBRATION_AUROC_REGRESSION = 0.05


def score_all(run_dir):
    rm = json.load(open(os.path.join(run_dir, "runmap.json")))
    res = rm["resources"]
    rows = []
    for r in rm["runs"]:
        if not r.get("run_id"):
            rows.append({**_key(r), "status": "NOT_LAUNCHED", "success": False})
            continue
        if r.get("reference"):
            rows.extend(score_reference_run(res, r))
            continue
        truth = (
            json.load(open(r["truth"]))
            if r.get("truth") and os.path.exists(r["truth"])
            else None
        )
        try:
            sc = analyze.score_doc(
                res["output_bucket"],
                res["tracking_table"],
                r["run_id"],
                r["doc_name"],
                truth,
            )
        except Exception as e:
            sc = {"status": "SCORE_ERROR", "success": False, "error": str(e)}
        # Synthetic rows let the scorer's "doc" (the PDF file name) win — the
        # committed baselines are keyed that way, so changing it would break
        # release pairing. Reference rows do the opposite; see score_reference_run.
        rows.append({**_key(r), **sc})
    return rm, rows


def reference_doc_names(prefixes, run_id):
    """``<run_id>/<doc>/`` S3 prefixes -> document names, in a stable order."""
    names = []
    for p in prefixes:
        rest = p[len(run_id) + 1 :] if p.startswith(run_id + "/") else p
        name = rest.strip("/")
        if name:
            names.append(name)
    return sorted(names)


def score_reference_run(res, r, list_prefixes=None, score=None):
    """One reference-corpus run holds ``n_docs`` documents; score each (#766).

    Every row keeps the corpus id as ``doc`` and carries the document under
    ``sub_doc``, so cell_stats' per-cell roll-up averages over the corpus the
    way it averages over repeats — a 20-document real corpus contributes a mean
    weighted accuracy, not 20 phantom "documents" in the summary. No local truth
    exists for these: ``analyze.score_doc`` with ``truth=None`` dispatches to
    ``score_reference``, which reads the stack's own evaluation. A run whose S3
    prefix holds no documents is reported as such rather than vanishing.
    """
    list_prefixes = list_prefixes or lib.list_doc_prefixes
    score = score or analyze.score_doc
    names = reference_doc_names(
        list_prefixes(res["output_bucket"], r["run_id"]), r["run_id"]
    )
    if not names:
        return [{**_key(r), "status": "NO_DOCS", "success": False}]
    rows = []
    for name in names:
        try:
            sc = score(
                res["output_bucket"], res["tracking_table"], r["run_id"], name, None
            )
        except Exception as e:
            sc = {"status": "SCORE_ERROR", "success": False, "error": str(e)}
        # The scorer reports the document it scored under "doc" (its file name);
        # the run key must win so the row stays keyed on the CORPUS id, with the
        # file name under sub_doc. Caught live: the first cut let the scorer's
        # "doc" overwrite the corpus id, and the roll-up saw 20 one-off documents.
        rows.append({**sc, **_key(r), "sub_doc": name})
    expected = int(r.get("n_docs") or 0)
    if expected and len(names) != expected:
        for row in rows:
            row["coverage_note"] = f"{len(names)} of {expected} documents found"
    return rows


def _key(r):
    return {
        "cell": r["cell"],
        "doc": r["doc"],
        # Set only on rows expanded from a reference-corpus run (#766).
        "sub_doc": None,
        "repeat": r.get("repeat", 0),
        "resolved": r.get("resolved", {}),
        "run_id": r.get("run_id"),
    }


CSV_COLS = [
    "cell",
    "doc",
    "sub_doc",
    "repeat",
    "status",
    "success",
    "page_count",
    "completeness_recall",
    "truncation_prefix",
    "scalar_accuracy",
    "typed_accuracy",
    "cell_accuracy",
    # Mean over repeats IS the boundary-detection pass rate.
    "sections_correct",
    "weighted_accuracy",
    "parse_failures",
    # Audit metadata the extraction stage recorded about itself. Without these a
    # feature A/B cannot distinguish "no effect" from "never ran" — see
    # analyze.score_audit_metadata.
    "forced_tool_attempted",
    "forced_tool_honored",
    "forced_tool_honored_rate",
    "validation_valid_rate",
    "validation_errors",
    "coercions",
    "coercion_refusals",
    "mean_confidence",
    "pct_conf_below_0.9",
    # Confidence COVERAGE (#997): the share of extracted list rows carrying a
    # confidence, by the same rule the assessment_coverage_incomplete guard fires
    # on. Distinct from mean_confidence, which averages the scores that exist and
    # is silent about the rows that have none. Carried in the CSV as well as the
    # JSON because DictWriter(extrasaction="ignore") drops any row key absent from
    # this list without a word — which is how n_conf_leaves came to be written by
    # the scorer and readable from nothing.
    "conf_rows_expected",
    "conf_rows_scored",
    "conf_rows_unscored",
    "conf_coverage",
    # Calibration against exact per-cell truth (#935). `calibration_separation`
    # below is a different and much weaker instrument — a difference of two means,
    # available only on the reference path — and it cannot distinguish a grader that
    # is well calibrated from one that ranks errors usefully. These can:
    # `calibration_ece` is the gate's calibration error, `calibration_auroc` its
    # ranking power, and `calibration_observations` is what says whether either is
    # thick enough to read (30 / 100 are the shipped floors).
    "calibration_observations",
    "calibration_ece",
    "calibration_ece_mean_conf",
    "calibration_auroc",
    "calibration_auroc_unbinned",
    "calibration_brier",
    "calibration_bin_coverage",
    "calibration_separation",
    "class_accuracy",
    "class_mean_confidence",
    "class_calibration_separation",
    "wall_s",
    "cost",
]


def _stats(vals):
    """n, mean, stdev, and coefficient of variation (stdev/mean) for a list."""
    xs = [v for v in vals if isinstance(v, (int, float))]
    n = len(xs)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "stdev": None,
            "cv": None,
            "min": None,
            "max": None,
        }
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    stdev = var**0.5
    return {
        "n": n,
        "mean": round(mean, 5),
        "stdev": round(stdev, 5),
        "cv": round(stdev / mean, 4) if mean else None,
        "min": round(min(xs), 5),
        "max": round(max(xs), 5),
    }


def cell_stats(rows):
    """Per-cell roll-up across docs×repeats: cost/accuracy/recall mean±stdev+CV, plus
    a repeats count so cost-variance is measurable and comparable between configs.
    Cost CV is the key signal — agentic cells vary run-to-run, so a cost DIFFERENCE
    between two configs is only trustworthy when it exceeds their sampling spread."""
    by = {}
    for r in rows:
        by.setdefault(r["cell"], []).append(r)
    out = {}
    for cell, rs in by.items():
        succ = [r for r in rs if r.get("success")]
        out[cell] = {
            "resolved": rs[0].get("resolved", {}),
            "n_runs": len(rs),
            "n_success": len(succ),
            "n_fail": len(rs) - len(succ),
            "max_repeat": max((r.get("repeat", 0) for r in rs), default=0) + 1,
            "cost": _stats([r.get("cost") for r in succ]),
            "completeness_recall": _stats([r.get("completeness_recall") for r in succ]),
            "scalar_accuracy": _stats([r.get("scalar_accuracy") for r in succ]),
            "typed_accuracy": _stats([r.get("typed_accuracy") for r in succ]),
            "cell_accuracy": _stats([r.get("cell_accuracy") for r in succ]),
            # The mean here is the boundary-detection PASS RATE over repeats,
            # which is the only meaningful reading of a non-deterministic failure.
            "sections_correct": _stats([r.get("sections_correct") for r in succ]),
            "weighted_accuracy": _stats([r.get("weighted_accuracy") for r in succ]),
            # Did the feature under test actually engage? A forcing arm whose
            # honored rate is 0 has measured nothing, and a delta of zero on an
            # enforcement arm that coerced nothing is not evidence about coercion.
            "forced_tool_honored_rate": _stats(
                [r.get("forced_tool_honored_rate") for r in succ]
            ),
            "coercions": _stats([r.get("coercions") for r in succ]),
            "validation_valid_rate": _stats(
                [r.get("validation_valid_rate") for r in succ]
            ),
            "wall_s": _stats([r.get("wall_s") for r in succ]),
            # #997 asked for the DISTRIBUTION, not just the mean: a mean of 0.99
            # is consistent both with every document at 0.99 and with 99 documents
            # at 1.0 and one at 0.0, and only the second says anything about the
            # guard's false-positive rate. _stats carries min/max/stdev/CV, and it
            # drops Nones — so documents with no list attribute (coverage
            # undefined) are excluded rather than counted as perfect.
            "conf_coverage": _stats([r.get("conf_coverage") for r in succ]),
            # Calibration is POOLED over the cell's documents and repeats rather
            # than averaged (#935). Averaging per-document ECEs would weight a
            # 5-row form like a 400-row statement, and per-document AUROC is
            # usually undefined outright because a single document rarely contains
            # both a wrong cell and a right one at different confidences. Pooling
            # the stored bin counts is exact and needs no second pass over S3.
            "calibration": analyze.pool_calibration(
                [r.get("calibration_curve") for r in succ]
            ),
        }
    return out


def _write_summary_csvs(rows, cells, out):
    """The two CSV views beside `summary.json`.

    Factored out so `augment_summary` rewrites them the same way `write_summary`
    writes them: a backfill that updated the JSON and left the CSV describing the
    previous state would put two disagreeing artifacts in one directory.
    """
    with open(os.path.join(out, "summary.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    # per-cell cost-variance CSV (the "can we detect cost differences?" view)
    with open(os.path.join(out, "cell_stats.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "cell",
                "n_success",
                "n_fail",
                "cost_mean",
                "cost_stdev",
                "cost_cv",
                "cost_min",
                "cost_max",
                "recall_mean",
                "acc_mean",
                "wall_mean",
            ]
        )
        for cell, s in sorted(cells.items()):
            c = s["cost"]
            w.writerow(
                [
                    cell,
                    s["n_success"],
                    s["n_fail"],
                    c["mean"],
                    c["stdev"],
                    c["cv"],
                    c["min"],
                    c["max"],
                    s["completeness_recall"]["mean"],
                    s["scalar_accuracy"]["mean"],
                    s["wall_s"]["mean"],
                ]
            )


def write_summary(rm, rows, out):
    os.makedirs(out, exist_ok=True)
    # Surfaced at scoring time as well as in the artifact: whoever runs
    # aggregate.py is the person about to copy these numbers somewhere, and they
    # did not necessarily watch the launch.
    if rm.get("cells_skipped_config_upload"):
        print(
            "⚠ INCOMPLETE GRID — configuration upload failed for "
            f"{rm.get('config_upload_failed_versions')}, so these cells were "
            f"never launched: {rm['cells_skipped_config_upload']}. This summary "
            "does not cover the whole suite."
        )
    if rm.get("docs_missing_truth"):
        print(
            "⚠ scored WITHOUT exact ground truth for "
            f"{rm['docs_missing_truth']} — these rows come from the stack's own "
            "evaluation, which is a different scorer and not comparable with "
            "locally-scored rows."
        )
    cells = cell_stats(rows)
    json.dump(
        {"meta": _meta(rm), "rows": rows, "cell_stats": cells},
        open(os.path.join(out, "summary.json"), "w"),
        indent=2,
    )
    _write_summary_csvs(rows, cells, out)
    # warn loudly when cost CV is high at low n (means are untrustworthy)
    noisy = [
        (cell, s["cost"]["cv"], s["cost"]["n"])
        for cell, s in cells.items()
        if s["cost"]["cv"] and s["cost"]["cv"] > 0.25
    ]
    if noisy:
        print(
            "⚠ high cost variance (CV>0.25) — increase repeats for reliable cost comparison:"
        )
        for cell, cv, n in sorted(noisy, key=lambda x: -(x[1] or 0)):
            print(f"    {cell}: cost CV={cv} over n={n}")
    print(
        f"summary -> {out}/summary.{{json,csv}} + cell_stats.csv ({len(rows)} rows, {len(cells)} cells)"
    )


def _meta(rm):
    import subprocess

    commit = subprocess.run(
        "git rev-parse --short HEAD",
        shell=True,  # nosec B602 - fixed local command
        capture_output=True,
        text=True,
        cwd=BENCH,
    ).stdout.strip()
    ph = subprocess.run(
        f"sha256sum {lib.PRICING_PATH}",
        shell=True,
        capture_output=True,
        text=True,  # nosec B602 - fixed local command
    ).stdout.split()[:1]
    return {
        "stack": rm.get("stack"),
        "stack_version": _stack_version(rm.get("stack")),
        "suite": rm.get("suite"),
        "class": rm.get("class"),
        # The `--set` axis overrides the grid ran with. (suite, class) does not
        # identify a measurement on its own: one release legitimately runs the same
        # suite twice with different overrides — `cost` at the cross-version control
        # model for the release A/B and at the shipped default for the config paper.
        # Absent (None) on a runmap written before run_matrix recorded them; an
        # empty list means "ran with the committed default_cell", which is different.
        "overrides": rm.get("overrides"),
        # Coverage, carried through from the runmap. The runmap itself is
        # gitignored (results/RETENTION.md), so without these the committed
        # meta.json a release page cites records nothing about which of the
        # suite's documents were actually measured (#766). None on a runmap
        # written before run_matrix recorded them — that means unknown, not
        # complete.
        "docs_named": rm.get("docs_named"),
        "docs_run": rm.get("docs_run"),
        "docs_reference": rm.get("docs_reference"),
        "docs_unlaunchable": rm.get("docs_unlaunchable"),
        "docs_other_class": rm.get("docs_other_class"),
        # Setup failures, carried through for the same reason as the coverage
        # keys above: the runmap is gitignored, so summary.json's meta is the
        # only durable record. A non-empty `cells_skipped_config_upload` means
        # this summary does NOT cover the whole suite — those cells ran against
        # no uploaded configuration and were never launched.
        "config_upload_failed_versions": rm.get("config_upload_failed_versions"),
        "cells_skipped_config_upload": rm.get("cells_skipped_config_upload"),
        "docs_missing_truth": rm.get("docs_missing_truth"),
        # NOTE: `commit` is the LOCAL repo HEAD at scoring time, which is not
        # necessarily the code that ran — a run against a published template, or a
        # run scored after further local commits, will differ. `stack_version` above
        # is the authoritative "what code produced these numbers", read from the
        # deployed stack's own CloudFormation Description.
        "commit": commit,
        "pricing_sha256": ph[0] if ph else None,
        "scored_at": datetime.datetime.utcnow().isoformat() + "Z",
        "region": lib.REGION,
    }


def _stack_version(stack_name):
    """The deployed accelerator version, from the stack's CFN Description.

    The Description is `... (vX.Y.Z)`, set at publish time, so it identifies the
    code that actually served the run — the one fact a release A/B cannot get
    from the local checkout. Returns None (never raises) if the stack is gone or
    credentials don't reach it; a missing version must not lose a scored run.
    """
    if not stack_name:
        return None
    try:
        desc = (
            lib.session()
            .client("cloudformation")
            .describe_stacks(StackName=stack_name)["Stacks"][0]
            .get("Description", "")
        )
        m = re.search(r"\(v([0-9][^)]*)\)", desc)
        return m.group(1) if m else None
    except Exception:
        return None


def _cells(summary):
    """Return cell_stats from a summary dict, recomputing from rows if absent
    (back-compat with summaries written before cell_stats existed)."""
    return summary.get("cell_stats") or cell_stats(summary.get("rows", []))


QUALITY_METRICS = ("scalar_accuracy", "completeness_recall", "weighted_accuracy")


def _paired_quality_deltas(cur_summary, base_summary):
    """Per-(cell, metric) list of per-document deltas, for a PAIRED comparison.

    Both releases run the identical document set, so pairing on (cell, doc,
    repeat) removes document heterogeneity — essential when a doc set spans
    5-row and 100-row documents, where the across-document spread is large even
    if every document moved identically.
    """

    def index(summary):
        out = {}
        for r in summary.get("rows", []):
            # sub_doc is the document inside a reference-corpus run (#766); without
            # it a 20-document corpus collapsed onto one key and the paired delta
            # compared one arbitrary document.
            out[(r.get("cell"), r.get("doc"), r.get("sub_doc"), r.get("repeat", 0))] = r
        return out

    cur_rows, base_rows = index(cur_summary), index(base_summary)
    deltas: dict[tuple[str, str], list[float]] = {}
    for key, cr in cur_rows.items():
        br = base_rows.get(key)
        if not br:
            continue
        cell = key[0]
        for m in QUALITY_METRICS:
            cv, bv = cr.get(m), br.get(m)
            if isinstance(cv, (int, float)) and isinstance(bv, (int, float)):
                deltas.setdefault((cell, m), []).append(cv - bv)
    return deltas


def _delta_spread(deltas):
    """Spread of the paired per-document deltas, floored.

    With <2 pairs there is no measured spread, so the floor stands in: at n=1 a
    shift must clear QUALITY_SPREAD_FLOOR to be reported, which keeps a genuinely
    large single-sample movement visible while refusing to call a small one a
    finding.
    """
    xs = [d for d in deltas if isinstance(d, (int, float))]
    if len(xs) < 2:
        return QUALITY_SPREAD_FLOOR
    mean = sum(xs) / len(xs)
    sd = (sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5
    return max(sd, QUALITY_SPREAD_FLOOR)


def calibration_findings(cur, base, regression=True):
    """Calibration movements between two cells' POOLED curves, as report lines.

    Answers two different questions, and the second is the one that matters:

    1. Did the number move more than ``CALIBRATION_ECE_REGRESSION`` /
       ``CALIBRATION_AUROC_REGRESSION``?
    2. Did the cell CROSS one of the bars the shipped estimator acts on —
       ``ECE_UNRELIABLE_THRESHOLD`` or ``AUROC_UNRELIABLE_THRESHOLD``? A crossing is
       reported whatever its size, because on the far side of it the product stops
       recommending a worst-first review subset at all. That is a product behaviour
       change, not a metric wobble.

    ⚠️ **The two questions read DIFFERENT calibration-error estimators, and they have
    to.** The crossing check reads ``ece`` — the midpoint-based one — because
    ``ECE_UNRELIABLE_THRESHOLD`` is a bar the shipped product applies to exactly that
    value via ``CalibrationHealth.ece``, so a crossing computed from anything else
    would not predict what the estimator does. The magnitude check reads
    ``ece_mean_conf``, because ``ece`` is nearly immobile: it compares each bin's
    accuracy to the bin MIDPOINT, so any movement in confidence WITHIN a bin is
    invisible to it. Measured across the six grader arms of the published study,
    ``ece`` spans 0.0013 while ``ece_mean_conf`` spans 0.0239 — so the midpoint
    estimator cannot move far enough to trip any threshold worth setting. Two concrete
    failures follow from getting this wrong, and both are reachable on the single-bin
    shape the study shows the shipped default produces:

    * *Blind.* Hold accuracy at 0.97 with all mass in the top bin and let mean
      confidence drift 0.97 → 0.995. Real calibration error worsens by 0.025;
      midpoint ECE moves by exactly 0.0000 and the accuracy gate is silent too,
      because accuracy did not move.
    * *Inverted.* Hold confidence at 0.995 and let accuracy fall 0.999 → 0.95. Real
      error worsens by 0.041; midpoint ECE moves −0.049 and the cell is reported as
      an **improvement**.

    Both sides must carry enough observations for the statistic to mean anything —
    the shipped floors, ``MIN_OBSERVATIONS_FOR_MEASURED`` for calibration error and
    ``MIN_OBSERVATIONS_FOR_AUROC`` for ranking power. Below them nothing is reported
    in either direction, because a thin sample moving is not a finding and a thin
    sample holding still is not reassurance.

    A baseline with no ``calibration`` block makes every comparison here vacuous.
    Skipping is the right behaviour, but it must not be invisible, so
    ``compare_cells`` prints one line per such cell — see ``_missing_metric_notes``.
    """
    from idp_common.evaluation.confidence_curve import (
        AUROC_UNRELIABLE_THRESHOLD,
        ECE_UNRELIABLE_THRESHOLD,
        MIN_OBSERVATIONS_FOR_AUROC,
        MIN_OBSERVATIONS_FOR_MEASURED,
    )

    cc, bc = cur.get("calibration"), base.get("calibration")
    if not cc or not bc:
        return []
    findings = []

    def enough(floor):
        return cc["observations"] >= floor and bc["observations"] >= floor

    def note(cur_v, base_v):
        return (
            f"(n {bc['observations']}->{cc['observations']}, {base_v:.3f}->{cur_v:.3f})"
        )

    # Calibration error: HIGHER is worse, so the sign convention is inverted
    # relative to every other metric in this comparison. Magnitude on the
    # mean-confidence estimator, crossing on the gate's own — see the docstring.
    if enough(MIN_OBSERVATIONS_FOR_MEASURED):
        cur_mc, base_mc = cc.get("ece_mean_conf"), bc.get("ece_mean_conf")
        cur_gate, base_gate = cc.get("ece"), bc.get("ece")
        moved = None
        if None not in (cur_mc, base_mc):
            moved = cur_mc - base_mc
        crossed = healed = False
        if None not in (cur_gate, base_gate):
            crossed = cur_gate > ECE_UNRELIABLE_THRESHOLD >= base_gate
            healed = base_gate > ECE_UNRELIABLE_THRESHOLD >= cur_gate
        big = moved is not None and moved >= CALIBRATION_ECE_REGRESSION
        small = moved is not None and moved <= -CALIBRATION_ECE_REGRESSION
        if regression and (big or crossed):
            tag = (
                f"calibration ECE {moved:+.3f} {note(cur_mc, base_mc)}"
                if moved is not None
                else "calibration ECE unmeasured"
            )
            if crossed:
                tag += (
                    f"  [CROSSED the {ECE_UNRELIABLE_THRESHOLD} unreliable bar "
                    f"({base_gate:.3f}->{cur_gate:.3f} on the gate's own midpoint "
                    "estimator) — the estimator will now recommend reviewing "
                    "everything]"
                )
            findings.append(tag)
        elif not regression and (small or healed):
            tag = (
                f"calibration ECE {moved:+.3f} {note(cur_mc, base_mc)}"
                if moved is not None
                else "calibration ECE unmeasured"
            )
            if healed:
                tag += (
                    f"  [back inside the {ECE_UNRELIABLE_THRESHOLD} unreliable bar "
                    f"({base_gate:.3f}->{cur_gate:.3f} midpoint)]"
                )
            findings.append(tag)

    # Ranking power: higher is better, and it is the only property worst-first
    # review depends on. An AUROC that goes undefined on one side is not compared —
    # "no wrong cells to rank" is not a change in ranking power.
    if enough(MIN_OBSERVATIONS_FOR_AUROC) and None not in (cc["auroc"], bc["auroc"]):
        delta = cc["auroc"] - bc["auroc"]
        crossed = cc["auroc"] <= AUROC_UNRELIABLE_THRESHOLD < bc["auroc"]
        healed = bc["auroc"] <= AUROC_UNRELIABLE_THRESHOLD < cc["auroc"]
        if regression and (delta <= -CALIBRATION_AUROC_REGRESSION or crossed):
            tag = f"confidence AUROC {delta:+.3f} {note(cc['auroc'], bc['auroc'])}"
            if crossed:
                tag += (
                    f"  [CROSSED the {AUROC_UNRELIABLE_THRESHOLD} chance bar — "
                    "confidence no longer ranks errors, so worst-first review is "
                    "not justified]"
                )
            findings.append(tag)
        elif not regression and (delta >= CALIBRATION_AUROC_REGRESSION or healed):
            findings.append(
                f"confidence AUROC {delta:+.3f} {note(cc['auroc'], bc['auroc'])}"
            )
    return findings


# Metrics whose comparison is skipped outright when the baseline predates them, and
# where to look for each. The point of naming them is that "skipped" and "unchanged"
# print identically otherwise, and a gate that cannot be distinguished from a passing
# gate is not a gate — the defect class this repository keeps rediscovering as "a
# control that exists but is never consulted". `conf_coverage` (#997) is in the same
# state as `calibration` (#935) for the same reason: both were added after the
# baseline was promoted.
BASELINE_ONLY_METRICS = {
    "calibration": "pooled confidence ECE / AUROC (#935)",
    "conf_coverage": "confidence coverage (#997)",
}


def _missing_metric_notes(cur, base):
    """Which comparisons this cell pair cannot make, and why.

    Reports only the case that matters — present on the current side, absent on the
    baseline — because that is the one where a reader would otherwise assume the gate
    looked and found nothing. A metric absent from BOTH sides is not yet collected
    anywhere and says nothing about the baseline.
    """
    notes = []
    for metric, label in BASELINE_ONLY_METRICS.items():
        cur_value = cur.get(metric)
        if metric == "conf_coverage":
            # A `_stats` block, so "collected" means it saw at least one observation.
            cur_present = bool(cur_value) and bool(cur_value.get("n"))
            base_present = bool(base.get(metric)) and bool(base[metric].get("n"))
        else:
            cur_present, base_present = bool(cur_value), bool(base.get(metric))
        if cur_present and not base_present:
            notes.append(label)
    return notes


def compare_cells(summary_path, baseline_path):
    """Variance-aware CELL-level comparison — the reliable way to detect a real
    cost/accuracy DIFFERENCE between releases (or, reused, between configs). A cost
    change is only flagged when the mean shift exceeds the combined sampling spread
    (max(stdev, 8% floor) of both sides), so single-sample agentic noise (which can
    swing ~4x) does not masquerade as a regression, and a genuine shift is caught."""
    cur_summary, base_summary = (
        json.load(open(summary_path)),
        json.load(open(baseline_path)),
    )
    cur = _cells(cur_summary)
    base = _cells(base_summary)
    paired = _paired_quality_deltas(cur_summary, base_summary)
    reg, imp, weak = [], [], []
    unread = {}
    for cell, c in cur.items():
        b = base.get(cell)
        if not b:
            continue
        for label in _missing_metric_notes(c, b):
            unread.setdefault(label, []).append(cell)
        cc, bc = c["cost"], b["cost"]
        if cc["mean"] is not None and bc["mean"] and bc["mean"] > 0:
            delta = cc["mean"] - bc["mean"]
            pct = 100 * delta / bc["mean"]
            # combined spread: stdevs (or an 8% floor when n<2 / stdev missing)
            spread = (
                (cc["stdev"] or bc["mean"] * 0.08) ** 2
                + (bc["stdev"] or bc["mean"] * 0.08) ** 2
            ) ** 0.5
            significant = abs(delta) > spread
            tag = (
                f"cost {pct:+.0f}% ({bc['mean']:.3f}±{bc['stdev'] or 0:.3f} n{bc['n']} "
                f"-> {cc['mean']:.3f}±{cc['stdev'] or 0:.3f} n{cc['n']})"
            )
            if not significant:
                if abs(pct) >= 15:
                    weak.append(
                        (cell, tag + "  [within noise — inconclusive, add repeats]")
                    )
            elif pct >= 15:
                reg.append((cell, tag))
            elif pct <= -15:
                imp.append((cell, tag))
        # Accuracy/recall at cell level — variance-aware, exactly like cost above.
        #
        # These used to be compared on the raw mean shift alone, so a
        # NON-DETERMINISTIC quality swing was promoted to a headline cell-level
        # regression/improvement on n=1 evidence. That bit for real at v0.6.5: the
        # integrated-confidence cell reads recall 0.10 or 1.00 on the SAME document
        # depending on the run, and the release A/B duly reported "recall
        # 0.700->1.000, CELL-LEVEL IMPROVEMENT" — in the direction that flattered
        # the release — until a 4x repeat showed the cell is simply bimodal.
        #
        # The test is PAIRED, not a comparison of the two sides' own spreads.
        # Both releases run the identical document set, so the per-document
        # differences remove document heterogeneity entirely — which matters here
        # because the corefast docs differ hugely (5 rows vs 100 rows), so the
        # across-document stdev is large even when every document moved the same
        # way. Comparing each side's own stdev would therefore mask a genuine
        # uniform shift; the spread of the paired DELTAS would not.
        for m, lbl in (
            ("scalar_accuracy", "acc"),
            ("completeness_recall", "recall"),
            ("weighted_accuracy", "wacc"),
        ):
            cm, bm = c[m]["mean"], b[m]["mean"]
            if cm is None or bm is None:
                continue
            d = cm - bm
            if abs(d) < 0.02:
                continue
            deltas = paired.get((cell, m), [])
            spread = _delta_spread(deltas)
            tag = (
                f"{lbl} {d:+.3f} ({bm:.3f}->{cm:.3f}); paired per-doc deltas "
                f"n={len(deltas)} spread±{spread:.3f}"
            )
            if abs(d) <= spread:
                weak.append(
                    (
                        cell,
                        tag
                        + "  [within run-to-run spread — inconclusive, add repeats]",
                    )
                )
            elif d < 0:
                reg.append((cell, tag))
            else:
                imp.append((cell, tag))
        # Calibration, pooled over the cell's documents and repeats (#935).
        reg.extend((cell, t) for t in calibration_findings(c, b, regression=True))
        imp.extend((cell, t) for t in calibration_findings(c, b, regression=False))
        # new systematic failures
        if b["n_fail"] == 0 and c["n_fail"] > 0:
            reg.append((cell, f"NEW FAILURES {c['n_fail']}/{c['n_runs']}"))
    print(f"\n=== CELL-LEVEL REGRESSIONS ({len(reg)}) ===")
    for cell, w in reg:
        print(f"  {cell}: {w}")
    print(f"\n=== CELL-LEVEL IMPROVEMENTS ({len(imp)}) ===")
    for cell, w in imp:
        print(f"  {cell}: {w}")
    if weak:
        print(
            f"\n=== INCONCLUSIVE (large % but within sampling noise) ({len(weak)}) ==="
        )
        for cell, w in weak:
            print(f"  {cell}: {w}")
    if unread:
        # NOT silent. A skipped comparison and a passing comparison print the same
        # thing otherwise, so a reader has no way to tell that a gate looked at
        # nothing — see BASELINE_ONLY_METRICS.
        print(f"\n=== NOT COMPARED — baseline predates the metric ({len(unread)}) ===")
        for label, cells in sorted(unread.items()):
            print(
                f"  {label}: current run has it, baseline does not, for "
                f"{len(cells)} cell(s) — this gate is INERT until the baseline is "
                "re-promoted from a grid scored with current code "
                f"(e.g. {', '.join(sorted(cells)[:3])})"
            )
    return reg, imp, weak


def _by_cell_doc(rows):
    """Group rows by ``(cell, doc)``, collapsing repeats.

    The repeat INDEX carries no identity — repeat 2 of one run is not "the same
    run" as repeat 2 of another, they are independent samples of the same
    (cell, doc). Pairing them by index (which ``compare`` used to do) throws away
    the only thing repeats buy you and, on a bimodal cell, reports a regression
    and an improvement from the same pair of runs depending on how the samples
    happened to land.
    """
    out = {}
    for r in rows:
        key = f"{r['cell']}|{r['doc']}"
        # A reference-corpus run contributes one row per document; pooling them
        # under the corpus would make "spread" the difference between two real
        # documents, not run-to-run noise. Pair per document, pool repeats only.
        if r.get("sub_doc"):
            key += f"|{r['sub_doc']}"
        out.setdefault(key, []).append(r)
    return out


def _mean(rows, metric, successes_only=True):
    src = [r for r in rows if r.get("success")] if successes_only else rows
    xs = [r.get(metric) for r in src]
    xs = [x for x in xs if isinstance(x, (int, float))]
    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)


def _spread(rows, metric):
    """Observed max-min of a metric within one (cell, doc) — the run-to-run noise
    floor measured on THIS side of the comparison. A delta smaller than the
    baseline's own spread is not evidence of a change."""
    xs = [
        r.get(metric)
        for r in rows
        if r.get("success") and isinstance(r.get(metric), (int, float))
    ]
    return (max(xs) - min(xs)) if len(xs) > 1 else 0.0


def compare(summary_path, baseline_path):
    """Compare two summaries per ``(cell, doc)``, aggregating over repeats.

    With ``repeats: 1`` this behaves exactly as the previous per-run comparison
    (mean == the single value, spread == 0). With repeats > 1 it stops reporting
    single-sample noise as a release regression — the concrete failure that
    motivated this: a one-off agentic failure and a 0.143 accuracy dip both
    appeared as regressions in a repeats=1 grid and neither reproduced.
    """
    cur = _by_cell_doc(json.load(open(summary_path))["rows"])
    base = _by_cell_doc(json.load(open(baseline_path))["rows"])
    regressions, improvements = [], []
    for k, cs in cur.items():
        bs = base.get(k)
        if not bs:
            continue

        # Failures: compare RATES, not "did this one run fail". A cell that fails
        # 1 in 3 on both sides is not a regression; 0/3 -> 3/3 is.
        c_fail = sum(1 for r in cs if not r.get("success"))
        b_fail = sum(1 for r in bs if not r.get("success"))
        if c_fail / len(cs) > b_fail / len(bs):
            statuses = sorted(
                {str(r.get("status")) for r in cs if not r.get("success")}
            )
            regressions.append(
                (
                    k,
                    f"FAILURE RATE {b_fail}/{len(bs)} -> {c_fail}/{len(cs)}"
                    + (
                        "  [both sides fail sometimes — confirm before believing]"
                        if b_fail
                        else ""
                    ),
                    f"{b_fail}/{len(bs)}",
                    f"{c_fail}/{len(cs)} {','.join(statuses)}",
                )
            )
        elif b_fail and not c_fail:
            improvements.append(
                (k, f"FAILURE RATE {b_fail}/{len(bs)} -> 0/{len(cs)}", b_fail, 0)
            )

        # Quality: mean-vs-mean, and require the delta to clear the noise the
        # baseline itself shows across its repeats.
        for m in ("completeness_recall", "scalar_accuracy", "weighted_accuracy"):
            cb, nb = _mean(bs, m)
            cc, nc = _mean(cs, m)
            if cb is None or cc is None:
                continue
            delta = cc - cb
            noise = max(_spread(bs, m), _spread(cs, m))
            if abs(delta) < 0.02:
                continue
            n_note = f" (n={nb}->{nc})" if max(nb, nc) > 1 else ""
            if abs(delta) <= noise:
                # Reported, but never as a verdict: this is exactly the shape of
                # the two findings that wasted a verification cycle.
                tag = (
                    f"{m} {delta:+.3f}{n_note}  [within run-to-run spread "
                    f"{noise:.3f} — INCONCLUSIVE, add repeats]"
                )
                print(f"  ~ {k}: {tag}")
                continue
            if delta <= -0.02:
                regressions.append((k, f"{m} {delta:+.3f}{n_note}", cb, cc))
            else:
                improvements.append((k, f"{m} {delta:+.3f}{n_note}", cb, cc))

        # Cost: mean-vs-mean. Agentic cost spreads ~4x run-to-run, so a single
        # sample cannot resolve a cost difference at all (see the `cost` suite).
        cb, nb = _mean(bs, "cost")
        cc, nc = _mean(cs, "cost")
        if cb and cc and cb > 0:
            rel = (cc - cb) / cb
            noise = max(_spread(bs, "cost"), _spread(cs, "cost")) / cb
            if rel >= 0.15:
                n_note = f" (n={nb}->{nc})" if max(nb, nc) > 1 else ""
                if rel <= noise:
                    print(
                        f"  ~ {k}: cost {100 * rel:+.0f}%{n_note}  [within spread "
                        f"{100 * noise:.0f}% — INCONCLUSIVE, add repeats]"
                    )
                else:
                    regressions.append(
                        (
                            k,
                            f"cost +{100 * rel:.0f}%{n_note}",
                            round(cb, 4),
                            round(cc, 4),
                        )
                    )

        # Calibration — field-level, then class-level. Same -0.03 threshold and
        # the same spread guard; a confidence that stops separating right from
        # wrong is a regression even when accuracy is unchanged, because
        # downstream escalation is driven by the score, not the accuracy.
        for metric, label in (
            ("calibration_separation", "calibration"),
            ("class_calibration_separation", "class calibration"),
        ):
            cb, nb = _mean(bs, metric)
            cc, nc = _mean(cs, metric)
            if cb is not None and cc is not None and cc - cb <= -0.03:
                noise = max(_spread(bs, metric), _spread(cs, metric))
                if abs(cc - cb) <= noise:
                    print(
                        f"  ~ {k}: {label} {cc - cb:+.3f}  [within spread "
                        f"{noise:.3f} — INCONCLUSIVE, add repeats]"
                    )
                else:
                    regressions.append((k, f"{label} {cc - cb:+.3f}", cb, cc))

    print(f"\n=== REGRESSIONS ({len(regressions)}) ===")
    for k, what, was, now in regressions:
        print(f"  {k}: {what}  ({was} -> {now})")
    print(f"\n=== IMPROVEMENTS ({len(improvements)}) ===")
    for k, what, was, now in improvements:
        print(f"  {k}: {what}  ({was} -> {now})")
    return regressions, improvements


CALIBRATION_GROUP_DEFAULT = ("assessment", "confidence_model")

# The metrics `augment_summary` re-derives. All three are computed from the section
# `result.json` files in S3 and from the local truth file — never from DynamoDB — so
# re-deriving them cannot disturb cost, tokens, latency or status, which come from
# metering rows whose table may no longer exist. That containment is the whole reason
# this is a targeted augmentation rather than a re-score.
AUGMENTED_METRICS = ("confidence coverage (#997)", "confidence calibration (#935)")


def augment_summary(path, corpus_dir, dry_run=False):
    """Add the S3-derived confidence metrics to an already-scored summary.

    Two metrics were added after most of the committed grids were scored, so their
    summaries carry neither: confidence coverage (#997) and confidence calibration
    (#935). Both are pure functions of data already in the output bucket, so they can
    be filled in retroactively — which is what makes the regression gate live against
    an existing `baseline.json` instead of shipping inert, and what puts the
    calibration sufficient statistic into the committed artifact so the published
    study survives the deletion of the stack it was measured on.

    Deliberately NOT a re-score. `score_doc` would also re-read the tracking table for
    metering and re-price it, so a stack whose DynamoDB table has been deleted would
    silently rewrite every cost in the file to zero, and a `pricing.yaml` that has
    moved since would rewrite them to different numbers. This touches only the keys in
    `AUGMENTED_METRICS` and recomputes `cell_stats`; every other value in every row is
    passed through untouched.

    Returns (rows_updated, rows_skipped). `meta.augmented` records what was added and
    when, so a summary's provenance still reads correctly afterwards: `scored_at` is
    when the run was scored, not when these two metrics were backfilled.
    """
    summary = json.load(open(path))
    stack = (summary.get("meta") or {}).get("stack")
    bucket = _resolve_output_bucket(stack)
    if not bucket:
        print(f"⚠ {path}: no output bucket for stack {stack!r} — cannot augment")
        return 0, len(summary.get("rows") or [])
    updated = 0
    reasons = {"not_success": 0, "no_sections": 0}
    unreadable = None
    for row in summary.get("rows") or []:
        if not row.get("run_id") or not row.get("success"):
            reasons["not_success"] += 1
            continue
        doc = row.get("sub_doc") or row.get("doc") or ""
        prefix = f"{row['run_id']}/{doc}/"
        sections = list(lib.iter_section_results(bucket, prefix))
        if not sections:
            reasons["no_sections"] += 1
            # A section that is LISTED but does not parse is a read failure, not an
            # absence, and `lib.get_json` returns None for both. Surface the
            # underlying error once: the case that prompted this was a stack whose KMS
            # key had entered pending-deletion, so every object was present, listable
            # and undecryptable — which without this reads as "this grid recorded no
            # confidence" and would be written into the artifact as exactly that.
            if unreadable is None:
                unreadable = _first_read_error(bucket, prefix)
            continue
        row.update(analyze.score_confidence_coverage(sections))
        truth = _truth_for(corpus_dir, row.get("doc") or "")
        row.update(
            analyze.score_calibration(
                sections,
                (truth or {}).get("rows_typed"),
                (truth or {}).get("list_key"),
            )
        )
        updated += 1
    skipped = sum(reasons.values())
    detail = ", ".join(f"{n} {why}" for why, n in reasons.items() if n)
    print(
        f"{path}: {updated} row(s) augmented, {skipped} skipped"
        + (f" ({detail})" if detail else "")
    )
    if unreadable:
        print(
            f"  ⚠ a section object under this grid could not be READ, not merely "
            f"found missing: {unreadable}. Its rows are recorded as un-augmented "
            "rather than as having no confidence."
        )
    if not updated:
        # Nothing to record. Writing `meta.augmented` here would claim a backfill that
        # did not happen, and rewriting the file for no change is pure diff noise.
        print(f"  {path} left untouched — no row could be augmented")
        return 0, skipped
    summary["cell_stats"] = cell_stats(summary.get("rows") or [])
    summary.setdefault("meta", {})["augmented"] = {
        "metrics": list(AUGMENTED_METRICS),
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rows_updated": updated,
        "rows_skipped": skipped,
        "rows_skipped_by_reason": {k: v for k, v in reasons.items() if v},
    }
    if dry_run:
        return updated, skipped
    json.dump(summary, open(path, "w"), indent=2)
    out_dir = os.path.dirname(path)
    if os.path.basename(path) == "summary.json":
        _write_summary_csvs(summary.get("rows") or [], summary["cell_stats"], out_dir)
    return updated, skipped


def _first_read_error(bucket, prefix):
    """The error behind an empty section list, or None if the prefix is genuinely empty.

    ``lib.get_json`` returns None for a missing object and for an unreadable one alike,
    which is convenient for scoring and indistinguishable for diagnosis.
    """
    try:
        listing = lib.s3().list_objects_v2(
            Bucket=bucket, Prefix=prefix + "sections/", MaxKeys=25
        )
    except Exception as exc:  # noqa: BLE001 - reporting, not handling
        return f"cannot list {prefix}sections/: {exc}"
    for obj in listing.get("Contents", []):
        if not obj["Key"].endswith("result.json"):
            continue
        try:
            lib.s3().get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            return f"{obj['Key']}: {exc}"
        return None
    return None


def _resolve_output_bucket(stack):
    """The stack's output bucket, by name prefix, as ``run_matrix.resolve_stack`` does.

    Imported here rather than at module scope: ``run_matrix`` pulls the launch path
    in with it, and scoring must not depend on being able to launch.
    """
    import run_matrix

    return run_matrix.resolve_stack(stack)["output_bucket"]


def _truth_for(corpus_dir, doc):
    path = os.path.join(corpus_dir, f"{doc}.truth.json")
    return json.load(open(path)) if os.path.exists(path) else None


def calibration_study(
    summary_paths, corpus_dir=None, group_by=None, out_path=None, from_s3=False
):
    """Pool confidence calibration across a scored grid, per configuration arm (#935).

    The release-level counterpart to the per-document figures ``score_synthetic``
    records. It exists because the question "can this confidence configuration
    support worst-first human review?" is not answerable per document: one document
    contributes a few hundred cells of which nearly all are correct, so its AUROC is
    usually undefined and its ECE is mostly a statement about that document's
    accuracy. Pooled across an arm there are tens of thousands of cells and the
    shipped observation floors (30 for calibration error, 100 for ranking power) are
    comfortably cleared.

    It re-reads the extraction output from S3 rather than reading the summary,
    because scoring is retroactive: the confidence values and the extracted cells
    are both already in the output bucket from the original run, so this costs S3
    GETs and no inference. That is why a calibration study can be run over a grid
    that completed months ago.

    ``group_by`` names keys of a row's ``resolved`` config, defaulting to the
    confidence mode and the grader model — the two axes that decide what confidence
    MEANS. Rows are pooled within an arm across documents and repeats.

    **It prefers the ``calibration_curve`` stored in the summary and only reads S3
    when a row has none.** That payload is an exact sufficient statistic for every
    figure reported here, so a grid whose stack has since been deleted stays
    reproducible from the committed artifact — which matters because three v0.6.x
    release stacks are already gone and, with them, any possibility of re-deriving
    their calibration at all. ``from_s3=True`` forces the re-read, which is what a
    freshly-completed grid needs and what verifies the stored statistic.

    Per-arm ``runs``, ``documents`` and ``excluded`` are reported, not just the grid
    total. The arms are NOT equally powered — the published study has arms at 112, 102
    and 58 runs over 7, 7 and 6 documents — so a reader given only a grid-level
    denominator would credit the thin arms with the thick ones' sample.
    """
    group_by = list(group_by or CALIBRATION_GROUP_DEFAULT)
    lib.s3()  # build the client once rather than per object read
    arms: dict[tuple, dict] = {}
    skipped = {"no_truth": 0, "no_observations": 0, "not_success": 0}

    def arm_for(resolved):
        key = tuple(str(resolved.get(k)) for k in group_by)
        return key, arms.setdefault(
            key,
            {
                "group": dict(zip(group_by, key)),
                "payloads": [],
                "docs": set(),
                "runs": 0,
                "excluded": {"not_success": 0, "no_truth": 0, "no_observations": 0},
                "sources": set(),
                "suites": set(),
                "stacks": set(),
            },
        )

    for path in summary_paths:
        summary = json.load(open(path))
        stack = (summary.get("meta") or {}).get("stack")
        label = os.path.basename(os.path.dirname(path))
        rows = summary.get("rows") or []
        # Only a row that could actually contribute forces an S3 read. Two rows
        # cannot: an unsuccessful run has nothing to read, and a row whose
        # `calibration_curve` key is PRESENT but null was already measured and had no
        # joinable cell (a cell with the confidence mode off, or a class with no list
        # attribute). Testing presence rather than truthiness is what separates
        # "measured, nothing to join" from "never measured" — reading them as the same
        # thing demanded AWS access for a grid entirely covered by its own artifact.
        missing = [
            r
            for r in rows
            if r.get("success") and r.get("run_id") and "calibration_curve" not in r
        ]
        bucket = None
        if from_s3 or missing:
            try:
                bucket = _resolve_output_bucket(stack)
            except Exception as exc:  # noqa: BLE001 - offline is a supported mode
                print(f"⚠ {path}: cannot reach S3 to resolve stack {stack!r}: {exc}")
            if not bucket:
                print(
                    f"⚠ {path}: no output bucket for stack {stack!r}; "
                    f"{len(missing)} of {len(rows)} row(s) carry no stored "
                    "calibration_curve and are counted as excluded"
                )
        for row in rows:
            _key, arm = arm_for(row.get("resolved") or {})
            arm["suites"].add(label)
            arm["stacks"].add(stack)
            if "calibration_curve" in row and not from_s3:
                stored = row["calibration_curve"]
                if stored:
                    arm["payloads"].append(stored)
                    arm["sources"].add("summary")
                    arm["docs"].add(row.get("doc"))
                    arm["runs"] += 1
                else:
                    # Measured and empty — not a failure and not unread.
                    skipped["no_observations"] += 1
                    arm["excluded"]["no_observations"] += 1
                continue
            if not row.get("success") or not row.get("run_id") or not bucket:
                skipped["not_success"] += 1
                arm["excluded"]["not_success"] += 1
                continue
            truth = _truth_for(corpus_dir, row.get("doc") or "")
            if not truth or not truth.get("rows_typed"):
                skipped["no_truth"] += 1
                arm["excluded"]["no_truth"] += 1
                continue
            sections = list(
                lib.iter_section_results(bucket, f"{row['run_id']}/{row['doc']}/")
            )
            scored = analyze.score_calibration(
                sections, truth.get("rows_typed"), truth.get("list_key")
            )
            if not scored["calibration_curve"]:
                skipped["no_observations"] += 1
                arm["excluded"]["no_observations"] += 1
                continue
            arm["payloads"].append(scored["calibration_curve"])
            arm["sources"].add("s3")
            arm["docs"].add(row["doc"])
            arm["runs"] += 1

    report = {"group_by": group_by, "skipped": skipped, "arms": []}
    for _key, arm in sorted(arms.items()):
        pooled = analyze.pool_calibration(arm["payloads"])
        if not pooled:
            continue
        report["arms"].append(
            {
                **arm["group"],
                "runs": arm["runs"],
                "documents": sorted(d for d in arm["docs"] if d),
                "excluded": dict(arm["excluded"]),
                "excluded_total": sum(arm["excluded"].values()),
                "read_from": sorted(arm["sources"]),
                "suites": sorted(arm["suites"]),
                "stacks": sorted(arm["stacks"]),
                **pooled,
            }
        )
    _print_calibration(report)
    if out_path:
        json.dump(report, open(out_path, "w"), indent=2)
        print(f"\ncalibration report -> {out_path}")
    return report


def _print_calibration(report):
    cols = " / ".join(report["group_by"])
    print(f"\n=== CONFIDENCE CALIBRATION ({cols}) ===")
    # `errs` is the count of WRONG cells, and it is the column that bounds how
    # precisely AUROC can be known: ranking power is estimated over
    # errs x correct pairs, so an arm with 70,000 cells and 40 errors is a
    # 40-observation measurement of discrimination however large the cell count
    # looks. Printed next to AUROC for that reason.
    # `runs`, `docs` and `excl` are per ARM, not per grid. The arms are not equally
    # powered and a grid-level denominator would credit the thin ones with the thick
    # ones' sample; `excl` is the runs that contributed nothing, so the surviving
    # runs' conditioning on success is visible rather than implied.
    header = (
        f"{'arm':34s} {'runs':>5s} {'docs':>5s} {'excl':>5s} {'cells':>7s} {'errs':>5s} "
        f"{'acc':>6s} {'ECE':>6s} {'ECEmc':>6s} {'AUROC':>6s} {'AUROCu':>7s} "
        f"{'Brier':>6s} {'bins':>4s} verdict"
    )
    print(header)

    def fmt(value, width=6):
        # A dash, not 0.000: an AUROC is None when one class is absent, and printing
        # a zero there would read as "ranks perfectly badly" rather than "unmeasured".
        return (
            f"{value:>{width}.3f}" if isinstance(value, float) else f"{'-':>{width}s}"
        )

    for arm in report["arms"]:
        name = "|".join(str(arm[k]) for k in report["group_by"])
        verdict = []
        if arm["degenerate"]:
            verdict.append("DEGENERATE")
        if arm["overconfident"]:
            verdict.append("OVERCONFIDENT")
        if arm["undiscriminating"]:
            verdict.append("UNDISCRIMINATING")
        print(
            f"{name:34s} {arm['runs']:>5d} {len(arm['documents']):>5d} "
            f"{arm.get('excluded_total', 0):>5d} {arm['observations']:>7d} "
            f"{arm['observations'] - arm['correct']:>5d} "
            f"{fmt(arm['accuracy'])} {fmt(arm['ece'])} {fmt(arm['ece_mean_conf'])} "
            f"{fmt(arm['auroc'])} {fmt(arm['auroc_unbinned'], 7)} {fmt(arm['brier'])} "
            f"{arm['bin_coverage']:>4d} {','.join(verdict) or 'reliable'}"
        )
    s = report["skipped"]
    print(
        f"skipped: {s['not_success']} unsuccessful, {s['no_truth']} without exact "
        f"per-cell truth, {s['no_observations']} with no joinable confidence"
    )
    if s["no_observations"]:
        # Said plainly because the instruction "chase a number materially above zero
        # here" otherwise fires on its own benign output: a cell configured with
        # `confidence.mode: off` produces no confidence at all and is counted here.
        print(
            "  (a run with the confidence mode OFF has no confidence leaves and lands "
            "in the third bucket — expect one per off-cell per document. Chase it only "
            "when it exceeds the off-cells in the grid, which would mean a run's S3 "
            "prefix is gone or its assessment failed)"
        )


def reliability_figure(report, out_dir=None):
    """Reliability diagram per arm: observed accuracy against mean confidence.

    Plotted against each bin's MEAN CONFIDENCE, not the bin midpoint, so the
    diagonal is the real "perfectly calibrated" line for these observations. Bin
    markers are sized by how many cells they hold, because on this corpus the mass
    is overwhelmingly in the top bin and an unweighted diagram invites reading a
    3-cell bin as a finding. The Wilson bounds on each bin's accuracy are drawn for
    the same reason.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("(matplotlib not installed — skipping the reliability diagram)")
        return None
    arms = [a for a in report.get("arms") or [] if a.get("bins")]
    if not arms:
        return None
    out_dir = out_dir or os.path.join(BENCH, "paper", "figures")
    os.makedirs(out_dir, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.plot(
        [0, 1], [0, 1], "--", color="#888", linewidth=1, label="perfect calibration"
    )
    for arm in arms:
        xs, ys, sizes, lows, highs = [], [], [], [], []
        for b in arm["bins"]:
            if not b["observations"] or b["meanConfidence"] is None:
                continue
            xs.append(b["meanConfidence"])
            ys.append(b["observedAccuracy"])
            sizes.append(b["observations"])
            # Clamped at 0: a Wilson interval is centred on a shrunk estimate, not
            # on p, so at p=1.0 the upper bound sits BELOW the point and a raw
            # subtraction goes negative. The bounds are authoritative; the error bar
            # is a drawing of them.
            lows.append(
                max(0.0, b["observedAccuracy"] - (b["observedAccuracyLow"] or 0))
            )
            highs.append(
                max(0.0, (b["observedAccuracyHigh"] or 0) - b["observedAccuracy"])
            )
        if not xs:
            continue
        name = "|".join(str(arm[k]) for k in report["group_by"])
        biggest = max(sizes)
        ax.errorbar(xs, ys, yerr=[lows, highs], fmt="none", ecolor="#bbb", elinewidth=1)
        ax.scatter(
            xs,
            ys,
            s=[30 + 220 * (n / biggest) for n in sizes],
            alpha=0.7,
            label=f"{name} (n={arm['observations']})",
        )
    ax.set_xlabel("mean confidence in bin")
    ax.set_ylabel("observed cell accuracy")
    ax.set_title("Confidence reliability, per configuration arm")
    ax.set_xlim(0, 1.02)
    ax.set_ylim(0, 1.02)
    ax.legend(fontsize=8, loc="lower right")
    path = os.path.join(out_dir, "reliability-diagram.png")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    print(f"reliability diagram -> {path}")
    return path


def figures(summary_path):
    """Emit charts if matplotlib available; else skip gracefully."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("matplotlib not available; skipping figures")
        return
    rows = json.load(open(summary_path))["rows"]
    figdir = os.path.join(BENCH, "paper", "figures")
    os.makedirs(figdir, exist_ok=True)
    # scaling: completeness + cost vs rows, by mode (if scaling docs present)
    scaling = [r for r in rows if r.get("rows_truth")]
    if scaling:
        by_mode = {}
        for r in scaling:
            mode = r.get("resolved", {}).get("extraction_mode", "?")
            by_mode.setdefault(mode, []).append(r)
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
        for mode, rs in by_mode.items():
            rs = sorted(rs, key=lambda x: x.get("rows_truth") or 0)
            xs = [r["rows_truth"] for r in rs]
            ax1.plot(xs, [r.get("completeness_recall") for r in rs], "o-", label=mode)
            ax2.plot(xs, [r.get("cost") for r in rs], "o-", label=mode)
        ax1.set(
            xlabel="rows", ylabel="completeness recall", title="Completeness vs size"
        )
        ax2.set(xlabel="rows", ylabel="cost $/doc", title="Cost vs size")
        for ax in (ax1, ax2):
            ax.legend()
            ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(figdir, "scaling.png"), dpi=120)
        print(f"figures -> {figdir}/scaling.png")


def figures_compare(new_path, base_path, new_label="new", base_label="baseline"):
    """Emit the two release-A/B charts: per-cell cost, and paired accuracy/recall.

    The release audit trail cites these, but they used to be produced ad hoc — so
    the "every number here comes from the harness" claim did not extend to the
    figures. Both are computed from the same two summary.json files the prose
    tables are computed from.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("matplotlib not available; skipping figures")
        return
    new, base = json.load(open(new_path)), json.load(open(base_path))
    figdir = os.path.join(BENCH, "paper", "figures")
    os.makedirs(figdir, exist_ok=True)
    cn, cb = _cells(new), _cells(base)
    cells = [c for c in cb if c in cn]
    if not cells:
        print("no shared cells; skipping compare figures")
        return
    short = [c.replace("core-", "") for c in cells]
    idx = range(len(cells))
    w = 0.38

    def mean(cs, cell, key):
        v = cs[cell].get(key)
        return (v.get("mean") if isinstance(v, dict) else v) or 0

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.bar(
        [i - w / 2 for i in idx],
        [mean(cb, c, "cost") for c in cells],
        w,
        label=base_label,
    )
    ax.bar(
        [i + w / 2 for i in idx],
        [mean(cn, c, "cost") for c in cells],
        w,
        label=new_label,
    )
    ax.set(
        ylabel="cost $/doc (mean over docs)",
        title=f"Cost per config cell — {base_label} vs {new_label}",
    )
    ax.set_xticks(list(idx))
    ax.set_xticklabels(short, rotation=30, ha="right")
    ax.legend()
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    cost_png = os.path.join(figdir, "version_cost_compare.png")
    fig.savefig(cost_png, dpi=120)

    # Paired per-(cell,doc) accuracy + recall: a scatter on the identity line, so
    # any point off the diagonal is a real per-run change rather than an average.
    rn = {(r["cell"], r["doc"], r.get("sub_doc")): r for r in new["rows"]}
    rb = {(r["cell"], r["doc"], r.get("sub_doc")): r for r in base["rows"]}
    keys = [k for k in rb if k in rn]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, key, title in (
        (ax1, "scalar_accuracy", "Scalar accuracy"),
        (ax2, "completeness_recall", "Completeness recall"),
    ):
        xs = [rb[k].get(key) or 0 for k in keys]
        ys = [rn[k].get(key) or 0 for k in keys]
        ax.scatter(xs, ys, alpha=0.65)
        ax.plot([0, 1], [0, 1], "--", color="gray", linewidth=1)
        ax.set(
            xlabel=f"{base_label}",
            ylabel=f"{new_label}",
            title=f"{title} (paired, n={len(keys)})",
        )
        ax.set_xlim(-0.05, 1.05)
        ax.set_ylim(-0.05, 1.05)
        ax.grid(True, alpha=0.3)
    fig.tight_layout()
    acc_png = os.path.join(figdir, "version_accuracy_compare.png")
    fig.savefig(acc_png, dpi=120)
    print(f"figures -> {cost_png}\nfigures -> {acc_png}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="results/run-XXXX dir to score")
    ap.add_argument("--out", help="output release dir")
    ap.add_argument("--compare", help="summary.json to compare")
    ap.add_argument("--baseline", help="baseline.json")
    ap.add_argument("--figures", help="summary.json to chart")
    ap.add_argument(
        "--figures-compare",
        nargs=2,
        metavar=("NEW_SUMMARY", "BASE_SUMMARY"),
        help="emit the release-A/B cost + paired-accuracy charts from two summary.json files",
    )
    ap.add_argument(
        "--labels",
        nargs=2,
        metavar=("NEW_LABEL", "BASE_LABEL"),
        default=["new", "baseline"],
        help="legend labels for --figures-compare (default: new baseline)",
    )
    ap.add_argument(
        "--cost-var", help="summary.json: print per-cell cost mean±stdev+CV"
    )
    ap.add_argument(
        "--calibration",
        nargs="+",
        metavar="SUMMARY",
        help="pool confidence calibration (ECE/Brier/AUROC vs exact per-cell truth) "
        "across one or more scored summary.json files, per configuration arm",
    )
    ap.add_argument(
        "--calibration-group",
        default=",".join(CALIBRATION_GROUP_DEFAULT),
        help="comma-separated `resolved` config keys to pool by "
        f"(default: {','.join(CALIBRATION_GROUP_DEFAULT)})",
    )
    ap.add_argument(
        "--corpus",
        default=os.path.join(BENCH, "corpus", "docs"),
        help="directory holding <doc>.truth.json (default: benchmarks/corpus/docs)",
    )
    ap.add_argument("--calibration-out", help="write the calibration report JSON here")
    ap.add_argument(
        "--calibration-from-s3",
        action="store_true",
        help="re-read every run from S3 instead of using the stored calibration_curve "
        "(slower; verifies the stored sufficient statistic)",
    )
    ap.add_argument(
        "--augment",
        nargs="+",
        metavar="SUMMARY",
        help="backfill the S3-derived confidence metrics (coverage #997, calibration "
        "#935) into already-scored summary.json files, in place. Touches nothing "
        "priced from DynamoDB metering",
    )
    a = ap.parse_args()
    if a.run:
        rm, rows = score_all(a.run)
        write_summary(rm, rows, a.out or a.run)
    if a.compare and a.baseline:
        compare(a.compare, a.baseline)  # per-(cell,doc) rows
        compare_cells(a.compare, a.baseline)  # variance-aware cell level
    if a.augment:
        for path in a.augment:
            augment_summary(path, a.corpus)
    if a.calibration:
        report = calibration_study(
            a.calibration,
            a.corpus,
            group_by=[k for k in a.calibration_group.split(",") if k],
            out_path=a.calibration_out,
            from_s3=a.calibration_from_s3,
        )
        reliability_figure(report)
    if a.figures:
        figures(a.figures)
    if a.figures_compare:
        figures_compare(
            *a.figures_compare, new_label=a.labels[0], base_label=a.labels[1]
        )
    if a.cost_var:
        cs = _cells(json.load(open(a.cost_var)))
        print(
            f"{'cell':26s} {'n':>3s} {'cost_mean':>9s} {'stdev':>7s} {'CV':>6s} {'min':>7s} {'max':>7s}"
        )
        for cell, s in sorted(cs.items(), key=lambda kv: -(kv[1]["cost"]["mean"] or 0)):
            c = s["cost"]
            flag = "  <<noisy" if (c["cv"] or 0) > 0.25 else ""
            print(
                f"{cell:26s} {c['n']:>3d} {str(c['mean']):>9s} {str(c['stdev']):>7s} "
                f"{str(c['cv']):>6s} {str(c['min']):>7s} {str(c['max']):>7s}{flag}"
            )


if __name__ == "__main__":
    main()
