#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which document classes actually cache, on which extraction model?

A ``<<CACHEPOINT>>`` only creates a cache entry if the prefix before it clears the
model's **minimum cacheable prefix**, and that minimum is model-dependent and **not
monotonic across generations**: 512 on Opus 5 / Fable 5, 1,024 on Sonnet 5 / Sonnet
4.6 / Opus 4.8, 2,048 on Opus 4.7, and **4,096 on Opus 4.6 / Opus 4.5 / Haiku 4.5**.
Below it there is no error and no metric — the request is simply billed at full input
price forever (see ``docs/benchmarking/prompt-caching.md``).

So "does my configuration benefit from prompt caching?" is a per-class **and**
per-model question, and nothing in the product answers it. This does: it measures each
class's real prompt prefix (system prompt + the task prompt up to the marker, with the
schema substituted, optionally plus the forced toolSpec) by calling Converse and
reading back the token count, then reports which model tiers would cache it.

Measuring rather than estimating matters: ``chars/4`` is within ~2% on real prompt text
but the classes that matter sit within ~5% of the boundary, so an estimate cannot
decide them.

    AWS_PROFILE=default python3 cache_prefix_survey.py \\
        --preset ocr-benchmark --preset lending-package-sample [--forced-tool] [--pad N]
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time

import boto3
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

#: Published minimum cacheable prefix, by model tier. The ordering is deliberate — the
#: tiers are NOT increasing with recency, and presenting them sorted by size is what
#: makes "newer is not safer" visible.
TIERS = [
    (512, "Opus 5, Fable 5"),
    (1024, "Sonnet 5, Sonnet 4.6, Opus 4.8"),
    (2048, "Opus 4.7"),
    (4096, "Opus 4.6, Opus 4.5, Haiku 4.5"),
]

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def measure_prefix(client, model: str, system_text: str, msg_text: str, tool_config=None) -> int:
    """Actual token count of the span before the cachePoint, per Bedrock."""
    kw = dict(
        modelId=model,
        system=[{"text": system_text}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": msg_text},
                    {"cachePoint": {"type": "default"}},
                    {"text": "Reply OK."},
                ],
            }
        ],
        inferenceConfig={"maxTokens": 8},
    )
    if tool_config:
        kw["toolConfig"] = tool_config
    u = client.converse(**kw)["usage"]
    return (
        u.get("inputTokens", 0)
        + u.get("cacheWriteInputTokens", 0)
        + u.get("cacheReadInputTokens", 0)
    )


def survey(preset: str, model: str, forced_tool: bool, pad: str, region: str):
    from idp_common.config.merge_utils import merge_config_with_defaults
    from idp_common.extraction.forced_tool import build_extraction_tool_config
    from idp_common.extraction.service import ExtractionService

    path = os.path.join(REPO, "config_library", "unified", preset, "config.yaml")
    if not os.path.exists(path):
        return []
    cfg = merge_config_with_defaults(copy.deepcopy(yaml.safe_load(open(path))), validate=False)
    ext = cfg["extraction"]
    task = ext.get("task_prompt") or ""
    if "<<CACHEPOINT>>" not in task:
        print(f"  ({preset}: task prompt has no <<CACHEPOINT>> — nothing would cache)")
        return []
    head = task.split("<<CACHEPOINT>>")[0] + pad
    svc = ExtractionService(region=region, config=cfg)
    client = boto3.client("bedrock-runtime", region_name=region)

    out = []
    for c in cfg.get("classes") or []:
        cid = str(c.get("$id") or c.get("x-aws-idp-document-type"))
        attrs = svc._format_schema_for_prompt(c)
        msg = (
            head.replace("{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}", attrs)
            .replace("{DOCUMENT_CLASS}", cid)
            .replace("{FEW_SHOT_EXAMPLES}", "")
        )
        tc = build_extraction_tool_config(c)[0] if forced_tool else None
        n = measure_prefix(client, model, ext.get("system_prompt") or "", msg, tc)
        out.append({"preset": preset, "class": cid, "prefix_tokens": n})
        time.sleep(0.3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", action="append", required=True)
    ap.add_argument("--model", default="us.anthropic.claude-sonnet-4-6",
                    help="model used only to COUNT tokens; the verdict is per tier")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--forced-tool", action="store_true",
                    help="include the forced toolSpec, which renders at position 0")
    ap.add_argument("--pad-file", default=None, help="file whose text is appended to the prefix")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    pad = open(a.pad_file).read() if a.pad_file else ""
    rows = []
    for p in a.preset:
        print(f"measuring {p} ...", flush=True)
        rows += survey(p, a.model, a.forced_tool, pad, a.region)
    if not rows:
        raise SystemExit("nothing measured")

    hdr = "  ".join(f"{m:>5}" for m, _ in TIERS)
    print(f"\ntoken counter: {a.model}"
          f"{'  + forced toolSpec' if a.forced_tool else ''}"
          f"{f'  + pad ({len(pad)} chars)' if pad else ''}")
    print(f"\n{'preset / class':52} {'prefix':>7}   {hdr}")
    print(f"{'':52} {'':>7}   " + "  ".join(f"{'':>5}" for _ in TIERS))
    fails = {m: 0 for m, _ in TIERS}
    for r in sorted(rows, key=lambda x: (x["preset"], -x["prefix_tokens"])):
        marks = []
        for m, _ in TIERS:
            ok = r["prefix_tokens"] >= m
            marks.append(f"{'  ok ' if ok else ' MISS'}")
            if not ok:
                fails[m] += 1
        print(f"{(r['preset'] + ' / ' + r['class'])[:51]:52} {r['prefix_tokens']:>7}   " + "  ".join(marks))

    print(f"\n{'minimum':>8}  {'models':44} {'classes that NEVER cache':>26}")
    for m, names in TIERS:
        print(f"{m:>8}  {names:44} {fails[m]:>4} of {len(rows)}"
              f"  ({100 * fails[m] / len(rows):.0f}%)")
    if a.json:
        json.dump({"model_counter": a.model, "forced_tool": a.forced_tool,
                   "pad_chars": len(pad), "rows": rows, "tiers": TIERS},
                  open(a.json, "w"), indent=2)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
