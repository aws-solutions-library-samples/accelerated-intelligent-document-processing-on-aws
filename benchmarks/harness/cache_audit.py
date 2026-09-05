#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Did prompt caching actually engage? Per phase, per model, per document CLASS.

Every cost claim this repo makes about schema duplication rests on one unverified
assumption — that the duplicated copies "sit inside the prompt-cache prefix, so
they are cache reads at roughly a tenth of input price". That sentence is in the
shipped docs for ``restate_schema_in_system_prompt``, in #710, and in the
guidance paper. **Nothing ever measured it.** If cache reads are in fact zero, the
duplicated schema costs FULL input price and every token saving is worth ~10x more
than we have been saying.

Two mechanisms can make it zero, and neither raises an error:

1. **Below the minimum cacheable prefix.** Anthropic's minimum is model-dependent
   (512 tokens on Opus 5 / Fable 5; **1024 on Sonnet 5 and Sonnet 4.6**; 2048 on
   Opus 4.7; **4096 on Opus 4.6/4.5 and Haiku 4.5** — and deliberately NOT
   monotonic across generations). A shorter prefix silently does not cache:
   ``cacheWriteInputTokens: 0``, no error. ``bedrock/client.py`` inserts a
   ``cachePoint`` wherever ``<<CACHEPOINT>>`` appears with **no length check**, so
   a small class simply never caches. Static analysis of the shipped presets puts
   ``ocr-benchmark/GLOSSARY`` at ~963 tokens and ``lending/Bank-checks`` at ~944 —
   both under 1024 — while several other classes sit within 20% of the cliff.
2. **Written but never read.** A write costs **1.25x** and a read **0.1x**, so a
   prefix that is written and not read inside the TTL is a net LOSS of 0.25x on
   the cached span. Break-even on the default 5-minute TTL is two requests. A
   workload with one section of a class per document, arriving spread out, pays
   the write every time and never collects.

So "is caching on?" has three answers, not two, and only the metering can tell
them apart:

    write>0, read>0   caching works; the 0.1x claim holds for the read fraction
    write>0, read==0  caching is ACTIVE AND COSTING 1.25x for nothing
    write==0          the cachePoint did nothing; everything is full-price input

Usage:
    AWS_PROFILE=default python3 cache_audit.py --stack IDPBench \\
        --run <runId> [--run <runId> ...] [--label off] [--json out.json]

Reads the TrackingTable metering map (authoritative — it is what the cost model
prices) and joins each document to the CLASS its sections were extracted under,
so a per-class cliff is visible rather than averaged away.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib  # noqa: E402

#: Minimum cacheable prefix by model family, from Anthropic's published table.
#: Deliberately keyed on a substring of the Bedrock model id. NOT monotonic across
#: generations — that is the trap this whole module exists to catch.
CACHE_MINIMUMS = {
    "claude-opus-5": 512,
    "claude-fable-5": 512,
    "claude-opus-4-8": 1024,
    "claude-sonnet-5": 1024,
    "claude-sonnet-4-6": 1024,
    "claude-sonnet-4-5": 1024,
    "claude-opus-4-7": 2048,
    "claude-opus-4-6": 4096,
    "claude-opus-4-5": 4096,
    "claude-haiku-4-5": 4096,
}

#: Units we care about. `inputTokens` here means UNCACHED input — Bedrock reports
#: cache reads/writes separately, so the three do not overlap.
UNITS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")


def cache_minimum_for(model_id: str) -> int | None:
    """Published minimum cacheable prefix for a model id, or None if unknown.

    Nova is deliberately absent: Bedrock's Nova caching has its own rules and its
    own price multipliers (0.25x read / 1.0x write in ``pricing.yaml``, not
    0.1x / 1.25x), so applying a Claude minimum to it would invent a finding.
    """
    for name, minimum in CACHE_MINIMUMS.items():
        if name in (model_id or ""):
            return minimum
    return None


def doc_classes(bucket: str, run_id: str, doc_name: str) -> list[str]:
    """The class each of a document's sections was extracted under."""
    out = []
    for sec in lib.iter_section_results(bucket, f"{run_id}/{doc_name}/"):
        cls = (sec.get("document_class") or {}).get("type")
        if cls:
            out.append(str(cls))
    return out


def audit_run(stack_res: dict, run_id: str) -> list[dict]:
    """One row per (doc, metering key). Metering keys are ``phase/model/...``.

    ``doc`` is qualified with the runId. The same document processed in five runs is
    five observations, and keying on the bare name collapses them to one — which
    silently multiplies every per-document token figure by the repeat count.
    """
    tracking = stack_res["tracking_table"]
    bucket = stack_res["output_bucket"]
    rows = []
    for prefix in lib.list_doc_prefixes(bucket, run_id):
        # `list_doc_prefixes` yields the full `runId/doc/` S3 prefix; `doc_metering`
        # keys on the BARE document name. Passing the prefix through builds a
        # tracking key that does not exist and reports "no metering found" as
        # though the run had failed.
        doc_name = prefix[len(run_id) :].strip("/") if prefix.startswith(run_id) else prefix.strip("/")
        metering = lib.doc_metering(tracking, run_id, doc_name) or {}
        classes = doc_classes(bucket, run_id, doc_name)
        for key, units in metering.items():
            if not isinstance(units, dict):
                continue
            parts = key.split("/")
            phase = parts[0]
            model = parts[1] if len(parts) > 1 else "?"
            rec = {
                "doc": f"{run_id}/{doc_name}",
                "classes": classes,
                "phase": phase,
                "model": model,
            }
            for u in UNITS:
                try:
                    rec[u] = int(units.get(u, 0) or 0)
                except (TypeError, ValueError):
                    rec[u] = 0
            rows.append(rec)
    return rows


def _agg(rows: list[dict]) -> dict:
    tot = {u: sum(r[u] for r in rows) for u in UNITS}
    cached = tot["cacheReadInputTokens"] + tot["cacheWriteInputTokens"]
    denom = tot["inputTokens"] + cached
    return {
        "n": len(rows),
        "docs": len({r["doc"] for r in rows}),
        **tot,
        # THE number the 0.1x claim rests on: what share of input was a cache READ.
        "read_share": round(tot["cacheReadInputTokens"] / denom, 4) if denom else None,
        "write_share": round(tot["cacheWriteInputTokens"] / denom, 4) if denom else None,
        # A request that wrote and never read paid 1.25x for nothing.
        "write_without_read": tot["cacheWriteInputTokens"] > 0
        and tot["cacheReadInputTokens"] == 0,
        "never_cached": cached == 0,
    }


def verdict(a: dict) -> str:
    if a["never_cached"]:
        return "NEVER CACHED (cachePoint did nothing — full-price input)"
    if a["write_without_read"]:
        return "WRITE-ONLY (paying 1.25x, collecting nothing)"
    return f"caching active (read share {a['read_share']:.1%})"


def report(label: str, rows: list[dict]) -> dict:
    print(f"\n{'=' * 78}\nARM: {label}   ({len({r['doc'] for r in rows})} docs, "
          f"{len(rows)} metering entries)\n{'=' * 78}")

    overall = _agg(rows)
    print(f"OVERALL: {verdict(overall)}")
    print(f"  uncached input {overall['inputTokens']:>12,}   "
          f"cacheRead {overall['cacheReadInputTokens']:>12,}   "
          f"cacheWrite {overall['cacheWriteInputTokens']:>10,}")

    print(f"\n{'phase/model':52} {'docs':>5} {'input':>10} {'cRead':>10} {'cWrite':>9} {'read%':>7}  verdict")
    by_pm = collections.defaultdict(list)
    for r in rows:
        by_pm[(r["phase"], r["model"])].append(r)
    for (phase, model), rs in sorted(by_pm.items()):
        a = _agg(rs)
        minimum = cache_minimum_for(model)
        tag = "" if minimum is None else f" [min {minimum}]"
        rs_pct = "—" if a["read_share"] is None else f"{a['read_share']:.1%}"
        print(f"{(phase + '/' + model)[:51]:52} {a['docs']:>5} {a['inputTokens']:>10,} "
              f"{a['cacheReadInputTokens']:>10,} {a['cacheWriteInputTokens']:>9,} {rs_pct:>7}  "
              f"{verdict(a)}{tag}")

    # Per-CLASS on the extraction phase only: this is where the prefix-length cliff
    # lives, and averaging across classes is exactly what would hide it.
    ext = [r for r in rows if r["phase"].lower().startswith("extract")]
    if ext:
        print("\n--- extraction, per document class (the prefix-length cliff) ---")
        print(f"{'class':34} {'docs':>5} {'input/doc':>10} {'cRead/doc':>10} {'cWrite/doc':>11} {'read%':>7}  verdict")
        by_cls = collections.defaultdict(list)
        for r in ext:
            for c in (r["classes"] or ["(unknown)"]):
                by_cls[c].append(r)
        for cls, rs in sorted(by_cls.items()):
            a = _agg(rs)
            d = max(a["docs"], 1)
            rs_pct = "—" if a["read_share"] is None else f"{a['read_share']:.1%}"
            print(f"{cls[:33]:34} {a['docs']:>5} {a['inputTokens'] // d:>10,} "
                  f"{a['cacheReadInputTokens'] // d:>10,} {a['cacheWriteInputTokens'] // d:>11,} "
                  f"{rs_pct:>7}  {verdict(a)}")
    return overall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", required=True)
    ap.add_argument("--run", action="append", required=True,
                    help="runId; repeat for several runs in the same arm")
    ap.add_argument("--label", default="run")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    res = {
        "output_bucket": _find(a.stack, "outputbucket"),
        "tracking_table": _find_table(a.stack, "TrackingTable"),
    }
    print(f"stack resources: {json.dumps(res, indent=2)}")
    rows = []
    for run_id in a.run:
        rows += audit_run(res, run_id)
    if not rows:
        raise SystemExit("no metering found — check the runIds")
    overall = report(a.label, rows)
    if a.json:
        json.dump({"label": a.label, "runs": a.run, "overall": overall, "rows": rows},
                  open(a.json, "w"), indent=2)
        print(f"\nwrote {a.json}")


def _find(stack, kind):
    s3 = lib.session().client("s3")
    for b in s3.list_buckets()["Buckets"]:
        n = b["Name"]
        if n.startswith(stack.lower()) and kind in n:
            return n
    raise SystemExit(f"no bucket for {stack}/{kind}")


def _find_table(stack, kind):
    """Physical name of ``<stack>-<kind>-<suffix>``.

    Anchored on ``<stack>-<kind>-`` rather than a substring match, because
    ``BootstrapTrackingTable`` CONTAINS ``TrackingTable`` and sorts first — a
    substring match silently returns the bootstrap table, which holds no metering,
    and the audit reports "no metering found" as though the run had failed.
    """
    ddb = lib.session().client("dynamodb")
    candidates = []
    for page in ddb.get_paginator("list_tables").paginate():
        candidates += page["TableNames"]
    exact = [t for t in candidates if t.startswith(f"{stack}-{kind}-")]
    if exact:
        return exact[0]
    loose = [t for t in candidates if t.startswith(f"{stack}-") and kind in t]
    if loose:
        return loose[0]
    raise SystemExit(f"no table for {stack}/{kind}")


if __name__ == "__main__":
    main()
