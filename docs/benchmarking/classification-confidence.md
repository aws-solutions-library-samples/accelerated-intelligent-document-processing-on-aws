---
title: "Classification Confidence — Does the Score Carry Signal?"
---

> **A focused study, not a release audit.** It answers one question: when
> classification reports a confidence, is the number worth acting on — and does the
> answer depend on the classifier? For "which extraction config should I pick?" see
> [Configuration Guidance](./config-guidance.md); for release-over-release safety see
> the [Release Audit Trail](./releases/).

# Classification Confidence — Does the Score Carry Signal?

**Release:** v0.6.7.dev (branch `feature/issue-673-classification-confidence`) ·
**Region:** us-west-2 · **Stack:** `IDPBench066`
**Test set:** `docsplit` (DocSplit-Poly-Seq — RVL-CDIP-derived packets, 13 classes)
· first **20 packets**, **298 pages** per model, one run each, no failures
**Config:** `config_library/unified/docsplit/config.yaml` + `classification.confidence.mode: topk`
(`top_k_candidates: 3`), everything else at system defaults; summarization disabled
**Pricing:** `config_library/pricing.yaml` (rates as of 2026-08)

Reproduce with:

```bash
AWS_PROFILE=default python3 benchmarks/harness/run_classification_bench.py \
    --stack <STACK> --testset docsplit --n 20 --models nova2lite,haiku45 --mode topk
```

Raw data: `benchmarks/results/v0.6.7.dev1/clsconf/` (`summary.json`, per-page
`pages_*.json`, `escalation.json`).

---

## Abstract

Classification confidence was added in [#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)
and shipped **off by default**, on the argument that a self-reported number is
usually ~0.95 on everything and therefore worse than no score. This study measures
that argument on a genuinely confusable 13-class corpus, for the default
classification model (Amazon Nova 2 Lite) and a mid-tier alternative (Anthropic
Claude Haiku 4.5).

**The two models are equally accurate and not remotely equally calibrated.**
Accuracy differs by 0.7 points (84.6 % vs 85.2 %, inside noise at this sample size),
while the calibration separation differs by 4.7× (+0.044 vs +0.207). Nova 2 Lite put
**90 % of all pages at exactly 0.95** and emitted only 6 distinct confidence values
across 298 pages; Haiku 4.5 produced 11 values spread from 0.50 to 0.98. At a usable
threshold, Haiku catches **73 % of misclassifications by reviewing 11 % of pages at
94 % precision**; Nova catches 43 % by reviewing 8 % at 80 % precision, and half its
errors sit at 0.95 where nothing distinguishes them from correct pages.

Classification confidence therefore costs 6.4× more per page on the model where it
is actually informative. That is the trade to decide deliberately — which is why the
feature is opt-in and why this page exists.

---

## 1. Headline numbers

| Metric | Nova 2 Lite (default) | Claude Haiku 4.5 |
|---|---|---|
| Pages evaluated / scored | 298 / 298 | 298 / 298 |
| **Page classification accuracy** | **0.846** | **0.852** |
| Misclassified pages | 46 (15.4 %) | 44 (14.8 %) |
| **Calibration separation** (mean conf right − wrong) | **+0.044** | **+0.207** |
| Mean confidence, correct pages | 0.947 | 0.948 |
| Mean confidence, **wrong** pages | 0.903 | 0.741 |
| Median confidence, wrong pages | **0.95** | 0.735 |
| Distinct confidence values emitted | **6** | 11 |
| Pages below 0.9 confidence | 8.4 % | 20.8 % |
| **Classification cost / page** | **$0.00090** | **$0.00573** |
| Classification output tokens / page | 199 | 353 |

Accuracy being equal is itself worth noting: on this corpus the cheaper default
classifies as well as the 6× model. The difference is entirely in *knowing when it
is wrong*.

## 2. The failure mode, quantified

Confidence distribution over all 298 pages:

| Confidence | Nova 2 Lite | Haiku 4.5 |
|---|---|---|
| 0.98 | — | 114 |
| 0.95 | **269** | 79 |
| 0.92 | — | 43 |
| 0.90 | 2 | — |
| 0.85 | 22 | 28 |
| 0.75 | — | 11 |
| 0.70–0.72 | 2 | 8 |
| ≤ 0.65 | — | 15 |
| 1.00 | 2 | — |

Nova 2 Lite answers **0.95 for 90 % of pages** — including 23 of its 46 errors. This
is the "just ask for a number" failure the design warned about, and top-K only
partially mitigates it: the model did move 22 pages to 0.85 (18 of them genuinely
wrong), so the signal is a coarse two-level flag rather than a probability.

Haiku 4.5's distribution is graded, and its errors concentrate in the low bins.

## 3. What it buys you: escalation at a threshold

Flag pages below a confidence threshold for review. *Error recall* = share of the
model's misclassifications caught; *precision* = share of flagged pages that really
were wrong.

**Nova 2 Lite** (46 errors in 298 pages)

| Threshold | Pages flagged | Errors caught | Error recall | Precision |
|---|---|---|---|---|
| 0.95 | 27 (9.1 %) | 21 | 0.46 | 0.78 |
| **0.90** | **25 (8.4 %)** | **20** | **0.43** | **0.80** |
| 0.85 | 3 (1.0 %) | 2 | 0.04 | 0.67 |

**Claude Haiku 4.5** (44 errors in 298 pages)

| Threshold | Pages flagged | Errors caught | Error recall | Precision |
|---|---|---|---|---|
| 0.95 | 105 (35.2 %) | 39 | 0.89 | 0.37 |
| 0.90 | 62 (20.8 %) | 37 | 0.84 | 0.60 |
| **0.85** | **34 (11.4 %)** | **32** | **0.73** | **0.94** |
| 0.70 | 15 (5.0 %) | 14 | 0.32 | 0.93 |

Haiku dominates on both axes at its best operating point: fewer *wasted* reviews
(94 % of flagged pages are genuine errors) for far more coverage (73 % vs 43 %).
Nova's ceiling is structural — 23 errors are indistinguishable at 0.95, so no
threshold can reach them.

## 4. Prompt caching does not engage for Haiku here

Measured per page: Nova 2 Lite served **~934 input tokens/page from prompt cache**;
Haiku 4.5 reported **zero** cache reads and zero cache writes, paying its full
~3.3 K input tokens on every page.

Haiku 4.5 *is* in the accelerator's `CACHEPOINT_SUPPORTED_MODELS`, and the
classification prompt does carry `<<CACHEPOINT>>` (with the confidence block spliced
*before* it, deliberately). The likely cause is prefix size: the cacheable prefix
measured on Nova is ~934 tokens, below Anthropic's minimum cacheable prefix for
Haiku 4.5, so Bedrock silently declines to cache. This is an observation with a
probable cause, **not a verified diagnosis** — a config with a larger class
vocabulary or few-shot examples would push the prefix over the minimum and is the
obvious way to test it.

Practical consequence: on a small class vocabulary, do not assume caching softens the
cost of an Anthropic classifier. The 6.4× figure above is what you actually pay.

## 5. Guidance

1. **Leave `classification.confidence.mode` off unless you will act on the score.**
   It costs output tokens on every page and, on the default model, yields a coarse
   two-level flag.
2. **If you want an actionable score, change the classifier, not just the mode.** On
   this corpus Haiku 4.5's confidence supports review routing (73 % of errors from
   11 % of pages); Nova 2 Lite's supports a weak triage at best.
3. **Prefer `topk` over `verbalized`.** Even on Nova, ranked candidates pulled 22
   pages off the 0.95 pile; a single self-reported number has no such pressure.
   (This study did not run `verbalized` — treat that as untested here, not equal.)
4. **Set the threshold from your own measured table, not from a default.** The right
   cut differs per model: 0.90 for Nova, 0.85 for Haiku on this corpus.
5. **Re-measure on your own documents.** 298 pages of one corpus, one run per model.
   The harness prints `class_calibration_separation`, `class_accuracy` and
   `n_class_scored_pages` for exactly this purpose.

## 6. Honesty / limits

- **One run per model, 20 packets, 298 pages.** No repeats, so the 0.7-point accuracy
  gap is not a claim of difference; the calibration gap (4.7×, plus a 269-page spike
  at one value) is far larger than any plausible single-run noise, but it is still one
  sample of one corpus.
- **One corpus.** RVL-CDIP-derived scans over 13 classes — deliberately confusable,
  and not representative of a 2-class deployment where errors are rarer.
- Both models ran the identical config, identical prompt (the composed block is
  byte-identical), and the same first 20 documents (test-set file selection is sorted
  and deterministic). Runs were **sequential** so neither model's throttling shows up
  as the other's latency or cost.
- Cost covers the **classification step only** (`Classification/*` metering), which is
  the step the setting changes. Accuracy is page-level class accuracy from the
  evaluation report's `doc_split_metrics`, against the test set's curated baseline.
- Summarization was disabled; extraction ran at its defaults and is not scored here.
