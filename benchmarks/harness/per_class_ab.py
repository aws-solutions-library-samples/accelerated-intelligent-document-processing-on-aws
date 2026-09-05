#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Paired A/B broken out PER DOCUMENT CLASS, for a treatment that applies to some.

``real_corpus_ab.py`` reports the corpus mean. That is the right summary for a
config-wide toggle, and the wrong one for a treatment applied to a subset of classes:
a change confined to 2 of 9 classes (41 of 293 documents) is diluted ~7x in the mean,
so a real effect on the treated classes and a real regression on the untreated ones can
both hide inside "no significant difference".

Used for the cache-padding study, where the treated classes are exactly those whose
prompt prefix falls below the model's minimum cacheable prefix — so the treated set is
small by construction and the untreated classes are the control.

Class is taken from the document's own section results, not from the filename, so it
reflects what the pipeline actually classified it as.

    AWS_PROFILE=default python3 per_class_ab.py --stack IDPBench \\
        --arm-a <baselineRunId> --arm-b <treatedRunId> \\
        --treated GLOSSARY --treated SHIFT_SCHEDULE
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
from math import comb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cache_audit  # noqa: E402
import real_corpus_ab as rc  # noqa: E402


def _sign_p(better, worse):
    n = better + worse
    if n == 0:
        return 1.0
    k = min(better, worse)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / (2**n))


def _paired(deltas):
    if len(deltas) < 2:
        return None
    mean = statistics.fmean(deltas)
    sd = statistics.stdev(deltas)
    se = sd / (len(deltas) ** 0.5)
    return mean, sd, (mean / se if se else None), len(deltas)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", required=True)
    ap.add_argument("--arm-a", required=True, help="baseline runId")
    ap.add_argument("--arm-b", required=True, help="treated runId")
    ap.add_argument("--treated", action="append", default=[],
                    help="class name the treatment applies to; repeat")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    bucket = rc._find(a.stack, "outputbucket")
    tracking = cache_audit._find_table(a.stack, "TrackingTable")

    rows_a = {r["doc"].split("/", 1)[1]: r for r in cache_audit.audit_run(
        {"output_bucket": bucket, "tracking_table": tracking}, a.arm_a)
        if r["phase"].lower().startswith("extract") and r["model"] != "lambda"}
    rows_b = {r["doc"].split("/", 1)[1]: r for r in cache_audit.audit_run(
        {"output_bucket": bucket, "tracking_table": tracking}, a.arm_b)
        if r["phase"].lower().startswith("extract") and r["model"] != "lambda"}

    items_a = rc._docs_of_run(tracking, a.arm_a)
    items_b = rc._docs_of_run(tracking, a.arm_b)
    shared = sorted(set(items_a) & set(items_b))

    class Acc:
        """Per-class accumulator. A plain dict mixing lists and counters type-checks
        badly and reads worse; this keeps the two kinds of field apart."""

        def __init__(self):
            self.acc: list[float] = []
            self.cost: list[float] = []
            self.cr: list[float] = []
            self.inp: list[float] = []
            self.better = self.worse = self.same = self.n = 0

    by_class: dict[str, Acc] = collections.defaultdict(Acc)
    for doc in shared:
        ia, ib = items_a[doc], items_b[doc]
        if ia.get("ObjectStatus", {}).get("S") == "FAILED" or ib.get("ObjectStatus", {}).get("S") == "FAILED":
            continue
        cls = (rows_a.get(doc, {}).get("classes") or ["(unknown)"])[0]
        g = by_class[cls]
        g.n += 1
        sa = rc._score(bucket, a.arm_a, doc)
        sb = rc._score(bucket, a.arm_b, doc)
        if sa is not None and sb is not None:
            g.acc.append(sb - sa)
            if sb > sa:
                g.better += 1
            elif sb < sa:
                g.worse += 1
            else:
                g.same += 1
        g.cost.append(rc._cost(ib) - rc._cost(ia))
        ra, rb = rows_a.get(doc), rows_b.get(doc)
        if ra and rb:
            g.cr.append(rb["cacheReadInputTokens"] - ra["cacheReadInputTokens"])
            g.inp.append(rb["inputTokens"] - ra["inputTokens"])

    treated = set(a.treated)
    print(f"\narm A (baseline) {a.arm_a}\narm B (treated)  {a.arm_b}")
    print(f"paired non-failed documents: {sum(g.n for g in by_class.values())}\n")
    print(f"{'class':28} {'grp':>4} {'n':>4} {'acc Δ':>9} {'t':>6} {'sign p':>7} "
          f"{'cost Δ':>10} {'cost t':>7} {'cRead Δ':>9} {'input Δ':>9}")
    out = {}
    for cls, g in sorted(by_class.items(), key=lambda kv: (kv[0] not in treated, kv[0])):
        grp = "TREAT" if cls in treated else "ctrl"
        acc = _paired(g.acc)
        cost = _paired(g.cost)
        cr = statistics.fmean(g.cr) if g.cr else 0
        inp = statistics.fmean(g.inp) if g.inp else 0
        print(f"{cls[:27]:28} {grp:>4} {g.n:>4} "
              f"{(f'{acc[0]:+.4f}' if acc else '—'):>9} "
              f"{(f'{acc[2]:+.2f}' if acc and acc[2] is not None else '—'):>6} "
              f"{_sign_p(g.better, g.worse):>7.3f} "
              f"{(f'{cost[0]:+.5f}' if cost else '—'):>10} "
              f"{(f'{cost[2]:+.2f}' if cost and cost[2] is not None else '—'):>7} "
              f"{cr:>+9,.0f} {inp:>+9,.0f}")
        out[cls] = {"group": grp, "n": g.n,
                    "acc": acc and {"mean": acc[0], "sd": acc[1], "t": acc[2], "n": acc[3]},
                    "cost": cost and {"mean": cost[0], "sd": cost[1], "t": cost[2], "n": cost[3]},
                    "sign_p": _sign_p(g.better, g.worse),
                    "better": g.better, "worse": g.worse, "same": g.same,
                    "cache_read_delta": cr, "input_delta": inp}

    for label, keys in (("TREATED", [c for c in out if out[c]["group"] == "TREAT"]),
                        ("CONTROL", [c for c in out if out[c]["group"] == "ctrl"])):
        accs = [d for c in keys for d in by_class[c].acc]
        costs = [d for c in keys for d in by_class[c].cost]
        s_acc, s_cost = _paired(accs), _paired(costs)
        b = sum(by_class[c].better for c in keys)
        w = sum(by_class[c].worse for c in keys)
        ndocs = sum(by_class[c].n for c in keys)
        print(f"\n{label} pooled ({len(keys)} classes, {ndocs} docs)")
        if s_acc:
            print(f"  accuracy Δ {s_acc[0]:+.4f}  sd {s_acc[1]:.4f}  t {s_acc[2]:+.2f}  "
                  f"n={s_acc[3]}   better {b} / worse {w}  sign p={_sign_p(b, w):.4f}")
        if s_cost:
            print(f"  cost Δ     {s_cost[0]:+.5f}  t {s_cost[2]:+.2f}  n={s_cost[3]}")

    if a.json:
        json.dump(out, open(a.json, "w"), indent=2)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
