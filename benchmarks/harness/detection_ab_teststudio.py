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


def _require_profiles(stack, profiles):
    """Fail before launching anything if a profile is not on the stack.

    The runner now refuses such a run itself (#878), but an older stack queued
    it and every document then failed in OCR; checking here costs one GetItem
    per profile and gives a message that names the fix.
    """
    table = _find_table(stack, "ConfigurationTable")
    ddb = lib.ddb()
    for prof in sorted(set(profiles)):
        r = ddb.get_item(
            TableName=table,
            Key={"Configuration": {"S": f"Config#{prof}"}},
            ProjectionExpression="PublishedRevision",
        )
        if "Item" not in r:
            raise SystemExit(
                f"configuration profile {prof!r} does not exist on {stack}; upload "
                f"it first (idp-cli config-upload --stack-name {stack} "
                f"--config-profile {prof} --config-file ...)"
            )


def cmd_launch(a):
    lam = lib.session().client("lambda", region_name=lib.REGION)
    runner = _find_fn(a.stack, "TestRunnerFunction")
    if not runner:
        raise SystemExit("TestRunnerFunction not found")
    print("runner:", runner)
    pairs = []
    for spec in a.pair:
        try:
            pairs.append(spec.split(":"))
            testset, off_prof, on_prof = pairs[-1]
        except ValueError:
            raise SystemExit(f"--pair wants testset:offProfile:onProfile, got {spec!r}")
    _require_profiles(a.stack, [p for _, off, on in pairs for p in (off, on)])
    out = []
    # Ids are <set>-<timestamp to the second>; a stack whose runner predates the
    # #879 fix hands two arms launched within a second the SAME id, and the
    # second silently replaces the first. Refuse to record such a launch.
    seen_ids = set()
    for testset, off_prof, on_prof in pairs:
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
            # A failed invoke has no id (rid is None); only a real id can collide.
            if rid and rid in seen_ids:
                raise SystemExit(
                    f"run id {rid} was issued twice: the stack's TestRunner collapses "
                    f"runs started within one second (#879). Abort the other arms in "
                    f"Test Studio and relaunch with a gap, or deploy the fix."
                )
            if rid:
                seen_ids.add(rid)
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
    """The document's own weighted evaluation score, and why there is none if so.

    Returns ``(score, unread_reason)``. A report that is not there (evaluation off for
    this run) and one that would not read are different facts, and the second is not a
    document that scored nothing (GitHub #1079).
    """
    read = lib.read_json(bucket, f"{run_id}/{doc}/evaluation/results.json")
    if read.is_failed:
        return None, read.error
    d = read.value_or(None)
    if not d:
        return None, None
    return (d.get("overall_metrics") or {}).get("weighted_overall_score"), None


def _tokens(item):
    """``(input tokens, output tokens, unread_reason)``.

    Both counts are null when the reason is not, because the caller averages them
    across paired documents: a zero from a metering row that would not read moves a
    token mean exactly as a measured zero does, and the mean carries nothing that
    says which it was (GitHub #1205). This was a local decoder answering ``{}`` for
    anything it could not decode, one function below ``_score``, which had already
    been given the three-state treatment — so the two halves of the same row
    disagreed about what an unreadable row means.
    """
    read = lib.metering_of_item(item)
    if not read.is_present:
        return None, None, f"metering {read.state}: {read.error}"
    inp = outp = 0.0
    unreadable = []
    for key, svc in read.value.items():
        if not isinstance(svc, dict):
            continue
        for k, raw in svc.items():
            kl = k.lower()
            if "token" not in kl or not ("input" in kl or "output" in kl):
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                unreadable.append(f"{key!r} unit {k!r}={raw!r}")
                continue
            if "input" in kl:
                inp += v
            else:
                outp += v
    if unreadable:
        return None, None, "token count is not a number: " + "; ".join(unreadable)
    return inp, outp, None


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
                inp, outp, tok_unread = _tokens(item)
                score, unread = _score(res["output_bucket"], r["run_id"], key)
                rows[key] = {
                    "status": lib.ddb_to_py(item.get("ObjectStatus")),
                    "score": score,
                    "score_unread": unread,
                    "in_tok": inp,
                    "out_tok": outp,
                    # Why the two counts above are null, when they are. Sits beside
                    # `score_unread` because it is the same fact about the other half
                    # of the row (#1205).
                    "tokens_unread": tok_unread,
                    "suspected": _suspected(item),
                }
            data[arm] = rows
            done = sum(1 for v in rows.values() if v["status"] == "COMPLETED")
            print(f"  {arm:3s} run={r['run_id']}  docs={len(rows)}  completed={done}")
            # A document whose evaluation report would not read drops out of the
            # paired comparison below, exactly as an unscored one does. Naming it is
            # what keeps the two apart (#1079).
            unread_docs = [k for k, v in rows.items() if v["score_unread"]]
            if unread_docs:
                print(
                    f"      ⚠ {len(unread_docs)} document(s) whose evaluation report "
                    f"could not be READ (not absent): {rows[unread_docs[0]]['score_unread']}"
                )
            # The same, for the metering half of the row. Reported separately because
            # a document can score fine and carry an unreadable metering row, and
            # only the token figures below are affected by that one (#1205).
            tok_unread_docs = [k for k, v in rows.items() if v["tokens_unread"]]
            if tok_unread_docs:
                print(
                    f"      ⚠ {len(tok_unread_docs)} document(s) whose METERING could "
                    "not be read, contributing no token figures rather than zeros: "
                    f"{rows[tok_unread_docs[0]]['tokens_unread']}"
                )

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
            # A document whose metering would not read contributes to NEITHER arm's
            # mean: including it in one and not the other would compare two different
            # document sets, and including a zero for it is the defect (#1205).
            both = [
                d
                for d in scored
                if data["off"][d][field] is not None
                and data["on"][d][field] is not None
            ]
            if len(both) < len(scored):
                print(
                    f"  {label}: over {len(both)} of {len(scored)} scored document(s) "
                    "— the rest carried a metering row that would not read"
                )
            x = [data["off"][d][field] for d in both]
            y = [data["on"][d][field] for d in both]
            if not x:
                continue
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
    lp.add_argument(
        "--revision",
        type=int,
        default=None,
        help="pin this revision of each profile; default: the runner records the "
        "profile's published revision (r1 was assumed before, which is only "
        "true for a profile saved exactly once)",
    )
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
