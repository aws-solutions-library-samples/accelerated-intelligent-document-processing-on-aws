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
[Improving digitization](../idp-fundamentals/digitization.md#improving-digitization-once-your-pipeline-is-running).

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| OCR reader (`ocr.backend`) | Amazon Textract, a vision model, or a specialized OCR model | [DG-01](#does-the-ocr-reader-make-a-difference-to-extraction-accuracy) to [DG-03](#does-a-specialized-ocr-model-beat-textract) |
| Resolution (`ocr.image.dpi`) | The DPI the accelerator renders pages at | [DG-04](#what-resolution-should-pages-be-rendered-at) |
| Preprocessing (`ocr.image.preprocessing`) | Cleans up shadows, uneven lighting and low contrast | [DG-05](#does-image-preprocessing-help-on-poor-scans) |
| Textract features (`ocr.features`) | Tables, layout and signatures | [DG-06](#which-textract-features-do-i-need) |

## Questions

<!-- BEGIN GENERATED FAQ -->

### Does the OCR reader make a difference to extraction accuracy?

**Answer:** Keep Textract. On RealKIE invoices the three readers scored within the noise of each other (0.81, 0.80 and 0.81). On bank checks the Bedrock vision model scored lower than Textract (0.90 against 0.94), a gap near the edge of the noise. The vision model's shorter page text cut total tokens per page on invoices (16,400 against 19,600), but it returns no word confidence for the scoring model to use. [Details: DG-01](#dg-01-does-the-ocr-reader-make-a-difference-to-extraction-accuracy)

![Weighted accuracy against cost per 1,000 documents (USD) for each OCR reader](../../images/benchmark-guidance/DG-01-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [OmniAI, bank checks](datasets.md#omniai-ocr-benchmark-bank-checks-20). Models: OCR: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. Classification by dataset: OmniAI, bank checks — Amazon Nova 2 Lite, page by page. Extraction by dataset: RealKIE — Claude Sonnet 5, single-pass; OmniAI, bank checks — Amazon Nova 2 Lite, single-pass. N = 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### If I read pages with a vision model, which model?

**Answer:** Not yet measured. [Details: DG-02](#dg-02-if-i-read-pages-with-a-vision-model-which-model)

### Does a specialized OCR model beat Textract?

**Answer:** Not yet measured. [Details: DG-03](#dg-03-does-a-specialized-ocr-model-beat-textract)

### What resolution should pages be rendered at?

**Answer:** Keep 300 DPI unless tokens matter more than a small accuracy risk. On RealKIE invoices 200 DPI scored the same as 300 (0.82 against 0.81) with 7% fewer tokens per page; 150 DPI scored 0.79, a drop within the noise, with 12% fewer. Below about 200 DPI Textract starts to drop small and faint characters, so test a lower resolution on your own scans first. [Details: DG-04](#dg-04-what-resolution-should-pages-be-rendered-at)

![Weighted accuracy against cost per 1,000 documents (USD) for each `ocr.image.dpi`](../../images/benchmark-guidance/DG-04-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Does image preprocessing help on poor scans?

**Answer:** Not yet measured. [Details: DG-05](#dg-05-does-image-preprocessing-help-on-poor-scans)

### Which Textract features do I need?

**Answer:** Pick the features your documents need; none of the combinations we tried changed accuracy measurably. On RealKIE invoices and W-2 forms every combination scored within the noise of the default, and LAYOUT alone used about 3% fewer total tokens per page because it sends less page text. Textract prices each page by the features you select, so dropping a feature your documents do not need saves Textract charges rather than tokens. [Details: DG-06](#dg-06-which-textract-features-do-i-need)

![Weighted accuracy against cost per 1,000 documents (USD) for each `ocr.features`](../../images/benchmark-guidance/DG-06-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). Models: OCR: varies (the setting under test) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### How much accuracy does scan quality cost?

**Answer:** Not yet measured. [Details: DG-07](#dg-07-how-much-accuracy-does-scan-quality-cost)

### Which reader handles handwriting best?

**Answer:** Not yet measured. [Details: DG-08](#dg-08-which-reader-handles-handwriting-best)

<!-- END GENERATED FAQ -->

## Metrics on this page

- *Weighted accuracy:* Stickler's field-weighted score against ground truth.
- *Documents with every field right:* the share of documents with no wrong field.
- *Completeness* and *cell accuracy:* see [Key Information Extraction Benchmarks](extraction.md).

[Benchmark Datasets](datasets.md) describes each dataset.

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### DG-01: Does the OCR reader make a difference to extraction accuracy?

**Answer:** Keep Textract. On RealKIE invoices the three readers scored within the noise of each other (0.81, 0.80 and 0.81). On bank checks the Bedrock vision model scored lower than Textract (0.90 against 0.94), a gap near the edge of the noise. The vision model's shorter page text cut total tokens per page on invoices (16,400 against 19,600), but it returns no word confidence for the scoring model to use.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-200107, RealKIE-FCC-Verified-20261113-200110, RealKIE-FCC-Verified-20261113-200111, RealKIE-FCC-Verified-20261113-200734, RealKIE-FCC-Verified-20261113-200735, RealKIE-FCC-Verified-20261113-200737, RealKIE-FCC-Verified-20261113-201141, RealKIE-FCC-Verified-20261113-201235, RealKIE-FCC-Verified-20261113-201255, OmniAI-OCR-Benchmark-20261113-201450, OmniAI-OCR-Benchmark-20261113-201607, OmniAI-OCR-Benchmark-20261113-201632, OmniAI-OCR-Benchmark-20261113-201740, OmniAI-OCR-Benchmark-20261113-201755, OmniAI-OCR-Benchmark-20261113-201803, OmniAI-OCR-Benchmark-20261113-201905, OmniAI-OCR-Benchmark-20261113-201929, OmniAI-OCR-Benchmark-20261113-201940`.*

| | |
|---|---|
| **Changes** | OCR reader |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [OmniAI-OCR-Benchmark, bank checks (20)](datasets.md#omniai-ocr-benchmark-bank-checks-20); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. Classification by dataset: OmniAI, bank checks — Amazon Nova 2 Lite, page by page. Extraction by dataset: RealKIE — Claude Sonnet 5, single-pass; OmniAI, bank checks — Amazon Nova 2 Lite, single-pass. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | OCR reader | Weighted accuracy | OCR tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | MCoE vs cheapest |
|---|---|---|---|---|---|---|
| RealKIE | Textract, tables and layout (default) | 0.81 ± 0.06 | — | 19,629 ± 164 | $135 | no measurable gain |
| RealKIE | Textract, layout only | 0.8 ± 0.07 | — | 19,810 ± 612 | $94.2 | — |
| RealKIE | Bedrock vision model (Nova 2 Lite) | 0.81 ± 0.07 | 1,907 ± 111 | 16,382 ± 772 | $107 | no measurable gain |
| ConfBench | Textract, tables and layout (default) | — | — | — | — | — |
| ConfBench | Textract, layout only | — | — | — | — | — |
| ConfBench | Bedrock vision model (Nova 2 Lite) | — | — | — | — | — |
| OmniAI, bank checks | Textract, tables and layout (default) | 0.94 ± 0.03 | — | 9,613 ± 4.36 | $17.5 | no measurable gain |
| OmniAI, bank checks | Textract, layout only | 0.94 ± 0.04 | — | 9,225 ± 1.45 | $6.46 | no measurable gain |
| OmniAI, bank checks | Bedrock vision model (Nova 2 Lite) | 0.9 ± 0.06 | 727 ± 31 | 9,710 ± 111 | $3.74 | — |

| Dataset | OCR reader | List recall | List precision | OCR tokens per page | Total tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|---|---|
| Statement, 400 rows | Textract, tables and layout (default) | — | — | — | — | — |
| Statement, 400 rows | Textract, layout only | — | — | — | — | — |
| Statement, 400 rows | Bedrock vision model (Nova 2 Lite) | — | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### DG-02: If I read pages with a vision model, which model?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Model used for Bedrock vision OCR |
| **Everything else** | OCR reader set to the Bedrock vision model; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Model used for Bedrock vision OCR | Weighted accuracy | OCR tokens per page | Total tokens per page |
|---|---|---|---|---|
| RealKIE | Amazon Nova 2 Lite | — | — | — |
| RealKIE | Claude Haiku 5.5 | — | — | — |
| RealKIE | Claude Sonnet 5 | — | — | — |
| ConfBench | Amazon Nova 2 Lite | — | — | — |
| ConfBench | Claude Haiku 5.5 | — | — | — |
| ConfBench | Claude Sonnet 5 | — | — | — |

| Dataset | Model used for Bedrock vision OCR | List precision | OCR tokens per page | Total tokens per page |
|---|---|---|---|---|
| Statement, 400 rows | Amazon Nova 2 Lite | — | — | — |
| Statement, 400 rows | Claude Haiku 5.5 | — | — | — |
| Statement, 400 rows | Claude Sonnet 5 | — | — | — |


#### DG-03: Does a specialized OCR model beat Textract?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | OCR reader |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [OmniAI-OCR-Benchmark, bank checks (20)](datasets.md#omniai-ocr-benchmark-bank-checks-20) |
| **Runs** | 3 per document, per value |

| Dataset | OCR reader | Weighted accuracy | Documents with every field right | Total tokens per page |
|---|---|---|---|---|
| RealKIE | Textract, tables and layout (default) | — | — | — |
| RealKIE | Chandra (Lambda hook) | — | — | — |
| RealKIE | Mistral OCR (Lambda hook) | — | — | — |
| RealKIE | Cohere Parse (Lambda hook) | — | — | — |
| ConfBench | Textract, tables and layout (default) | — | — | — |
| ConfBench | Chandra (Lambda hook) | — | — | — |
| ConfBench | Mistral OCR (Lambda hook) | — | — | — |
| ConfBench | Cohere Parse (Lambda hook) | — | — | — |
| OmniAI, bank checks | Textract, tables and layout (default) | — | — | — |
| OmniAI, bank checks | Chandra (Lambda hook) | — | — | — |
| OmniAI, bank checks | Mistral OCR (Lambda hook) | — | — | — |
| OmniAI, bank checks | Cohere Parse (Lambda hook) | — | — | — |


#### DG-04: What resolution should pages be rendered at?

**Answer:** Keep 300 DPI unless tokens matter more than a small accuracy risk. On RealKIE invoices 200 DPI scored the same as 300 (0.82 against 0.81) with 7% fewer tokens per page; 150 DPI scored 0.79, a drop within the noise, with 12% fewer. Below about 200 DPI Textract starts to drop small and faint characters, so test a lower resolution on your own scans first.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-203126, RealKIE-FCC-Verified-20261113-203127, RealKIE-FCC-Verified-20261113-203128, RealKIE-FCC-Verified-20261113-203431, RealKIE-FCC-Verified-20261113-203443, RealKIE-FCC-Verified-20261113-203446, RealKIE-FCC-Verified-20261113-203719, RealKIE-FCC-Verified-20261113-203751, RealKIE-FCC-Verified-20261113-203812`.*

| | |
|---|---|
| **Changes** | `ocr.image.dpi` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `ocr.image.dpi` | Weighted accuracy | Total tokens per page | Cost per 1,000 documents (USD) | MCoE vs 150 |
|---|---|---|---|---|---|
| RealKIE | 300 (default) | 0.81 ± 0.06 | 20,875 ± 745 | $143 | no measurable gain |
| RealKIE | 200 | 0.82 ± 0.07 | 19,450 ± 67 | $134 | no measurable gain |
| RealKIE | 150 | 0.79 ± 0.07 | 18,267 ± 646 | $119 | — |
| ConfBench | 300 (default) | — | — | — | — |
| ConfBench | 200 | — | — | — | — |
| ConfBench | 150 | — | — | — | — |

| Dataset | `ocr.image.dpi` | List precision | Total tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|
| Statement, 400 rows | 300 (default) | — | — | — |
| Statement, 400 rows | 200 | — | — | — |
| Statement, 400 rows | 150 | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### DG-05: Does image preprocessing help on poor scans?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `ocr.image.preprocessing` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Runs** | 3 per document, per value |

| Dataset | `ocr.image.preprocessing` | Weighted accuracy | Documents with every field right | Total tokens per page |
|---|---|---|---|---|
| ConfBench | Off (default) | — | — | — |
| ConfBench | On | — | — | — |
| RealKIE | Off (default) | — | — | — |
| RealKIE | On | — | — | — |


#### DG-06: Which Textract features do I need?

**Answer:** Pick the features your documents need; none of the combinations we tried changed accuracy measurably. On RealKIE invoices and W-2 forms every combination scored within the noise of the default, and LAYOUT alone used about 3% fewer total tokens per page because it sends less page text. Textract prices each page by the features you select, so dropping a feature your documents do not need saves Textract charges rather than tokens.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-183155, RealKIE-FCC-Verified-20261113-183156, RealKIE-FCC-Verified-20261113-183157, RealKIE-FCC-Verified-20261113-183443, RealKIE-FCC-Verified-20261113-183516, RealKIE-FCC-Verified-20261113-183512, RealKIE-FCC-Verified-20261113-183741, RealKIE-FCC-Verified-20261113-183751, RealKIE-FCC-Verified-20261113-183840, RealKIE-FCC-Verified-20261113-184029, RealKIE-FCC-Verified-20261113-184136, RealKIE-FCC-Verified-20261113-184120, Fake-W2-Tax-Forms-20261113-184358, Fake-W2-Tax-Forms-20261113-184446, Fake-W2-Tax-Forms-20261113-184443, Fake-W2-Tax-Forms-20261113-184524, Fake-W2-Tax-Forms-20261113-184603, Fake-W2-Tax-Forms-20261113-184607, Fake-W2-Tax-Forms-20261113-184628, Fake-W2-Tax-Forms-20261113-184719, Fake-W2-Tax-Forms-20261113-184721, Fake-W2-Tax-Forms-20261113-184742, Fake-W2-Tax-Forms-20261113-184825, Fake-W2-Tax-Forms-20261113-184846`.*

| | |
|---|---|
| **Changes** | `ocr.features` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: varies (the setting under test) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `ocr.features` | Weighted accuracy | Total tokens per page | Cost per 1,000 documents (USD) | MCoE vs LAYOUT only |
|---|---|---|---|---|---|
| RealKIE | TABLES, LAYOUT, SIGNATURES (default) | 0.8 ± 0.06 | 20,870 ± 671 | $137 | no measurable gain |
| RealKIE | LAYOUT only | 0.79 ± 0.07 | 19,457 ± 676 | $87.5 | — |
| RealKIE | TABLES only | 0.81 ± 0.06 | 21,293 ± 2,175 | $143 | no measurable gain |
| RealKIE | TABLES, LAYOUT, SIGNATURES, FORMS | 0.79 ± 0.07 | 21,429 ± 2,720 | $339 | no measurable gain |
| Fake-W2 | TABLES, LAYOUT, SIGNATURES (default) | 0.95 ± 0.02 | 15,195 ± 7.48 | $33.8 | — |
| Fake-W2 | LAYOUT only | 0.96 ± 0.02 | 14,705 ± 7.77 | $20.2 | — |
| Fake-W2 | TABLES only | 0.97 ± 0.01 | 15,178 ± 3.93 | $32.2 | no measurable gain |
| Fake-W2 | TABLES, LAYOUT, SIGNATURES, FORMS | 0.97 ± 0.02 | 15,197 ± 1.59 | $82.2 | no measurable gain |

| Dataset | `ocr.features` | List recall | List precision | Total tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|---|
| Statement, 400 rows | TABLES, LAYOUT, SIGNATURES (default) | — | — | — | — |
| Statement, 400 rows | LAYOUT only | — | — | — | — |
| Statement, 400 rows | TABLES only | — | — | — | — |
| Statement, 400 rows | TABLES, LAYOUT, SIGNATURES, FORMS | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### DG-07: How much accuracy does scan quality cost?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Scan quality |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise) |
| **Runs** | 3 per document, per value |

| Dataset | Scan quality | Weighted accuracy | Documents with every field right | AUROC |
|---|---|---|---|---|
| ConfBench | Clean original | — | — | — |
| ConfBench | Light noise | — | — | — |
| ConfBench | Moderate noise | — | — | — |
| ConfBench | Heavy noise | — | — | — |


#### DG-08: Which reader handles handwriting best?

**Answer:** Not yet measured.

Needs a dataset we haven't built yet: Handwritten forms (to be built).

| | |
|---|---|
| **Changes** | OCR reader |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Handwritten forms (to be built)](datasets.md#handwritten-forms-to-be-built) |
| **Runs** | 3 per document, per value |

| Dataset | OCR reader | Weighted accuracy | Documents with every field right | Total tokens per page |
|---|---|---|---|---|
| Handwritten forms | Textract, tables and layout (default) | — | — | — |
| Handwritten forms | Bedrock vision model (Claude Sonnet 5) | — | — | — |
| Handwritten forms | Chandra (Lambda hook) | — | — | — |

<!-- END GENERATED ABLATIONS -->
