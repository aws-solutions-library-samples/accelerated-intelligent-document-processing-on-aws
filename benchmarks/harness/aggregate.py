#!/usr/bin/env python3
"""Score every run in a runmap, roll into summary tables, compare to a baseline.

Usage:
  AWS_PROFILE=default python3 aggregate.py --run results/run-XXXX --out results/<release>
  python3 aggregate.py --compare results/<release>/summary.json --baseline results/baseline.json
  python3 aggregate.py --figures results/<release>/summary.json   # emit charts

Writes summary.json (per (cell,doc) full scores) + summary.csv (+ meta.json).
Regression thresholds: accuracy -0.02, cost +15%, any new failure, calibration -0.03.
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


def score_all(run_dir):
    rm = json.load(open(os.path.join(run_dir, "runmap.json")))
    res = rm["resources"]
    rows = []
    for r in rm["runs"]:
        if not r.get("run_id"):
            rows.append({**_key(r), "status": "NOT_LAUNCHED", "success": False})
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
        rows.append({**_key(r), **sc})
    return rm, rows


def _key(r):
    return {
        "cell": r["cell"],
        "doc": r["doc"],
        "repeat": r.get("repeat", 0),
        "resolved": r.get("resolved", {}),
        "run_id": r.get("run_id"),
    }


CSV_COLS = [
    "cell",
    "doc",
    "repeat",
    "status",
    "success",
    "page_count",
    "completeness_recall",
    "truncation_prefix",
    "scalar_accuracy",
    "weighted_accuracy",
    "parse_failures",
    "mean_confidence",
    "pct_conf_below_0.9",
    "calibration_separation",
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
            "weighted_accuracy": _stats([r.get("weighted_accuracy") for r in succ]),
            "wall_s": _stats([r.get("wall_s") for r in succ]),
        }
    return out


def write_summary(rm, rows, out):
    os.makedirs(out, exist_ok=True)
    cells = cell_stats(rows)
    json.dump(
        {"meta": _meta(rm), "rows": rows, "cell_stats": cells},
        open(os.path.join(out, "summary.json"), "w"),
        indent=2,
    )
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


def compare_cells(summary_path, baseline_path):
    """Variance-aware CELL-level comparison — the reliable way to detect a real
    cost/accuracy DIFFERENCE between releases (or, reused, between configs). A cost
    change is only flagged when the mean shift exceeds the combined sampling spread
    (max(stdev, 8% floor) of both sides), so single-sample agentic noise (which can
    swing ~4x) does not masquerade as a regression, and a genuine shift is caught."""
    cur = _cells(json.load(open(summary_path)))
    base = _cells(json.load(open(baseline_path)))
    reg, imp, weak = [], [], []
    for cell, c in cur.items():
        b = base.get(cell)
        if not b:
            continue
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
        # accuracy/recall at cell level (mean shift beyond 0.02)
        for m, lbl in (
            ("scalar_accuracy", "acc"),
            ("completeness_recall", "recall"),
            ("weighted_accuracy", "wacc"),
        ):
            cm, bm = c[m]["mean"], b[m]["mean"]
            if cm is not None and bm is not None:
                d = cm - bm
                if d <= -0.02:
                    reg.append((cell, f"{lbl} {d:+.3f} ({bm:.3f}->{cm:.3f})"))
                elif d >= 0.02:
                    imp.append((cell, f"{lbl} {d:+.3f} ({bm:.3f}->{cm:.3f})"))
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
    return reg, imp, weak


def compare(summary_path, baseline_path):
    cur = {(_id(r)): r for r in json.load(open(summary_path))["rows"]}
    base = {(_id(r)): r for r in json.load(open(baseline_path))["rows"]}
    regressions, improvements = [], []
    for k, c in cur.items():
        b = base.get(k)
        if not b:
            continue
        # new failure
        if b.get("success") and not c.get("success"):
            regressions.append((k, "NEW FAILURE", b.get("status"), c.get("status")))
        # accuracy
        for m in ("completeness_recall", "scalar_accuracy", "weighted_accuracy"):
            cb, cc = b.get(m), c.get(m)
            if isinstance(cb, (int, float)) and isinstance(cc, (int, float)):
                if cc - cb <= -0.02:
                    regressions.append((k, f"{m} -{cb - cc:.3f}", cb, cc))
                elif cc - cb >= 0.02:
                    improvements.append((k, f"{m} +{cc - cb:.3f}", cb, cc))
        # cost
        cb, cc = b.get("cost"), c.get("cost")
        if isinstance(cb, (int, float)) and cb > 0 and isinstance(cc, (int, float)):
            if (cc - cb) / cb >= 0.15:
                regressions.append((k, f"cost +{100 * (cc - cb) / cb:.0f}%", cb, cc))
        # calibration
        cb, cc = b.get("calibration_separation"), c.get("calibration_separation")
        if (
            isinstance(cb, (int, float))
            and isinstance(cc, (int, float))
            and cc - cb <= -0.03
        ):
            regressions.append((k, f"calibration -{cb - cc:.3f}", cb, cc))
    print(f"\n=== REGRESSIONS ({len(regressions)}) ===")
    for k, what, was, now in regressions:
        print(f"  {k}: {what}  ({was} -> {now})")
    print(f"\n=== IMPROVEMENTS ({len(improvements)}) ===")
    for k, what, was, now in improvements:
        print(f"  {k}: {what}  ({was} -> {now})")
    return regressions, improvements


def _id(r):
    return f"{r['cell']}|{r['doc']}|{r.get('repeat', 0)}"


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
    rn = {(r["cell"], r["doc"]): r for r in new["rows"]}
    rb = {(r["cell"], r["doc"]): r for r in base["rows"]}
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
    a = ap.parse_args()
    if a.run:
        rm, rows = score_all(a.run)
        write_summary(rm, rows, a.out or a.run)
    if a.compare and a.baseline:
        compare(a.compare, a.baseline)  # per-(cell,doc) rows
        compare_cells(a.compare, a.baseline)  # variance-aware cell level
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
