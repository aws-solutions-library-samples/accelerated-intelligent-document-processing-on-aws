---
title: "Token Use and Cost"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Token Use and Cost

Every step after digitization sends tokens to a model, and tokens are what you pay for. We
report tokens, not prices, because tokens stay the same when prices change. To estimate cost,
multiply each step's tokens by the current price of its model.

## How to use fewer tokens on your documents

**Your definitions.** The accelerator sends your field descriptions and few-shot examples
with every request, so they add tokens to every section. Prompt caching reads a repeated
prompt prefix at a reduced rate.

**Data quality.** Page images cost tokens in every step that reads them, and long files cost
more in every step.

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| Models | Each step's model sets its price and how many tokens it writes | [TU-05](#how-many-tokens-does-each-extraction-model-use-for-the-same-document) |
| Prompt caching | Reads a repeated prompt prefix at a reduced rate | [TU-02](#how-much-does-prompt-caching-save-in-extraction) |
| Page images | Adds image tokens to each step that reads them | [TU-03](#what-do-page-images-cost-in-tokens-step-by-step) |
| All of the above | The cheapest configuration that keeps accuracy | [TU-04](#what-is-the-cheapest-configuration-that-keeps-accuracy) |

Some benchmarks here reuse runs from other pages.

## Questions

<!-- BEGIN GENERATED FAQ -->

### Where do the tokens go?

**Answer:** Not yet measured. [Details: TU-01](#tu-01-where-do-the-tokens-go)

### How much does prompt caching save in extraction?

**Answer:** Not yet measured. [Details: TU-02](#tu-02-how-much-does-prompt-caching-save-in-extraction)

### What do page images cost in tokens, step by step?

**Answer:** Not yet measured. [Details: TU-03](#tu-03-what-do-page-images-cost-in-tokens-step-by-step)

### What is the cheapest configuration that keeps accuracy?

**Answer:** Claude Haiku 5.5 for extraction, Amazon Textract with Layout only, and confidence off or scored by Claude Haiku 5.5. On RealKIE invoices it cost $21 per 1,000 documents against $135 for the default, and scored the same (0.83 against 0.82); on W-2 forms it cost $5 against $32 and scored higher (0.998 against 0.966). Scoring with Claude Haiku 5.5 added $7 per 1,000 invoices and gave confidence that separates right from wrong (AUROC 0.84, against 0.51 for the default). [Details: TU-04](#tu-04-what-is-the-cheapest-configuration-that-keeps-accuracy)

![Weighted accuracy against cost per 1,000 documents (USD) for each Configuration](../../images/benchmark-guidance/TU-04-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). Models: OCR: varies (the setting under test) · Extraction: varies (the setting under test) · Confidence: varies (the setting under test). N = 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### How many tokens does each extraction model use for the same document?

**Answer:** Not yet measured. [Details: TU-05](#tu-05-how-many-tokens-does-each-extraction-model-use-for-the-same-document)

<!-- END GENERATED FAQ -->

## Metrics on this page

- *Tokens per page:* input, output and prompt cache reads and writes, per step.
- *Share of input from cache:* Amazon Bedrock bills cache reads at a reduced rate.
- *Accuracy:* where a benchmark trades tokens against it.

[Benchmark Datasets](datasets.md) describes each dataset.

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### TU-01: Where do the tokens go?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Nothing: the preset, measured step by step |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Nothing: the preset, measured step by step | OCR tokens per page | Classification tokens per page | Extraction tokens per page | Scoring tokens per page | Total tokens per page | Share of input served from cache |
|---|---|---|---|---|---|---|---|
| RealKIE | Preset | — | — | — | — | — | — |

| Dataset | Nothing: the preset, measured step by step | OCR tokens per page | Classification tokens per page | Total tokens per page | Share of input served from cache |
|---|---|---|---|---|---|
| DocSplit | Preset | — | — | — | — |

| Dataset | Nothing: the preset, measured step by step | OCR tokens per page | Classification tokens per page | Extraction tokens per page | Scoring tokens per page | Total tokens per page | Share of input served from cache |
|---|---|---|---|---|---|---|---|
| Statement, 400 rows | Preset | — | — | — | — | — | — |


#### TU-02: How much does prompt caching save in extraction?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.prompt_cache` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.prompt_cache` | Total tokens per page | Share of input served from cache | Median seconds per document |
|---|---|---|---|---|
| RealKIE | auto (default) | — | — | — |
| RealKIE | off | — | — | — |

| Dataset | `extraction.prompt_cache` | Total tokens per page | Share of input served from cache | Median seconds per document |
|---|---|---|---|---|
| DocSplit | auto (default) | — | — | — |
| DocSplit | off | — | — | — |

| Dataset | `extraction.prompt_cache` | Total tokens per page | Share of input served from cache | Median seconds per document |
|---|---|---|---|---|
| Statement, 400 rows | auto (default) | — | — | — |
| Statement, 400 rows | off | — | — | — |


#### TU-03: What do page images cost in tokens, step by step?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Page images |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Page images | Classification tokens per page | Extraction tokens per page | Scoring tokens per page | Total tokens per page |
|---|---|---|---|---|---|
| RealKIE | In every step (default) | — | — | — | — |
| RealKIE | Off in classification | — | — | — | — |
| RealKIE | Off in extraction | — | — | — | — |
| RealKIE | Off in scoring | — | — | — | — |

| Dataset | Page images | Classification tokens per page | Total tokens per page |
|---|---|---|---|
| DocSplit | In every step (default) | — | — |
| DocSplit | Off in classification | — | — |
| DocSplit | Off in extraction | — | — |
| DocSplit | Off in scoring | — | — |


#### TU-04: What is the cheapest configuration that keeps accuracy?

**Answer:** Claude Haiku 5.5 for extraction, Amazon Textract with Layout only, and confidence off or scored by Claude Haiku 5.5. On RealKIE invoices it cost $21 per 1,000 documents against $135 for the default, and scored the same (0.83 against 0.82); on W-2 forms it cost $5 against $32 and scored higher (0.998 against 0.966). Scoring with Claude Haiku 5.5 added $7 per 1,000 invoices and gave confidence that separates right from wrong (AUROC 0.84, against 0.51 for the default).

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-213122, RealKIE-FCC-Verified-20261113-213123, RealKIE-FCC-Verified-20261113-213124, RealKIE-FCC-Verified-20261113-213133, RealKIE-FCC-Verified-20261113-213135, RealKIE-FCC-Verified-20261113-213136, RealKIE-FCC-Verified-20261113-213134, RealKIE-FCC-Verified-20261113-213137, RealKIE-FCC-Verified-20261113-213138, Fake-W2-Tax-Forms-20261113-213138, Fake-W2-Tax-Forms-20261113-213139, Fake-W2-Tax-Forms-20261113-213145, Fake-W2-Tax-Forms-20261113-213146, Fake-W2-Tax-Forms-20261113-213148, Fake-W2-Tax-Forms-20261113-213149, Fake-W2-Tax-Forms-20261113-213147, Fake-W2-Tax-Forms-20261113-213150, Fake-W2-Tax-Forms-20261113-213151`.*

| | |
|---|---|
| **Changes** | Configuration |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: varies (the setting under test) · Extraction: varies (the setting under test) · Confidence: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Configuration | Weighted accuracy | Documents with every field right | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Claude Haiku 5.5, Layout only, confidence off |
|---|---|---|---|---|---|---|---|
| RealKIE | Default | 0.82 ± 0.07 | 0.03 ± 0.03 | 1,600,251 ± 88,489 | $135 ± $8.69 | 38 ± 3.39 | — |
| RealKIE | Claude Haiku 5.5, Layout only, confidence off | 0.83 ± 0.07 | 0.03 ± 0.03 | 391,701 ± 384,461 | $21.0 ± $0.16 | 23 ± 0.58 | — |
| RealKIE | Claude Haiku 5.5, Layout only, Claude Haiku 5.5 scoring | 0.83 ± 0.07 | 0.1 ± 0 | 1,640,857 ± 112,582 | $28.2 ± $0.70 | 39 ± 0.51 | no measurable gain |
| Fake-W2 | Default | 0.97 ± 0.01 | 0.25 ± 0.15 | 203,563 ± 203,563 | $32.4 ± $0.10 | 25 ± 0.31 | — |
| Fake-W2 | Claude Haiku 5.5, Layout only, confidence off | 1 ± 0 | 0.8 ± 0.06 | 101,095 ± 93,670 | $5.34 ± $0.02 | 17 ± 0.36 | — |
| Fake-W2 | Claude Haiku 5.5, Layout only, Claude Haiku 5.5 scoring | 1 ± 0 | 0.8 ± 0 | 248,024 ± 229,433 | $7.80 ± $0.23 | 30 ± 0.53 | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*


#### TU-05: How many tokens does each extraction model use for the same document?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Extraction model |
| **Everything else** | Computed from the EX-02 runs; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | None: computed from the EX-02 runs |

| Dataset | Extraction model | Extraction tokens per page | Total tokens per page |
|---|---|---|---|
| RealKIE | The models in EX-02 | — | — |

| Dataset | Extraction model | Extraction tokens per page | Total tokens per page |
|---|---|---|---|
| Statement, 400 rows | The models in EX-02 | — | — |

<!-- END GENERATED ABLATIONS -->
