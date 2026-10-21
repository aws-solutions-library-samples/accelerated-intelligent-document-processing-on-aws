#!/usr/bin/env python3
"""Measure CLASSIFICATION accuracy + confidence calibration on a labeled test set.

The config/scaling matrix in `run_matrix.py` is about extraction, and it only
launches SYNTHETIC docs (one PDF per run). Classification calibration needs the
opposite: a real, multi-class, genuinely confusable corpus with per-page class
ground truth, and many pages per run. This driver runs one such REFERENCE test
set (e.g. `docsplit` — 500 RVL-CDIP packets over 13 classes) once per
classification model, and reports:

  class_accuracy                  pages classified correctly / pages scored
  boundary_accuracy               sections whose PAGE RANGE matched, class ignored
  label_accuracy                  of those, the fraction whose CLASS also matched
  class_calibration_separation    mean(conf | right) - mean(conf | wrong)
  cost per page AND per document, input/output tokens, calls per document

The separation is the number that decides whether a reported confidence is worth
acting on (GitHub #673). It is computed over the POOLED per-page rows across all
documents, not as a mean of per-document separations: most single documents have
too few misclassified pages for a per-doc figure to mean anything, and averaging
undefined values silently drops the hard documents.

`boundary_accuracy` / `label_accuracy` decompose section accuracy, which
`split_accuracy_without_order` conflates by requiring a predicted section to
match on both its page range and its class (GitHub #1329). The split is what
makes a granularity comparison answerable: a model shown the whole packet should
place boundaries better, and `class_accuracy` cannot show that because it is per
PAGE and a page can carry the right class inside the wrong section.

It also compares CLASSIFICATION CONFIGURATIONS, not just models — `--variants`
crosses the models with named combinations of `granularity`, `modality` and
`strategy` (GitHub #1328, #1329, and #1513 for why this axis exists at all).

Usage:
  AWS_PROFILE=default python3 run_classification_bench.py --stack IDPBench066 \
      --testset docsplit --n 20 --models nova2lite,haiku45 --mode topk

  # Compare granularity and modality on one model, against the shipped default:
  ... --models nova2lite --variants page-both,packet-both,packet-text
  # Is two-stage cheaper on this corpus?
  ... --models nova2lite --variants page-both,twostage-packet

  ... --estimate      # print the plan (and the document count) and exit
  ... --score-only benchmarks/results/clsconf-<stamp>/runmap.json

⚠️ The matrix is MULTIPLICATIVE and cells drain one at a time, so the document
count is models x variants x n. Use --estimate before committing wall-clock.
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
    # Claude Haiku 5.5. Both numbers this driver reports are open questions on it and
    # neither can be read off the haiku45 row: at 10x less per token it is the
    # cheapest Claude that can hold a class vocabulary, and it is the first Haiku with
    # an effort control, which is exactly the kind of change that moves a confidence
    # distribution. class_calibration_separation is the one to watch — a cheap model
    # can score well on class_accuracy while reporting a confidence that does not
    # separate its right answers from its wrong ones, and that combination is worse
    # for a user than a lower accuracy with an honest confidence, because the
    # escalation path trusts the number (#673).
    #
    # Run it against haiku45 in the same invocation so the pair is one measurement:
    #   --models haiku45,haiku55 --mode topk
    "haiku55": "us.anthropic.claude-haiku-5-5",
    "haiku3": "us.anthropic.claude-3-haiku-20240307-v1:0",
    "sonnet46": "us.anthropic.claude-sonnet-4-6",
}

#: Reference test sets with per-page class ground truth, and the config_library
#: config whose class vocabulary matches them.
TESTSETS = {
    "docsplit": "config_library/unified/docsplit/config.yaml",
    "realkie": "config_library/unified/realkie-fcc-verified/config.yaml",
}


#: Classification configurations to compare, orthogonal to :data:`MODELS`.
#:
#: `granularity` decides how many calls are made, `modality` what each call sees,
#: and `strategy` whether boundaries and classes come from one call or two
#: (GitHub #1328, #1329). The three are independent, so this is a named subset of
#: their cross-product rather than an enum — the combinations worth the wall-clock
#: of a sequential drain, not all twelve.
#:
#: `page-both` is the shipped default and is the baseline every other cell is
#: read against. Keep it in any comparison; a cell with no baseline beside it
#: measures a number rather than a difference.
VARIANTS = {
    "page-both": {"granularity": "page", "text": True, "images": True},
    "page-text": {"granularity": "page", "text": True, "images": False},
    "packet-both": {"granularity": "packet", "text": True, "images": True},
    "packet-text": {"granularity": "packet", "text": True, "images": False},
    "packet-images": {"granularity": "packet", "text": False, "images": True},
    "twostage-packet": {
        "granularity": "packet",
        "text": True,
        "images": True,
        "strategy": "two_stage",
    },
    "twostage-page": {
        "granularity": "page",
        "text": True,
        "images": True,
        "strategy": "two_stage",
    },
}


def build_config(base_path, model_id, mode, top_k, variant=None):
    """Merge the test set's class config with system defaults + our overrides.

    Merging with the system defaults keeps the stored config self-contained (the
    same reason make_configs.py does it) and, critically, is what populates
    `classification.confidence.task_prompt_topk` — the block that gets spliced
    into the prompt. A config that set only `mode: topk` would ask for nothing.
    The same reasoning applies to a `granularity: packet` variant, which needs
    `classification.packet.task_prompt` from the defaults: packet granularity has
    its own prompt because it asks for a different output shape, and a config
    carrying only the flag would send the page prompt to the segments parser.

    Args:
        variant: A key of :data:`VARIANTS`, or None for the stored config's own
            settings (which is what this driver did before the axis existed).
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
    if variant:
        spec = VARIANTS[variant]
        classification["granularity"] = spec["granularity"]
        # Set EXPLICITLY rather than left empty. An unset modality flag is
        # inferred from the prompt's placeholders, which is the right default for
        # a user and the wrong one for a measurement: the cell would then be
        # describing whatever the prompt happens to say rather than the
        # combination its name claims.
        classification["modality"] = {
            "text": bool(spec["text"]),
            "images": bool(spec["images"]),
        }
        classification["strategy"] = spec.get("strategy", "single_pass")
    # Summarization is an unscored late step here; turning it off keeps the cost
    # figures attributable to OCR + classification + extraction.
    cfg.setdefault("summarization", {})["enabled"] = False
    return merge_config_with_defaults(copy.deepcopy(cfg), validate=True)


def assert_variant_merged(cfg, variant, mode):
    """Refuse a config whose variant did not survive the merge.

    The same reasoning as the confidence-block check: a cell whose `granularity`
    silently fell back to the default would launch, drain, score and look
    perfectly normal while measuring the baseline under another cell's name —
    and it would be indistinguishable from a real result saying the two
    granularities perform identically, which is precisely the question being
    asked. Checked here rather than trusted.

    Returns the list of problems; empty means the config really is what the
    cell's name claims.
    """
    problems = []
    cls = cfg.get("classification") or {}
    spec = VARIANTS[variant]

    if cls.get("granularity") != spec["granularity"]:
        problems.append(
            f"granularity is {cls.get('granularity')!r}, expected "
            f"{spec['granularity']!r}"
        )
    modality = cls.get("modality") or {}
    for flag in ("text", "images"):
        if bool(modality.get(flag)) is not bool(spec[flag]):
            problems.append(
                f"modality.{flag} is {modality.get(flag)!r}, expected {spec[flag]!r}"
            )
    expected_strategy = spec.get("strategy", "single_pass")
    if cls.get("strategy") != expected_strategy:
        problems.append(
            f"strategy is {cls.get('strategy')!r}, expected {expected_strategy!r}"
        )

    # The prompt a cell actually sends. Packet granularity reads
    # classification.packet.task_prompt and must ask for a `segments` array; the
    # page prompt asks for one class plus a boundary verdict and is read by a
    # different parser, so a packet cell running the page prompt fails every
    # document rather than measuring anything.
    if spec["granularity"] == "packet":
        packet_prompt = (cls.get("packet") or {}).get("task_prompt") or ""
        # Checked against the keys the PARSER requires rather than the word
        # "segments": a prompt merely mentioning segments in prose would satisfy
        # a substring test while asking for an output shape
        # `_parse_model_segments` cannot read, and every document would fail.
        missing = [
            key
            for key in ("segments", "ordinal_start_page", "ordinal_end_page")
            if key not in packet_prompt
        ]
        if missing:
            problems.append(
                "classification.packet.task_prompt does not ask for the output "
                f"shape the segments parser reads (missing {missing}) after merge"
            )
    if spec.get("strategy") == "two_stage":
        segmentation = cls.get("segmentation") or {}
        stage_a = (
            (segmentation.get("packet") or {}).get("task_prompt")
            if spec["granularity"] == "packet"
            else segmentation.get("task_prompt")
        ) or ""
        if not stage_a:
            problems.append(
                f"the {spec['granularity']} Stage A task_prompt is empty after "
                "merge, so two-stage would fall back to the single-pass prompt "
                "and carry the class vocabulary this strategy exists to drop"
            )
        if not (cls.get("labeling") or {}).get("task_prompt"):
            problems.append("classification.labeling.task_prompt is empty after merge")

    if mode != "off":
        conf = cls.get("confidence") or {}
        suffix = "topk" if mode == "topk" else "verbalized"
        # ⚠️ Mirror the service's own fallback rather than reading the page key
        # alone: at packet granularity it resolves `task_prompt_<mode>_packet or
        # task_prompt_<mode>`, so a config carrying only the packet-specific key
        # is legitimate and would otherwise be reported as having no confidence
        # prompt — a false problem, which is the one thing a guard like this must
        # not produce. Today's shipped defaults carry both keys, so this is about
        # a hand-written config rather than about the default.
        candidates = [f"task_prompt_{suffix}"]
        if cls.get("granularity") == "packet":
            candidates.insert(0, f"task_prompt_{suffix}_packet")
        block = next((conf.get(k) or "" for k in candidates if conf.get(k)), "")
        if "<class-confidence>" not in block:
            problems.append(
                f"confidence.task_prompt_{mode} block is empty after merge — the "
                "run would ask for no confidence at all"
            )
    return problems


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


#: Metering context prefixes that belong to the classification step.
#:
#: ⚠️ **Three, not one.** `single_pass` bills under `Classification/`; `two_stage`
#: bills its halves under `Segmentation/` and `Labeling/` and emits NO
#: `Classification/` entry at all (GitHub #1329). Filtering on `Classification/`
#: alone — which this function did — matched nothing for a two-stage run and
#: returned a cost of `0.0`, so the configuration with an extra call per segment
#: appeared to be the free one. That is the most misleading possible answer in a
#: cost comparison, and it is the same silent-zero this module refuses elsewhere
#: (#1079, #1146): adding nothing is the same arithmetic as adding zero.
CLASSIFICATION_CONTEXTS = ("Classification/", "Segmentation/", "Labeling/")


def classification_cost(metering):
    """``(cost, tokens, unpriced_reason, by_context)`` for the classification step.

    ``cost`` is null when something in the step's metering cannot be priced: the
    pooled `classification_cost_per_page` is a sum, so an entry that contributes
    nothing lowers it exactly as a zero would, and the model whose price is missing
    is the classification model this suite exists to compare (GitHub #1146). The
    token counts are a separate measurement and stay — they were read, not priced.

    ``by_context`` reports each of :data:`CLASSIFICATION_CONTEXTS` separately as
    well as summed. For a two-stage run that split is most of the point — "which
    half is the money in, finding the boundaries or labelling the segments?" is
    the question the strategy is chosen on, and a single total cannot answer it.
    """
    cls = {
        k: v
        for k, v in (metering or {}).items()
        if k.startswith(CLASSIFICATION_CONTEXTS)
    }
    priced = lib.price_metering(cls)
    tokens = {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0}
    by_context = {}
    for key, units in cls.items():
        context = key.split("/", 1)[0]
        bucket = by_context.setdefault(
            context,
            {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": 0,
                "lambda_invocations": 0,
                # None, not 0: see the note below on "not metered" vs "zero".
                "bedrock_calls": None,
            },
        )
        for unit, n in (units or {}).items():
            if not isinstance(n, (int, float)):
                continue
            if unit in tokens:
                tokens[unit] += n
                bucket[unit] += n
            # ⚠️ TWO different counts, and conflating them is the mistake this
            # comment exists to stop. Measured on IDP1:
            #
            #   `<ctx>/lambda/requests`  -> Lambda INVOCATIONS. A 6-page document
            #       at `page` granularity makes six Bedrock calls inside ONE
            #       invocation, so this reads 1 for `page` and `packet` alike and
            #       cannot tell them apart. In a two-stage run the single
            #       invocation lands under `Classification` while the spend lands
            #       under `Segmentation`/`Labeling`, so those two read 0.
            #
            #   `<ctx>/bedrock/<model>` `requests` -> BEDROCK CALLS. This is the
            #       count packet granularity's N-calls-to-one claim is about, and
            #       it is per stage: a two-stage page-granularity run on a 6-page
            #       document measured 6 under `Segmentation` and 6 under
            #       `Labeling`.
            #
            # ⚠️ The Bedrock count is NOT always present — the same stack emitted
            # a `Classification/bedrock/...` entry without a `requests` key on one
            # document and with `requests: 6` on another — so it is reported as
            # `None` rather than `0` when no entry carried it. Zero calls and "not
            # metered" are different facts, and a 0 here would read as the former.
            if unit == "requests":
                if key.endswith("/lambda/requests"):
                    bucket["lambda_invocations"] += n
                elif "/bedrock/" in key:
                    bucket["bedrock_calls"] = (bucket["bedrock_calls"] or 0) + n
    for context in CLASSIFICATION_CONTEXTS:
        name = context.rstrip("/")
        if name in by_context:
            per_context = lib.price_metering(
                {k: v for k, v in cls.items() if k.startswith(context)}
            )
            by_context[name]["cost"] = (
                round(per_context.total, 6) if per_context.complete else None
            )
    if not priced.complete:
        return None, tokens, priced.why, by_context
    return priced.total, tokens, None, by_context


def drain(res, runs, n, poll_interval, timeout_min):
    """Wait until every doc in each run has completed or failed."""
    # `cell` not `model`: with --variants, two cells of the same model print
    # identically and the in-flight list stops identifying which run finished.
    # Same fallback `--score-only` uses.
    print(f"  draining {[r.get('cell') or r['model'] for r in runs]}...")
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        pending = []
        for r in runs:
            st = lib.poll_run(res["tracking_table"], r["run_id"])
            if st["obj_done"] + st["failed"] < n:
                pending.append(
                    f"{r.get('cell') or r['model']}:"
                    f"{st['obj_done']}done+{st['failed']}fail/{n}"
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
    cls_by_context = {}
    page_count = 0
    # Pooled across documents rather than averaged per document, for the same
    # reason the confidence separation is: most single documents have too few
    # sections for a per-doc figure to mean anything, and averaging a
    # `label_accuracy` of None as 0 would report "every label wrong" for a
    # document whose labels were never reached.
    boundary_matched = boundary_total = 0
    label_matched = label_total = 0

    unread = []
    unpriced = []
    for prefix in lib.list_doc_prefixes(bucket, run_id):
        doc_name = prefix[len(run_id) + 1 :].rstrip("/")
        ev_read = lib.read_json(bucket, prefix + "evaluation/results.json")
        if ev_read.is_failed:
            # An evaluation report that would not read contributes no pages. Counting
            # it among `n_docs` with zero scored pages would thin every pooled figure
            # below without saying so, and a report that is merely ABSENT (evaluation
            # not enabled) is a different and benign state (GitHub #1079).
            unread.append(f"{doc_name}: eval report {ev_read.error}")
            continue
        docs.append(doc_name)
        ev = ev_read.value_or(None)
        one = score_classification(ev)
        per_doc.append({"doc": doc_name, **one})
        ds = (ev or {}).get("doc_split_metrics") or {}
        # Pool the COUNTS, not the per-document ratios: a 1-section document and
        # a 12-section one would otherwise weigh the same.
        for counter, key in (
            ("boundary", "boundary_matched_sections"),
            ("label", "label_matched_sections"),
        ):
            matched = ds.get(key)
            if isinstance(matched, int):
                if counter == "boundary":
                    boundary_matched += matched
                else:
                    label_matched += matched
        # ⚠️ `total_splits` predates the two halves (#1329), so a report from an
        # older stack — or an older runmap replayed through `--score-only`, both
        # supported, and comparing across releases is a stated purpose of this
        # driver — carries the denominator and NOT the numerator. Adding to it
        # unconditionally made the pooled figure read `boundary_accuracy: 0.0`
        # alongside `sections_evaluated: 8`: "every boundary placed wrong", for a
        # corpus where no boundary was judged at all. Measured, not supposed.
        #
        # That is the silent zero #1079/#1146 put off limits, and `label_total`
        # below already avoided it by keying off a field only the new reports
        # carry. The denominator therefore advances only for a document that also
        # carries the numerator, so an old report pools nothing and the figure
        # comes out `None` — not scored, which is the truth.
        if isinstance(ds.get("total_splits"), int) and isinstance(
            ds.get("boundary_matched_sections"), int
        ):
            boundary_total += ds["total_splits"]
        # The label denominator is the sections whose BOUNDARIES matched — the
        # only ones where a label was judged at all.
        if isinstance(ds.get("boundary_matched_sections"), int):
            label_total += ds["boundary_matched_sections"]
        for row in ds.get("page_details") or []:
            pages_total += 1
            if row.get("correct"):
                pages_correct += 1
            c = row.get("predicted_confidence")
            if isinstance(c, (int, float)):
                (pooled_right if row.get("correct") else pooled_wrong).append(c)
        metering_read = lib.read_metering(tracking, run_id, doc_name)
        if metering_read.is_present:
            cost, tokens, why, by_context = classification_cost(metering_read.value)
            for k in cls_tokens:
                cls_tokens[k] += tokens[k]
            # ⚠️ NOT `bucket`: that name is bound at the top of this function to
            # the S3 OUTPUT BUCKET and is still read by `lib.read_json` on every
            # later iteration of the enclosing `for prefix in ...` loop. Rebinding
            # it here and botocore gets a dict as `Bucket=` from the second
            # document onward — the run still completes, prints read failures
            # that look environmental, and reports `n_docs: 1`, pooling every
            # accuracy and cost figure over one document instead of the corpus.
            for context, ctx_tokens in by_context.items():
                acc = cls_by_context.setdefault(
                    context,
                    {
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "cacheReadInputTokens": 0,
                        "lambda_invocations": 0,
                        "bedrock_calls": None,
                        "cost": 0.0,
                        "cost_unpriced_docs": 0,
                    },
                )
                for k in (
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                    "lambda_invocations",
                ):
                    acc[k] += ctx_tokens.get(k, 0)
                calls = ctx_tokens.get("bedrock_calls")
                if calls is not None:
                    acc["bedrock_calls"] = (acc["bedrock_calls"] or 0) + calls
                if ctx_tokens.get("cost") is None:
                    acc["cost_unpriced_docs"] += 1
                else:
                    acc["cost"] += ctx_tokens["cost"]
            if cost is None:
                # Same arithmetic as adding zero, so the run's cost is withheld
                # rather than reported short (#1146).
                unpriced.append(f"{doc_name}: metering {why}")
            else:
                cls_cost += cost
        else:
            # Adding nothing is the same arithmetic as adding zero, so the cost is
            # reported as unpriced instead: `classification_cost_per_page` over a
            # partially-read run understates the per-page cost in proportion to how
            # much went unread (GitHub #1079).
            unread.append(f"{doc_name}: metering {metering_read.state}")
        row = lib.doc_row(tracking, run_id, doc_name)
        try:
            page_count += int(row.get("PageCount") or 0)
        except (TypeError, ValueError):
            pass

    scored = pooled_right + pooled_wrong
    sep = None
    if pooled_right and pooled_wrong:
        sep = round(statistics.fmean(pooled_right) - statistics.fmean(pooled_wrong), 4)
    if unread:
        print(f"  ⚠ {len(unread)} read(s) FAILED on {run_id} — excluded, not zeroed:")
        for note in unread[:5]:
            print(f"      {note}")
    if unpriced:
        print(
            f"  ⚠ {len(unpriced)} doc(s) on {run_id} carry metering `pricing.yaml` "
            "cannot price — this run reports NO cost rather than one below truth:"
        )
        for note in unpriced[:5]:
            print(f"      {note}")
    # A pooled sum over some of the documents is below truth by an unknown amount,
    # and per-page cost is the figure this suite compares models on (#1146).
    priceable = not unpriced
    return {
        "run_id": run_id,
        "n_docs": len(docs),
        # Reads that failed, so every figure below is over fewer documents than the
        # run holds. Null on a clean run; a number here makes the cost and accuracy
        # provisional rather than wrong-and-silent (#1079).
        "unread": unread or None,
        "classification_cost_unread_docs": (
            sum(1 for n in unread if "metering" in n) or None
        ),
        # Documents whose metering read fine and could not be fully priced (#1146).
        "classification_cost_unpriced": unpriced or None,
        "pages_evaluated": pages_total,
        "pages_scored": len(scored),
        "class_accuracy": round(pages_correct / pages_total, 4)
        if pages_total
        else None,
        # Section accuracy split into boundary and label halves (#1329), pooled
        # over sections across the run. `split_accuracy_without_order` requires
        # both to match, so it cannot say whether a granularity wins on
        # boundaries and loses on labels — which is the question a
        # page-vs-packet comparison is actually asking.
        "sections_evaluated": boundary_total or None,
        "boundary_accuracy": round(boundary_matched / boundary_total, 4)
        if boundary_total
        else None,
        # ⚠️ Null means no section's boundaries matched, so no label was judged.
        # Distinct from 0.0, which would mean every judged label was wrong.
        "label_accuracy": round(label_matched / label_total, 4)
        if label_total
        else None,
        "sections_boundary_matched": boundary_matched,
        "sections_label_matched": label_matched,
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
        "classification_cost": round(cls_cost, 5) if priceable else None,
        "pages_processed": page_count,
        "classification_cost_per_page": round(cls_cost / page_count, 6)
        if page_count and priceable
        else None,
        # Reported alongside per-page because the two flatter opposite
        # granularities: per-page favours packet on long packets, per-document
        # favours page on short ones, and quoting one alone invites the wrong
        # conclusion about which setting to use.
        "classification_cost_per_document": round(cls_cost / len(docs), 6)
        if docs and priceable
        else None,
        "classification_output_tokens_per_page": round(
            cls_tokens["outputTokens"] / page_count, 1
        )
        if page_count
        else None,
        # The prefix saving is an INPUT effect and the per-page reasoning saving
        # is an OUTPUT effect; a single total hides which one a given corpus
        # realises, and they move in opposite directions between granularities.
        "classification_input_tokens_per_page": round(
            cls_tokens["inputTokens"] / page_count, 1
        )
        if page_count
        else None,
        "classification_tokens": cls_tokens,
        # Per metering context, so a two-stage run shows where the money and the
        # tokens went — Segmentation or Labeling — rather than only their sum.
        # See the note on `lambda_invocations` above for what that count does and
        # does not evidence.
        # The loop variable is `totals`, not `bucket`: a comprehension scopes its
        # own name so this one cannot leak, but `bucket` is the S3 output bucket
        # in this function and copying this block somewhere it would leak is the
        # whole shape of the defect fixed above.
        "classification_by_context": {
            context: {
                **totals,
                "cost": round(totals["cost"], 6)
                if not totals["cost_unpriced_docs"]
                else None,
                "lambda_invocations_per_document": round(
                    totals["lambda_invocations"] / len(docs), 2
                )
                if docs
                else None,
                # The figure the N-calls-to-one claim is actually about. `None`
                # when nothing carried a Bedrock request count, never 0.
                "bedrock_calls_per_document": round(
                    totals["bedrock_calls"] / len(docs), 2
                )
                if docs and totals["bedrock_calls"] is not None
                else None,
            }
            for context, totals in sorted(cls_by_context.items())
        }
        or None,
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
    ap.add_argument(
        "--variants",
        default="",
        help=(
            "comma-separated classification configurations to compare, from: "
            f"{','.join(VARIANTS)}. Empty (the default) runs one cell per model "
            "using the test set's own stored classification settings, which is "
            "what this driver did before the axis existed. ⚠️ The matrix is "
            "MULTIPLICATIVE and cells drain sequentially, so --variants with 7 "
            "entries and 2 models is 14 cells one after another — use "
            "--estimate first. Include page-both as the baseline; a cell with "
            "no baseline beside it measures a number rather than a difference."
        ),
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
            # Keyed by the cell, falling back to the model for a runmap written
            # before the variant axis existed.
            r.get("cell") or r["model"]: score_run(res, r["run_id"])
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

    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    unknown_variants = [v for v in variants if v not in VARIANTS]
    if unknown_variants:
        sys.exit(f"unknown variant(s) {unknown_variants}; known: {list(VARIANTS)}")
    if variants and "page-both" not in variants:
        # A warning, not an error: comparing two packet variants against each
        # other is a legitimate question. But a run with no baseline cannot say
        # whether either beats what is shipped, which is usually the question.
        print(
            "  ⚠ page-both (the shipped default) is not in --variants, so these "
            "cells have no baseline to be read against."
        )
    # One cell per (model, variant). An empty --variants keeps the pre-axis
    # shape: one cell per model on the test set's own stored settings.
    cells = [{"model": m, "variant": v} for m in models for v in (variants or [None])]

    res = resolve_stack(a.stack)
    print("stack resources:", json.dumps(res, indent=2))
    for k, v in res.items():
        if not v:
            sys.exit(f"could not resolve {k} for stack {a.stack}")

    print(
        f"plan: testset={a.testset} n={a.n} docs x {len(cells)} cell(s) "
        f"({len(models)} model(s) {models} x "
        f"{len(variants) or 1} variant(s) {variants or ['(stored config)']}), "
        f"classification.confidence.mode={a.mode}"
    )
    print(f"       {len(cells) * a.n} documents total, drained one cell at a time")
    if a.estimate:
        print("Run without --estimate to execute.")
        return

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(RESULTS, f"clsconf-{stamp}")
    os.makedirs(outdir, exist_ok=True)
    runs = []

    for cell in cells:
        name, variant = cell["model"], cell["variant"]
        cell_id = f"{name}-{variant}" if variant else name
        version = f"bench-clsconf-{cell_id}-{a.mode}"
        cfg = build_config(
            TESTSETS[a.testset], MODELS[name], a.mode, a.top_k, variant=variant
        )
        path = os.path.join(outdir, f"{version}.yaml")
        yaml.safe_dump(cfg, open(path, "w"), sort_keys=False)
        # Prove the composed config really is what the cell claims. A cell whose
        # granularity or confidence block failed to merge would launch, drain and
        # score while measuring the DEFAULT under another cell's name — and that
        # is indistinguishable from a real result saying the two settings perform
        # identically, which is the question being asked.
        if variant:
            problems = assert_variant_merged(cfg, variant, a.mode)
        else:
            block = (cfg.get("classification", {}).get("confidence") or {}).get(
                f"task_prompt_{'topk' if a.mode == 'topk' else 'verbalized'}", ""
            )
            problems = (
                []
                if a.mode == "off" or "<class-confidence>" in block
                else ["confidence prompt block is empty after merge"]
            )
        if problems:
            sys.exit(
                f"{version}: the merged config is not what this cell claims, so "
                f"the run would measure something else:\n  - "
                + "\n  - ".join(problems)
                + "\nCheck base-classification.yaml."
            )
        ok = upload_config(a.stack, version, path, res=res)
        print(f"  config {version}: {'ok' if ok else 'FAIL'} ({path})")
        if not ok:
            sys.exit(f"config upload failed for {version}")
        rid = launch(a.stack, a.testset, version, f"clsconf-{cell_id}-{a.mode}", a.n)
        print(f"  launched {cell_id} -> run {rid}")
        runs.append(
            {
                "cell": cell_id,
                "model": name,
                "model_id": MODELS[name],
                "variant": variant,
                "variant_spec": VARIANTS[variant] if variant else None,
                "version": version,
                "run_id": rid,
            }
        )
        json.dump(
            {
                "stack": a.stack,
                "testset": a.testset,
                "n": a.n,
                "mode": a.mode,
                "top_k": a.top_k,
                "variants": variants or None,
                "resources": res,
                "runs": runs,
            },
            open(os.path.join(outdir, "runmap.json"), "w"),
            indent=2,
        )
        # Drain THIS cell before launching the next. Running cells concurrently
        # would have them competing for the same Bedrock quota, so one cell's
        # throttling-and-backoff would show up as another's latency — and a
        # retried call is also a cost the comparison would misattribute. This
        # applies at least as strongly across variants as across models: a
        # packet cell and a page cell issue very different call volumes for the
        # same documents, so run them concurrently and the page cell's
        # per-page-call burst throttles the packet cell it is being compared
        # against. Sequential costs wall-clock and buys a fair comparison.
        drain(res, [runs[-1]], a.n, a.poll_interval, a.timeout_min)

    # Evaluation lands after the document completes; give it a moment.
    time.sleep(30)
    out = {r["cell"]: score_run(res, r["run_id"]) for r in runs}
    print(json.dumps(out, indent=2, default=str))
    json.dump(
        out, open(os.path.join(outdir, "summary.json"), "w"), indent=2, default=str
    )
    print(f"\nwrote {outdir}/summary.json")


if __name__ == "__main__":
    main()
