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
| Models | Each step's model sets its price and how many tokens it writes | [TU-05](#planned) |
| Prompt caching | Reads a repeated prompt prefix at a reduced rate | [TU-02](#planned) |
| Page images | Adds image tokens to each step that reads them | [TU-03](#planned) |
| All of the above | The cheapest configuration that keeps accuracy | [TU-04](#what-is-the-cheapest-configuration-that-keeps-accuracy) |

Some benchmarks here reuse runs from other pages.

## Questions

<details>
<summary>What the metrics mean</summary>

- *Tokens per page:* input, output and prompt cache reads and writes, per step.
- *Share of input from cache:* Amazon Bedrock bills cache reads at a reduced rate.
- *Accuracy:* where a benchmark trades tokens against it.

[Benchmark Datasets](datasets.md) describes each dataset.

</details>

<!-- BEGIN GENERATED FAQ -->

### What is the cheapest configuration that keeps accuracy?

![Claude Haiku 5.5 and Layout only: same accuracy, a sixth the cost: weighted accuracy against cost per 1,000 documents, one point per configuration](../../images/benchmark-guidance/TU-04-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Claude Haiku 5.5 extraction with Textract Layout only is a good place to start. With confidence off it cost $21 per 1,000 invoices, against $135 for the default, at the same weighted accuracy. Scoring confidence with Claude Haiku 5.5 raises that to $28, and it got every field right on more invoices (10% against 3%). Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on each dataset's preset, not the baseline setup.*

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 18 Test Studio runs, listed in `benchmarks/results/guidance/TU-04.json`.*

On W-2 forms it cost $5 per 1,000 against $32 for the default, and scored higher (0.998 against 0.966). Scoring with Claude Haiku 5.5 added $7 per 1,000 invoices and gave confidence that separates right from wrong values (AUROC 0.84, against 0.51 for the default).

| | |
|---|---|
| **Changes** | Configuration |
| **Everything else** | As each dataset's [preset](index.md#baseline-setup) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: varies (the setting under test) · Extraction: varies (the setting under test) · Scoring: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Configuration | Weighted accuracy | Documents with every field right | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Haiku 5.5, no scoring |
|---|---|---|---|---|---|---|---|
| RealKIE | Preset default | 0.82 ± 0.07 | 0.03 ± 0.03 | 20,264 ± 1,113 | $135 ± $8.69 | 38 ± 3.39 | no measurable gain |
| RealKIE | Claude Haiku 5.5, Layout only, confidence off | 0.83 ± 0.07 | 0.03 ± 0.03 | **7,229 ± 72** | **$21.0 ± $0.16** | **23 ± 0.58** | — |
| RealKIE | **Claude Haiku 5.5, Layout only, Claude Haiku 5.5 scoring (recommended)** | 0.83 ± 0.07 | **0.1 ± 0** | 20,257 ± 1,390 | $28.2 ± $0.70 | 39 ± 0.51 | no measurable gain |
| Fake-W2 | Preset default | 0.97 ± 0.01 | 0.25 ± 0.15 | 15,267 ± 7.4 | $32.4 ± $0.10 | 25 ± 0.31 | no measurable gain |
| Fake-W2 | Claude Haiku 5.5, Layout only, confidence off | 1 ± 0 | 0.8 ± 0.06 | **7,406 ± 30** | **$5.34 ± $0.02** | **17 ± 0.36** | — |
| Fake-W2 | **Claude Haiku 5.5, Layout only, Claude Haiku 5.5 scoring (recommended)** | 1 ± 0 | 0.8 ± 0 | 18,289 ± 305 | $7.80 ± $0.23 | 30 ± 0.53 | no measurable gain |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the reference configuration.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Planned

We haven't run these yet.

- How much does prompt caching save in extraction? (TU-02)
- What do page images cost in tokens, step by step? (TU-03)
- How many tokens does each extraction model use for the same document? (TU-05)

<!-- END GENERATED FAQ -->
