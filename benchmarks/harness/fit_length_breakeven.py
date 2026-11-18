"""Where does holistic classification stop beating page by page, by packet length?

Pairs each packet's section accuracy under two values of SC-14 (mean over the runs that
scored it under each), and fits the paired difference against the packet's page count,
on the short (DocSplit, 5-20 pages) and long (30-60 pages) sets together. Writes the
fit, a bootstrap interval for where the line crosses zero, the difference per length
band, and each packet's difference with the two section accuracies it is taken from
(`a_score`, `b_score`) to a JSON file the page's charts read.

It also pairs every other packet-splitting metric of the packets' evaluations
(METRICS: grouping score, boundary accuracy, V-measure, Rand index, ordering score,
page and label accuracy, with section accuracy among them). Each packet's figure is
its mean over the runs that scored it, and `metrics` gives, per length set:

    a_minus_b   holistic minus page by page, over the packets both scored
    c_minus_a   split then classify (C) minus holistic, over the packets both scored
    means       each value's mean over its packets

each as a mean, a standard error across packets and n. Each point carries its
packets' per-metric figures (`metrics`: {"a": {...}, "b": {...}, "c": {...}}, "c"
only where C scored the packet in two runs or more).

    python3 benchmarks/harness/fit_length_breakeven.py [manifest] --out <json>
"""

import argparse
import json
import random
import statistics as st
from pathlib import Path

from collect_guidance_results import DOC_EVAL_CACHE as W

ap = argparse.ArgumentParser()
ap.add_argument(
    "manifest",
    nargs="?",
    default=str(Path(__file__).parents[1] / "results/guidance/manifests/SC-14.json"),
)
ap.add_argument("--out", required=True)
cli = ap.parse_args()
M = json.load(open(cli.manifest))["runs"]
A, B = "Holistic · Claude Haiku 5.5", "Page by page · Claude Haiku 5.5"
C = "Split, then an LLM classifies each section · Claude Haiku 5.5"
#: The packet-splitting metrics of a packet's evaluation (doc_split_metrics) paired here.
METRICS = (
    "split_accuracy_without_order",
    "final_score",
    "boundary_accuracy",
    "v_measure",
    "rand_index",
    "avg_ordering_score",
    "page_level_accuracy",
    "label_accuracy",
)
FULL: dict[str, dict[str, list[dict]]] = {}  # run set -> packet -> each run's metrics


def per_packet(run_ids):
    acc = {}
    full = FULL.setdefault("|".join(run_ids), {})
    for rid in run_ids:
        for f in (W / rid).glob("*/evaluation/results.json"):
            m = json.load(open(f)).get("doc_split_metrics") or {}
            if not m.get("total_splits"):
                continue
            acc.setdefault(f.parts[-3], []).append(
                (
                    m["correctly_split_without_order"],
                    m["total_splits"],
                    m["total_pages"],
                )
            )
            full.setdefault(f.parts[-3], []).append(m)
    return acc


def packet_metrics(run_ids, doc):
    """A packet's mean over its runs of each metric a run reported (None skipped)."""
    runs = FULL["|".join(run_ids)].get(doc) or []
    out = {}
    for k in METRICS:
        vals = [float(m[k]) for m in runs if m.get(k) is not None]
        if vals:
            out[k] = st.fmean(vals)
    return out


def mean_se(xs):
    return {
        "mean": st.fmean(xs),
        "se": st.stdev(xs) / len(xs) ** 0.5 if len(xs) > 1 else None,
        "n": len(xs),
    }


rows = []
scores = []  # each row's per-metric figures, by value
paired = {"a_minus_b": {}, "c_minus_a": {}, "means": {"a": {}, "b": {}, "c": {}}}
for ds in ("docsplit_100", "docsplit_long"):
    a, b = per_packet(M[ds][A]), per_packet(M[ds][B])
    c = per_packet(M[ds][C]) if C in M[ds] else {}
    for doc in sorted(set(a) & set(b)):
        if len(a[doc]) < 2 or len(b[doc]) < 2:
            continue
        sa = st.fmean(c_ / t for c_, t, _ in a[doc])
        sb = st.fmean(c_ / t for c_, t, _ in b[doc])
        rows.append((a[doc][0][2], a[doc][0][1], sa - sb, ds, sa, sb))
        found = {"a": packet_metrics(M[ds][A], doc), "b": packet_metrics(M[ds][B], doc)}
        if len(c.get(doc) or []) >= 2:
            found["c"] = packet_metrics(M[ds][C], doc)
        scores.append(found)
    mine = [(r, s) for r, s in zip(rows, scores, strict=True) if r[3] == ds]
    for pair, (x, y) in (("a_minus_b", ("a", "b")), ("c_minus_a", ("c", "a"))):
        paired[pair][ds] = {
            k: mean_se(d)
            for k in METRICS
            if len(
                d := [
                    s[x][k] - s[y][k]
                    for _, s in mine
                    if x in s and k in s[x] and k in s[y]
                ]
            )
            > 1
        }
    for v in ("a", "b", "c"):
        paired["means"][v][ds] = {
            k: mean_se(vals)
            for k in METRICS
            if len(vals := [s[v][k] for _, s in mine if v in s and k in s[v]]) > 1
        }
print(
    "packets:",
    len(rows),
    {
        ds: sum(1 for r in rows if r[3] == ds)
        for ds in ("docsplit_100", "docsplit_long")
    },
)


def ols(pts):
    xs = [p[0] for p in pts]
    ys = [p[2] for p in pts]
    mx, my = st.fmean(xs), st.fmean(ys)
    b1 = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum(
        (x - mx) ** 2 for x in xs
    )
    return my - b1 * mx, b1


b0, b1 = ols(rows)
print(
    f"linear: delta = {b0:.3f} + {b1:.4f} * pages; crossing at {(-b0 / b1) if b1 else float('nan'):.1f} pages"
)
random.seed(7)
cross = []
for _ in range(2000):
    s = [random.choice(rows) for _ in rows]
    c0, c1 = ols(s)
    if c1 < 0:
        cross.append(-c0 / c1)
cross.sort()
print(
    f"bootstrap crossing (n={len(cross)} of 2000 with negative slope): median {cross[len(cross) // 2]:.1f}, 95% CI {cross[int(0.025 * len(cross))]:.1f}–{cross[int(0.975 * len(cross))]:.1f}"
)
bands = []
for lo, hi in ((5, 10), (11, 15), (16, 20), (30, 37), (38, 45), (46, 53), (54, 60)):
    d = [r[2] for r in rows if lo <= r[0] <= hi]
    if d:
        se = st.stdev(d) / len(d) ** 0.5 if len(d) > 1 else float("nan")
        bands.append({"pages": [lo, hi], "n": len(d), "mean": st.fmean(d), "se": se})
        print(
            f"  {lo:>2}-{hi:<2} pages: n={len(d):>3}  holistic - page by page = {st.fmean(d):+.3f} ± {2 * se:.3f} (2 SE)"
        )
Path(cli.out).write_text(
    json.dumps(
        {
            "a": A,
            "b": B,
            "c": C,
            "packets": len(rows),
            "line": {
                "intercept": b0,
                "slope": b1,
                "crossing_pages": (-b0 / b1) if b1 else None,
                "crossing_ci95": [
                    cross[int(0.025 * len(cross))],
                    cross[int(0.975 * len(cross))],
                ]
                if cross
                else None,
                "negative_slope_share": len(cross) / 2000,
            },
            "bands": bands,
            "metrics": {"keys": list(METRICS), **paired},
            "points": [
                {
                    "pages": r[0],
                    "sections": r[1],
                    "delta": r[2],
                    "dataset": r[3],
                    "a_score": r[4],
                    "b_score": r[5],
                    "metrics": s,
                }
                for r, s in zip(rows, scores, strict=True)
            ],
        },
        indent=1,
    )
    + "\n"
)
