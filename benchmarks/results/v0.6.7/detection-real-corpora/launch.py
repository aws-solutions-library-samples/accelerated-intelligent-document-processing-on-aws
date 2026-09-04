#!/usr/bin/env python3
"""Launch Test Studio runs for the multi-instance detection A/B.

Uses the same TestRunner Lambda the Test Studio UI calls, so the runs are ordinary
test executions: scored against each test set's committed baselines, with the
config profile + revision captured on the run.

`numberOfFiles` takes the FIRST N documents of a set deterministically, so both
arms of a pair see the identical documents — the comparison is paired by
construction, which matters because document difficulty dominates variance on a
real corpus.
"""
import json, sys, boto3

REGION = "us-west-2"
STACK = "IDPMulti"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

PAIRS = [
    ("testset#ocr-benchmark", "ocr-benchmark", ["mid-off-ocr", "mid-on-ocr"]),
    ("testset#realkie-fcc-verified", "realkie-fcc-verified", ["mid-off-rk", "mid-on-rk"]),
]

lam = boto3.client("lambda", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)


def find_fn(substr):
    fns = []
    p = boto3.client("lambda", region_name=REGION).get_paginator("list_functions")
    for page in p.paginate():
        for f in page["Functions"]:
            if f["FunctionName"].startswith(STACK) and substr in f["FunctionName"]:
                fns.append(f["FunctionName"])
    return fns[0] if fns else None


runner = find_fn("TestRunnerFunction")
if not runner:
    raise SystemExit("TestRunnerFunction not found")
print("runner:", runner)

out = []
for pk, label, profiles in PAIRS:
    ts_id = pk.split("#", 1)[1]
    for prof in profiles:
        payload = {"arguments": {"input": {
            "testSetId": ts_id,
            "configVersion": prof,
            "configRevision": 1,
            "numberOfFiles": N,
            "context": f"midetect-ab {prof} n={N}",
        }}}
        r = lam.invoke(FunctionName=runner, Payload=json.dumps(payload))
        res = json.loads(r["Payload"].read())
        rid = res.get("testRunId")
        print(f"  {label:24s} {prof:14s} -> {rid or res}")
        out.append({"corpus": label, "profile": prof, "run_id": rid, "n": N})
json.dump(out, open("scratch/mi-teststudio/runs.json", "w"), indent=2)
print("\nwrote scratch/mi-teststudio/runs.json")
