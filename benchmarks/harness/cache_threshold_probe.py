#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where does Bedrock actually START caching? A direct sweep, not an assumption.

Anthropic publishes a minimum cacheable prefix per model (512 on Opus 5 / Fable 5;
1024 on Sonnet 5 / Sonnet 4.6 / Opus 4.8; 2048 on Opus 4.7; 4096 on Opus 4.6/4.5
and Haiku 4.5) and states that a shorter prefix "silently won't cache" — no error,
just ``cacheWriteInputTokens: 0``. Our client inserts a ``cachePoint`` wherever
``<<CACHEPOINT>>`` appears with **no length check**, so any class whose prompt
prefix falls under the model's minimum silently pays full input price forever.

Everything downstream of that claim in this repo is inference. This probe measures
it directly: build a prefix of a target size, put a ``cachePoint`` after it, call
Converse **twice** (the first call can only write, the second can read), and report
the actual token classes Bedrock returns. Sweeping the target across the documented
threshold locates the real boundary for a given model on Bedrock.

It deliberately does NOT go through ``idp_common`` — a mechanism test should not
inherit the pipeline's prompt assembly, cachePoint placement or model routing. Two
independent measurements of the same thing are the point.

Cost is negligible (a few thousand input tokens per point).

    AWS_PROFILE=default python3 cache_threshold_probe.py \\
        --model us.anthropic.claude-sonnet-4-6 \\
        --targets 400 800 950 1000 1050 1100 1300 2000
"""

from __future__ import annotations

import argparse
import json
import time

import boto3

# Deterministic, non-repeating filler. Repetition is avoided on purpose: a prefix
# that compresses oddly would make the token count a poor proxy for its length, and
# the whole probe is about locating a token-count boundary.
_WORDS = (
    "ledger reconcile invoice tariff schedule appendix clause remittance custodian "
    "escrow indemnity covenant amortize accrual disbursement receivable payable "
    "warrant lien collateral fiduciary arbitration jurisdiction assignee novation "
).split()


def filler(target_tokens: int) -> str:
    """~``target_tokens`` worth of prose, at the conventional ~4 chars/token."""
    out, i = [], 0
    while sum(len(w) + 1 for w in out) < target_tokens * 4:
        out.append(_WORDS[i % len(_WORDS)] + str(i))
        i += 1
    return " ".join(out)


def probe(client, model: str, target: int, calls: int = 2) -> list[dict]:
    """Call Converse ``calls`` times with an identical cached prefix."""
    prefix = filler(target)
    results = []
    for n in range(calls):
        r = client.converse(
            modelId=model,
            system=[{"text": prefix}, {"cachePoint": {"type": "default"}}],
            messages=[{"role": "user", "content": [{"text": "Reply with the single word OK."}]}],
            inferenceConfig={"maxTokens": 8},
        )
        u = r["usage"]
        results.append(
            {
                "target": target,
                "call": n + 1,
                "inputTokens": u.get("inputTokens", 0),
                "cacheWrite": u.get("cacheWriteInputTokens", 0),
                "cacheRead": u.get("cacheReadInputTokens", 0),
                # Total prefix actually presented, however Bedrock classified it.
                "total_in": u.get("inputTokens", 0)
                + u.get("cacheWriteInputTokens", 0)
                + u.get("cacheReadInputTokens", 0),
            }
        )
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="us.anthropic.claude-sonnet-4-6")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--targets", type=int, nargs="+",
                    default=[400, 800, 950, 1000, 1050, 1100, 1300, 2000])
    ap.add_argument("--calls", type=int, default=2)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    client = boto3.client("bedrock-runtime", region_name=a.region)
    print(f"model={a.model}  region={a.region}\n")
    print(f"{'target':>7} {'call':>5} {'total_in':>9} {'uncached':>9} {'cWrite':>8} {'cRead':>8}  caching?")
    rows = []
    for t in a.targets:
        for r in probe(client, a.model, t, a.calls):
            rows.append(r)
            cached = r["cacheWrite"] or r["cacheRead"]
            print(f"{r['target']:>7} {r['call']:>5} {r['total_in']:>9} {r['inputTokens']:>9} "
                  f"{r['cacheWrite']:>8} {r['cacheRead']:>8}  "
                  f"{'YES' if cached else 'no  (silently uncached)'}")
        time.sleep(0.5)

    # Locate the boundary from what Bedrock actually reported, not from the target.
    on = [r["total_in"] for r in rows if (r["cacheWrite"] or r["cacheRead"])]
    off = [r["total_in"] for r in rows if not (r["cacheWrite"] or r["cacheRead"])]
    print("\n--- boundary, measured ---")
    if off:
        print(f"  largest prefix that did NOT cache : {max(off):>6} tokens")
    if on:
        print(f"  smallest prefix that DID cache    : {min(on):>6} tokens")
    if on and off and max(off) < min(on):
        print(f"  => effective minimum lies in ({max(off)}, {min(on)}] tokens")
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=2)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
