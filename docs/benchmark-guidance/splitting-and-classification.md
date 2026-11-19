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

<!-- BEGIN GENERATED APPROACHES -->

## Which approach should I use?

![Four ways to find the documents in a packet and classify each one: page by page, holistic, split then an LLM classifies each section, and split then a decision model classifies each section](../../images/benchmark-guidance/figures/splitting-approaches.svg)

**Answer:** Holistic classification with Claude Haiku 5.5 is a good place to start. On DocSplit packets it got 74% of sections exactly right for $25.6 of classification per 1,000 packets; no approach under $200 got measurably more right. With Claude Sonnet 5.5 it got the most right, 91%, for $200, 7.8 times as much. Reading text only, without page images, does better still ([SC-02](#do-i-need-page-images-in-the-classification-prompt)). Test it on your own packets.

**The economics:** On DocSplit packets, Claude Sonnet 5.5 instead of Claude Haiku 5.5 adds $175 of classification per 1,000 packets and gets 671 more sections right, so it costs $0.26 per section it saves. Choose it when finding and fixing a misfiled or mis-split document costs you more than $0.26.

![The model matters more than the approach: sections exactly right against classification cost per 1,000 packets, one point per approach and model, on DocSplit packets and same-class packets](../../images/benchmark-guidance/approaches/sections-against-cost.svg)

*Bands show ±2 standard errors. The cost axis jumps from $40 to $150.*

![Where each approach loses sections: for each approach and model, the share of true sections lost to each kind of error (wrong class, merged with a neighbour and split into pieces), on DocSplit packets](../../images/benchmark-guidance/approaches/where-sections-are-lost.svg)

*Measured on the 80 DocSplit packets every run scored, as are the error, sections-per-packet and confidence figures below.*

### Page by page

One call per page picks its class; neighbouring pages of the same class become one section.

- It never merged two documents into one.
- With documents of the same class back to back, it got the most sections right with Amazon Nova 2 Lite (0.51).
- It cuts documents into pieces: with Claude Haiku 5.5 it predicted 6.4 sections per packet against 3.95 true, so 28% of sections were split.
- Classification costs $13.2 with Amazon Nova 2 Lite, $14.5 with Claude Haiku 5.5 and $252 with Claude Sonnet 5.5 per 1,000 packets.

Use it when most of your documents are a single page, or with Amazon Nova 2 Lite when documents of the same class sit back to back.

### Holistic

One call reads the whole packet and returns each document's pages and class.

- No other approach got measurably more sections right with any model.
- It can merge neighbouring documents: Amazon Nova 2 Lite merged 28% of sections into a neighbour, Claude Haiku 5.5 7% and Claude Sonnet 5.5 none.
- With documents of the same class back to back, Amazon Nova 2 Lite merged 40% of those pairs into one, against 28% of other pairs.
- It often returns no confidence: none for 166 of 740 sections with Claude Haiku 5.5 and 715 of 875 sections with Claude Sonnet 5.5 ([#2039](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/2039)).
- With Claude Haiku 5.5 it costs more than page by page ($25.6 against $14.5 per 1,000 packets): a 15-to-20-page packet with its page images is one request of more than 100,000 tokens, which Claude Haiku 5.5 bills at its long-context rate. Reading text only keeps the call under that.
- Classification costs $5.39 with Amazon Nova 2 Lite, $25.6 with Claude Haiku 5.5 and $200 with Claude Sonnet 5.5 per 1,000 packets.

Use it to start, with Claude Sonnet 5.5 where you need the most sections right.

### Split, then an LLM classifies each section

One call finds where each document starts, then one call per section picks its class.

- Its confidence separates right classes from wrong ones well: AUROC 0.96 with Claude Haiku 5.5 and 0.96 with Claude Sonnet 5.5, the highest of any approach with Claude Sonnet 5.5.
- With every model it cost more than holistic, for no measurable gain in sections right.
- Classification costs $7.83 with Amazon Nova 2 Lite, $31.5 with Claude Haiku 5.5 and $315 with Claude Sonnet 5.5 per 1,000 packets.

Use it when you send low-confidence sections to a person for review.

### Split, then a decision model classifies each section

One call finds where each document starts, then the Strands Decider 2B picks each section's class from its first two page images.

- Its confidence separates right classes from wrong ones well: AUROC 0.93 with Amazon Nova 2 Lite finding the sections, 0.91 with Claude Haiku 5.5 finding the sections and 0.90 with Claude Sonnet 5.5 finding the sections, the highest of any approach with Amazon Nova 2 Lite.
- It keeps the section detector's mistakes: with Amazon Nova 2 Lite, 36% of sections were merged into a neighbour.
- Classification costs $9.30 with Amazon Nova 2 Lite, $31.9 with Claude Haiku 5.5 and $204 with Claude Sonnet 5.5 per 1,000 packets.

Use it when you want a small model on AWS Lambda to classify each section; give it one-line class descriptions ([SC-12](#can-a-decision-model-classify-the-sections)).

### Long packets

Holistic stays a good place to start on long packets: there its advantage is in grouping pages, not in getting every boundary exactly right. On Claude Haiku 5.5 it costs 2.3 times as much as page by page ($103 against $44.6 of classification per 1,000 long packets), because a 20-page window with page images goes over the 100,000 tokens above which Claude Haiku 5.5 bills its long-context rate; a 10- or 15-page window keeps most calls under it ([SC-04](#how-long-a-packet-can-holistic-classification-handle)), as does reading text only.

Long packets are harder, clearly so for holistic: on 30–60 pages both got 62% of sections exactly right, against 76% for holistic and 68% for page by page on 5–20 pages. On that strict measure, holistic's lead on short packets (+0.08 ± 0.05) isn't measurable on long ones (0.00 ± 0.06). But on the grouping score, which gives credit for nearly right sections, holistic still leads clearly on long packets (+0.06 ± 0.02), as it does on V-measure, Rand index and ordering. Page accuracy is the same, and page by page labels the sections it gets right slightly better (label accuracy −0.05 ± 0.02).

Splitting first, then having an LLM classify each section, got no measurably more sections exactly right than holistic on long packets, nor a measurably better grouping score; it was narrowly ahead on ordering (+0.04 ± 0.04) and page accuracy (+0.03 ± 0.03), at 1.2 times the cost ($125 against $103).

![With Claude Haiku 5.5, long packets are harder, clearly so for holistic, and holistic's lead on long packets is unclear: section accuracy per packet for holistic and page by page on 5–20 pages and 30–60 pages, and the paired difference per packet, as boxes with the mean and two standard errors](../../images/benchmark-guidance/approaches/long-packets.svg)

*Boxes span the middle half of the packets, with a line at the median; whiskers reach the 5th and 95th percentiles; the dot is the mean, with a bar of ±2 standard errors.*

![With Claude Haiku 5.5, holistic still groups long packets better than page by page: holistic minus page by page on each packet-splitting metric, with two standard errors either side, on 5–20 pages and 30–60 pages](../../images/benchmark-guidance/approaches/long-packets-metrics.svg)

*Each dot is the mean of the packets' paired differences, with a bar of ±2 standard errors; a bar that crosses the zero line is no measurable difference.*

<details>
<summary>What these metrics mean</summary>

- *Sections exactly right:* the share of true sections returned with exactly the right pages and class (section accuracy).
- *Grouping score:* DocSplit's packet score, from 0 to 1: how pages are grouped (V-measure and Rand index) and ordered, so a nearly right section still earns credit.
- *Boundary accuracy:* the share of sections with exactly the right page range, whatever their class.
- *V-measure:* how well pages are grouped into the true documents, from 0 to 1: each section should hold one document, and each document should sit in one section.
- *Rand index:* the share of pairs of pages put together, or kept apart, as in the true documents.
- *Ordering:* how close each document's pages are to their true order, from 0 to 1 (Kendall's tau, averaged over documents).
- *Page accuracy:* the share of pages with the right class.
- *Label accuracy:* of the sections with the right page range, the share with the right class.

The page's [metric definitions](#questions) and Stickler's [Document Packet Splitting Evaluation](https://awslabs.github.io/stickler/Guides/Document_Packet_Splitting/) have more.

</details>

**How much one holistic call can hold.** A holistic call sends every page of the packet in one request, so its size is set by service limits, not by accuracy:

- **Images:** Amazon Bedrock sets no limit of its own on how many images a request carries; each model's limits apply ([Bedrock API restrictions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-api-restrictions.html)). For Claude that is a request of up to 32 MB with images up to 8,000 pixels, and in a request with more than 20 images every image must be 2,000 pixels or smaller ([Claude vision limits](https://platform.claude.com/docs/en/build-with-claude/vision#request-limits)). The accelerator's packet window is 20 pages by default so pages keep their full resolution; a wider window works, at 2,000 pixels a page.
- **Tokens:** in our runs a page cost about 6,100 tokens with its image and 1,200 as text alone on the Claude models, and about 1,050 with its image on Amazon Nova 2 Lite. [Claude Haiku 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html), [Claude Sonnet 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5-5.html) and [Amazon Nova 2 Lite](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-2-lite.html) each read up to 1 million tokens, so text alone fits hundreds of pages.
- **Price:** Claude Haiku 5.5 bills a prompt of more than 100,000 tokens at higher long-context rates ([model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html#model-card-anthropic-claude-haiku-5-5-pricing)); in the Bedrock price list they are five times the standard rates. That is about 16 pages with images, or about 80 pages of text alone.

A packet longer than that is not a reason to switch to page by page: the accelerator reads it in overlapping 20-page windows and merges them ([Packet granularity and windowing](../classification.md#packet-granularity-images-few-shot-examples-and-windowing)). Prefer page by page when a single document in your packets is longer than the window and its pages need images.

<details>
<summary>All the numbers</summary>

| Instead of Holistic · Haiku 5.5 | Extra classification cost per 1,000 packets | Extra sections right per 1,000 packets | [Cost per section it saves](marginal-cost-of-error.md) |
|---|---|---|---|
| Page by page · Nova 2 Lite | −$12.4 | −1,017 | saves $0.01 per extra wrong section |
| Holistic · Nova 2 Lite | −$20.3 | −938 | saves $0.02 per extra wrong section |
| Split, LLM classifies · Nova 2 Lite | −$17.8 | −1,092 | saves $0.02 per extra wrong section |
| Split, Decider 2B classifies · Nova 2 Lite | −$16.3 | −1,104 | saves $0.01 per extra wrong section |
| Page by page · Haiku 5.5 | −$11.2 | no measurable difference | cheaper, no measurable loss |
| Split, LLM classifies · Haiku 5.5 | +$5.83 | no measurable difference | no measurable gain |
| Split, Decider 2B classifies · Haiku 5.5 | +$6.21 | no measurable difference | no measurable gain |
| Page by page · Sonnet 5.5 | +$227 | no measurable difference | no measurable gain |
| Holistic · Sonnet 5.5 | +$175 | +671 | $0.26 |
| Split, LLM classifies · Sonnet 5.5 | +$290 | +562 | $0.51 |
| Split, Decider 2B classifies · Sonnet 5.5 | +$179 | +429 | $0.42 |

*DocSplit packets, N = 90–100 packets × 3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

**DocSplit packets: where sections were lost**

| Approach · model | Wrong class | Merged with a neighbour | Split into pieces | Pages left out | Predicted sections per packet | Confidence AUROC | Sections with no confidence |
|---|---|---|---|---|---|---|---|
| Page by page · Amazon Nova 2 Lite | 4% | 0% | 48% | 0% | 8.29 | 0.49 ± 0.02 | 0% |
| Page by page · Claude Haiku 5.5 | 2% | 0% | 28% | 0% | 6.39 | 0.92 ± 0.07 | 0% |
| Page by page · Claude Sonnet 5.5 | 1% | 0% | 25% | 0% | 6.05 | 0.87 ± 0.01 | 0% |
| Holistic · Amazon Nova 2 Lite | 7% | 28% | 14% | 1% | 3.55 | 0.66 ± 0.09 | 0% |
| Holistic · Claude Haiku 5.5 | 4% | 7% | 13% | 1% | 4.65 | 0.91 ± 0.03 | 22% |
| Holistic · Claude Sonnet 5.5 | 1% | 0% | 8% | 0% | 4.76 | — | 82% |
| Split, then an LLM classifies each section · Amazon Nova 2 Lite | 5% | 34% | 14% | 0% | 3.39 | 0.78 ± 0.01 | 0% |
| Split, then an LLM classifies each section · Claude Haiku 5.5 | 2% | 7% | 16% | 0% | 4.79 | 0.96 ± 0.02 | 0% |
| Split, then an LLM classifies each section · Claude Sonnet 5.5 | 2% | 0% | 10% | 0% | 4.84 | 0.96 ± 0.01 | 0% |
| Split, then a decision model classifies each section · Amazon Nova 2 Lite | 3% | 36% | 14% | 0% | 3.31 | 0.93 ± 0.02 | 0% |
| Split, then a decision model classifies each section · Claude Haiku 5.5 | 4% | 6% | 15% | 1% | 4.80 | 0.91 ± 0 | 0% |
| Split, then a decision model classifies each section · Claude Sonnet 5.5 | 6% | 0% | 10% | 0% | 4.84 | 0.9 ± 0.01 | 0% |

*80 packets × 3 runs: the packets every run scored; 3.95 true sections per packet; AUROC is shown over two runs or more, for the sections whose pages were right.*

**Same class back to back: where sections were lost**

| Approach · model | Wrong class | Merged with a neighbour | Split into pieces | Pages left out | Predicted sections per packet | Confidence AUROC | Sections with no confidence |
|---|---|---|---|---|---|---|---|
| Page by page · Amazon Nova 2 Lite | 3% | 1% | 45% | 0% | 9.97 | 0.47 ± 0.05 | 0% |
| Page by page · Claude Haiku 5.5 | 2% | 2% | 29% | 0% | 8.01 | 0.83 ± 0.02 | 0% |
| Page by page · Claude Sonnet 5.5 | 1% | 2% | 28% | 0% | 7.67 | 0.8 ± 0.04 | 0% |
| Holistic · Amazon Nova 2 Lite | 8% | 32% | 14% | 0% | 4.47 | 0.61 ± 0.06 | 0% |
| Holistic · Claude Haiku 5.5 | 6% | 8% | 12% | 1% | 5.67 | 0.75 ± 0.07 | 21% |
| Holistic · Claude Sonnet 5.5 | 2% | 0% | 9% | 0% | 5.82 | 0.97 ± 0.02 | 73% |
| Split, then an LLM classifies each section · Amazon Nova 2 Lite | 3% | 41% | 14% | 0% | 4.01 | 0.71 ± 0.09 | 0% |
| Split, then an LLM classifies each section · Claude Haiku 5.5 | 3% | 6% | 15% | 0% | 5.87 | 0.86 ± 0.05 | 0% |
| Split, then an LLM classifies each section · Claude Sonnet 5.5 | 1% | 0% | 10% | 0% | 5.88 | 0.91 ± 0.03 | 0% |
| Split, then a decision model classifies each section · Amazon Nova 2 Lite | 3% | 41% | 14% | 0% | 4.04 | 0.94 ± 0 | 0% |
| Split, then a decision model classifies each section · Claude Haiku 5.5 | 5% | 6% | 14% | 0% | 5.85 | 0.95 ± 0.01 | 0% |
| Split, then a decision model classifies each section · Claude Sonnet 5.5 | 5% | 0% | 10% | 0% | 5.85 | 0.94 ± 0 | 0% |

*57 packets × 3 runs: the packets every run scored; 4.91 true sections per packet; AUROC is shown over two runs or more, for the sections whose pages were right.*

*Measured on each dataset's preset, not the baseline setup.*

*Measured on release 0.6.15-develop-d1be93f+fixes-2034-2035-2036, November 2026, us-east-1. Source: 72 Test Studio runs (26 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-13.json`.*

In the two split rows the model named in the column finds the section boundaries (reading the whole packet); the LLM row then has that same model classify each section, and the decision-model row has the Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, 8-bit, on AWS Lambda, images at 448 pixels) classify it from the section's first two page images, with one-line class descriptions (the setting SC-12 found works best for it; the section detection step in that row reads the same descriptions).

| | |
|---|---|
| **Changes** | How the packet is split and who classifies each section, by model |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (100 packets)](datasets.md#docsplit-poly-seq-100-packets); [DocSplit same-class runs (60 packets)](datasets.md#docsplit-same-class-runs-60-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N: DocSplit — 90–100 packets × 3 runs; up to 10 packets failed in a run and are left out of it; Same-class runs — 59–60 packets × 3 runs; a packet that failed in a run is left out of that run |

**DocSplit: section accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.48 ± 0.01 | 0.70 ± 0.00 | 0.74 ± 0.00 |
| Holistic | 0.50 ± 0.01 | 0.74 ± 0.02 | **0.91 ± 0.00** |
| Split, then an LLM classifies each section | 0.46 ± 0.00 | 0.75 ± 0.00 | 0.88 ± 0.01 |
| Split, then a decision model classifies each section | 0.46 ± 0.01 | 0.75 ± 0.01 | 0.85 ± 0.00 |

**DocSplit: boundary accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.48 ± 0.01 | 0.70 ± 0.01 | 0.75 ± 0.01 |
| Holistic | 0.62 ± 0.01 | 0.81 ± 0.02 | **0.92 ± 0.00** |
| Split, then an LLM classifies each section | 0.56 ± 0.02 | 0.78 ± 0.00 | 0.90 ± 0.01 |
| Split, then a decision model classifies each section | 0.55 ± 0.01 | 0.79 ± 0.01 | 0.90 ± 0.00 |

**DocSplit: label accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.91 ± 0.01 | 0.98 ± 0.00 | **0.99 ± 0.00** |
| Holistic | 0.85 ± 0.00 | 0.94 ± 0.01 | 0.98 ± 0.00 |
| Split, then an LLM classifies each section | 0.89 ± 0.01 | 0.96 ± 0.01 | 0.98 ± 0.00 |
| Split, then a decision model classifies each section | 0.94 ± 0.00 | 0.96 ± 0.00 | 0.94 ± 0.00 |

**DocSplit: page accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.85 ± 0.00 | 0.90 ± 0.00 | 0.92 ± 0.00 |
| Holistic | 0.80 ± 0.01 | 0.90 ± 0.01 | 0.97 ± 0.00 |
| Split, then an LLM classifies each section | 0.76 ± 0.01 | 0.92 ± 0.01 | 0.98 ± 0.00 |
| Split, then a decision model classifies each section | 0.76 ± 0.01 | 0.87 ± 0.00 | 0.91 ± 0.00 |

**DocSplit: grouping score**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.66 ± 0.00 | 0.82 ± 0.01 | 0.85 ± 0.00 |
| Holistic | 0.90 ± 0.00 | 0.91 ± 0.01 | **0.94 ± 0.00** |
| Split, then an LLM classifies each section | 0.88 ± 0.01 | 0.89 ± 0.00 | 0.94 ± 0.00 |
| Split, then a decision model classifies each section | 0.87 ± 0.00 | 0.90 ± 0.00 | 0.94 ± 0.00 |

**DocSplit: calibration error**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Holistic | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then an LLM classifies each section | 0.90 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then a decision model classifies each section | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |

**DocSplit: median seconds per packet**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 207.78 ± 57.24 | 294.29 ± 54.02 | 144.31 ± 13.55 |
| Holistic | 122.26 ± 6.29 | 137.64 ± 4.05 | 128.63 ± 6.22 |
| Split, then an LLM classifies each section | **106.08 ± 2.36** | 188.74 ± 24.56 | 143.69 ± 5.90 |
| Split, then a decision model classifies each section | 174.45 ± 24.34 | 225.52 ± 15.35 | 158.02 ± 6.72 |

**DocSplit: classification cost per 1,000 packets (USD)**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | $13.2 ± $0.01 | $14.5 ± $0.03 | $252 ± $0.53 |
| Holistic | **$5.39 ± $0.00** | $25.6 ± $0.26 | $200 ± $0.37 |
| Split, then an LLM classifies each section | $7.83 ± $0.03 | $31.5 ± $0.69 | $315 ± $0.46 |
| Split, then a decision model classifies each section | $9.30 ± $0.27 | $31.9 ± $0.20 | $204 ± $0.10 |

**Same-class runs: section accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.51 ± 0.01 | 0.67 ± 0.01 | 0.68 ± 0.00 |
| Holistic | 0.46 ± 0.00 | 0.74 ± 0.02 | 0.90 ± 0.01 |
| Split, then an LLM classifies each section | 0.43 ± 0.00 | 0.75 ± 0.02 | 0.89 ± 0.00 |
| Split, then a decision model classifies each section | 0.42 ± 0.01 | 0.74 ± 0.01 | 0.85 ± 0.01 |

**Same-class runs: boundary accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.54 ± 0.01 | 0.69 ± 0.01 | 0.70 ± 0.00 |
| Holistic | 0.55 ± 0.01 | 0.80 ± 0.02 | 0.92 ± 0.01 |
| Split, then an LLM classifies each section | 0.47 ± 0.00 | 0.79 ± 0.02 | 0.91 ± 0.00 |
| Split, then a decision model classifies each section | 0.46 ± 0.01 | 0.80 ± 0.00 | 0.91 ± 0.01 |

**Same-class runs: label accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.94 ± 0.01 | 0.97 ± 0.00 | 0.98 ± 0.00 |
| Holistic | 0.85 ± 0.01 | 0.93 ± 0.01 | 0.98 ± 0.00 |
| Split, then an LLM classifies each section | 0.95 ± 0.01 | 0.97 ± 0.00 | **0.98** |
| Split, then a decision model classifies each section | 0.93 ± 0.01 | 0.93 ± 0.00 | 0.94 ± 0.00 |

**Same-class runs: page accuracy**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.79 ± 0.00 | 0.83 ± 0.00 | 0.86 ± 0.00 |
| Holistic | 0.77 ± 0.01 | 0.88 ± 0.01 | **0.95 ± 0.00** |
| Split, then an LLM classifies each section | 0.79 ± 0.00 | 0.90 ± 0.01 | 0.95 ± 0.00 |
| Split, then a decision model classifies each section | 0.78 ± 0.00 | 0.86 ± 0.00 | 0.88 ± 0.00 |

**Same-class runs: grouping score**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.69 ± 0.00 | 0.81 ± 0.00 | 0.84 ± 0.00 |
| Holistic | 0.87 ± 0.00 | 0.92 ± 0.01 | 0.95 ± 0.00 |
| Split, then an LLM classifies each section | 0.86 ± 0.00 | 0.90 ± 0.01 | 0.95 ± 0.00 |
| Split, then a decision model classifies each section | 0.86 ± 0.01 | 0.91 ± 0.01 | 0.95 ± 0.00 |

**Same-class runs: calibration error**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Holistic | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then an LLM classifies each section | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |
| Split, then a decision model classifies each section | 0.89 ± 0.00 | 0.89 ± 0.00 | 0.89 ± 0.00 |

**Same-class runs: median seconds per packet**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | 206.64 ± 55.01 | 338.17 ± 21.46 | 140.68 ± 45.25 |
| Holistic | 164.29 ± 44.03 | 199.71 ± 35.10 | 153.90 ± 26.99 |
| Split, then an LLM classifies each section | 145.10 ± 6.30 | 179.41 ± 27.80 | 179.36 ± 19.56 |
| Split, then a decision model classifies each section | 137.83 ± 23.62 | 176.47 ± 12.01 | 151.41 ± 30.72 |

**Same-class runs: classification cost per 1,000 packets (USD)**

| | Amazon Nova 2 Lite | Claude Haiku 5.5 | Claude Sonnet 5.5 |
|---|---|---|---|
| Page by page | $12.8 ± $0.03 | $14.4 ± $0.06 | $249 ± $0.61 |
| Holistic | **$5.47 ± $0.06** | $24.8 ± $0.02 | $197 ± $0.43 |
| Split, then an LLM classifies each section | $7.83 ± $0.03 | $31.2 ± $0.25 | $325 ± $0.29 |
| Split, then a decision model classifies each section | $9.31 ± $0.41 | $31.1 ± $0.19 | $202 ± $0.27 |

Failed packets per run: DocSplit — Holistic · Claude Haiku 5.5: 1, 0, 1; DocSplit — Split, then an LLM classifies each section · Amazon Nova 2 Lite: 1, 1, 0; DocSplit — Split, then an LLM classifies each section · Claude Haiku 5.5: 8, 10, 0; DocSplit — Split, then a decision model classifies each section · Amazon Nova 2 Lite: 0, 1, 0; Same-class runs — Holistic · Claude Sonnet 5.5: 0, 1, 1; Same-class runs — Split, then a decision model classifies each section · Claude Haiku 5.5: 0, 1, 0.

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Holistic* with *Claude Haiku 5.5*. A bold figure beats every other in its table by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

<!-- END GENERATED APPROACHES -->

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
| Window size | How many pages a holistic call sees at once | [SC-04](#how-long-a-packet-can-holistic-classification-handle) |
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

**Answer:** Holistic classification is a good place to start. On 20 packets, Claude Sonnet 5.5 got 91% of sections exactly right holistically, against 76% page by page, for $201 of classification per 1,000 packets. Claude Haiku 5.5 reading text only got 88% on 98 packets for $2.58 ([SC-02](#do-i-need-page-images-in-the-classification-prompt)). [Which way of splitting and classifying should I use?](#which-way-of-splitting-and-classifying-should-i-use) compares the approaches on 100 packets. Test it on your own packets.

**The economics:** Claude Sonnet 5.5 instead of Claude Haiku 5.5 adds $177 of classification per 1,000 packets and gets 710 more sections right, so it costs $0.25 per section it saves. Choose it when finding and fixing a misfiled or mis-split document costs you more than $0.25.

| Instead of Holistic · Haiku 5.5 | Extra classification cost per 1,000 packets | Extra sections right per 1,000 packets | [Cost per section it saves](marginal-cost-of-error.md) |
|---|---|---|---|
| Page by page · Nova 2 Lite | −$10.6 | −845 | saves $0.01 per extra wrong section |
| Holistic · Nova 2 Lite | −$18.5 | −683 | saves $0.03 per extra wrong section |
| Two steps, page by page · Nova 2 Lite | −$11.1 | no measurable difference | cheaper, no measurable loss |
| Two steps, holistic · Nova 2 Lite | −$15.9 | −874 | saves $0.02 per extra wrong section |
| Page by page · Haiku 5.5 | −$8.93 | −294 | saves $0.03 per extra wrong section |
| Two steps, page by page · Haiku 5.5 | −$5.76 | −255 | saves $0.02 per extra wrong section |
| Two steps, holistic · Haiku 5.5 | +$5.31 | no measurable difference | no measurable gain |
| Page by page · Sonnet 5.5 | +$230 | no measurable difference | no measurable gain |
| Holistic · Sonnet 5.5 | +$177 | +710 | $0.25 |
| Two steps, page by page · Sonnet 5.5 | +$326 | +287 | $1.13 |
| Two steps, holistic · Sonnet 5.5 | +$302 | +600 | $0.50 |

*DocSplit packets, N = 14–20 packets × 3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

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
| Holistic | **$5.31 ± $0.02** | $23.8 ± $0.02 | $201 ± $0.35 |
| Two steps: split page by page, then classify | $12.7 ± $0.01 | $18.0 ± $0.66 | $349 ± $0.53 |
| Two steps: split holistically, then classify | $7.90 ± $0.14 | $29.1 ± $1.70 | $326 ± $0.46 |

Failed packets per run: Page by page · Amazon Nova 2 Lite: 1, 0, 0; Page by page · Claude Haiku 5.5: 0, 2, 2; Page by page · Claude Sonnet 5.5: 0, 0, 2; Holistic · Claude Sonnet 5.5: 0, 0, 1; Two steps: split page by page, then classify · Amazon Nova 2 Lite: 1, 0, 0; Two steps: split page by page, then classify · Claude Haiku 5.5: 2, 5, 6; Two steps: split page by page, then classify · Claude Sonnet 5.5: 0, 0, 2; Two steps: split holistically, then classify · Amazon Nova 2 Lite: 0, 0, 1; Two steps: split holistically, then classify · Claude Haiku 5.5: 0, 0, 3.

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Holistic* with *Claude Haiku 5.5*. A bold figure beats every other in its table by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Do I need page images in the classification prompt?

![Text alone gets the most sections right, for the least: section accuracy against classification cost per 1,000 packets, one point per classification input](../../images/benchmark-guidance/SC-02-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Text only is a good place to start. Holistic classification with Claude Haiku 5.5 got 88% of sections exactly right from the OCR text alone, against 75% with page images as well and 65% with images alone. It also cost a tenth as much: $2.58 against $26 of classification per 1,000 packets. Test it on your own packets.

**The economics:** Text only instead of text and images saves $23.1 of classification per 1,000 packets and gets 551 more sections right.

| Instead of Text and images (baseline) | Extra classification cost per 1,000 packets | Extra sections right per 1,000 packets | [Cost per section it saves](marginal-cost-of-error.md) |
|---|---|---|---|
| Text only | −$23.1 | +551 | more right, for less |
| Images only | −$16.8 | −395 | saves $0.04 per extra wrong section |

*DocSplit packets, N = 99–100 packets × 3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

<details>
<summary>Details</summary>

*Measured on release 0.6.15-develop-d1be93f+fixes-2034-2035-2036, November 2026, us-east-1. Source: 9 Test Studio runs (2 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-02.json`.*

| | |
|---|---|
| **Changes** | What the classification model reads |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (100 packets)](datasets.md#docsplit-poly-seq-100-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: Claude Haiku 5.5, holistic · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 99–100 packets × 3 runs; a packet that failed in a run is left out of that run |

| Dataset | What the classification model reads | Section accuracy | Page accuracy | Boundary accuracy | Label accuracy | Classification tokens per page | Classification cost per 1,000 packets (USD) | [Classification cost per wrong section prevented](marginal-cost-of-error.md) vs Text only |
|---|---|---|---|---|---|---|---|---|
| DocSplit | **Text and images (baseline)** | 0.75 ± 0.04 | 0.9 ± 0.01 | 0.8 ± 0.04 | 0.95 ± 0.02 | 6,168 ± 7.23 | $25.6 ± $0.52 | no measurable gain |
| DocSplit | Text only | **0.88 ± 0** | **0.95 ± 0** | **0.91 ± 0.01** | **0.97 ± 0** | **1,283 ± 11** | **$2.58 ± $0.03** | — |
| DocSplit | Images only | 0.65 ± 0.03 | 0.89 ± 0 | 0.68 ± 0.02 | 0.93 ± 0.02 | 5,117 ± 6.88 | $8.86 ± $0.06 | no measurable gain |

Failed packets per run: Text and images (baseline): 1, 0, 1.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### How long a packet can holistic classification handle?

![A 10-page window costs 60% less on long packets, and gets at least as many sections right: section accuracy against classification cost per 1,000 packets, one point per pages per holistic call](../../images/benchmark-guidance/SC-04-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Holistic classification reads a long packet in overlapping windows of pages. On packets of 30 to 60 pages with Claude Haiku 5.5, a 10-page window is a good place to start. It got the most sections exactly right, 67% against 62% with the default 20 pages, and cost 60% less: $39 against $103 of classification per 1,000 packets. The saving comes from pricing: a 20-page window with page images usually goes over 100,000 tokens, and Claude Haiku 5.5 charges more above that. A 15-page window cost about the same as 10 but got no more right than 20; a 40-page window cost the most. Test it on your own packets.

**The economics:** 10 pages instead of 20 pages saves $64.4 of classification per 1,000 packets and gets 599 more sections right.

| Instead of 20 pages | Extra classification cost per 1,000 packets | Extra sections right per 1,000 packets | [Cost per section it saves](marginal-cost-of-error.md) |
|---|---|---|---|
| 10 pages | −$64.4 | +599 | more right, for less |
| 15 pages | −$67.7 | no measurable difference | cheaper, no measurable loss |
| 40 pages | +$20.1 | no measurable difference | no measurable gain |

*Long packets, N = 38–40 packets × 3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

<details>
<summary>Details</summary>

*Measured on release 0.6.15-develop-d1be93f+fixes-2034-2035-2036, November 2026, us-east-1. Source: 12 Test Studio runs (2 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-04.json`.*

Holistic classification with Claude Haiku 5.5 reading text and page images; a packet longer than the window is read in overlapping windows (2 pages of overlap) and merged. The 20-page runs are the same runs as SC-14's holistic long-packet runs. A window of more than 20 pages sends more than 20 page images, so Claude scales each to 2,000 pixels.

| | |
|---|---|
| **Changes** | Pages per holistic call (`window_pages`), on 30-to-60-page packets |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit long packets (40 packets, 30 to 60 pages)](datasets.md#docsplit-long-packets-40-packets-30-to-60-pages) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: Claude Haiku 5.5, holistic · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 38–40 packets × 3 runs; up to 2 packets failed in a run and are left out of it |

| Dataset | Pages per holistic call (`window_pages`), on 30-to-60-page packets | Section accuracy | Grouping score | Boundary accuracy | Label accuracy | Page accuracy | Classification tokens per page | Classification cost per 1,000 packets (USD) | Median seconds per packet | [Classification cost per wrong section prevented](marginal-cost-of-error.md) vs 15 pages |
|---|---|---|---|---|---|---|---|---|---|---|
| Long packets | **20 pages (default)** | 0.62 ± 0.03 | 0.88 ± 0 | 0.68 ± 0.02 | 0.92 ± 0.01 | 0.84 ± 0.01 | 6,732 ± 86 | $103 ± $0.69 | 595 ± 222 | no measurable gain |
| Long packets | 10 pages | **0.67 ± 0.03** | 0.9 ± 0.01 | 0.72 ± 0.03 | 0.94 ± 0.01 | **0.85 ± 0** | 7,371 ± 12 | $39.0 ± $0.15 | 693 ± 90 | under $0.01 |
| Long packets | 15 pages | 0.6 ± 0.01 | 0.87 ± 0.02 | 0.65 ± 0.03 | 0.92 ± 0.02 | 0.83 ± 0.01 | 6,770 ± 8.49 | **$35.7 ± $0.21** | 692 ± 111 | — |
| Long packets | 40 pages | 0.59 ± 0.03 | 0.87 ± 0.02 | 0.64 ± 0.02 | 0.92 ± 0.01 | 0.84 ± 0.01 | **5,388 ± 77** | $124 ± $1.56 | 547 ± 140 | no measurable gain |

Failed packets per run: 20 pages (default): 2, 0, 0.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Can a decision model classify the sections?

![With one-line descriptions, Decider 2B comes close to Haiku 5.5 at classifying sections: label accuracy against classification cost per 1,000 packets, one point per which model classifies each section, and the class descriptions](../../images/benchmark-guidance/SC-12-packet.svg)

*Bands show ±2 standard errors.*

**Answer:** Claude Haiku 5.5 is a good place to start for classifying sections. The Strands Decider 2B comes close if you give each class a one-line description of what its pages look like and send it the first two page images. It then gave the right class to 95% of correctly split sections, against 98% for Claude Haiku 5.5 with the same descriptions, at about the same cost ($31 per 1,000 packets). With the preset's longer class descriptions, written for an LLM, it got 77%. Both got about the same share of sections exactly right (77% and 78%), because most errors come from finding where each section starts, which Claude Haiku 5.5 does in every row. Test it on your own packets.

**The economics:** The Strands Decider 2B, reading the first two page images with one-line class descriptions, and Claude Haiku 5.5 show no measurable difference in cost or accuracy.

| Instead of LLM · Haiku 5.5 | Extra classification cost per 1,000 packets | Extra sections right per 1,000 packets | [Cost per section it saves](marginal-cost-of-error.md) |
|---|---|---|---|
| LLM · Haiku 5.5 · short desc. | no measurable difference | no measurable difference | — |
| Decider 2B · first page image | no measurable difference | −720 | — |
| Decider 2B · first page image · short desc. | no measurable difference | −239 | — |
| Decider 2B · first 2 page images · short desc. | no measurable difference | no measurable difference | — |
| Decider 2B · first page image and text · short desc. | no measurable difference | no measurable difference | — |

*DocSplit packets, N = 90–100 packets × 2–3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

<details>
<summary>Details</summary>

*Measured on release 0.6.15-develop-d1be93f+fixes-2034-2035-2036, November 2026, us-east-1. Source: 17 Test Studio runs (62 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-12.json`.*

Short descriptions replace each class's preset description with one line about what the page looks like (for example "a typed formal letter with a salutation (Dear ...) and a signature"); the splitter and the namer both read them. The decision model is the Strands Decider 2B (StrandsAgents/strands-decider-2B-qwen3.5-v1-2610, 8-bit, on AWS Lambda, images at 448 pixels).

| | |
|---|---|
| **Changes** | Which model classifies each section after Claude Haiku 5.5 detects the sections, and the class descriptions it reads |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (100 packets)](datasets.md#docsplit-poly-seq-100-packets) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: Claude Haiku 5.5 finds the sections (holistic), then Claude Haiku 5.5 classifies them · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 90–100 packets × 2–3 runs; up to 10 packets failed in a run and are left out of it |

| Dataset | Which model classifies each section after Claude Haiku 5.5 detects the sections, and the class descriptions it reads | Section accuracy | Label accuracy | Page accuracy | Boundary accuracy | Classification tokens per page | Total tokens per page | Classification cost per 1,000 packets (USD) | Median seconds per packet |
|---|---|---|---|---|---|---|---|---|---|
| DocSplit | **LLM · Claude Haiku 5.5 (baseline)** | 0.78 ± 0.02 | 0.96 ± 0.04 | 0.94 ± 0.04 | 0.8 ± 0.02 | 9,731 ± 17 | 24,447 ± 3.78 | $31.1 ± $1.92 | 210 ± 41 |
| DocSplit | LLM · Claude Haiku 5.5 · short descriptions | 0.78 ± 0.03 | 0.98 ± 0.01 | 0.96 ± 0 | 0.8 ± 0.03 | 9,699 ± 72 | 24,376 ± 116 | $30.9 ± $1.99 | 253 ± 100 |
| DocSplit | Decision model · first page image | 0.62 ± 0.01 | 0.77 ± 0.01 | 0.79 ± 0 | 0.79 ± 0.02 | 6,101 ± 7.98 | 20,830 ± 82 | $31.7 ± $0.41 | 172 ± 28 |
| DocSplit | Decision model · first page image · short descriptions | 0.74 ± 0.01 | 0.9 ± 0.01 | 0.84 ± 0.01 | 0.8 ± 0.01 | 6,103 ± 12 | 20,914 ± 8.02 | $30.7 ± $0.37 | **151 ± 8.88** |
| DocSplit | Decision model · first 2 page images · short descriptions | 0.77 ± 0.03 | 0.95 ± 0.02 | 0.87 ± 0.02 | 0.81 ± 0.03 | 6,096 ± 14 | 20,871 ± 26 | $31.7 ± $0.28 | 161 ± 1.91 |
| DocSplit | Decision model · first page image and text · short descriptions | 0.75 ± 0.02 | 0.94 ± 0.01 | 0.87 ± 0 | 0.8 ± 0.01 | 6,079 ± 9.55 | 20,800 ± 47 | $32.3 ± $0.28 | 194 ± 47 |

Failed packets per run: LLM · Claude Haiku 5.5: 8, 10; LLM · Claude Haiku 5.5 · short descriptions: 9, 0, 0; Decision model · first page image: 0, 3, 2; Decision model · first page image · short descriptions: 2, 1, 0; Decision model · first 2 page images · short descriptions: 0, 1, 0; Decision model · first page image and text · short descriptions: 8, 10, 8.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Which way of splitting and classifying should I use?

See [Which approach should I use?](#which-approach-should-i-use).

### Do long packets change which approach to use?

![With Claude Haiku 5.5, long packets cost every approach whole sections: section accuracy against classification cost per 1,000 packets, one point per approach and packet length](../../images/benchmark-guidance/SC-14-packet.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** On packets of 30 to 60 pages with Claude Haiku 5.5, holistic classification with a 10-page window is a good place to start ([SC-04](#how-long-a-packet-can-holistic-classification-handle)). It got 67% of sections exactly right, as many as splitting first and then classifying each section, for $39 of classification per 1,000 packets, against $125 for split-then-classify and $45 for page by page. With the default 20-page window, holistic and page by page both got 62%. Holistic grouped pages better (grouping score 0.88 against 0.82) but cost 2.3 times as much ($103), because its calls went over the 100,000 tokens above which Claude Haiku 5.5 charges more. Every approach did worse on long packets than on packets of 5 to 20 pages. Test it on your own packets.

<details>
<summary>Details</summary>

*Measured on release 0.6.15-develop-d1be93f+fixes-2034-2035-2036, November 2026, us-east-1. Source: 18 Test Studio runs (22 failed documents left out of the scores), listed in `benchmarks/results/guidance/SC-14.json`.*

The 5-to-20-page results are the same runs as SC-13. A holistic call reads at most the preset's page window at once, so a long packet is read in several windows.

| | |
|---|---|
| **Changes** | How the packet is split and classified, on packets of 5 to 20 pages and of 30 to 60 pages |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [DocSplit-Poly-Seq (100 packets)](datasets.md#docsplit-poly-seq-100-packets); [DocSplit long packets (40 packets, 30 to 60 pages)](datasets.md#docsplit-long-packets-40-packets-30-to-60-pages) |
| **Models** | OCR: Amazon Textract (Layout) · Classification: varies (the setting under test) · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N: DocSplit — 90–100 packets × 3 runs; up to 10 packets failed in a run and are left out of it; Long packets — 38–40 packets × 3 runs; up to 2 packets failed in a run and are left out of it |

| Dataset | How the packet is split and classified, on packets of 5 to 20 pages and of 30 to 60 pages | Section accuracy | Boundary accuracy | Label accuracy | Page accuracy | Over-splits | Merges | Classification tokens per page | Classification cost per 1,000 packets (USD) | Median seconds per packet | [Classification cost per wrong section prevented](marginal-cost-of-error.md) vs Page by page · Haiku 5.5 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| DocSplit | Page by page · Claude Haiku 5.5 | 0.7 ± 0 | 0.7 ± 0.01 | 0.98 ± 0 | 0.88 ± 0 | — | — | 8,842 ± 6.96 | **$14.5 ± $0.06** | 294 ± 108 | — |
| DocSplit | **Holistic · Claude Haiku 5.5 (baseline)** | 0.75 ± 0.04 | 0.8 ± 0.04 | 0.95 ± 0.01 | 0.9 ± 0.01 | — | — | **6,168 ± 7.23** | $25.6 ± $0.52 | **138 ± 8.1** | $0.05 |
| DocSplit | Split, then an LLM classifies each section · Claude Haiku 5.5 | 0.75 ± 0.01 | 0.77 ± 0.01 | 0.97 ± 0.02 | 0.92 ± 0.02 | — | — | 9,762 ± 64 | $31.5 ± $1.37 | 189 ± 49 | $0.08 |
| Long packets | Page by page · Claude Haiku 5.5 | 0.62 ± 0.01 | 0.65 ± 0.01 | **0.97 ± 0.01** | 0.83 ± 0.01 | — | — | 8,772 ± 1.84 | **$44.6 ± $0.15** | 880 ± 75 | — |
| Long packets | **Holistic · Claude Haiku 5.5 (baseline)** | 0.62 ± 0.03 | 0.68 ± 0.02 | 0.92 ± 0.01 | 0.84 ± 0.01 | — | — | **6,732 ± 86** | $103 ± $0.69 | **595 ± 222** | no measurable gain |
| Long packets | Split, then an LLM classifies each section · Claude Haiku 5.5 | **0.67 ± 0.01** | **0.71 ± 0.01** | 0.95 ± 0 | **0.87 ± 0** | — | — | 10,432 ± 2.52 | $125 ± $0.27 | 1,128 ± 216 | $0.14 |

Failed packets per run: DocSplit — Holistic · Claude Haiku 5.5: 1, 0, 1; DocSplit — Split, then an LLM classifies each section · Claude Haiku 5.5: 8, 10, 0; Long packets — Holistic · Claude Haiku 5.5: 2, 0, 0.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Classification cost counts only the classification step: its Amazon Bedrock calls and a decision model's AWS Lambda time. OCR, extraction, confidence scoring and other AWS charges are left out.*

</details>

### Planned

We haven't run these yet.

- Do few-shot examples help classification? (SC-06)
- How much do class descriptions matter? (SC-07)
- Does accuracy drop as I add document classes? (SC-08)
- How small can classification images be? (SC-09)
- If every file is one document, should I turn splitting off? (SC-10)

<!-- END GENERATED FAQ -->
