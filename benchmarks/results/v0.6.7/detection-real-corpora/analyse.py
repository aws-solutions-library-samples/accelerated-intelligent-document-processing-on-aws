#!/usr/bin/env python3
"""Paired analysis of the multi-instance detection A/B over Test Studio runs.

Pairs on DOCUMENT, because document difficulty dominates variance on a real
corpus: comparing arm means over different documents wastes most of the signal.
Reports accuracy (the stack's own evaluation score), tokens, cost, and the
false-positive count for `extraction_multi_instance_suspected` — every one of
these documents is a genuine single-document section, so any occurrence is a
false alarm.
"""
import json, sys, statistics, collections, boto3

REGION = "us-west-2"
TRACKING = "IDPMulti-TrackingTable-193T8YMCUSQ5C"
OUTPUT = "idpmulti-outputbucket-z2qmefbzn12r"

ddb = boto3.client("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def docs_of_run(run_id):
    """Every tracked document of a run, keyed by its object key suffix."""
    out = {}
    kw = {
        "TableName": TRACKING,
        "FilterExpression": "contains(PK, :r)",
        "ExpressionAttributeValues": {":r": {"S": f"doc#{run_id}/"}},
    }
    while True:
        r = ddb.scan(**kw)
        for it in r.get("Items", []):
            if it.get("SK", {}).get("S") != "none":
                continue
            pk = it["PK"]["S"][len("doc#"):]
            out[pk[len(run_id) + 1:]] = it
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return out


def unwrap(v):
    if not isinstance(v, dict):
        return v
    for t, f in (("S", str), ("N", float), ("BOOL", bool)):
        if t in v:
            return f(v[t])
    if "M" in v:
        return {k: unwrap(x) for k, x in v["M"].items()}
    if "L" in v:
        return [unwrap(x) for x in v["L"]]
    if "NULL" in v:
        return None
    return v


def score_of(run_id, doc_key):
    """weighted_overall_score from the document's own evaluation report."""
    key = f"{run_id}/{doc_key}/evaluation/results.json"
    try:
        d = json.loads(s3.get_object(Bucket=OUTPUT, Key=key)["Body"].read())
    except Exception:
        return None
    return (d.get("overall_metrics") or {}).get("weighted_overall_score")


def metering_of(item):
    m = unwrap(item.get("Metering", {})) if "Metering" in item else None
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = None
    inp = outp = 0
    if isinstance(m, dict):
        for svc in m.values():
            if not isinstance(svc, dict):
                continue
            for k, v in svc.items():
                try:
                    v = float(v)
                except Exception:
                    continue
                kl = k.lower()
                if "input" in kl and "token" in kl:
                    inp += v
                elif "output" in kl and "token" in kl:
                    outp += v
    return inp, outp


def main():
    runs = json.load(open("scratch/mi-teststudio/runs.json"))
    by_corpus = collections.defaultdict(dict)
    for r in runs:
        arm = "on" if "-on-" in r["profile"] else "off"
        by_corpus[r["corpus"]][arm] = r

    for corpus, arms in sorted(by_corpus.items()):
        print("=" * 78)
        print(f"CORPUS: {corpus}   (n requested = {arms['off']['n']})")
        data = {}
        for arm, r in arms.items():
            docs = docs_of_run(r["run_id"])
            rows = {}
            for key, item in docs.items():
                st = unwrap(item.get("ObjectStatus", {}))
                inp, outp = metering_of(item)
                issues = unwrap(item.get("Sections", {})) or []
                fp = 0
                for sec in issues if isinstance(issues, list) else []:
                    for iss in (sec or {}).get("ProcessingIssues") or []:
                        if iss.get("code") == "extraction_multi_instance_suspected":
                            fp += 1
                rows[key] = {
                    "status": st,
                    "score": score_of(r["run_id"], key),
                    "in_tok": inp,
                    "out_tok": outp,
                    "fp": fp,
                }
            data[arm] = rows
            done = sum(1 for v in rows.values() if v["status"] == "COMPLETED")
            print(f"  {arm:3s} run={r['run_id']}  docs={len(rows)}  completed={done}")

        common = sorted(set(data["off"]) & set(data["on"]))
        scored = [d for d in common
                  if data["off"][d]["score"] is not None and data["on"][d]["score"] is not None]
        print(f"  documents in both arms: {len(common)}   scored in both: {len(scored)}")
        if not scored:
            print("  (no scored pairs yet)")
            continue

        off = [data["off"][d]["score"] for d in scored]
        on = [data["on"][d]["score"] for d in scored]
        diffs = [b - a for a, b in zip(off, on)]
        better = sum(1 for x in diffs if x > 1e-9)
        worse = sum(1 for x in diffs if x < -1e-9)
        same = len(diffs) - better - worse
        print()
        print(f"  ACCURACY (weighted_overall_score), paired over {len(scored)} docs")
        print(f"    detection OFF mean {statistics.mean(off):.4f}")
        print(f"    detection ON  mean {statistics.mean(on):.4f}")
        print(f"    mean paired delta  {statistics.mean(diffs):+.4f}"
              + (f"  (sd {statistics.stdev(diffs):.4f})" if len(diffs) > 1 else ""))
        print(f"    ON better on {better} docs / worse on {worse} / identical on {same}")
        if better + worse:
            # Two-sided sign test: the distribution-free question "is one arm
            # systematically better", which is what we actually want to know.
            from math import comb
            n_eff, k = better + worse, min(better, worse)
            p = min(1.0, 2 * sum(comb(n_eff, i) for i in range(k + 1)) / 2 ** n_eff)
            print(f"    sign test on {n_eff} discordant pairs: p = {p:.4f}")

        for label, field in (("INPUT tokens", "in_tok"), ("OUTPUT tokens", "out_tok")):
            a = [data["off"][d][field] for d in scored]
            b = [data["on"][d][field] for d in scored]
            if sum(a) == 0 and sum(b) == 0:
                continue
            ma, mb = statistics.mean(a), statistics.mean(b)
            pct = ((mb - ma) / ma * 100) if ma else 0.0
            print(f"  {label}: off {ma:,.0f}  on {mb:,.0f}  ({pct:+.2f}%)")

        fp_off = sum(data["off"][d]["fp"] for d in common)
        fp_on = sum(data["on"][d]["fp"] for d in common)
        print(f"  FALSE POSITIVES (extraction_multi_instance_suspected on "
              f"single-document sections): off {fp_off}, on {fp_on}")
        fail_off = sum(1 for d in common if data["off"][d]["status"] != "COMPLETED")
        fail_on = sum(1 for d in common if data["on"][d]["status"] != "COMPLETED")
        print(f"  non-COMPLETED: off {fail_off}, on {fail_on}")


if __name__ == "__main__":
    main()
