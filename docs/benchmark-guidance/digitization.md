---
title: "Digitization (OCR) Benchmarks"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Digitization (OCR) Benchmarks

Digitization turns each page into page text that the later steps read.
[Digitization (OCR)](../idp-fundamentals/digitization.md) explains how it works.

We don't benchmark OCR on its own today. We measure its effect on the steps after it: each
benchmark here scores the extraction accuracy an OCR reader produces, because that is what
lands in your database. To compare readers on OCR alone, use public OCR benchmarks.

## How to improve digitization on your documents

**Your definitions.** The reader doesn't use them: it reads every page the same way. When a
value is wrong, open [View Page Text](../web-ui.md#view-page-text) first. If the value is
right in the page text, the fix is in your classification or extraction definitions.

**Data quality.** Scan at 300 DPI, and turn on preprocessing for photos and poor scans. See
[Improving digitization](../idp-fundamentals/digitization.md#improving-accuracy).

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| OCR reader (`ocr.backend`) | Amazon Textract, a vision model, or a specialized OCR model | [DG-01](#does-the-ocr-reader-make-a-difference-to-extraction-accuracy) to [DG-03](#planned) |
| Resolution (`ocr.image.dpi`) | The DPI the accelerator renders pages at | [DG-04](#what-resolution-should-pages-be-rendered-at) |
| Preprocessing (`ocr.image.preprocessing`) | Cleans up shadows, uneven lighting and low contrast | [DG-05](#planned) |
| Textract features (`ocr.features`) | Tables, layout and signatures | [DG-06](#which-textract-features-do-i-need) |

## Questions

<details>
<summary>What the metrics mean</summary>

- *Weighted accuracy:* Stickler's field-weighted score against ground truth.
- *Documents with every field right:* the share of documents with no wrong field.

Full definitions: Stickler's [Thresholds and Metrics](https://awslabs.github.io/stickler/Getting-Started/thresholds-and-metrics/), and [Field Weighting](../evaluation.md#field-weighting-for-business-criticality) in the evaluation guide.

[Benchmark Datasets](datasets.md) describes each dataset.

</details>

<!-- BEGIN GENERATED FAQ -->

### Does the OCR reader make a difference to extraction accuracy?

![Textract Layout only matches the other readers: weighted accuracy against cost per 1,000 documents, one point per OCR reader](../../images/benchmark-guidance/DG-01-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Amazon Textract with Layout only is a good place to start: on RealKIE invoices it scored as well as the other readers, for a third of the cost of Textract with tables. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 18 Test Studio runs (8 failed documents left out of the scores), listed in `benchmarks/results/guidance/DG-01.json`.*

On OmniAI bank checks, Textract with Layout only got every field right on 47% of checks, against 16% with Amazon Nova 2 Lite, which cost less ($4.84 against $7.76 per 1,000).

| | |
|---|---|
| **Changes** | OCR reader |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [OmniAI-OCR-Benchmark, bank checks (20)](datasets.md#omniai-ocr-benchmark-bank-checks-20) |
| **Models** | OCR: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. Classification by dataset: OmniAI, bank checks — Claude Haiku 5.5, holistic. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 3 runs; OmniAI, bank checks — 17–20 documents × 3 runs; up to 3 documents failed in a run and are left out of it |

| Dataset | OCR reader | Weighted accuracy | Documents with every field right | OCR tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | [Cost per error prevented](marginal-cost-of-error.md) vs cheapest |
|---|---|---|---|---|---|---|---|
| RealKIE | **Textract, layout only (recommended)** | 0.8 ± 0.08 | 0 ± 0 | — | 16,908 ± 464 | **$22.8 ± $0.89** | — |
| RealKIE | Textract, tables and layout | 0.76 ± 0.08 | 0 ± 0 | — | 18,054 ± 2,156 | $69.0 ± $0.66 | no measurable gain |
| RealKIE | Amazon Nova 2 Lite vision | 0.79 ± 0.07 | 0 ± 0 | 2,020 ± 30 | 16,849 ± 185 | $26.2 ± $0.25 | no measurable gain |
| OmniAI, bank checks | **Textract, layout only (recommended)** | 0.99 ± 0.01 | 0.47 ± 0.03 | — | 21,346 ± 945 | $7.76 ± $0.36 | $0.07 |
| OmniAI, bank checks | Textract, tables and layout | 0.99 ± 0.01 | 0.31 ± 0.17 | — | 21,830 ± 480 | $18.7 ± $0.19 | no measurable gain |
| OmniAI, bank checks | Amazon Nova 2 Lite vision | 0.95 ± 0.04 | 0.16 ± 0.06 | 750 ± 45 | 22,221 ± 322 | **$4.84 ± $0.21** | — |

Failed documents per run: OmniAI, bank checks — Textract, layout only (recommended): 0, 2, 1; OmniAI, bank checks — Textract, tables and layout: 0, 0, 3; OmniAI, bank checks — Amazon Nova 2 Lite vision: 0, 1, 1.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### What resolution should pages be rendered at?

![Lower resolution saved nothing; keep 300 DPI: weighted accuracy against cost per 1,000 documents, one point per page resolution](../../images/benchmark-guidance/DG-04-fields.svg)

*Bands show ±2 standard errors.*

**Answer:** 300 DPI is a good place to start: on RealKIE invoices, 200 and 150 DPI scored no better and cost more. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 9 Test Studio runs, listed in `benchmarks/results/guidance/DG-04.json`.*

| | |
|---|---|
| **Changes** | `ocr.image.dpi` |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `ocr.image.dpi` | Weighted accuracy | Total tokens per page | Cost per 1,000 documents (USD) | [Cost per error prevented](marginal-cost-of-error.md) vs 300 DPI (preset default) |
|---|---|---|---|---|---|
| RealKIE | **300 DPI (preset default)** | 0.81 ± 0.07 | 16,552 ± 394 | **$22.1 ± $0.14** | — |
| RealKIE | 200 DPI | 0.81 ± 0.07 | 16,941 ± 360 | $26.0 ± $0.11 | no measurable gain |
| RealKIE | 150 DPI | 0.77 ± 0.08 | **12,126 ± 166** | $23.8 ± $0.01 | no measurable gain |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Which Textract features do I need?

![Layout only scores best and costs least: weighted accuracy and documents with every field right against cost per 1,000 documents, one point per Textract features](../../images/benchmark-guidance/DG-06-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Layout only is a good place to start: it got every field right on 82% of W-2 forms, against 55% with the preset's tables, layout and signatures, for the lowest cost. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 24 Test Studio runs, listed in `benchmarks/results/guidance/DG-06.json`.*

| | |
|---|---|
| **Changes** | `ocr.features` |
| **Everything else** | The [baseline setup](index.md#baseline-setup) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `ocr.features` | Weighted accuracy | Documents with every field right | OCR tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | [Cost per error prevented](marginal-cost-of-error.md) vs Layout only (recommended) |
|---|---|---|---|---|---|---|---|
| RealKIE | **Layout only (recommended)** | 0.8 ± 0.08 | 0 ± 0 | — | 16,504 ± 300 | **$22.1 ± $0.10** | — |
| RealKIE | Tables, layout, signatures (preset default) | 0.76 ± 0.08 | 0 ± 0 | — | 20,208 ± 0 | $69.4 | no measurable gain |
| RealKIE | Tables only | 0.8 ± 0.07 | **0.05 ± 0** | — | 17,512 ± 1,460 | $67.0 ± $0.39 | no measurable gain |
| RealKIE | Tables, layout, signatures, forms | 0.76 ± 0.07 | 0 ± 0 | — | 17,941 ± 351 | $270 ± $0.62 | no measurable gain |
| Fake-W2 | **Layout only (recommended)** | **1 ± 0** | **0.82 ± 0.03** | — | **18,256 ± 197** | **$7.40 ± $0.02** | — |
| Fake-W2 | Tables, layout, signatures (preset default) | 0.96 ± 0.01 | 0.55 ± 0.06 | — | 18,835 ± 375 | $18.5 ± $0.09 | no measurable gain |
| Fake-W2 | Tables only | 0.93 ± 0.02 | 0.37 ± 0.15 | — | 18,895 ± 429 | $18.5 ± $0.06 | no measurable gain |
| Fake-W2 | Tables, layout, signatures, forms | 0.97 ± 0.01 | 0.57 ± 0.03 | — | 18,696 ± 59 | $68.5 ± $0.03 | no measurable gain |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#baseline-setup) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Planned

We haven't run these yet.

- If I read pages with a vision model, which model? (DG-02)
- Does a specialized OCR model beat Textract? (DG-03)
- Does image preprocessing help on poor scans? (DG-05)
- How much accuracy does scan quality cost? (DG-07)
- Which reader handles handwriting best? (DG-08)

<!-- END GENERATED FAQ -->
