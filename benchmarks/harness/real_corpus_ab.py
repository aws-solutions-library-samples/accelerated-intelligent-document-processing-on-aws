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


def _metering(item) -> lib.Reading[dict]:
    """The row's metering map, three-stated. See ``lib.metering_of_item``.

    This used to be a local decoder answering ``{}`` for everything it could not
    read, so a document whose ``Metering`` would not decode contributed a confident
    $0.00 cost and a zero to every token class (GitHub #1205). It delegates now: a
    second decoder is what let the two drift apart in the first place.
    """
    return lib.metering_of_item(item)


def _token_classes(item):
    """``(the four token classes kept SEPARATE, unread_reason)``.

    Every value in the returned map has to be a real count, because the caller
    averages them across documents: a zero from a row that could not be read moves
    a token mean exactly as a measured zero would, and there is nothing in the mean
    that says which it was. So the whole map is withheld rather than partially
    filled — including when the row read fine and a single count is not a number,
    since a map short by one class is no more reportable than one short by four.

    See the module docstring for why the four are not summed.
    """
    read = _metering(item)
    if not read.is_present:
        return None, f"metering {read.state}: {read.error}"
    tot = dict.fromkeys(UNITS, 0)
    unreadable = []
    for key, units in read.value.items():
        if not isinstance(units, dict):
            continue  # priced-and-reported by `_cost`; contributes no token count
        for u in UNITS:
            if u not in units:
                continue
            try:
                tot[u] += int(float(units[u]))
            except (TypeError, ValueError):
                unreadable.append(f"{key!r} unit {u!r}={units[u]!r}")
    if unreadable:
        return None, "token count is not a number: " + "; ".join(unreadable)
    return tot, None


def _cost(item):
    """``(cost, reason)`` — where ``reason`` is null only if the cost is trustworthy.

    Two facts reach this slot and both mean "no cost to contribute". A metering row
    that would not decode is unknown, not free (GitHub #1205). A map carrying
    something ``pricing.yaml`` cannot price is below truth by an unknown amount, and
    both arms of a paired comparison would be shifted by different amounts, so the
    delta moves in an unknown direction rather than merely by an unknown size
    (GitHub #1146). Either way the pair is dropped and named, exactly as ``_score``
    drops an unread report. The reason says which, because the remedies differ.
    """
    read = _metering(item)
    if not read.is_present:
        return None, f"metering {read.state}: {read.error}"
    priced = lib.price_metering(read.value)
    if not priced.complete:
        return None, priced.why
    return priced.total, None


def _score(bucket, run_id, doc):
    """``(weighted_overall_score, unread_reason)``.

    A report that is not there and one that would not read are different facts; only
    the first is a document this A/B has nothing to say about (GitHub #1079).
    """
    read = lib.read_json(bucket, f"{run_id}/{doc}/evaluation/results.json")
    if read.is_failed:
        return None, read.error
    d = read.value_or(None)
    if not d:
        return None, None
    return (d.get("overall_metrics") or {}).get("weighted_overall_score"), None


def _counters_from_sections(bucket, run_id, doc, dotted_paths):
    """``({path: (hits, seen)}, unread_reason)`` over a document's ``metadata`` blocks.

    ``seen`` is a denominator, so a section that would not read lowers it and makes the
    hit RATE look better than it is. The reason comes back with the counters so the
    caller can refuse the observation rather than average it in (GitHub #1079).
    """
    out = {p: [0, 0] for p in dotted_paths}
    read = lib.read_sections(bucket, f"{run_id}/{doc}/")
    if not read.complete:
        return {k: tuple(v) for k, v in out.items()}, read.why
    for sec in read.sections:
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
    return {k: tuple(v) for k, v in out.items()}, ""


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
        unread_notes = []
        for doc, arms in sorted(paired.items()):
            sa, ua = _score(res["output_bucket"], arms["A"][0], doc)
            sb, ub = _score(res["output_bucket"], arms["B"][0], doc)
            for arm_name, why in (("A", ua), ("B", ub)):
                if why:
                    unread_notes.append(f"{doc} [{arm_name}] score: {why}")
            if sa is not None and sb is not None:
                acc.append((sb, sa))  # B - A  (treatment minus control)
                if sb > sa:
                    better += 1
                elif sb < sa:
                    worse += 1
                else:
                    same += 1
            cb, pb = _cost(arms["B"][1])
            ca, pa = _cost(arms["A"][1])
            for arm_name, why in (("A", pa), ("B", pb)):
                if why:
                    unread_notes.append(f"{doc} [{arm_name}] cost: {why}")
            if cb is not None and ca is not None:
                cost.append((cb, ca))
            # Tokens are a separate measurement from cost — a row can price fine and
            # still carry an unreadable count — so they are excluded on their own
            # reason rather than on the cost's (#1205).
            ta, tka = _token_classes(arms["A"][1])
            tb, tkb = _token_classes(arms["B"][1])
            for arm_name, why in (("A", tka), ("B", tkb)):
                if why:
                    unread_notes.append(f"{doc} [{arm_name}] tokens: {why}")
            if ta is not None and tb is not None:
                for u in UNITS:
                    toks[u].append((tb[u], ta[u]))
            for arm in ("A", "B"):
                if not a.counter:
                    continue
                got, why = _counters_from_sections(
                    res["output_bucket"], arms[arm][0], doc, a.counter
                )
                if why:
                    # Not counted at all rather than counted short: a partial `seen`
                    # inflates the hit rate this A/B is judged on (#1079).
                    unread_notes.append(f"{doc} [{arm}] counters: {why}")
                    continue
                for p, (h, s) in got.items():
                    counters[p][arm][0] += h
                    counters[p][arm][1] += s

        if unread_notes:
            print(
                f"\n  ⚠ {len(unread_notes)} observation(s) EXCLUDED because a read "
                "failed or a cost could not be priced, not because there was nothing "
                "there — the figures below are over the remainder:"
            )
            for note in unread_notes[:5]:
                print(f"      {note}")

        print(f"\n  ACCURACY (weighted_overall_score), paired over {len(acc)} docs")
        if acc:
            print(f"    A mean {statistics.fmean(x[1] for x in acc):.4f}")
            print(f"    B mean {statistics.fmean(x[0] for x in acc):.4f}")
            st = _paired_stats(acc, "accuracy")
            acc_t = lib.format_t(st and st["t"])
            print(
                f"    mean paired delta (B-A) {st['mean_delta']:+.4f}  "
                f"sd {st['sd']:.4f}  t {acc_t}"
                if st
                else "    (too few pairs)"
            )
            print(f"    B better {better} / worse {worse} / identical {same}")
            print(
                f"    sign test on {better + worse} discordant pairs: "
                f"p = {_sign_test(better, worse):.4f}"
            )

        st = _paired_stats(cost, "cost")
        if st:
            ma = statistics.fmean(x[1] for x in cost)
            mb = statistics.fmean(x[0] for x in cost)
            pct = 100 * st["mean_delta"] / ma if ma else float("nan")
            cost_t = lib.format_t(st["t"])
            verdict = "SEPARATES" if abs(st["t"] or 0) > 2 else "not resolvable"
            short = (
                f"  ⚠ over {st['n_pairs']} of {len(paired)} paired document(s)"
                if st["n_pairs"] < len(paired)
                else ""
            )
            print(
                f"\n  COST/doc: A ${ma:.4f}  B ${mb:.4f}  "
                f"delta {st['mean_delta']:+.4f} ({pct:+.1f}%)  t {cost_t}  "
                f"{verdict}"
                f"{short}"
            )

        print(
            "\n  TOKENS/doc (kept separate — a cache shift moves tokens BETWEEN these)"
            f"\n  over {len(toks[UNITS[0]])} of {len(paired)} paired document(s)"
            + (
                "  ⚠ the rest carried a metering row that would not read"
                if len(toks[UNITS[0]]) < len(paired)
                else ""
            )
        )
        print(f"    {'class':26} {'A':>12} {'B':>12} {'delta':>12} {'%':>8}")
        for u in UNITS:
            ma = statistics.fmean(x[1] for x in toks[u]) if toks[u] else 0
            mb = statistics.fmean(x[0] for x in toks[u]) if toks[u] else 0
            d = mb - ma
            pct = f"{100 * d / ma:+.1f}%" if ma else "—"
            print(f"    {u:26} {ma:>12,.0f} {mb:>12,.0f} {d:>+12,.0f} {pct:>8}")

        if a.counter:
            print("\n  SECTION METADATA (did the arm actually engage?)")
            for p, v in counters.items():
                print(
                    f"    {p:34} A {v['A'][0]}/{v['A'][1]}   B {v['B'][0]}/{v['B'][1]}"
                )

        report[corpus] = {
            "arm_a": arm_a,
            "arm_b": arm_b,
            "paired": len(paired),
            "accuracy": _paired_stats(acc, "accuracy"),
            "cost": _paired_stats(cost, "cost"),
            "tokens": {u: _paired_stats(toks[u], u) for u in UNITS},
            # Observations excluded because something could not be read or priced,
            # rather than because there was nothing there. Null on a clean run. The
            # console said this already and the artifact did not, which leaves a
            # reader of the committed summary unable to tell a thinned figure from a
            # whole one — `n_pairs` below a paired count is the only other trace, and
            # the defect these record used to contribute a zero instead, so there was
            # no trace at all (#1205).
            "excluded": unread_notes or None,
            "n_excluded": len(unread_notes) or None,
            "sign_test_p": _sign_test(better, worse),
            "counters": {
                k: {kk: list(vv) for kk, vv in v.items()} for k, v in counters.items()
            },
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
    L.add_argument(
        "--pair", action="append", required=True, help="testset:armAProfile:armBProfile"
    )
    L.add_argument("--n", type=int, default=40)
    L.add_argument("--revision", default=None)
    L.add_argument("--label", default="ab")
    L.add_argument("--outdir", default=".")
    L.set_defaults(func=cmd_launch)

    A = sub.add_parser("analyse")
    A.add_argument("--stack", required=True)
    A.add_argument("--outdir", default=".")
    A.add_argument(
        "--counter",
        action="append",
        help="dotted path in section metadata, e.g. forced_tool.honored",
    )
    A.set_defaults(func=cmd_analyse)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
