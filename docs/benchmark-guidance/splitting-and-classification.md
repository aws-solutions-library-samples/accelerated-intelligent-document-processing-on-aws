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

![Four ways to find the documents in a packet and classify each one: page by page, holistic, split then an LLM classifies each section, and split then a decision model classifies each section](../../images/benchmark-guidance/figures/splitting-approaches.svg)

## How to improve splitting and classification on your documents

**Your definitions.** The classification model picks a document class by comparing each
section with the description of every document class. Name the features that tell a class
apart from its closest neighbor. For classes that still get confused, add a few-shot example.
A small decision model is more sensitive to this wording than an LLM: give it one line per
class about what the page looks like, such as "a typed formal letter with a salutation
(Dear ...) and a signature" ([SC-12](#can-a-decision-model-classify-the-sections)).
See [Improving accuracy](../idp-fundamentals/splitting-and-classification.md#improving-accuracy-once-your-pipeline-is-running).

**Data quality.** Long files, documents of the same class back to back, and pages that match
no class are the hard cases. Poor scans hide the headers and page numbers the model uses to
find where a document starts.

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| Granularity and strategy | Page by page or the whole file at once, in one call or with section detection first | [SC-11](#does-holistic-classification-beat-page-by-page-classification) |
| What the model reads | Page text, page images, or both | [SC-02](#do-i-need-page-images-in-the-classification-prompt), [SC-09](#planned) |
| Classification model | The model that reads the pages | [SC-11](#does-holistic-classification-beat-page-by-page-classification), [SC-13](#which-way-of-splitting-and-classifying-should-i-use) |
| Who classifies each section | An LLM, or a small decision model, after section detection | [SC-12](#can-a-decision-model-classify-the-sections), [SC-13](#which-way-of-splitting-and-classifying-should-i-use) |
| Class descriptions | Descriptions written for the LLM prompt, or one line about what each page looks like | [SC-12](#can-a-decision-model-classify-the-sections) |
| Window size | How many pages a holistic call sees at once | [SC-04](#planned) |
| Few-shot examples | Sample pages with their correct class, added to the prompt | [SC-06](#planned) |
| Section splitting | Detect sections, or keep every file as one section | [SC-10](#planned) |

## Questions

<details>
<summary>What the metrics mean</summary>

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

Full definitions: [Document Split Classification Metrics](../evaluation.md#document-split-classification-metrics) in the evaluation guide, [Telling a wrong boundary from a wrong label](../classification.md#telling-a-wrong-boundary-from-a-wrong-label) for boundary and label accuracy, and Stickler's [Document Packet Splitting Evaluation](https://awslabs.github.io/stickler/Guides/Document_Packet_Splitting/), which computes page accuracy, section accuracy and the grouping score.

[Benchmark Datasets](datasets.md) describes each dataset.

</details>

<!-- BEGIN GENERATED FAQ -->

### Does holistic classification beat page-by-page classification?

![Holistic classification with Claude Sonnet 5.5 beats page by page: section accuracy against classification cost per 1,000 packets, one point per granularity, strategy and model](../../images/benchmark-guidance/SC-11-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Holistic classification is a good place to start. With Claude Sonnet 5.5 it got 0.91 of sections exactly right, against 0.76 page by page, for $201 of classification per 1,000 packets; with Claude Haiku 5.5 reading text only it got 0.88 for $2.55 (SC-02). Test it on your own packets.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 36 Test Studio runs (28 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-11.json`.*

| | |
|---|---|
| **Changes** | Granularity and strategy, by classification model |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 14–20 packets × 3 runs; up to 6 packets failed in a run and are left out of it |

**Section accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.57 ± 0.01 | 0.69 ± 0.01 | 0.76 ± 0.00 |
| Holistic | 0.60 ± 0.00 | 0.76 ± 0.01 | 0.91 ± 0.01 |
| Two steps: split page by page, then classify | 0.73 ± 0.01 | 0.70 ± 0.02 | 0.82 ± 0.01 |
| Two steps: split holistically, then classify | 0.56 ± 0.03 | 0.76 ± 0.03 | 0.89 ± 0.01 |

**Page accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.83 ± 0.00 | 0.85 ± 0.02 | 0.88 ± 0.00 |
| Holistic | 0.77 ± 0.01 | 0.92 ± 0.01 | 0.95 ± 0.02 |
| Two steps: split page by page, then classify | 0.87 ± 0.00 | 0.82 ± 0.03 | 0.95 ± 0.00 |
| Two steps: split holistically, then classify | 0.73 ± 0.03 | 0.90 ± 0.01 | 0.96 ± 0.01 |

**Grouping score**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.68 ± 0.00 | 0.80 ± 0.01 | 0.85 ± 0.00 |
| Holistic | 0.90 ± 0.00 | 0.90 ± 0.00 | 0.94 ± 0.01 |
| Two steps: split page by page, then classify | 0.83 ± 0.01 | 0.91 ± 0.01 | 0.89 ± 0.00 |
| Two steps: split holistically, then classify | 0.89 ± 0.01 | 0.91 ± 0.01 | 0.93 ± 0.00 |

**Boundary accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.55 ± 0.00 | 0.69 ± 0.01 | 0.76 ± 0.00 |
| Holistic | 0.67 ± 0.00 | 0.79 ± 0.01 | 0.92 ± 0.01 |
| Two steps: split page by page, then classify | 0.75 ± 0.01 | 0.73 ± 0.02 | 0.81 ± 0.01 |
| Two steps: split holistically, then classify | 0.63 ± 0.01 | 0.77 ± 0.04 | 0.90 ± 0.00 |

**Label accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.94 ± 0.01 | 0.96 ± 0.00 | 0.99 ± 0.00 |
| Holistic | 0.92 ± 0.00 | 0.95 ± 0.02 | 0.98 ± 0.01 |
| Two steps: split page by page, then classify | 0.94 ± 0.01 | 0.91 ± 0.02 | 0.99 ± 0.00 |
| Two steps: split holistically, then classify | 0.90 ± 0.01 | 0.96 ± 0.01 | 0.98 ± 0.00 |

**Classification cost per 1,000 packets (USD)**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | $13.2 ± $0.03 | $14.9 ± $0.10 | $254 ± $1.72 |
| Holistic | **$5.31 ± $0.02** | $10.4 ± $0.01 | $201 ± $0.35 |
| Two steps: split page by page, then classify | $12.7 ± $0.01 | $18.0 ± $0.66 | $349 ± $0.53 |
| Two steps: split holistically, then classify | $7.90 ± $0.14 | $16.9 ± $0.38 | $326 ± $0.46 |

Failed packets per run: Page by page · Amazon Nova 2 Lite: 1, 0, 0; Page by page · Claude Haiku 5.5: 0, 2, 2; Page by page · Claude Sonnet 5.5: 0, 0, 2; Holistic · Claude Sonnet 5.5: 0, 0, 1; Two steps: split page by page, then classify · Amazon Nova 2 Lite: 1, 0, 0; Two steps: split page by page, then classify · Claude Haiku 5.5: 2, 5, 6; Two steps: split page by page, then classify · Claude Sonnet 5.5: 0, 0, 2; Two steps: split holistically, then classify · Amazon Nova 2 Lite: 0, 0, 1; Two steps: split holistically, then classify · Claude Haiku 5.5: 0, 0, 3.

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Holistic* with *Claude Haiku 5.5*. A bold figure beats every other in its table by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Do I need page images in the classification prompt?

![Text alone gets the most sections right, for the least: section accuracy against classification cost per 1,000 packets, one point per classification input](../../images/benchmark-guidance/SC-02-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Text only is a good place to start: holistic Claude Haiku 5.5 got 0.88 of sections exactly right from the OCR text alone, against 0.76 with page images too, and cost less. Test it on your own packets.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 9 Test Studio runs (3 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-02.json`.*

| | |
|---|---|
| **Changes** | What the classification model reads |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: Claude Haiku 5.5, holistic · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 19–20 packets × 3 runs; a packet that failed in a run is left out of that run |

| Dataset | What the classification model reads | Section accuracy | Page accuracy | Boundary accuracy | Label accuracy | Classification tokens per page | Classification cost per 1,000 packets (USD) | [Classification cost per error prevented](marginal-cost-of-error.md) vs Text only |
|---|---|---|---|---|---|---|---|---|
| DocSplit | **Text and images (baseline)** | 0.76 ± 0.04 | 0.91 ± 0.02 | 0.8 ± 0.04 | 0.95 ± 0.02 | 6,074 ± 13 | $10.4 ± $0.08 | no measurable gain |
| DocSplit | Text only | **0.88 ± 0.01** | **0.95 ± 0.01** | **0.91 ± 0.02** | 0.96 ± 0.02 | **1,201 ± 40** | **$2.55 ± $0.05** | — |
| DocSplit | Images only | 0.74 ± 0.06 | 0.88 ± 0.05 | 0.75 ± 0.05 | 0.95 ± 0.03 | 5,139 ± 32 | $9.13 ± $0.27 | no measurable gain |

Failed packets per run: Text and images (baseline): 0, 1, 0; Images only: 1, 1, 0.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Can a decision model classify the sections?

![With the preset's descriptions, Claude Haiku 5.5 classifies sections better than the Decider 2B: label accuracy against classification cost per 1,000 packets, one point per which model classifies each section](../../images/benchmark-guidance/SC-12-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Not yet, with the preset's class descriptions: Claude Haiku 5.5 classified 0.96 of sections right, against 0.81 to 0.83 for the Strands Decider 2B, whose classification cost slightly more ($19 to $20 against $17 per 1,000 packets). The decision model is more sensitive to how classes are described: a 100-packet re-run with one-line descriptions of what each page looks like is in progress, and in its first runs the Strands Decider 2B classified 0.90 of sections right with them. Test it on your own packets.

<details>
<summary>Details</summary>

*Measured on each dataset's preset, not the baseline setup.*

*Measured on release 0.6.15-develop-d1be93f, November 2026, us-east-1. Source: 15 Test Studio runs (6 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-12.json`.*

The decision model is the Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, 8-bit), on AWS Lambda, reading each section's first page at 448 pixels; its cost is the function's duration, with cold starts left out. These runs used the docsplit preset's class descriptions, written for the LLM prompt.

| | |
|---|---|
| **Changes** | Which model classifies each section after Claude Haiku 5.5 detects the sections: an LLM or a decision model |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 18–20 packets × 3 runs; up to 2 packets failed in a run and are left out of it |

| Dataset | Which model classifies each section after Claude Haiku 5.5 detects the sections: an LLM or a decision model | Section accuracy | Label accuracy | Page accuracy | Boundary accuracy | Classification tokens per page | Total tokens per page | Classification cost per 1,000 packets (USD) | Median seconds per packet |
|---|---|---|---|---|---|---|---|---|---|
| DocSplit | **LLM · Claude Haiku 5.5 (baseline)** | 0.77 ± 0.05 | **0.96 ± 0.01** | 0.89 ± 0.05 | 0.76 ± 0.07 | 9,988 ± 57 | 24,723 ± 126 | $17.2 ± $0.17 | 112 ± 14 |
| DocSplit | LLM · Amazon Nova 2 Lite | 0.74 ± 0.06 | 0.94 ± 0.02 | 0.87 ± 0.04 | 0.75 ± 0.08 | 6,894 ± 16 | 21,658 ± 67 | **$15.3 ± $0.11** | 128 ± 18 |
| DocSplit | Decision model · first page image | 0.66 ± 0.01 | 0.81 ± 0.02 | 0.76 ± 0 | 0.79 ± 0.02 | 6,000 ± 3.69 | 20,790 ± 51 | $20.3 ± $0.71 | 139 ± 9.2 |
| DocSplit | Decision model · first page image and text | 0.64 ± 0.04 | 0.83 ± 0.02 | 0.8 ± 0.01 | 0.72 ± 0.05 | 6,009 ± 3.13 | 20,757 ± 28 | $20.5 ± $0.12 | 131 ± 12 |
| DocSplit | Decision model · first 2 page images | 0.65 ± 0.02 | 0.82 ± 0.03 | 0.81 ± 0 | 0.77 ± 0.03 | 5,999 ± 2.41 | 20,787 ± 54 | $19.0 ± $0.43 | 151 ± 21 |

Failed packets per run: Decision model · first page image and text: 2, 2, 2.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Which way of splitting and classifying should I use?

![To be measured: section accuracy against classification cost per 1,000 packets, one point per approach and model](../../images/benchmark-guidance/SC-13-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Measured; see the table.

<details>
<summary>Details</summary>

*Measured on each dataset's preset, not the baseline setup.*

*Measured on release 0.6.15-develop-d1be93f, November 2026, us-east-1. Source: 26 Test Studio runs (22 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-13.json`.*

In the two split rows the model named in the column finds the section boundaries (reading the whole packet); the LLM row then has that same model classify each section, and the decision-model row has the Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, 8-bit, on AWS Lambda, images at 448 pixels) classify it from the section's first page image.

| | |
|---|---|
| **Changes** | How the packet is split and who names each section, by model |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (100 packets)](datasets.md#docsplit-poly-seq-100-packets); [DocSplit same-class runs (60 packets)](datasets.md#docsplit-same-class-runs-60-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 90–100 packets × 2–3 runs; up to 10 packets failed in a run and are left out of it |

**DocSplit: section accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.49 ± 0.01 | 0.70 ± 0.00 | 0.73 ± 0.00 |
| Holistic | 0.51 ± 0.01 | 0.75 ± 0.02 | **0.90 ± 0.00** |
| Split, then an LLM names each section | 0.46 ± 0.01 | 0.75 ± 0.01 | 0.88 ± 0.01 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: boundary accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.49 ± 0.01 | 0.70 ± 0.01 | 0.74 ± 0.01 |
| Holistic | 0.63 ± 0.02 | 0.80 ± 0.02 | **0.91 ± 0.00** |
| Split, then an LLM names each section | 0.56 ± 0.02 | 0.77 ± 0.00 | 0.89 ± 0.01 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: label accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.91 ± 0.01 | 0.98 ± 0.00 | **0.99 ± 0.00** |
| Holistic | 0.86 ± 0.00 | 0.95 ± 0.01 | 0.98 ± 0.00 |
| Split, then an LLM names each section | 0.90 ± 0.01 | 0.97 ± 0.01 | 0.98 ± 0.00 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: page accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.83 ± 0.00 | 0.88 ± 0.00 | 0.90 ± 0.00 |
| Holistic | 0.80 ± 0.01 | 0.90 ± 0.00 | 0.96 ± 0.00 |
| Split, then an LLM names each section | 0.75 ± 0.01 | 0.93 ± 0.02 | 0.96 ± 0.00 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: grouping score**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.66 ± 0.00 | 0.82 ± 0.01 | 0.85 ± 0.00 |
| Holistic | 0.90 ± 0.00 | 0.91 ± 0.01 | **0.94 ± 0.00** |
| Split, then an LLM names each section | 0.88 ± 0.01 | 0.89 ± 0.00 | 0.94 ± 0.00 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: calibration error**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Holistic | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then an LLM names each section | 0.90 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: median seconds per packet**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 207.78 ± 57.24 | 294.29 ± 54.02 | 144.31 ± 13.55 |
| Holistic | 122.26 ± 6.29 | 137.64 ± 4.05 | 128.63 ± 6.22 |
| Split, then an LLM names each section | **106.08 ± 2.36** | 210.32 ± 20.29 | 143.69 ± 5.90 |
| Split, then a decision model names each section | — | — | — |

**DocSplit: classification cost per 1,000 packets (USD)**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | $13.2 ± $0.01 | $14.5 ± $0.03 | $252 ± $0.53 |
| Holistic | **$5.39 ± $0.00** | $10.4 ± $0.03 | $200 ± $0.37 |
| Split, then an LLM names each section | $7.83 ± $0.03 | $16.4 ± $0.05 | $315 ± $0.46 |
| Split, then a decision model names each section | — | — | — |

Failed packets per run: DocSplit — Holistic · Claude Haiku 5.5: 1, 0, 1; DocSplit — Split, then an LLM names each section · Amazon Nova 2 Lite: 1, 1, 0; DocSplit — Split, then an LLM names each section · Claude Haiku 5.5: 8, 10.

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Holistic* with *Claude Haiku 5.5*. A bold figure beats every other in its table by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Planned

We haven't run these yet.

- How long a packet can holistic classification handle? (SC-04)
- Do few-shot examples help classification? (SC-06)
- How much do class descriptions matter? (SC-07)
- Does accuracy drop as I add document classes? (SC-08)
- How small can classification images be? (SC-09)
- If every file is one document, should I turn splitting off? (SC-10)
- Do long packets change which approach to use? (SC-14)

<!-- END GENERATED FAQ -->
