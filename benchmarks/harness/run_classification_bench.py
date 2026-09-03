#!/usr/bin/env python3
"""Measure CLASSIFICATION accuracy + confidence calibration on a labeled test set.

The config/scaling matrix in `run_matrix.py` is about extraction, and it only
launches SYNTHETIC docs (one PDF per run). Classification calibration needs the
opposite: a real, multi-class, genuinely confusable corpus with per-page class
ground truth, and many pages per run. This driver runs one such REFERENCE test
set (e.g. `docsplit` — 500 RVL-CDIP packets over 13 classes) once per
classification model, and reports:

  class_accuracy                  pages classified correctly / pages scored
  class_calibration_separation    mean(conf | right) - mean(conf | wrong)
  cost per page + classification output tokens per page

The separation is the number that decides whether a reported confidence is worth
acting on (GitHub #673). It is computed over the POOLED per-page rows across all
documents, not as a mean of per-document separations: most single documents have
too few misclassified pages for a per-doc figure to mean anything, and averaging
undefined values silently drops the hard documents.

Usage:
  AWS_PROFILE=default python3 run_classification_bench.py --stack IDPBench066 \
      --testset docsplit --n 20 --models nova2lite,haiku45 --mode topk
  ... --estimate      # print the plan and exit
  ... --score-only benchmarks/results/clsconf-<stamp>/runmap.json
"""

# ruff: noqa: E402  (local sibling imports require the sys.path bootstrap first)
import argparse
import copy
import datetime
import json
import os
import statistics
import sys
import time

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import score_classification
from run_matrix import resolve_stack, upload_config

import lib

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BENCH)
RESULTS = os.path.join(BENCH, "results")

#: Classification models to compare. Keys are the CLI names.
MODELS = {
    "nova2lite": "us.amazon.nova-2-lite-v1:0",
    "novalite": "us.amazon.nova-lite-v1:0",
    "haiku45": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "haiku3": "us.anthropic.claude-3-haiku-20240307-v1:0",
    "sonnet46": "us.anthropic.claude-sonnet-4-6",
}

#: Reference test sets with per-page class ground truth, and the config_library
#: config whose class vocabulary matches them.
TESTSETS = {
    "docsplit": "config_library/unified/docsplit/config.yaml",
    "realkie": "config_library/unified/realkie-fcc-verified/config.yaml",
}


def build_config(base_path, model_id, mode, top_k):
    """Merge the test set's class config with system defaults + our overrides.

    Merging with the system defaults keeps the stored config self-contained (the
    same reason make_configs.py does it) and, critically, is what populates
    `classification.confidence.task_prompt_topk` — the block that gets spliced
    into the prompt. A config that set only `mode: topk` would ask for nothing.
    """
    from idp_common.config.merge_utils import merge_config_with_defaults

    cfg = yaml.safe_load(open(os.path.join(REPO, base_path)))
    cfg.pop("description", None)
    cfg.pop("managed", None)
    classification = cfg.setdefault("classification", {})
    classification["model"] = model_id
    classification["confidence"] = {
        "mode": mode,
        "top_k_candidates": str(top_k),
    }
    # Summarization is an unscored late step here; turning it off keeps the cost
    # figures attributable to OCR + classification + extraction.
    cfg.setdefault("summarization", {})["enabled"] = False
    return merge_config_with_defaults(copy.deepcopy(cfg), validate=True)


def launch(stack, testset_id, version, context, n_files):
    """Invoke the TestRunner Lambda for a whole test set (n_files documents)."""
    from run_matrix import _find_fn

    lam = lib.session().client("lambda", region_name=lib.REGION)
    runner = _find_fn(stack, "TestRunnerFunction")
    if not runner:
        sys.exit(f"TestRunnerFunction not found for stack {stack}")
    resolver = _find_fn(stack, "TestSetResolverFunction")
    if resolver:  # refresh test-set discovery (non-fatal)
        try:
            lam.invoke(
                FunctionName=resolver,
                Payload=json.dumps(
                    {"info": {"fieldName": "getTestSets"}, "arguments": {}}
                ),
            )
        except Exception:
            pass
    payload = {
        "arguments": {
            "input": {
                "testSetId": testset_id,
                "configVersion": version,
                "numberOfFiles": n_files,
                "context": context,
            }
        }
    }
    resp = lam.invoke(FunctionName=runner, Payload=json.dumps(payload))
    result = json.loads(resp["Payload"].read())
    if "errorMessage" in result:
        sys.exit(f"TestRunner error: {result['errorMessage']}")
    return result.get("testRunId")


def classification_cost(metering):
    """Cost + token counts attributable to the Classification step only."""
    cls = {k: v for k, v in (metering or {}).items() if k.startswith("Classification/")}
    cost, _ = lib.price_metering(cls)
    tokens = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0}
    for units in cls.values():
        for unit, n in (units or {}).items():
            if unit in tokens and isinstance(n, (int, float)):
                tokens[unit] += n
    return cost, tokens


def drain(res, runs, n, poll_interval, timeout_min):
    """Wait until every doc in each run has completed or failed."""
    print(f"  draining {[r['model'] for r in runs]}...")
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        pending = []
        for r in runs:
            st = lib.poll_run(res["tracking_table"], r["run_id"])
            if st["obj_done"] + st["failed"] < n:
                pending.append(
                    f"{r['model']}:{st['obj_done']}done+{st['failed']}fail/{n}"
                )
        if not pending:
            print("  all runs settled")
            return True
        print(f"    pending {pending}")
        time.sleep(poll_interval)
    print(f"  TIMEOUT after {timeout_min}min — scoring what completed")
    return False


def score_run(res, run_id):
    """Pool per-page classification rows for one run and score them."""
    bucket, tracking = res["output_bucket"], res["tracking_table"]
    pooled_right, pooled_wrong = [], []
    pages_total = pages_correct = 0
    docs, per_doc = [], []
    cls_cost = 0.0
    cls_tokens = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0}
    page_count = 0

    for prefix in lib.list_doc_prefixes(bucket, run_id):
        doc_name = prefix[len(run_id) + 1 :].rstrip("/")
        docs.append(doc_name)
        ev = lib.get_json(bucket, prefix + "evaluation/results.json")
        one = score_classification(ev)
        per_doc.append({"doc": doc_name, **one})
        ds = (ev or {}).get("doc_split_metrics") or {}
        for row in ds.get("page_details") or []:
            pages_total += 1
            if row.get("correct"):
                pages_correct += 1
            c = row.get("predicted_confidence")
            if isinstance(c, (int, float)):
                (pooled_right if row.get("correct") else pooled_wrong).append(c)
        metering = lib.doc_metering(tracking, run_id, doc_name)
        cost, tokens = classification_cost(metering)
        cls_cost += cost
        for k in cls_tokens:
            cls_tokens[k] += tokens[k]
        row = lib.doc_row(tracking, run_id, doc_name)
        try:
            page_count += int(row.get("PageCount") or 0)
        except (TypeError, ValueError):
            pass

    scored = pooled_right + pooled_wrong
    sep = None
    if pooled_right and pooled_wrong:
        sep = round(statistics.fmean(pooled_right) - statistics.fmean(pooled_wrong), 4)
    return {
        "run_id": run_id,
        "n_docs": len(docs),
        "pages_evaluated": pages_total,
        "pages_scored": len(scored),
        "class_accuracy": round(pages_correct / pages_total, 4)
        if pages_total
        else None,
        "class_calibration_separation": sep,
        "mean_conf_correct": round(statistics.fmean(pooled_right), 4)
        if pooled_right
        else None,
        "mean_conf_wrong": round(statistics.fmean(pooled_wrong), 4)
        if pooled_wrong
        else None,
        "n_conf_correct": len(pooled_right),
        "n_conf_wrong": len(pooled_wrong),
        "median_conf_correct": round(statistics.median(pooled_right), 4)
        if pooled_right
        else None,
        "median_conf_wrong": round(statistics.median(pooled_wrong), 4)
        if pooled_wrong
        else None,
        "pct_conf_below_0.9": round(
            100 * sum(1 for c in scored if c < 0.9) / len(scored), 1
        )
        if scored
        else None,
        "classification_cost": round(cls_cost, 5),
        "pages_processed": page_count,
        "classification_cost_per_page": round(cls_cost / page_count, 6)
        if page_count
        else None,
        "classification_output_tokens_per_page": round(
            cls_tokens["outputTokens"] / page_count, 1
        )
        if page_count
        else None,
        "classification_tokens": cls_tokens,
        "per_doc": per_doc,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stack", required=True)
    ap.add_argument("--testset", default="docsplit", choices=sorted(TESTSETS))
    ap.add_argument("--n", type=int, default=20, help="documents per model")
    ap.add_argument(
        "--models",
        default="nova2lite,haiku45",
        help=f"comma-separated, from: {','.join(sorted(MODELS))}",
    )
    ap.add_argument("--mode", default="topk", choices=["off", "topk", "verbalized"])
    ap.add_argument("--top-k", type=int, default=3)
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--score-only", help="path to a runmap.json from a previous run")
    ap.add_argument("--poll-interval", type=int, default=60)
    ap.add_argument("--timeout-min", type=int, default=90)
    a = ap.parse_args()

    if a.score_only:
        runmap = json.load(open(a.score_only))
        res = runmap["resources"]
        out = {
            r["model"]: score_run(res, r["run_id"])
            for r in runmap["runs"]
            if r["run_id"]
        }
        print(json.dumps(out, indent=2, default=str))
        summary_path = os.path.join(os.path.dirname(a.score_only), "summary.json")
        json.dump(out, open(summary_path, "w"), indent=2, default=str)
        print(f"\nwrote {summary_path}")
        return

    models = [m.strip() for m in a.models.split(",") if m.strip()]
    unknown = [m for m in models if m not in MODELS]
    if unknown:
        sys.exit(f"unknown model(s) {unknown}; known: {sorted(MODELS)}")

    res = resolve_stack(a.stack)
    print("stack resources:", json.dumps(res, indent=2))
    for k, v in res.items():
        if not v:
            sys.exit(f"could not resolve {k} for stack {a.stack}")

    print(
        f"plan: testset={a.testset} n={a.n} docs x {len(models)} model(s) "
        f"{models}, classification.confidence.mode={a.mode}"
    )
    if a.estimate:
        print("Run without --estimate to execute.")
        return

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(RESULTS, f"clsconf-{stamp}")
    os.makedirs(outdir, exist_ok=True)
    runs = []

    for name in models:
        version = f"bench-clsconf-{name}-{a.mode}"
        cfg = build_config(TESTSETS[a.testset], MODELS[name], a.mode, a.top_k)
        path = os.path.join(outdir, f"{version}.yaml")
        yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)
        # Prove the composed prompt really asks for what the cell claims: a config
        # whose confidence block failed to merge would run, score, and look
        # perfectly normal while measuring nothing.
        block = (cfg.get("classification", {}).get("confidence") or {}).get(
            f"task_prompt_{'topk' if a.mode == 'topk' else 'verbalized'}", ""
        )
        if a.mode != "off" and "<class-confidence>" not in block:
            sys.exit(
                f"{version}: confidence prompt block is empty after merge — the "
                "run would measure nothing. Check base-classification.yaml."
            )
        ok = upload_config(a.stack, version, path, res=res)
        print(f"  config {version}: {'ok' if ok else 'FAIL'} ({path})")
        if not ok:
            sys.exit(f"config upload failed for {version}")
        rid = launch(a.stack, a.testset, version, f"clsconf-{name}-{a.mode}", a.n)
        print(f"  launched {name} -> run {rid}")
        runs.append(
            {"model": name, "model_id": MODELS[name], "version": version, "run_id": rid}
        )
        json.dump(
            {
                "stack": a.stack,
                "testset": a.testset,
                "n": a.n,
                "mode": a.mode,
                "top_k": a.top_k,
                "resources": res,
                "runs": runs,
            },
            open(os.path.join(outdir, "runmap.json"), "w"),
            indent=2,
        )
        # Drain THIS model before launching the next. Running the models
        # concurrently would have them competing for the same Bedrock quota, so
        # one model's throttling-and-backoff would show up as the other's
        # latency — and a retried call is also a cost the comparison would
        # misattribute. Sequential costs wall-clock and buys a fair comparison.
        drain(res, [runs[-1]], a.n, a.poll_interval, a.timeout_min)

    # Evaluation lands after the document completes; give it a moment.
    time.sleep(30)
    out = {r["model"]: score_run(res, r["run_id"]) for r in runs}
    print(json.dumps(out, indent=2, default=str))
    json.dump(
        out, open(os.path.join(outdir, "summary.json"), "w"), indent=2, default=str
    )
    print(f"\nwrote {outdir}/summary.json")


if __name__ == "__main__":
    main()
