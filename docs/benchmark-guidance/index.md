---
title: "How We Benchmark Configurations"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# How We Benchmark Configurations

- When is agentic extraction worth it?
- Does holistic classification beat page-by-page classification?
- Does OCR make a difference?
- Which extraction model should I use?
- Can I route review by confidence?
- What is the cheapest configuration that keeps accuracy?

We answer these questions with benchmark datasets, one page per problem set:
[Digitization](digitization.md), [Splitting and Section Classification](splitting-and-classification.md),
[Key Information Extraction](extraction.md), [Confidence Assessment](confidence-assessment.md),
[Rule Validation](rule-validation.md) and [Token Use and Cost](token-use.md).

## What we recommend

Start from these choices, then confirm them on a test set of your own documents.

| Decision | Choose | Why |
|---|---|---|
| Splitting packets into documents | Holistic classification with Claude Haiku 5.5; Claude Sonnet 5.5 for the most accuracy | [0.80 of sections exactly right, against 0.52 for the default, at the same cost](splitting-and-classification.md#does-holistic-classification-beat-page-by-page-classification) |
| Single-pass or agentic extraction | Single-pass, unless a document's lists run past about 100 rows; then agentic | [Above 300 line items agentic scored 0.71 against 0 for single-pass, and cost about 6× as much; below 100, single-pass scored higher and agentic cost 1.1× to 2.8× as much](extraction.md#when-is-agentic-extraction-worth-it) |
| Scoring model, if people review by confidence | Claude Haiku 5.5 | [Catches every error on W-2s when reviewing 30% of values, at the default's cost; the default does worse than random](confidence-assessment.md#which-scoring-model-should-i-use) |
| Confidence, if nobody reviews by it | Off | [Scoring is 55% to 65% of tokens and changes no extracted value](confidence-assessment.md#separate-scoring-pass-or-integrated-into-extraction) |
| Schema enforcement | Structured output | [As accurate or slightly better, and a little cheaper; the forced tool lowered accuracy](extraction.md#does-structured-output-make-single-pass-extraction-more-accurate) |
| OCR | Amazon Textract; Layout only for forms and invoices, add Tables for long tables | [No feature combination changed accuracy; agentic extraction reads the tables Textract builds](digitization.md#which-textract-features-do-i-need) |
| Lowest cost | Claude Haiku 5.5 extraction, Textract Layout only, confidence off or scored by Claude Haiku 5.5 | [$21 against $135 per 1,000 invoices at the same accuracy](token-use.md#what-is-the-cheapest-configuration-that-keeps-accuracy) |

## How we run a benchmark

Each dataset has a configuration preset built for it. A benchmark starts from that preset,
changes one setting, and runs the same documents with each value of the setting. Each
benchmark has an ID, such as `EX-01`, and a results table. A table reads *Not yet
measured* until we run it.

[Benchmark Datasets](datasets.md) lists each dataset with its preset and its Test Studio
test set.

Defaults differ from preset to preset. To configure your own stack, start from the
[preset](../../config_library/unified/README.md) closest to your documents.

## Why you can trust the results

- **Known answers.** Every dataset has ground truth, generated exactly for synthetic
  documents or checked by people for public ones. See
  [Benchmark Datasets](../idp-fundamentals/benchmark-datasets.md).
- **One change at a time,** on one stack, so the setting explains the difference.
- **Same documents, repeated.** We run every value on the same documents at least three
  times and report the spread.
- **Real differences only.** We name a winner only when the difference exceeds twice its
  standard error.
- **Pinned to a release.** Each table names the release, month and region, and the results
  file behind it.
- **Tokens, not prices.** Tokens stay the same when prices change. Multiply by current
  prices to estimate cost.
- **Repeatable in your account.** Most datasets come with every stack as Test Studio test
  sets.

## What the results don't cover

- **Your documents.** Confirm a choice on your own test set; see
  [Measure with ground truth](../idp-fundamentals/index.md#4-measure-with-ground-truth).
- **BDA mode.** These benchmarks cover Pipeline mode.
- **Datasets we haven't built yet:** long packets, same-class packets, packets with field
  ground truth, handwritten forms, and rule verdicts. Benchmarks that need one say so.

## Questions for every problem set

<!-- BEGIN GENERATED FAQ -->

### How much do results vary between identical runs?

**Answer:** Not yet measured. [Details: TR-01](#tr-01-how-much-do-results-vary-between-identical-runs)

### How long does a document take, and how many documents an hour?

**Answer:** Not yet measured. [Details: TR-02](#tr-02-how-long-does-a-document-take-and-how-many-documents-an-hour)

<!-- END GENERATED FAQ -->

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### TR-01: How much do results vary between identical runs?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Nothing: the preset, run 10 times |
| **Everything else** | As each dataset's [preset](#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 10 per document, per value |

| Dataset | Nothing: the preset, run 10 times | Weighted accuracy | Total tokens per page |
|---|---|---|---|
| RealKIE | Preset | — | — |

| Dataset | Nothing: the preset, run 10 times | Boundary accuracy | Total tokens per page |
|---|---|---|---|
| DocSplit | Preset | — | — |

| Dataset | Nothing: the preset, run 10 times | List recall | Total tokens per page |
|---|---|---|---|
| Statement, 400 rows | Preset | — | — |


#### TR-02: How long does a document take, and how many documents an hour?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Documents submitted at once |
| **Everything else** | As each dataset's [preset](#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Documents submitted at once | Median seconds per document |
|---|---|---|
| RealKIE | 1 | — |
| RealKIE | 10 | — |
| RealKIE | 50 | — |

| Dataset | Documents submitted at once | Median seconds per document |
|---|---|---|
| Statement, 400 rows | 1 | — |
| Statement, 400 rows | 10 | — |
| Statement, 400 rows | 50 | — |

<!-- END GENERATED ABLATIONS -->
