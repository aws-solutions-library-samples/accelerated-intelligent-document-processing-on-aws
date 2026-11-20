---
title: "Confidence Assessment Benchmarks"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Confidence Assessment Benchmarks

Confidence assessment gives every extracted value a score, so a person reviews the values
most likely to be wrong instead of every value.
[Confidence Assessment](../idp-fundamentals/confidence-assessment.md) explains how it works.

## Improving confidence

**Your definitions.** The scoring model reads each field's description when it judges a
value. Say what a right value looks like. Mark the fields you can't do without as required:
an empty required field goes to review even though it has no score. See
[Improving confidence](../idp-fundamentals/confidence-assessment.md#improving-accuracy).

**Data quality.** Poor scans cause more extraction mistakes, and catching those mistakes is
the job confidence does. We measure it on [ConfBench](../extensions/confbench-testset.md),
which adds scan noise to real invoices.

**Settings.**

| Setting | What it changes | Benchmarks |
|---|---|---|
| Scoring mode | A separate scoring pass, scores inside the extraction call, or none | [CF-03](#separate-or-integrated-scoring) |
| Scoring model | The model that scores each value | [CF-02](#scoring-model) |
| Confidence threshold | The score below which a value goes to review | [CF-01](#planned), [CF-04](#planned) |
| Page images | Whether the scoring model sees the pages | [CF-06](#planned) |

## Results

<details>
<summary>What the metrics mean</summary>

- *AUROC:* the chance that a wrong value scores lower than a right one. 0.5 is a coin flip.
- *Calibration error:* the gap between the scores and the measured accuracy.
- *Values below threshold:* the share of values the threshold sends to review.
- *Errors below threshold:* the share of wrong values the threshold sends to review.
- *ECARB at a 30% review budget:* review the 30% of values with the lowest scores, and count how many times as many wrong values you find as a random 30% review would. 1× is no better than random, which the charts draw as a dashed line; the most possible is about 3.3×.
- *Errors caught reviewing 30% of values:* the share of wrong values a reviewer finds by
  checking the 30% of values with the lowest scores. Checking 30% picked at random finds
  about 30%.

Full definitions: Stickler's [Confidence Metrics](https://awslabs.github.io/stickler/Advanced/confidence-metrics/), which computes AUROC and calibration error.

[Benchmark Datasets](datasets.md) describes each dataset.

</details>

<!-- BEGIN GENERATED FAQ -->

### Scoring model

![Haiku 5.5 catches errors like Sonnet 5.5, at a fifth the cost: AUROC and ECARB at a 30% review budget against cost per 1,000 documents, one point per scoring model](../../images/benchmark-guidance/CF-02-fields.svg)

*Bands show ±2 standard errors. Dashed lines mark coin flip (50%, no better than chance) and random review (1×, what reviewing a random 30% of values catches).*

**Finding:** Claude Haiku 5.5 is a good place to start. On RealKIE invoices, reviewing the 30% of values it was least sure of found 2.4 times as many wrong values as reviewing a random 30% (this ratio is ECARB). That's as many as Claude Sonnet 5.5, for a fifth of the cost. Amazon Nova Lite, the preset's default, did worse than a random review (0.6 times). Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 23 Test Studio runs, listed in `benchmarks/results/guidance/CF-02.json`.*

Amazon Nova Lite, the preset's scoring model, caught 18%, fewer than reviewing a random 30%. Amazon Nova 2 Lite caught 48%, fewer than Claude Haiku 5.5's 71% at the same cost, and varied more between runs. W-2 forms aren't shown: after Claude Haiku 5.5 extraction a run has about one wrong value, too few to compare scoring models.

| | |
|---|---|
| **Changes** | Scoring model |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: Claude Haiku 5.5, single-pass · Scoring: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 2–3 runs |

| Dataset | Scoring model | AUROC | ECARB at a 30% review budget | Errors caught reviewing 30% of values | Calibration error | Scoring tokens per page | Total tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|---|---|---|---|
| RealKIE | Amazon Nova Lite (preset default) | 0.51 ± 0.03 | 0.6× ± 0.0× | 0.18 ± 0 | 0.27 ± 0 | 8,497 ± 437 | 15,264 ± 426 | **$20.0 ± $0.03** |
| RealKIE | Amazon Nova 2 Lite | 0.66 ± 0.09 | 1.6× ± 0.4× | 0.48 ± 0.11 | 0.25 ± 0.01 | **3,695 ± 101** | **10,466 ± 104** | $24.2 ± $0.04 |
| RealKIE | **Claude Haiku 5.5 (baseline)** | 0.84 ± 0.07 | 2.4× ± 0.4× | 0.71 ± 0.11 | 0.17 ± 0.01 | 9,751 ± 496 | 16,521 ± 496 | $24.4 ± $1.81 |
| RealKIE | Claude Sonnet 5.5 | 0.89 ± 0.01 | 2.3× ± 0.1× | 0.69 ± 0.03 | 0.17 ± 0.02 | 9,010 ± 505 | 15,774 ± 491 | $119 ± $4.22 |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Separate or integrated scoring

![Integrated scoring costs W-2 accuracy; off is cheapest: weighted accuracy against cost per 1,000 documents, one point per confidence mode](../../images/benchmark-guidance/CF-03-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Finding:** If people review values by confidence, a separate scoring pass is a good place to start. If nobody does, turn confidence off: accuracy stayed the same and cost fell 13% to 28%. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 18 Test Studio runs, listed in `benchmarks/results/guidance/CF-03.json`.*

Integrated scoring lowered W-2 accuracy from 1.00 to 0.96 to save $0.81 per 1,000 forms. On RealKIE invoices it ran as a separate pass anyway: the accelerator scores a document with line items in a separate pass even when integrated scoring is on.

| | |
|---|---|
| **Changes** | Confidence mode |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: Claude Haiku 5.5, single-pass · Scoring: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Confidence mode | Weighted accuracy | AUROC | ECARB at a 30% review budget | Extraction tokens per page | Scoring tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Off |
|---|---|---|---|---|---|---|---|---|---|---|
| RealKIE | **Separate (recommended)** | 0.8 ± 0.07 | 0.83 ± 0.07 | 2.2× ± 0.5× | 6,774 ± 11 | 10,041 ± 423 | 16,815 ± 431 | $22.2 ± $0.14 | 37 ± 6.14 | no measurable gain |
| RealKIE | Integrated | 0.82 ± 0.07 | 0.83 ± 0.09 | 2.2× ± 0.1× | 6,774 ± 6.42 | 10,763 ± 1,068 | 17,537 ± 1,061 | $22.3 ± $0.13 | 38 ± 4.41 | no measurable gain |
| RealKIE | Off | 0.82 ± 0.07 | — | — | 6,774 ± 25 | — | **6,774 ± 25** | **$19.4 ± $0.06** | **21 ± 2.07** | — |
| Fake-W2 | **Separate (recommended)** | 1 ± 0 | **1 ± 0** | 3.3× ± 0.0× | **7,412 ± 16** | 10,828 ± 291 | 18,240 ± 307 | $7.42 ± $0.03 | 31 ± 0.95 | no measurable gain |
| Fake-W2 | Integrated | 0.96 ± 0.01 | 0.9 ± 0.02 | 3.2× ± 0.2× | 9,782 ± 352 | — | 9,782 ± 352 | $6.61 ± $0.22 | 23 ± 2.91 | no measurable gain |
| Fake-W2 | Off | 1 ± 0 | — | — | 7,453 ± 21 | — | **7,453 ± 21** | **$5.37 ± $0.01** | **17 ± 0.41** | — |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Decision model for confidence scoring

![An LLM scores confidence better than the Strands Decider 2B, for less: AUROC and ECARB at a 30% review budget against cost per 1,000 documents, one point per who scores the values](../../images/benchmark-guidance/CF-08-fields.svg)

*Bands show ±2 standard errors. Dashed lines mark coin flip (50%, no better than chance) and random review (1×, what reviewing a random 30% of values catches).*

**Finding:** Not yet; score confidence with an LLM, such as Claude Haiku 5.5. The Strands Decider 2B, reading page images, told right values from wrong ones less well (AUROC 0.68 against 0.80) and cost more than three times as much ($104 against $29 per 1,000 invoices). Reviewing its least-sure 30% of values found 2.0 times as many errors as a random review, against 2.5 times for Claude Haiku 5.5, too close to call. Claude Sonnet 5.5 found the most, 3.0 times, for $130. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15-develop-d1be93f, November 2026, us-east-1. Source: 12 Test Studio runs, listed in `benchmarks/results/guidance/CF-08.json`.*

The decision model is the Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, 8-bit), on an AWS Lambda function with 10 GB of memory; its cost is that function's duration, with cold starts left out. It asks one yes/no question per extracted value against each page image of the section (up to 20 pages). Extraction ran on Claude Haiku 5.5 for every value. Reading OCR text instead of page images could not be measured on this release: the accelerator sent each section's whole text in one request, longer than the 4,096 tokens the model reads, so half the invoices got no score (issue #2032). Its raw scores are not calibrated, so its calibration error depends on how often values are wrong and does not carry over to other documents.

| | |
|---|---|
| **Changes** | Who scores each extracted value: an LLM or a decision model (one yes/no question per value) |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: Claude Haiku 5.5, single-pass · Scoring: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Who scores each extracted value: an LLM or a decision model (one yes/no question per value) | AUROC | ECARB at a 30% review budget | Errors caught reviewing 30% of values | Calibration error | Scoring tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document |
|---|---|---|---|---|---|---|---|---|---|
| RealKIE | **LLM · Claude Haiku 5.5 (baseline)** | 0.8 ± 0.03 | 2.5× ± 0.4× | 0.76 ± 0.11 | 0.06 ± 0.02 | 15,178 ± 673 | 22,295 ± 568 | **$29.0 ± $0.54** | 84 ± 8.54 |
| RealKIE | LLM · Claude Sonnet 5.5 | **0.9 ± 0.01** | **3.0× ± 0.0×** | **0.91 ± 0.01** | 0.05 ± 0.02 | **12,305 ± 2,129** | 19,272 ± 2,305 | $130 ± $8.39 | **52 ± 17** |
| RealKIE | Decision model · page images | 0.68 ± 0.11 | 2.0× ± 0.5× | 0.6 ± 0.14 | 0.3 ± 0.02 | — | **7,064 ± 64** | $104 ± $10.2 | 114 ± 29 |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Planned

We haven't run these yet.

- Routing review by confidence (CF-01)
- Confidence thresholds (CF-04)
- Classification confidence (CF-05)
- Page images for the scoring model (CF-06)

<!-- END GENERATED FAQ -->
