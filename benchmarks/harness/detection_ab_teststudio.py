#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A/B a config toggle over a REAL labeled corpus, via Test Studio.

``run_matrix.py`` cannot do this: it launches one local PDF per run and a reference
corpus is a test set on the stack, so a suite naming ``realkie`` or ``ocr_bench``
runs nothing for it. (It used to skip them silently; as of #766 it reports them as
unlaunchable and records the shortfall in the runmap, but there is still no launch
path — that is what this script is.) This drives the **TestRunner Lambda** — the
same entry point the Test Studio UI uses — so the runs are ordinary test
executions, scored against each test set's committed baselines with the config
profile and revision captured on the run.

Written for the #753 detection A/B and kept because the shape is general: any
per-config-toggle question that needs a real corpus rather than the synthetic grid.

``numberOfFiles`` takes the FIRST N documents deterministically, so both arms of a
pair see identical documents — the comparison is **paired**, which matters because
document difficulty dominates variance on a real corpus.

    # launch (two profiles must already exist, differing only in the toggle)
    python3 benchmarks/harness/detection_ab_teststudio.py launch \\
        --stack IDPMulti --n 40 \\
        --pair ocr-benchmark:mid-off-ocr:mid-on-ocr \\
        --pair realkie-fcc-verified:mid-off-rk:mid-on-rk

    # analyse (paired accuracy + tokens + false positives)
    python3 benchmarks/harness/detection_ab_teststudio.py analyse --stack IDPMulti
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
import lib  # noqa: E402

SUSPECTED = "extraction_multi_instance_suspected"
STATE = "runs.json"


def _resources(stack):
    """Testset / output bucket + tracking table, by name prefix."""
    return {
        "output_bucket": _find(stack, "outputbucket"),
        "testset_bucket": _find(stack, "testsetbucket"),
        "tracking_table": _find_table(stack, "TrackingTable"),
    }


def _find(stack, kind):
    """Physical name of ``<stack>-<kind>-<suffix>`` (lower-cased, as S3 requires)."""
    prefix = f"{stack.lower()}-{kind}-"
    for b in lib.s3().list_buckets()["Buckets"]:
        if b["Name"].startswith(prefix):
            return b["Name"]
    raise SystemExit(f"no bucket named {prefix}* for stack {stack}")


def _find_table(stack, logical_id):
    """Physical name of ``<stack>-<logical_id>-<suffix>``.

    Anchored on the full ``<stack>-<logical_id>-`` prefix, not a substring: this
    stack has BOTH ``IDPMulti-TrackingTable-…`` and
    ``IDPMulti-BootstrapTrackingTable-…``, and a substring match returned whichever
    came back first — silently scanning the wrong table and reporting 0 documents
    for every run.
    """
    prefix = f"{stack}-{logical_id}-"
    ddb = lib.ddb()
    token = None
    while True:
        kw = {"ExclusiveStartTableName": token} if token else {}
        r = ddb.list_tables(**kw)
        for t in r["TableNames"]:
            if t.startswith(prefix):
                return t
        token = r.get("LastEvaluatedTableName")
        if not token:
            raise SystemExit(f"no table named {prefix}* for stack {stack}")


def _find_fn(stack, substr):
    lam = lib.session().client("lambda", region_name=lib.REGION)
    for page in lam.get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            if f["FunctionName"].startswith(stack) and substr in f["FunctionName"]:
                return f["FunctionName"]
    return None


def cmd_launch(a):
    lam = lib.session().client("lambda", region_name=lib.REGION)
    runner = _find_fn(a.stack, "TestRunnerFunction")
    if not runner:
        raise SystemExit("TestRunnerFunction not found")
    print("runner:", runner)
    out = []
    for spec in a.pair:
        try:
            testset, off_prof, on_prof = spec.split(":")
        except ValueError:
            raise SystemExit(f"--pair wants testset:offProfile:onProfile, got {spec!r}")
        for prof in (off_prof, on_prof):
            payload = {
                "arguments": {
                    "input": {
                        "testSetId": testset,
                        "configVersion": prof,
                        "configRevision": a.revision,
                        "numberOfFiles": a.n,
                        "context": f"detection-ab {prof} n={a.n}",
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
                    "arm": "off" if prof == off_prof else "on",
                }
            )
    with open(os.path.join(a.outdir, STATE), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {os.path.join(a.outdir, STATE)}")


def _docs_of_run(tracking, run_id):
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


def _score(bucket, run_id, doc):
    """The document's own weighted evaluation score, or None."""
    d = lib.get_json(bucket, f"{run_id}/{doc}/evaluation/results.json")
    if not d:
        return None
    return (d.get("overall_metrics") or {}).get("weighted_overall_score")


def _tokens(item):
    m = lib.ddb_to_py(item.get("Metering")) if "Metering" in item else None
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    inp = outp = 0.0
    if isinstance(m, dict):
        for svc in m.values():
            if not isinstance(svc, dict):
                continue
            for k, v in svc.items():
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                kl = k.lower()
                if "token" in kl and "input" in kl:
                    inp += v
                elif "token" in kl and "output" in kl:
                    outp += v
    return inp, outp


def _suspected(item):
    secs = lib.ddb_to_py(item.get("Sections")) or []
    n = 0
    for sec in secs if isinstance(secs, list) else []:
        for iss in (sec or {}).get("ProcessingIssues") or []:
            if iss.get("code") == SUSPECTED:
                n += 1
    return n


def _sign_test(better, worse):
    """Two-sided sign test — the distribution-free "is one arm systematically
    better", which is the question, and it does not assume normal deltas."""
    n, k = better + worse, min(better, worse)
    if n == 0:
        return None
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def cmd_analyse(a):
    res = _resources(a.stack)
    runs = json.load(open(os.path.join(a.outdir, STATE)))
    by_corpus = collections.defaultdict(dict)
    for r in runs:
        # `arm` is recorded at launch; infer it from the profile name for a state
        # file written by an earlier version, so an existing run stays analysable.
        arm = r.get("arm") or ("on" if "-on-" in r["profile"] else "off")
        by_corpus[r["corpus"]][arm] = r

    for corpus, arms in sorted(by_corpus.items()):
        if len(arms) != 2:
            print(f"{corpus}: need both arms, have {sorted(arms)}")
            continue
        print("=" * 78)
        print(f"CORPUS: {corpus}  (n requested = {arms['off']['n']})")
        data = {}
        for arm, r in arms.items():
            rows = {}
            for key, item in _docs_of_run(res["tracking_table"], r["run_id"]).items():
                inp, outp = _tokens(item)
                rows[key] = {
                    "status": lib.ddb_to_py(item.get("ObjectStatus")),
                    "score": _score(res["output_bucket"], r["run_id"], key),
                    "in_tok": inp,
                    "out_tok": outp,
                    "suspected": _suspected(item),
                }
            data[arm] = rows
            done = sum(1 for v in rows.values() if v["status"] == "COMPLETED")
            print(f"  {arm:3s} run={r['run_id']}  docs={len(rows)}  completed={done}")

        common = sorted(set(data["off"]) & set(data["on"]))
        scored = [
            d
            for d in common
            if data["off"][d]["score"] is not None
            and data["on"][d]["score"] is not None
        ]
        print(f"  paired documents: {len(common)}   scored in both arms: {len(scored)}")
        if not scored:
            continue

        off = [data["off"][d]["score"] for d in scored]
        on = [data["on"][d]["score"] for d in scored]
        diffs = [b - x for x, b in zip(off, on)]
        better = sum(1 for x in diffs if x > 1e-9)
        worse = sum(1 for x in diffs if x < -1e-9)
        print(f"\n  ACCURACY (weighted_overall_score), paired over {len(scored)} docs")
        print(f"    off mean {statistics.mean(off):.4f}")
        print(f"    on  mean {statistics.mean(on):.4f}")
        print(f"    mean paired delta {statistics.mean(diffs):+.4f}")
        print(
            f"    on better {better} / worse {worse} / identical "
            f"{len(diffs) - better - worse}"
        )
        p = _sign_test(better, worse)
        if p is not None:
            print(f"    sign test on {better + worse} discordant pairs: p = {p:.4f}")

        for label, field in (("INPUT tokens", "in_tok"), ("OUTPUT tokens", "out_tok")):
            x = [data["off"][d][field] for d in scored]
            y = [data["on"][d][field] for d in scored]
            mx, my = statistics.mean(x), statistics.mean(y)
            if not mx:
                continue
            print(
                f"  {label}: off {mx:,.0f}  on {my:,.0f}  ({(my - mx) / mx * 100:+.2f}%)"
            )

        print(
            f"  '{SUSPECTED}' raised: off "
            f"{sum(data['off'][d]['suspected'] for d in common)}, on "
            f"{sum(data['on'][d]['suspected'] for d in common)}"
        )
        for arm in ("off", "on"):
            bad = [d for d in common if data[arm][d]["status"] != "COMPLETED"]
            if bad:
                print(f"  non-COMPLETED ({arm}): {len(bad)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stack", required=True)
    ap.add_argument(
        "--outdir",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "results"
        ),
        help="where runs.json is written/read",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("launch")
    lp.add_argument("--n", type=int, default=40, help="documents per arm (first N)")
    lp.add_argument("--revision", type=int, default=1)
    lp.add_argument(
        "--pair",
        action="append",
        required=True,
        metavar="TESTSET:OFF_PROFILE:ON_PROFILE",
    )
    lp.set_defaults(func=cmd_launch)
    sp = sub.add_parser("analyse")
    sp.set_defaults(func=cmd_analyse)
    a = ap.parse_args()
    a.outdir = os.path.abspath(a.outdir)
    a.func(a)


if __name__ == "__main__":
    main()
