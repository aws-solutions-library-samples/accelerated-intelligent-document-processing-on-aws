#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Paired A/B of one config toggle over a REAL labeled corpus, via Test Studio.

Generalizes ``detection_ab_teststudio.py`` (written for #753) and fixes the one
thing that made it blind to the question this exists to answer.

**Why not just reuse that script.** Its ``_tokens()`` sums every metering key whose
name contains both "token" and "input" — which folds ``cacheReadInputTokens`` and
``cacheWriteInputTokens`` into the same number as uncached ``inputTokens``. So its
"INPUT tokens: off 7,759 on 7,900" is input + cache reads + cache writes added
together, and a toggle that moved tokens *between* those classes — which is exactly
what a change to the cached prefix does — reports a delta of ~0. Every prior
real-corpus A/B in this repo was run with that instrument. Here the three classes
are kept separate, because the difference between them is a 12.5x price ratio
(1.25x write vs 0.1x read).

Reported per arm, paired on document identity (``numberOfFiles`` takes the first N
deterministically, so both arms see the same documents and document difficulty —
which dominates variance on a real corpus — cancels):

* **accuracy** ``weighted_overall_score`` from each document's own
  ``evaluation/results.json``, with a paired sign test over discordant pairs.
* **cost** priced from the metering map via ``pricing.yaml`` — the same path the
  product's own cost reporting uses.
* **tokens** uncached input / output / cache read / cache write, separately.
* **cache verdict** per phase and per document CLASS, via ``cache_audit``. A
  prefix under the model's minimum silently does not cache, and the minimum is
  model-dependent and not monotonic across generations, so this is per-class or it
  is meaningless.
* **section metadata counters** any dotted path (e.g.
  ``forced_tool.honored``), so "did the arm actually engage" is answerable rather
  than assumed. An A/B whose treatment never applied reports "no effect", which is
  indistinguishable from a real null without this.

    # launch (profiles must already exist, differing ONLY in the toggle)
    AWS_PROFILE=default python3 real_corpus_ab.py launch --stack IDPBench --n 293 \\
        --pair ocr-benchmark:force-off:force-on --outdir results/forcing

    # analyse
    AWS_PROFILE=default python3 real_corpus_ab.py analyse --stack IDPBench \\
        --outdir results/forcing --counter forced_tool.honored --counter forced_tool.skipped
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
import lib  # noqa: E402

STATE = "runs.json"

UNITS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")


# --------------------------------------------------------------------------- launch


def cmd_launch(a):
    lam = lib.session().client("lambda", region_name=lib.REGION)
    runner = _find_fn(a.stack, "TestRunnerFunction")
    if not runner:
        raise SystemExit("TestRunnerFunction not found")
    print("runner:", runner)
    os.makedirs(a.outdir, exist_ok=True)
    out = []
    for spec in a.pair:
        try:
            testset, prof_a, prof_b = spec.split(":")
        except ValueError:
            raise SystemExit(f"--pair wants testset:armA:armB, got {spec!r}")
        for prof in (prof_a, prof_b):
            payload = {
                "arguments": {
                    "input": {
                        "testSetId": testset,
                        "configVersion": prof,
                        "configRevision": a.revision,
                        "numberOfFiles": a.n,
                        "context": f"{a.label} {prof} n={a.n}",
                    }
                }
            }
            r = lam.invoke(FunctionName=runner, Payload=json.dumps(payload))
            res = json.loads(r["Payload"].read())
            rid = res.get("testRunId")
            print(f"  {testset:26s} {prof:14s} -> {rid or res}")
            out.append(
                {
                    "corpus": testset,
                    "profile": prof,
                    "run_id": rid,
                    "n": a.n,
                    "arm": "A" if prof == prof_a else "B",
                }
            )
    with open(os.path.join(a.outdir, STATE), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {os.path.join(a.outdir, STATE)}")


# --------------------------------------------------------------------------- score


def _docs_of_run(tracking, run_id):
    """Per-document tracking rows for a run, keyed by document name.

    A filtered Scan, paginated: DynamoDB bounds a page by items EXAMINED, not items
    matching, so an unpaginated version finds a document only when it happens to
    land in the first examined window (issue #599).
    """
    out = {}
    kw = {
        "TableName": tracking,
        "FilterExpression": "contains(PK, :r)",
        "ExpressionAttributeValues": {":r": {"S": f"doc#{run_id}/"}},
    }
    while True:
        r = lib.ddb().scan(**kw)
        for it in r.get("Items", []):
            if it.get("SK", {}).get("S") != "none":
                continue
            key = it["PK"]["S"][len("doc#") :]
            out[key[len(run_id) + 1 :]] = it
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return out


def _metering(item):
    m = lib.ddb_to_py(item.get("Metering")) if "Metering" in item else None
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    return m if isinstance(m, dict) else {}


def _token_classes(item):
    """The four token classes, kept SEPARATE. See the module docstring."""
    tot = dict.fromkeys(UNITS, 0)
    for units in _metering(item).values():
        if not isinstance(units, dict):
            continue
        for u in UNITS:
            if u in units:
                try:
                    tot[u] += int(float(units[u]))
                except (TypeError, ValueError):
                    pass
    return tot


def _cost(item):
    cost, _ = lib.price_metering(_metering(item))
    return cost


def _score(bucket, run_id, doc):
    d = lib.get_json(bucket, f"{run_id}/{doc}/evaluation/results.json")
    if not d:
        return None
    return (d.get("overall_metrics") or {}).get("weighted_overall_score")


def _counters_from_sections(bucket, run_id, doc, dotted_paths):
    """``{path: (hits, seen)}`` over a document's section ``metadata`` blocks."""
    out = {p: [0, 0] for p in dotted_paths}
    for sec in lib.iter_section_results(bucket, f"{run_id}/{doc}/"):
        md = sec.get("metadata")
        if not isinstance(md, dict):
            continue
        for p in dotted_paths:
            node = md
            ok = True
            for part in p.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    ok = False
                    break
            out[p][1] += 1
            if ok and node:
                out[p][0] += 1
    return {k: tuple(v) for k, v in out.items()}


def _sign_test(better, worse):
    """Two-sided exact sign test p-value over discordant pairs."""
    n = better + worse
    if n == 0:
        return 1.0
    k = min(better, worse)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def _paired_stats(pairs, name):
    """Paired mean/sd/t for a list of (a, b) numeric pairs."""
    deltas = [a - b for a, b in pairs if a is not None and b is not None]
    if len(deltas) < 2:
        return None
    mean = statistics.fmean(deltas)
    sd = statistics.stdev(deltas)
    se = sd / (len(deltas) ** 0.5)
    return {
        "metric": name,
        "n_pairs": len(deltas),
        "mean_delta": mean,
        "sd": sd,
        "se": se,
        "t": (mean / se) if se else None,
    }


def cmd_analyse(a):
    res = _resources(a.stack)
    runs = json.load(open(os.path.join(a.outdir, STATE)))
    by_corpus = collections.defaultdict(list)
    for r in runs:
        by_corpus[r["corpus"]].append(r)

    report = {}
    for corpus, rs in by_corpus.items():
        arm_a = next(r for r in rs if r["arm"] == "A")
        arm_b = next(r for r in rs if r["arm"] == "B")
        print("\n" + "=" * 78)
        print(f"CORPUS: {corpus}   (n requested = {arm_a['n']})")
        print(f"  A  {arm_a['profile']:16s} run={arm_a['run_id']}")
        print(f"  B  {arm_b['profile']:16s} run={arm_b['run_id']}")

        docs = {}
        for r in (arm_a, arm_b):
            items = _docs_of_run(res["tracking_table"], r["run_id"])
            for doc, item in items.items():
                docs.setdefault(doc, {})[r["arm"]] = (r["run_id"], item)
        paired = {d: v for d, v in docs.items() if "A" in v and "B" in v}
        print(f"  documents seen: {len(docs)}   paired in both arms: {len(paired)}")

        acc, cost, toks = [], [], {u: [] for u in UNITS}
        counters = {p: {"A": [0, 0], "B": [0, 0]} for p in (a.counter or [])}
        better = worse = same = 0
        for doc, arms in sorted(paired.items()):
            sa = _score(res["output_bucket"], arms["A"][0], doc)
            sb = _score(res["output_bucket"], arms["B"][0], doc)
            if sa is not None and sb is not None:
                acc.append((sb, sa))  # B - A  (treatment minus control)
                if sb > sa:
                    better += 1
                elif sb < sa:
                    worse += 1
                else:
                    same += 1
            cost.append((_cost(arms["B"][1]), _cost(arms["A"][1])))
            ta, tb = _token_classes(arms["A"][1]), _token_classes(arms["B"][1])
            for u in UNITS:
                toks[u].append((tb[u], ta[u]))
            for arm in ("A", "B"):
                if not a.counter:
                    continue
                got = _counters_from_sections(
                    res["output_bucket"], arms[arm][0], doc, a.counter
                )
                for p, (h, s) in got.items():
                    counters[p][arm][0] += h
                    counters[p][arm][1] += s

        print(f"\n  ACCURACY (weighted_overall_score), paired over {len(acc)} docs")
        if acc:
            print(f"    A mean {statistics.fmean(x[1] for x in acc):.4f}")
            print(f"    B mean {statistics.fmean(x[0] for x in acc):.4f}")
            st = _paired_stats(acc, "accuracy")
            print(f"    mean paired delta (B-A) {st['mean_delta']:+.4f}  "
                  f"sd {st['sd']:.4f}  t {st['t']:+.2f}" if st else "    (too few pairs)")
            print(f"    B better {better} / worse {worse} / identical {same}")
            print(f"    sign test on {better + worse} discordant pairs: "
                  f"p = {_sign_test(better, worse):.4f}")

        st = _paired_stats(cost, "cost")
        if st:
            ma = statistics.fmean(x[1] for x in cost)
            mb = statistics.fmean(x[0] for x in cost)
            pct = 100 * st["mean_delta"] / ma if ma else float("nan")
            print(f"\n  COST/doc: A ${ma:.4f}  B ${mb:.4f}  "
                  f"delta {st['mean_delta']:+.4f} ({pct:+.1f}%)  t {st['t']:+.2f}  "
                  f"{'SEPARATES' if abs(st['t'] or 0) > 2 else 'not resolvable'}")

        print(f"\n  TOKENS/doc (kept separate — a cache shift moves tokens BETWEEN these)")
        print(f"    {'class':26} {'A':>12} {'B':>12} {'delta':>12} {'%':>8}")
        for u in UNITS:
            ma = statistics.fmean(x[1] for x in toks[u]) if toks[u] else 0
            mb = statistics.fmean(x[0] for x in toks[u]) if toks[u] else 0
            d = mb - ma
            pct = f"{100 * d / ma:+.1f}%" if ma else "—"
            print(f"    {u:26} {ma:>12,.0f} {mb:>12,.0f} {d:>+12,.0f} {pct:>8}")

        if a.counter:
            print(f"\n  SECTION METADATA (did the arm actually engage?)")
            for p, v in counters.items():
                print(f"    {p:34} A {v['A'][0]}/{v['A'][1]}   B {v['B'][0]}/{v['B'][1]}")

        report[corpus] = {
            "arm_a": arm_a, "arm_b": arm_b, "paired": len(paired),
            "accuracy": _paired_stats(acc, "accuracy"),
            "cost": _paired_stats(cost, "cost"),
            "tokens": {u: _paired_stats(toks[u], u) for u in UNITS},
            "sign_test_p": _sign_test(better, worse),
            "counters": {k: {kk: list(vv) for kk, vv in v.items()} for k, v in counters.items()},
        }

        # Per-class cache verdict, one arm at a time — this is the mechanism.
        for r in (arm_a, arm_b):
            rows = cache_audit.audit_run(res, r["run_id"])
            if rows:
                cache_audit.report(f"{corpus} / {r['profile']}", rows)

    out = os.path.join(a.outdir, "summary.json")
    json.dump(report, open(out, "w"), indent=2, default=str)
    print(f"\nwrote {out}")


# --------------------------------------------------------------------------- infra


def _resources(stack):
    return {
        "output_bucket": _find(stack, "outputbucket"),
        "testset_bucket": _find(stack, "testsetbucket"),
        "tracking_table": cache_audit._find_table(stack, "TrackingTable"),
    }


def _find(stack, kind):
    s3 = lib.session().client("s3")
    for b in s3.list_buckets()["Buckets"]:
        n = b["Name"]
        if n.startswith(stack.lower()) and kind in n:
            return n
    raise SystemExit(f"no bucket for {stack}/{kind}")


def _find_fn(stack, substr):
    lam = lib.session().client("lambda", region_name=lib.REGION)
    for page in lam.get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            n = f["FunctionName"]
            if n.startswith(f"{stack}-") and substr in n:
                return n
    return None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    L = sub.add_parser("launch")
    L.add_argument("--stack", required=True)
    L.add_argument("--pair", action="append", required=True,
                   help="testset:armAProfile:armBProfile")
    L.add_argument("--n", type=int, default=40)
    L.add_argument("--revision", default=None)
    L.add_argument("--label", default="ab")
    L.add_argument("--outdir", default=".")
    L.set_defaults(func=cmd_launch)

    A = sub.add_parser("analyse")
    A.add_argument("--stack", required=True)
    A.add_argument("--outdir", default=".")
    A.add_argument("--counter", action="append",
                   help="dotted path in section metadata, e.g. forced_tool.honored")
    A.set_defaults(func=cmd_analyse)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
