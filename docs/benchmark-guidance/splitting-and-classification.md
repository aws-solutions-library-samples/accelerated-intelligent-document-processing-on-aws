---
title: "Splitting and Section Classification Benchmarks"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Splitting and Section Classification Benchmarks

Splitting finds where each document in a file starts and ends, and classification gives each
of those sections a document class.
[Splitting and Section Classification](../idp-fundamentals/splitting-and-classification.md) explains how it
works.

## How to improve splitting and classification on your documents

**Your definitions.** The classification model picks a document class by comparing each
section with the description of every document class. Name the features that tell a class
apart from its closest neighbor. For classes that still get confused, add a few-shot example.
See [Improving accuracy](../idp-fundamentals/splitting-and-classification.md#improving-accuracy-once-your-pipeline-is-running).

**Data quality.** Long files, documents of the same class back to back, and pages that match
no class are the hard cases. Poor scans hide the headers and page numbers the model uses to
find where a document starts.

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| Granularity and strategy | Page by page or the whole file at once, in one call or with section detection first | [SC-11](#does-holistic-classification-beat-page-by-page-classification) |
| What the model reads | Page text, page images, or both | [SC-02](#do-i-need-page-images-in-the-classification-prompt), [SC-09](#how-small-can-classification-images-be) |
| Classification model | The model that reads the pages | [SC-03](#which-classification-model-should-i-use) |
| Window size | How many pages a holistic call sees at once | [SC-04](#how-long-a-packet-can-holistic-classification-handle) |
| Context pages | Neighboring pages sent with each page, page by page | [SC-05](#does-neighboring-page-context-help-page-by-page-classification) |
| Few-shot examples | Sample pages with their correct class, added to the prompt | [SC-06](#do-few-shot-examples-help-classification) |
| Section splitting | Detect sections, or keep every file as one section | [SC-10](#if-every-file-is-one-document-should-i-turn-splitting-off) |

## Questions

<!-- BEGIN GENERATED FAQ -->

### Does holistic classification beat page-by-page classification?

**Answer:** Yes, once a Claude model classifies. Holistic classification with Claude Haiku 5.5 got 0.80 of DocSplit sections exactly right, against 0.52 for the default (Amazon Nova 2 Lite, page by page), at about the same cost ($470 against $493 per 1,000 packets). With Claude Sonnet 5.5 it got 0.90, for about $200 more per 1,000 packets. Holistic wins on finding where each document starts (boundary accuracy 0.92 with Claude Sonnet 5.5, against 0.75 page by page); once a document is found, every setup names it correctly (label accuracy 0.86 to 0.99). With Amazon Nova 2 Lite holistic barely helps, and splitting page by page first does a little better (0.65). [Details: SC-11](#sc-11-does-holistic-classification-beat-page-by-page-classification)

**Section accuracy**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.52 | 0.71 | 0.76 |
| Holistic | 0.55 | 0.80 | **0.90** |
| Two steps: split page by page, then classify | 0.65 | 0.73 | 0.78 |
| Two steps: split holistically, then classify | 0.55 | 0.76 | 0.87 |

**Page accuracy**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.82 | 0.87 | 0.88 |
| Holistic | 0.70 | 0.91 | 0.95 |
| Two steps: split page by page, then classify | 0.81 | 0.84 | 0.94 |
| Two steps: split holistically, then classify | 0.76 | 0.87 | **0.96** |

**Grouping score**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.66 | 0.81 | 0.85 |
| Holistic | 0.90 | 0.90 | **0.93** |
| Two steps: split page by page, then classify | 0.80 | 0.88 | 0.88 |
| Two steps: split holistically, then classify | 0.90 | 0.91 | 0.93 |

**Boundary accuracy**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.51 | 0.71 | 0.75 |
| Holistic | 0.67 | 0.84 | **0.92** |
| Two steps: split page by page, then classify | 0.68 | 0.75 | 0.77 |
| Two steps: split holistically, then classify | 0.62 | 0.78 | 0.88 |

**Label accuracy**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.92 | 0.98 | **0.99** |
| Holistic | 0.86 | 0.95 | 0.98 |
| Two steps: split page by page, then classify | 0.92 | 0.94 | **0.99** |
| Two steps: split holistically, then classify | 0.91 | 0.95 | 0.98 |

**Cost per 1,000 documents (USD)**

| | Amazon Nova 2 Lite (default) | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | $493 | $488 | $729 |
| Holistic | **$454** | $470 | $671 |
| Two steps: split page by page, then classify | $484 | $477 | $826 |
| Two steps: split holistically, then classify | $463 | $475 | $803 |

*N = 19–20 documents × 3 runs, 1 failed document per run left out. Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: varies (the setting under test) · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. The best value in each table is bold.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

![Section accuracy against cost per 1,000 documents (USD) for each Granularity and strategy, by classification model](../../images/benchmark-guidance/SC-11-packet.svg)

*Section accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Color and shape mark the model family, and each family has its own chart on the same axes, with the other families' values as gray dots. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [DocSplit](datasets.md#docsplit-poly-seq-20-packets). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: varies (the setting under test) · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 19–20 documents × 3 runs, 1 failed document per run left out. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Do I need page images in the classification prompt?

**Answer:** On these packets, page images alone did best. Classifying page by page with Amazon Nova 2 Lite, images alone gave more pages the right class (0.84, against 0.82 with text and images) and got more sections exactly right (0.57, against 0.50), with 25% fewer classification tokens. Text alone scored lowest (0.79 and 0.46). DocSplit's pages are scans with noisy OCR text; on clean digital documents the text may count for more, so test on your own files before you turn it off. [Details: SC-02](#sc-02-do-i-need-page-images-in-the-classification-prompt)

![Page accuracy against cost per 1,000 documents (USD) for each What the classification model reads](../../images/benchmark-guidance/SC-02-packet.svg)

*Page accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better.* Datasets: [DocSplit](datasets.md#docsplit-poly-seq-20-packets). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 19–20 documents × 3 runs, 1 failed document per run left out. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Which classification model should I use?

**Answer:** Claude Haiku 5.5 or Claude Sonnet 5 for packets. On DocSplit both got 0.71 of sections exactly right, against 0.51 for the default, Amazon Nova 2 Lite, and classified more pages correctly (0.86 and 0.89, against 0.82). They used about three times the classification tokens (about 8,800 per page, against 2,850) and about 37% more total tokens. Amazon Nova Pro scored below the default. [Details: SC-03](#sc-03-which-classification-model-should-i-use)

![Section accuracy against cost per 1,000 documents (USD) for each Classification model](../../images/benchmark-guidance/SC-03-packet.svg)

*Section accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Color and shape mark the model family, and each family has its own chart on the same axes, with the other families' values as gray dots. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [DocSplit](datasets.md#docsplit-poly-seq-20-packets). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: varies (the setting under test) · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 19–20 documents × 3 runs, 1 failed document per run left out. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### How long a packet can holistic classification handle?

**Answer:** Not yet measured. [Details: SC-04](#sc-04-how-long-a-packet-can-holistic-classification-handle)

### Does neighboring-page context help page-by-page classification?

**Answer:** Leave it at 0. One neighboring page got more sections exactly right (0.64 against 0.51) but gave fewer pages the right class (0.78 against 0.82), for 67% more classification tokens; two neighboring pages did no better than none. [Details: SC-05](#sc-05-does-neighboring-page-context-help-page-by-page-classification)

![Boundary accuracy against cost per 1,000 documents (USD) for each `contextPagesCount`](../../images/benchmark-guidance/SC-05-packet.svg)

*Boundary accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better.* Datasets: [DocSplit](datasets.md#docsplit-poly-seq-20-packets). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 19–20 documents × 3 runs, 1 failed document per run left out. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Do few-shot examples help classification?

**Answer:** Not yet measured. [Details: SC-06](#sc-06-do-few-shot-examples-help-classification)

### How much do class descriptions matter?

**Answer:** Not yet measured. [Details: SC-07](#sc-07-how-much-do-class-descriptions-matter)

### Does accuracy drop as I add document classes?

**Answer:** Not yet measured. [Details: SC-08](#sc-08-does-accuracy-drop-as-i-add-document-classes)

### How small can classification images be?

**Answer:** Not yet measured. [Details: SC-09](#sc-09-how-small-can-classification-images-be)

### If every file is one document, should I turn splitting off?

**Answer:** Not yet measured. [Details: SC-10](#sc-10-if-every-file-is-one-document-should-i-turn-splitting-off)

<!-- END GENERATED FAQ -->

## Metrics on this page

- *Page accuracy:* the share of pages with the right document class.
- *Section accuracy:* the share of true sections returned with exactly the right pages and
  the right document class.
- *Grouping score:* how well the accelerator groups pages into documents, from 0 to 1,
  whatever their class. It is DocSplit's packet score, which combines how pages are
  clustered and how they are ordered.
- *Boundary accuracy:* the share of sections with exactly the right page range. The
  document class is not checked.
- *Label accuracy:* of the sections with the right page range, the share with the right
  document class.

[Benchmark Datasets](datasets.md) describes each dataset.

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### SC-11: Does holistic classification beat page-by-page classification?

**Answer:** Yes, once a Claude model classifies. Holistic classification with Claude Haiku 5.5 got 0.80 of DocSplit sections exactly right, against 0.52 for the default (Amazon Nova 2 Lite, page by page), at about the same cost ($470 against $493 per 1,000 packets). With Claude Sonnet 5.5 it got 0.90, for about $200 more per 1,000 packets. Holistic wins on finding where each document starts (boundary accuracy 0.92 with Claude Sonnet 5.5, against 0.75 page by page); once a document is found, every setup names it correctly (label accuracy 0.86 to 0.99). With Amazon Nova 2 Lite holistic barely helps, and splitting page by page first does a little better (0.65).

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs DocSplit-Poly-Seq-20261113-192327, DocSplit-Poly-Seq-20261113-192328, DocSplit-Poly-Seq-20261113-192329, DocSplit-Poly-Seq-20261113-192706, DocSplit-Poly-Seq-20261113-192708, DocSplit-Poly-Seq-20261113-192711, DocSplit-Poly-Seq-20261113-194400, DocSplit-Poly-Seq-20261113-194401, DocSplit-Poly-Seq-20261113-194402, DocSplit-Poly-Seq-20261113-195220, DocSplit-Poly-Seq-20261113-195216, DocSplit-Poly-Seq-20261113-195221, DocSplit-Poly-Seq-20261113-205038, DocSplit-Poly-Seq-20261113-205043, DocSplit-Poly-Seq-20261113-205044, DocSplit-Poly-Seq-20261113-205039, DocSplit-Poly-Seq-20261113-205040, DocSplit-Poly-Seq-20261113-205041, DocSplit-Poly-Seq-20261113-205452, DocSplit-Poly-Seq-20261113-205554, DocSplit-Poly-Seq-20261113-205629, DocSplit-Poly-Seq-20261113-205634, DocSplit-Poly-Seq-20261113-205745, DocSplit-Poly-Seq-20261113-205746, DocSplit-Poly-Seq-20261113-230441, DocSplit-Poly-Seq-20261113-230443, DocSplit-Poly-Seq-20261113-230445, DocSplit-Poly-Seq-20261113-230442, DocSplit-Poly-Seq-20261113-230444, DocSplit-Poly-Seq-20261113-230446, DocSplit-Poly-Seq-20261113-230454, DocSplit-Poly-Seq-20261113-230456, DocSplit-Poly-Seq-20261113-230457, DocSplit-Poly-Seq-20261113-230455, DocSplit-Poly-Seq-20261113-230458, DocSplit-Poly-Seq-20261113-230459 (16 failed documents left out of the scores)`.*

| | |
|---|---|
| **Changes** | Granularity and strategy, by classification model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: varies (the setting under test) · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 19–20 documents × 3 runs, 1 failed document per run left out |

| Dataset | Granularity and strategy, by classification model | Page accuracy | Section accuracy | Grouping score | Boundary accuracy | Label accuracy | Classification tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | MCoE vs Holistic · Amazon Nova 2 Lite (default) |
|---|---|---|---|---|---|---|---|---|---|---|
| DocSplit | Page by page · Amazon Nova 2 Lite (default) | 0.82 ± 0.02 | 0.52 ± 0.02 | 0.66 ± 0.02 | 0.51 ± 0.02 | 0.92 ± 0.01 | 2,859 ± 16 | 14,822 ± 58 | $493 | — |
| DocSplit | Holistic · Amazon Nova 2 Lite (default) | 0.7 ± 0.03 | 0.55 ± 0.05 | 0.9 ± 0.01 | 0.67 ± 0.05 | 0.86 ± 0.05 | 1,133 ± 0.65 | 11,857 ± 96 | $454 | — |
| DocSplit | Two steps: split page by page, then classify · Amazon Nova 2 Lite (default) | 0.81 ± 0.02 | 0.65 ± 0.01 | 0.8 ± 0.01 | 0.68 ± 0.01 | 0.92 ± 0 | 2,821 ± 28 | 14,408 ± 116 | $484 | $0.29 per error prevented |
| DocSplit | Two steps: split holistically, then classify · Amazon Nova 2 Lite (default) | 0.76 ± 0.02 | 0.55 ± 0.04 | 0.9 ± 0.01 | 0.62 ± 0.05 | 0.91 ± 0.01 | 1,741 ± 26 | 12,350 ± 42 | $463 | — |
| DocSplit | Page by page · Claude Haiku 5.5 | 0.87 ± 0.01 | 0.71 ± 0.01 | 0.81 ± 0.01 | 0.71 ± 0.01 | 0.98 ± 0.01 | 8,987 ± 40 | 20,563 ± 110 | $488 | $0.21 per error prevented |
| DocSplit | Holistic · Claude Haiku 5.5 | 0.91 ± 0.01 | 0.8 ± 0.03 | 0.9 ± 0.01 | 0.84 ± 0.02 | 0.95 ± 0.01 | 6,297 ± 19 | 17,392 ± 120 | $470 | $0.06 per error prevented |
| DocSplit | Two steps: split page by page, then classify · Claude Haiku 5.5 | 0.84 ± 0.02 | 0.73 ± 0.02 | 0.88 ± 0 | 0.75 ± 0.02 | 0.94 ± 0.03 | 11,297 ± 36 | 22,632 ± 14 | $477 | $0.13 per error prevented |
| DocSplit | Two steps: split holistically, then classify · Claude Haiku 5.5 | 0.87 ± 0.02 | 0.76 ± 0.05 | 0.91 ± 0.01 | 0.78 ± 0.06 | 0.95 ± 0.02 | 10,295 ± 96 | 21,614 ± 412 | $475 | $0.10 per error prevented |
| DocSplit | Page by page · Claude Sonnet 5.5 | 0.88 ± 0 | 0.76 ± 0.02 | 0.85 ± 0.01 | 0.75 ± 0.02 | 0.99 ± 0 | 8,749 ± 4.67 | 20,186 ± 76 | $729 ± $0.78 | $1.28 per error prevented |
| DocSplit | Holistic · Claude Sonnet 5.5 | 0.95 ± 0.03 | 0.9 ± 0 | 0.93 ± 0 | 0.92 ± 0.01 | 0.98 ± 0.02 | 6,234 ± 15 | 17,287 ± 37 | $671 ± $3.43 | $0.61 per error prevented |
| DocSplit | Two steps: split page by page, then classify · Claude Sonnet 5.5 | 0.94 ± 0 | 0.78 ± 0.01 | 0.88 ± 0.01 | 0.77 ± 0.02 | 0.99 ± 0 | 11,292 ± 23 | 22,552 ± 128 | $826 ± $4.61 | $1.63 per error prevented |
| DocSplit | Two steps: split holistically, then classify · Claude Sonnet 5.5 | 0.96 ± 0.01 | 0.87 ± 0.02 | 0.93 ± 0.02 | 0.88 ± 0.02 | 0.98 ± 0.01 | 10,192 ± 252 | 21,387 ± 94 | $803 ± $10.4 | $1.07 per error prevented |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### SC-02: Do I need page images in the classification prompt?

**Answer:** On these packets, page images alone did best. Classifying page by page with Amazon Nova 2 Lite, images alone gave more pages the right class (0.84, against 0.82 with text and images) and got more sections exactly right (0.57, against 0.50), with 25% fewer classification tokens. Text alone scored lowest (0.79 and 0.46). DocSplit's pages are scans with noisy OCR text; on clean digital documents the text may count for more, so test on your own files before you turn it off.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs DocSplit-Poly-Seq-20261113-204126, DocSplit-Poly-Seq-20261113-204125, DocSplit-Poly-Seq-20261113-204127, DocSplit-Poly-Seq-20261113-204458, DocSplit-Poly-Seq-20261113-204500, DocSplit-Poly-Seq-20261113-204457, DocSplit-Poly-Seq-20261113-204849, DocSplit-Poly-Seq-20261113-204831, DocSplit-Poly-Seq-20261113-204830 (5 failed documents left out of the scores)`.*

| | |
|---|---|
| **Changes** | What the classification model reads |
| **Everything else** | Page-by-page granularity; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 19–20 documents × 3 runs, 1 failed document per run left out |

| Dataset | What the classification model reads | Page accuracy | Boundary accuracy | Label accuracy | Classification tokens per page | Cost per 1,000 documents (USD) | MCoE vs Images only |
|---|---|---|---|---|---|---|---|
| DocSplit | Text and images (default) | 0.82 ± 0.01 | 0.5 ± 0.05 | 0.93 ± 0.01 | 2,842 ± 1 | $498 | — |
| DocSplit | Text only | 0.79 ± 0 | 0.47 ± 0.03 | 0.9 ± 0 | 2,612 ± 16 | $499 | — |
| DocSplit | Images only | 0.84 ± 0 | 0.54 ± 0.03 | 0.97 ± 0 | 2,126 ± 1.9 | $484 | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### SC-03: Which classification model should I use?

**Answer:** Claude Haiku 5.5 or Claude Sonnet 5 for packets. On DocSplit both got 0.71 of sections exactly right, against 0.51 for the default, Amazon Nova 2 Lite, and classified more pages correctly (0.86 and 0.89, against 0.82). They used about three times the classification tokens (about 8,800 per page, against 2,850) and about 37% more total tokens. Amazon Nova Pro scored below the default.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs DocSplit-Poly-Seq-20261113-195743, DocSplit-Poly-Seq-20261113-195742, DocSplit-Poly-Seq-20261113-195744, DocSplit-Poly-Seq-20261113-200102, DocSplit-Poly-Seq-20261113-200054, DocSplit-Poly-Seq-20261113-200100, DocSplit-Poly-Seq-20261113-200705, DocSplit-Poly-Seq-20261113-200700, DocSplit-Poly-Seq-20261113-200701, DocSplit-Poly-Seq-20261113-201125, DocSplit-Poly-Seq-20261113-201126, DocSplit-Poly-Seq-20261113-201048 (5 failed documents left out of the scores)`.*

| | |
|---|---|
| **Changes** | Classification model |
| **Everything else** | Run at both granularities: page by page and holistic; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: varies (the setting under test) · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 19–20 documents × 3 runs, 1 failed document per run left out |

| Dataset | Classification model | Page accuracy | Section accuracy | Grouping score | Boundary accuracy | Label accuracy | Classification tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Claude Haiku 5.5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DocSplit | Amazon Nova 2 Lite (default) | 0.82 ± 0.01 | 0.51 ± 0.02 | 0.65 ± 0.02 | 0.5 ± 0.03 | 0.92 ± 0.01 | 2,850 ± 15 | 14,865 ± 38 | $494 | 114 ± 11 | — |
| DocSplit | Amazon Nova Pro | 0.76 ± 0 | 0.48 ± 0 | 0.6 ± 0.02 | 0.5 ± 0 | 0.85 ± 0.01 | 4,357 ± 9.85 | 16,630 ± 60 | $513 | 112 ± 8.2 | — |
| DocSplit | Claude Haiku 5.5 | 0.86 ± 0.01 | 0.71 ± 0.03 | 0.8 ± 0.02 | 0.7 ± 0.02 | 0.98 ± 0.01 | 8,987 ± 21 | 20,667 ± 124 | $488 | 144 ± 15 | — |
| DocSplit | Claude Sonnet 5 | 0.89 ± 0 | 0.71 ± 0.02 | 0.79 ± 0.02 | 0.67 ± 0.03 | 0.99 ± 0 | 8,671 ± 15 | 20,274 ± 67 | $738 | 120 ± 37 | no measurable gain |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### SC-04: How long a packet can holistic classification handle?

**Answer:** Not yet measured.

Needs a dataset we haven't built yet: Long packets, 30-60 pages (to be built).

| | |
|---|---|
| **Changes** | Pages per holistic call (`window_pages`) |
| **Everything else** | Holistic, one call; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Long packets, 30-60 pages (to be built)](datasets.md#long-packets-30-60-pages-to-be-built); [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Pages per holistic call (`window_pages`) | Page accuracy | Boundary accuracy | Label accuracy | Classification tokens per page |
|---|---|---|---|---|---|
| Long packets | 20 (default) | — | — | — | — |
| Long packets | 10 | — | — | — | — |
| Long packets | 40 | — | — | — | — |
| DocSplit | 20 (default) | — | — | — | — |
| DocSplit | 10 | — | — | — | — |
| DocSplit | 40 | — | — | — | — |


#### SC-05: Does neighboring-page context help page-by-page classification?

**Answer:** Leave it at 0. One neighboring page got more sections exactly right (0.64 against 0.51) but gave fewer pages the right class (0.78 against 0.82), for 67% more classification tokens; two neighboring pages did no better than none.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs DocSplit-Poly-Seq-20261113-205249, DocSplit-Poly-Seq-20261113-205248, DocSplit-Poly-Seq-20261113-205247, DocSplit-Poly-Seq-20261113-205744, DocSplit-Poly-Seq-20261113-205750, DocSplit-Poly-Seq-20261113-205747, DocSplit-Poly-Seq-20261113-210239, DocSplit-Poly-Seq-20261113-210357, DocSplit-Poly-Seq-20261113-210612 (3 failed documents left out of the scores)`.*

Needs a dataset we haven't built yet: Same-class documents back to back (to be built).

| | |
|---|---|
| **Changes** | `contextPagesCount` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets); [Same-class documents back to back (to be built)](datasets.md#same-class-documents-back-to-back-to-be-built) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page · Extraction: Claude Sonnet 5, single pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 19–20 documents × 3 runs, 1 failed document per run left out |

| Dataset | `contextPagesCount` | Boundary accuracy | Label accuracy | Over-splits | Merges | Classification tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|---|---|---|
| DocSplit | 0 (default) | 0.49 ± 0.02 | 0.92 ± 0.01 | — | — | 2,859 ± 14 | $496 |
| DocSplit | 1 | 0.63 ± 0.07 | 0.96 ± 0 | — | — | 4,762 ± 67 | $496 |
| DocSplit | 2 | 0.53 ± 0 | 0.94 ± 0.02 | — | — | 6,381 ± 76 | $505 |
| Same-class packets | 0 (default) | — | — | — | — | — | — |
| Same-class packets | 1 | — | — | — | — | — | — |
| Same-class packets | 2 | — | — | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### SC-06: Do few-shot examples help classification?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Few-shot examples |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Few-shot examples | Page accuracy | Label accuracy | Classification tokens per page |
|---|---|---|---|---|
| DocSplit | None (default) | — | — | — |
| DocSplit | One example per class | — | — | — |
| DocSplit | Two examples per class | — | — | — |


#### SC-07: How much do class descriptions matter?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Class descriptions |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Class descriptions | Page accuracy | Label accuracy |
|---|---|---|---|
| DocSplit | Class names only | — | — |
| DocSplit | Generic descriptions | — | — |
| DocSplit | Descriptions that name distinguishing features | — | — |


#### SC-08: Does accuracy drop as I add document classes?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Number of document classes in the configuration |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Number of document classes in the configuration | Page accuracy | Label accuracy | Classification tokens per page |
|---|---|---|---|---|
| DocSplit | 5 | — | — | — |
| DocSplit | 13 | — | — | — |
| DocSplit | 30 (13 plus 17 distractor classes) | — | — | — |


#### SC-09: How small can classification images be?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Classification image size (long edge) |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Classification image size (long edge) | Page accuracy | Boundary accuracy | Classification tokens per page |
|---|---|---|---|---|
| DocSplit | As rendered (default) | — | — | — |
| DocSplit | 1,024 pixels | — | — | — |
| DocSplit | 512 pixels | — | — | — |


#### SC-10: If every file is one document, should I turn splitting off?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Section splitting |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Section splitting | Weighted accuracy | Classification tokens per page |
|---|---|---|---|
| RealKIE | Model decides (default) | — | — |
| RealKIE | One section | — | — |

| Dataset | Section splitting | List recall | Classification tokens per page |
|---|---|---|---|
| Statement, 400 rows | Model decides (default) | — | — |
| Statement, 400 rows | One section | — | — |
| Statement, 800 rows | Model decides (default) | — | — |
| Statement, 800 rows | One section | — | — |

<!-- END GENERATED ABLATIONS -->
