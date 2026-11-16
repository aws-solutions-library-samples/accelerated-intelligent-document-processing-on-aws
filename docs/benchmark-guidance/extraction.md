---
title: "Key Information Extraction Benchmarks"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Key Information Extraction Benchmarks

Extraction fills your database. For each section, it returns the values of the fields you
defined for the section's document class.
[Key Information Extraction](../idp-fundamentals/extraction.md) explains how it works.

## What decides extraction accuracy on your documents

This page measures how configuration choices change extraction accuracy. On your own
documents, two things matter more than any setting.

**1. Your definitions.** The extraction model returns the fields you define, in the structure
you define, and reads each field's description as its instruction. Good definitions:

- **Name the right fields.** Ask for the values your database needs, one value per field.
- **Get the relationships right.** Put values that belong together, such as the date, rate
  and description of a line item, in one list, so each row stays whole.
- **Say clearly what each field is.** Say where the value appears and what it is not: "the
  date the invoice was issued, not the due date".
- **Show what a value looks like.** Give the format you want, such as YYYY-MM-DD, or a number
  without a currency sign. For a hard document class, add a few-shot example.

See [Improving accuracy](../idp-fundamentals/extraction.md#improving-accuracy-once-your-pipeline-is-running).

**2. Your annotations.** Test Studio measures accuracy against the ground truth in your test
set, so a wrong annotation counts as an extraction error. In our experience, annotation
errors usually make up more than half of the errors Test Studio reports. Reliable numbers
need a test set with few annotation errors, and getting there usually takes a few rounds of
review: check each reported error, and fix the annotation when the extraction was right. See
[Measure with ground truth](../idp-fundamentals/index.md#4-measure-with-ground-truth) and
[Draft labeling](../test-studio.md#draft-labeling-unlabeled-documents--ground-truth).

**Data quality** matters too. The extraction model reads the OCR text and the page images.
Poor scans and handwriting give it less to read, and long tables need agentic extraction,
which costs more ([EX-01](#when-is-agentic-extraction-worth-it)).

**Settings** are what the benchmarks below measure:

| Setting | What it changes | Questions |
|---|---|---|
| *Extraction mode* (`extraction.mode`) | One request per section (*Simple*), or an agent that works through the section in shards (*Advanced*) | [EX-01](#when-is-agentic-extraction-worth-it), [EX-07](#does-deterministic-table-parsing-help-agentic-extraction) to [EX-09](#should-agentic-extraction-load-page-images-only-when-it-needs-them) |
| *Extraction model* (`extraction.model`) | The model that reads the section | [EX-02](#which-extraction-model-should-i-use) |
| Reasoning effort (`extraction.reasoning_effort`) | How long the model reasons before it answers | [EX-03](#does-more-reasoning-effort-improve-extraction) |
| Few-shot examples | Sample documents with their correct values, added to the prompt | [EX-05](#do-few-shot-examples-help-extraction) |
| Page images | Whether the model sees the pages as well as the OCR text | [EX-06](#does-sending-page-images-to-the-extraction-model-help) |
| Value coercion and schema validation | Type repair and schema checks on every record | [EX-10](#do-schema-validation-and-value-coercion-improve-the-output), [EX-11](#should-i-retry-failed-fields-with-a-stronger-model) |

## Questions

<!-- BEGIN GENERATED FAQ -->

### When is agentic extraction worth it?

**Answer:** When an invoice has more than about 100 line items, and then with Claude Haiku 5.5. On all 75 RealKIE invoices, single-pass scored as well as agentic or better up to 100 line items (0.69 against 0.57 with Claude Haiku 5.5 at 1 to 20 items), and with the 5.5 models agentic cost about the same there. Above 100 line items single-pass got almost no line items right (0.00 to 0.22) while agentic got 0.44 to 0.79. On those long invoices agentic with Claude Haiku 5.5 scored as well as with Claude Sonnet 5.5 for about a third of the cost ($316 against $1,073 per 1,000 invoices). Only 5 RealKIE invoices have more than 100 line items, so the long bands rest on few invoices; Test Studio could not score the longest (1,490 items), because its evaluation step timed out although extraction finished. [Details: EX-01](#ex-01-when-is-agentic-extraction-worth-it)

![Agentic extraction wins only above about 100 line items: list score per invoice against line items per invoice](../../images/benchmark-guidance/EX-01-by-size-realkie-all.svg)

*List score per invoice, against line items per invoice on a log scale: one point per invoice per extraction mode and model, and a line through each one's running median. A list score is Stickler's score for the list: matched rows over the larger of the true and returned row counts.* N = 70 documents × 3 runs. Datasets: [RealKIE, all](datasets.md#realkie-fcc-verified-all-75-invoices).

| Line items | Invoices | Single-pass · Claude Haiku 5.5: line-item score | Single-pass · Claude Haiku 5.5: weighted accuracy | Single-pass · Claude Haiku 5.5: cost per 1,000 | Agentic · Claude Haiku 5.5: line-item score | Agentic · Claude Haiku 5.5: weighted accuracy | Agentic · Claude Haiku 5.5: cost per 1,000 | Single-pass · Claude Sonnet 5.5: line-item score | Single-pass · Claude Sonnet 5.5: weighted accuracy | Single-pass · Claude Sonnet 5.5: cost per 1,000 | Agentic · Claude Sonnet 5.5: line-item score | Agentic · Claude Sonnet 5.5: weighted accuracy | Agentic · Claude Sonnet 5.5: cost per 1,000 | Agentic costs, Claude Haiku 5.5 | Agentic costs, Claude Sonnet 5.5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No line items | 4 | 0.08 | 0.70 | $22.0 | 0.00 | 0.68 | $43.2 | 0.50 | 0.79 | $29.5 | **1.00** | 0.84 | $33.9 | 2.0× | 1.2× |
| 1–20 | 41 | **0.69** | 0.84 | $73.2 | 0.57 | 0.83 | $69.6 | 0.65 | 0.84 | $130 | 0.64 | 0.86 | $123 | 1.0× | 0.9× |
| 21–100 | 25 | **0.53** | 0.80 | $104 | 0.49 | 0.79 | $105 | 0.51 | 0.78 | $218 | 0.52 | 0.79 | $198 | 1.0× | 0.9× |
| 101–300 | 2 | 0.02 | 0.75 | $218 | 0.44 | 0.72 | $238 | 0.22 | 0.81 | $544 | **0.49** | 0.72 | $576 | 1.1× | 1.1× |
| More than 300 | 2 | 0.00 | 0.89 | $203 | **0.79** | 0.98 | $316 | 0.10 | 0.90 | $581 | 0.77 | 0.98 | $1,073 | 1.6× | 1.8× |
| **All invoices** | 74 | 0.57 | 0.82 | $88.3 | 0.52 | 0.81 | $91.3 | 0.57 | 0.82 | $178 | **0.62** | 0.84 | $181 | 1.0× | 1.0× |

*Invoices grouped by the line items their ground truth holds. Each score and cost is a mean over the row's invoices; only invoices every run of every value scored are counted, each once. The line-item score is Stickler's score for the list: matched rows over the larger of the true and returned row counts. Cost is each invoice's own Amazon Bedrock and Amazon Textract metering, averaged over runs that share no prompt cache; Agentic costs is agentic's cost over single-pass's for the same model. The best line-item score in each row is bold.* Datasets: [RealKIE, all](datasets.md#realkie-fcc-verified-all-75-invoices). N = 74 invoices × 3 runs.

Test Studio could not score 1 invoice for Agentic · Claude Haiku 5.5: its evaluation step timed out, although extraction finished (1,490 line items, 3 of 3 runs). Left out of every column.

Test Studio could not score 1 invoice for Agentic · Claude Sonnet 5.5: its evaluation step timed out, although extraction finished (1,490 line items, 3 of 3 runs). Left out of every column.

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

### Which extraction model should I use?

**Answer:** Claude Haiku 5.5 for most forms and invoices; Claude Opus 5.5 when every field must be right. Claude Haiku 5.5 scored 0.98 on W-2 forms and 0.79 on RealKIE invoices for about half the cost of the default, Claude Sonnet 5 (0.96 and 0.80). Claude Opus 5.5, OpenAI GPT-6 Astra and xAI Grok 4.6 were nearly perfect on W-2s (0.999); Claude Sonnet 5.5 scored below Claude Sonnet 5 there (0.92). On RealKIE invoices no model scored measurably different from the default. The Amazon Nova models scored 0.06 to 0.16 lower on W-2s. xAI Grok 4.6 took about three times as long per document. [Details: EX-02](#ex-02-which-extraction-model-should-i-use)

![Top models are near perfect on W-2s; invoices show no winner: weighted accuracy against cost per 1,000 documents, one point per extraction model](../../images/benchmark-guidance/EX-02-fields.svg)

*Weighted accuracy against cost per 1,000 documents, one point per extraction model. Bands show ±2 standard errors.* N: RealKIE — 20 documents × 2–3 runs; Fake-W2 — 20 documents × 3 runs. Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). [Details: EX-02](#ex-02-which-extraction-model-should-i-use). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Does more reasoning effort improve extraction?

**Answer:** A little, for few tokens. High effort raised accuracy from 0.79 to 0.83 on RealKIE invoices and from 0.95 to 0.97 on W-2 forms, for 2 to 5% more total tokens per page and a few seconds per document. Both gains are near the edge of the noise on 20 documents, so confirm on your own test set before you change the default. [Details: EX-03](#ex-03-does-more-reasoning-effort-improve-extraction)

![Higher reasoning effort adds a little accuracy for a little cost: weighted accuracy against cost per 1,000 documents, one point per reasoning effort](../../images/benchmark-guidance/EX-03-fields.svg)

*Weighted accuracy against cost per 1,000 documents, one point per reasoning effort. Bands show ±2 standard errors.* N = 20 documents × 3 runs. Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). [Details: EX-03](#ex-03-does-more-reasoning-effort-improve-extraction). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### How much do field descriptions matter?

**Answer:** Not yet measured. [Details: EX-04](#ex-04-how-much-do-field-descriptions-matter)

### Do few-shot examples help extraction?

**Answer:** Not yet measured. [Details: EX-05](#ex-05-do-few-shot-examples-help-extraction)

### Does sending page images to the extraction model help?

**Answer:** Not yet measured. [Details: EX-06](#ex-06-does-sending-page-images-to-the-extraction-model-help)

### Does deterministic table parsing help agentic extraction?

**Answer:** Not yet measured. [Details: EX-07](#ex-07-does-deterministic-table-parsing-help-agentic-extraction)

### What shard size should agentic extraction use?

**Answer:** Not yet measured. [Details: EX-08](#ex-08-what-shard-size-should-agentic-extraction-use)

### Should agentic extraction load page images only when it needs them?

**Answer:** Not yet measured. [Details: EX-09](#ex-09-should-agentic-extraction-load-page-images-only-when-it-needs-them)

### Do schema validation and value coercion improve the output?

**Answer:** Not yet measured. [Details: EX-10](#ex-10-do-schema-validation-and-value-coercion-improve-the-output)

### Should I retry failed fields with a stronger model?

**Answer:** Not yet measured. [Details: EX-11](#ex-11-should-i-retry-failed-fields-with-a-stronger-model)

### Does structured output make single-pass extraction more accurate?

**Answer:** Either; structured output guarantees valid JSON at no cost. With Claude Sonnet 5.5, structured output scored the same as the schema in the prompt on RealKIE invoices (0.82 against 0.80, within the noise) and on W-2 forms (0.92 both), at the same cost. With Claude Sonnet 5 the forced tool lowered W-2 accuracy, and it is not available on Claude Sonnet 5.5. [Details: EX-12](#ex-12-does-structured-output-make-single-pass-extraction-more-accurate)

![Structured output scores the same as the schema in the prompt: weighted accuracy against cost per 1,000 documents, one point per schema enforcement](../../images/benchmark-guidance/EX-12-fields.svg)

*Weighted accuracy against cost per 1,000 documents, one point per schema enforcement. Bands show ±2 standard errors.* N = 20 documents × 3 runs. Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). [Details: EX-12](#ex-12-does-structured-output-make-single-pass-extraction-more-accurate). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### How should I extract sections that hold several records?

**Answer:** Not yet measured. [Details: EX-13](#ex-13-how-should-i-extract-sections-that-hold-several-records)

### How accurate is extraction across many document formats?

**Answer:** Not yet measured. [Details: EX-14](#ex-14-how-accurate-is-extraction-across-many-document-formats)

### How much do splitting errors cost extraction in a packet?

**Answer:** Not yet measured. [Details: EX-15](#ex-15-how-much-do-splitting-errors-cost-extraction-in-a-packet)

<!-- END GENERATED FAQ -->

## Metrics on this page

- *List recall:* the share of ground-truth list cells extracted correctly; a lost row
  lowers it.
- *List precision:* the share of extracted list cells that are correct; an invented or
  misaligned row lowers it.
- *Weighted accuracy:* Stickler's field-weighted score against ground truth.
- *Documents with every field right:* the share of documents with no wrong field.

Weighted accuracy counts a whole list as one field, so a lost row barely moves it. Read list recall for lists.

[Benchmark Datasets](datasets.md) describes each dataset.

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### EX-01: When is agentic extraction worth it?

**Answer:** When an invoice has more than about 100 line items, and then with Claude Haiku 5.5. On all 75 RealKIE invoices, single-pass scored as well as agentic or better up to 100 line items (0.69 against 0.57 with Claude Haiku 5.5 at 1 to 20 items), and with the 5.5 models agentic cost about the same there. Above 100 line items single-pass got almost no line items right (0.00 to 0.22) while agentic got 0.44 to 0.79. On those long invoices agentic with Claude Haiku 5.5 scored as well as with Claude Sonnet 5.5 for about a third of the cost ($316 against $1,073 per 1,000 invoices). Only 5 RealKIE invoices have more than 100 line items, so the long bands rest on few invoices; Test Studio could not score the longest (1,490 items), because its evaluation step timed out although extraction finished.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-173220, RealKIE-FCC-Verified-20261113-173644, RealKIE-FCC-Verified-20261113-173641, RealKIE-FCC-Verified-20261113-173219, RealKIE-FCC-Verified-20261113-173642, RealKIE-FCC-Verified-20261113-173643, RealKIE-FCC-Verified-20261116-145655, RealKIE-FCC-Verified-20261116-145659, RealKIE-FCC-Verified-20261116-145657, RealKIE-FCC-Verified-20261116-145656, RealKIE-FCC-Verified-20261116-145658, RealKIE-FCC-Verified-20261116-145707, Fake-W2-Tax-Forms-20261113-181721, Fake-W2-Tax-Forms-20261113-181722, Fake-W2-Tax-Forms-20261113-181723, Fake-W2-Tax-Forms-20261113-181901, Fake-W2-Tax-Forms-20261113-182002, Fake-W2-Tax-Forms-20261113-182011, realkie-fcc-short-20261113-201254, realkie-fcc-short-20261113-201252, realkie-fcc-short-20261113-201253, realkie-fcc-short-20261113-201713, realkie-fcc-short-20261113-201703, realkie-fcc-short-20261113-201714, realkie-fcc-long-20261113-201929, realkie-fcc-long-20261113-201928, realkie-fcc-long-20261113-202020, realkie-fcc-long-20261113-202336, realkie-fcc-long-20261113-202804, realkie-fcc-long-20261113-202644, realkie-fcc-long-20261116-145655, realkie-fcc-long-20261116-145656, realkie-fcc-long-20261116-145654, realkie-fcc-long-20261116-145707, realkie-fcc-long-20261116-145708, realkie-fcc-long-20261116-145709, synth-statements-400-20261113-231559, synth-statements-400-20261113-231602, synth-statements-400-20261113-231603, synth-statements-400-20261113-231600, synth-statements-400-20261113-231601, synth-statements-400-20261113-231604, synth-statements-800-20261113-231613, synth-statements-800-20261113-231615, synth-statements-800-20261113-231617, synth-statements-800-20261113-231614, synth-statements-800-20261113-231616, synth-statements-1600-20261113-231626, synth-statements-1600-20261113-231629, synth-statements-1600-20261113-231631, RealKIE-FCC-Verified-20261116-152541, RealKIE-FCC-Verified-20261116-152542, RealKIE-FCC-Verified-20261116-152544, RealKIE-FCC-Verified-20261116-152951, RealKIE-FCC-Verified-20261116-152953, RealKIE-FCC-Verified-20261116-152954, RealKIE-FCC-Verified-20261116-152540, RealKIE-FCC-Verified-20261116-152543, RealKIE-FCC-Verified-20261116-152545, RealKIE-FCC-Verified-20261116-152955, RealKIE-FCC-Verified-20261116-152950, RealKIE-FCC-Verified-20261116-152952 (12 failed documents left out of the scores)`.*

| | |
|---|---|
| **Changes** | Extraction mode |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified, all 75 invoices](datasets.md#realkie-fcc-verified-all-75-invoices) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 74–75 documents × 3 runs, 1 failed document per run left out |

![Averaged over whole datasets, the two modes score within the noise: weighted accuracy against cost per 1,000 documents, one point per extraction mode and model](../../images/benchmark-guidance/EX-01-fields.svg)

*Weighted accuracy against cost per 1,000 documents, one point per extraction mode and model. Bands show ±2 standard errors.* N = 74–75 documents × 3 runs, 1 failed document per run left out. Datasets: [RealKIE, all](datasets.md#realkie-fcc-verified-all-75-invoices). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

| Dataset | Extraction mode | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Single-pass · Claude Haiku 5.5 |
|---|---|---|---|---|---|---|---|
| RealKIE, all | Single-pass · Claude Haiku 5.5 | 0.82 ± 0.04 | 7,673 ± 11 | 26,263 ± 1,091 | $105 ± $7.41 | 52 ± 2.33 | — |
| RealKIE, all | Agentic · Claude Haiku 5.5 | 0.81 ± 0.04 | 38,874 ± 1,241 | 50,089 ± 1,853 | $105 ± $1.92 | 60 ± 1.67 | — |
| RealKIE, all | Single-pass · Claude Sonnet 5.5 | 0.82 ± 0.04 | 7,428 ± 20 | 33,542 ± 1,190 | $200 ± $2.55 | 54 ± 9.18 | no measurable gain |
| RealKIE, all | Agentic · Claude Sonnet 5.5 | 0.84 ± 0.03 | 24,766 ± 3,108 | 37,144 ± 4,911 | $227 ± $46.1 | 59 ± 9.6 | no measurable gain |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*


#### EX-02: Which extraction model should I use?

**Answer:** Claude Haiku 5.5 for most forms and invoices; Claude Opus 5.5 when every field must be right. Claude Haiku 5.5 scored 0.98 on W-2 forms and 0.79 on RealKIE invoices for about half the cost of the default, Claude Sonnet 5 (0.96 and 0.80). Claude Opus 5.5, OpenAI GPT-6 Astra and xAI Grok 4.6 were nearly perfect on W-2s (0.999); Claude Sonnet 5.5 scored below Claude Sonnet 5 there (0.92). On RealKIE invoices no model scored measurably different from the default. The Amazon Nova models scored 0.06 to 0.16 lower on W-2s. xAI Grok 4.6 took about three times as long per document.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-183955, RealKIE-FCC-Verified-20261113-183954, RealKIE-FCC-Verified-20261113-183956, RealKIE-FCC-Verified-20261113-184302, RealKIE-FCC-Verified-20261113-184254, RealKIE-FCC-Verified-20261113-184301, RealKIE-FCC-Verified-20261113-184609, RealKIE-FCC-Verified-20261113-184643, RealKIE-FCC-Verified-20261113-184918, RealKIE-FCC-Verified-20261113-190709, RealKIE-FCC-Verified-20261113-192401, RealKIE-FCC-Verified-20261113-192310, RealKIE-FCC-Verified-20261113-192341, RealKIE-FCC-Verified-20261113-192701, RealKIE-FCC-Verified-20261113-192729, RealKIE-FCC-Verified-20261113-192743, RealKIE-FCC-Verified-20261113-194402, RealKIE-FCC-Verified-20261113-194401, RealKIE-FCC-Verified-20261113-194404, RealKIE-FCC-Verified-20261113-195220, RealKIE-FCC-Verified-20261113-195217, RealKIE-FCC-Verified-20261113-195219, RealKIE-FCC-Verified-20261113-195617, RealKIE-FCC-Verified-20261113-195659, RealKIE-FCC-Verified-20261113-195740, RealKIE-FCC-Verified-20261113-195936, RealKIE-FCC-Verified-20261113-195957, RealKIE-FCC-Verified-20261113-200106, RealKIE-FCC-Verified-20261113-200235, RealKIE-FCC-Verified-20261113-200733, RealKIE-FCC-Verified-20261113-200820, RealKIE-FCC-Verified-20261113-200702, Fake-W2-Tax-Forms-20261113-201217, Fake-W2-Tax-Forms-20261113-201204, Fake-W2-Tax-Forms-20261113-201242, Fake-W2-Tax-Forms-20261113-201354, Fake-W2-Tax-Forms-20261113-201419, Fake-W2-Tax-Forms-20261113-201509, Fake-W2-Tax-Forms-20261113-201521, Fake-W2-Tax-Forms-20261113-201543, Fake-W2-Tax-Forms-20261113-201623, Fake-W2-Tax-Forms-20261113-201638, Fake-W2-Tax-Forms-20261113-201728, Fake-W2-Tax-Forms-20261113-201737, Fake-W2-Tax-Forms-20261113-201754, Fake-W2-Tax-Forms-20261113-201833, Fake-W2-Tax-Forms-20261113-201841, Fake-W2-Tax-Forms-20261113-201858, Fake-W2-Tax-Forms-20261113-201947, Fake-W2-Tax-Forms-20261113-202015, Fake-W2-Tax-Forms-20261113-202016, Fake-W2-Tax-Forms-20261113-202114, Fake-W2-Tax-Forms-20261113-202132, Fake-W2-Tax-Forms-20261113-202129, Fake-W2-Tax-Forms-20261113-202220, Fake-W2-Tax-Forms-20261113-202242, Fake-W2-Tax-Forms-20261113-202246, Fake-W2-Tax-Forms-20261113-202327, Fake-W2-Tax-Forms-20261113-202346, Fake-W2-Tax-Forms-20261113-202412, Fake-W2-Tax-Forms-20261113-202453, Fake-W2-Tax-Forms-20261113-202510, Fake-W2-Tax-Forms-20261113-202644, Fake-W2-Tax-Forms-20261113-202609, Fake-W2-Tax-Forms-20261113-202529`.*

| | |
|---|---|
| **Changes** | Extraction model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 2–3 runs; Fake-W2 — 20 documents × 3 runs |

| Dataset | Extraction model | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Amazon Nova Lite |
|---|---|---|---|---|---|---|---|
| RealKIE | Claude Sonnet 5 (default) | 0.8 ± 0.06 | 7,041 ± 13 | 20,556 ± 1,144 | $143 | 44 ± 3.91 | no measurable gain |
| RealKIE | Amazon Nova Lite | 0.74 ± 0.07 | 3,710 ± 68 | 25,004 ± 5,956 | $66.2 | 41 ± 1.28 | — |
| RealKIE | Amazon Nova Pro | 0.78 ± 0.07 | 3,468 ± 28 | 14,516 ± 443 | $75.2 | 41 ± 1.99 | no measurable gain |
| RealKIE | Amazon Nova 2 Lite | 0.78 ± 0.09 | 1,862 ± 2.65 | 17,517 ± 1,681 | $74.8 | 56 ± 16 | no measurable gain |
| RealKIE | Claude Haiku 5.5 | 0.79 ± 0.08 | 7,345 ± 109 | 22,012 ± 1,166 | $73.9 | 58 ± 28 | no measurable gain |
| RealKIE | Claude Sonnet 5.5 | 0.8 ± 0.07 | 6,991 ± 1.4 | 20,214 ± 751 | $134 | 63 ± 4.58 | no measurable gain |
| RealKIE | Claude Opus 5.5 | 0.82 ± 0.07 | 7,046 ± 49 | 22,110 ± 1,257 | $198 | 43 ± 12 | no measurable gain |
| RealKIE | OpenAI GPT-6 Astra | 0.84 ± 0.07 | 9,884 ± 3 | 25,815 ± 16 | $661 | 62 ± 24 | $5.53 per error prevented |
| RealKIE | xAI Grok 4.6 | 0.84 ± 0.07 | 3,713 ± 6.42 | 17,552 ± 823 | $97.1 | 87 ± 41 | $0.31 per error prevented |
| Fake-W2 | Claude Sonnet 5 (default) | 0.96 ± 0.02 | 6,877 ± 2.89 | 15,195 ± 2.28 | $33.0 | 29 ± 6.1 | $0.19 per error prevented |
| Fake-W2 | Amazon Nova Lite | 0.87 ± 0.03 | 6,682 ± 1.23 | 14,993 ± 6.52 | $15.8 | 23 ± 0.9 | — |
| Fake-W2 | Amazon Nova Pro | 0.9 ± 0.02 | 6,684 ± 1.91 | 15,005 ± 7.56 | $20.7 | 24 ± 0.24 | no measurable gain |
| Fake-W2 | Amazon Nova 2 Lite | 0.8 ± 0.04 | 4,643 ± 0.25 | 12,980 ± 2.25 | $17.7 | 19 ± 0.17 | — |
| Fake-W2 | Claude Haiku 5.5 | 0.98 ± 0.01 | 7,889 ± 31 | 16,210 ± 35 | $16.9 | 24 ± 0.65 | $0.01 per error prevented |
| Fake-W2 | Claude Sonnet 5.5 | 0.92 ± 0.03 | 6,743 ± 4.47 | 15,067 ± 3.14 | $31.6 | 21 ± 0.84 | $0.29 per error prevented |
| Fake-W2 | Claude Opus 5.5 | 1 ± 0 | 6,901 ± 11 | 15,214 ± 8.83 | $45.6 | 25 ± 1.92 | $0.23 per error prevented |
| Fake-W2 | OpenAI GPT-6 Astra | 1 ± 0 | 4,958 ± 4.59 | 13,271 ± 5.37 | $111 | 23 ± 0.38 | $0.74 per error prevented |
| Fake-W2 | xAI Grok 4.6 | 1 ± 0 | 6,027 ± 18 | 14,338 ± 20 | $29.3 | 99 ± 24 | $0.10 per error prevented |

| Dataset | Extraction model | List recall | List precision | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document |
|---|---|---|---|---|---|---|---|
| Statement, 400 rows | Claude Sonnet 5 (default) | — | — | — | — | — | — |
| Statement, 400 rows | Amazon Nova Lite | — | — | — | — | — | — |
| Statement, 400 rows | Amazon Nova Pro | — | — | — | — | — | — |
| Statement, 400 rows | Amazon Nova 2 Lite | — | — | — | — | — | — |
| Statement, 400 rows | Claude Haiku 5.5 | — | — | — | — | — | — |
| Statement, 400 rows | Claude Sonnet 5.5 | — | — | — | — | — | — |
| Statement, 400 rows | Claude Opus 5.5 | — | — | — | — | — | — |
| Statement, 400 rows | OpenAI GPT-6 Astra | — | — | — | — | — | — |
| Statement, 400 rows | xAI Grok 4.6 | — | — | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### EX-03: Does more reasoning effort improve extraction?

**Answer:** A little, for few tokens. High effort raised accuracy from 0.79 to 0.83 on RealKIE invoices and from 0.95 to 0.97 on W-2 forms, for 2 to 5% more total tokens per page and a few seconds per document. Both gains are near the edge of the noise on 20 documents, so confirm on your own test set before you change the default.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-182228, RealKIE-FCC-Verified-20261113-182227, RealKIE-FCC-Verified-20261113-182229, RealKIE-FCC-Verified-20261113-182526, RealKIE-FCC-Verified-20261113-182544, RealKIE-FCC-Verified-20261113-182545, RealKIE-FCC-Verified-20261113-182844, RealKIE-FCC-Verified-20261113-182910, RealKIE-FCC-Verified-20261113-182913, Fake-W2-Tax-Forms-20261113-183212, Fake-W2-Tax-Forms-20261113-183229, Fake-W2-Tax-Forms-20261113-183239, Fake-W2-Tax-Forms-20261113-183329, Fake-W2-Tax-Forms-20261113-183342, Fake-W2-Tax-Forms-20261113-183353, Fake-W2-Tax-Forms-20261113-183514, Fake-W2-Tax-Forms-20261113-183519, Fake-W2-Tax-Forms-20261113-183520`.*

| | |
|---|---|
| **Changes** | `extraction.reasoning_effort` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `extraction.reasoning_effort` | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Low (default) |
|---|---|---|---|---|---|---|---|
| RealKIE | Low (default) | 0.79 ± 0.06 | 7,044 ± 4.43 | 20,562 ± 625 | $136 | 39 ± 3.15 | — |
| RealKIE | Medium | 0.81 ± 0.07 | 7,135 ± 35 | 20,586 ± 702 | $141 | 37 ± 4.02 | no measurable gain |
| RealKIE | High | 0.83 ± 0.07 | 7,366 ± 39 | 20,883 ± 672 | $149 | 42 ± 6.27 | no measurable gain |
| Fake-W2 | Low (default) | 0.95 ± 0.02 | 6,879 ± 4.2 | 15,196 ± 3.7 | $33.3 | 25 ± 1.07 | — |
| Fake-W2 | Medium | 0.97 ± 0.01 | 7,128 ± 63 | 15,444 ± 64 | $36.8 | 27 ± 0.5 | no measurable gain |
| Fake-W2 | High | 0.97 ± 0.01 | 7,585 ± 80 | 15,900 ± 80 | $40.8 | 33 ± 1.73 | no measurable gain |

| Dataset | `extraction.reasoning_effort` | List recall | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document |
|---|---|---|---|---|---|---|
| Statement, 400 rows | Low (default) | — | — | — | — | — |
| Statement, 400 rows | Medium | — | — | — | — | — |
| Statement, 400 rows | High | — | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### EX-04: How much do field descriptions matter?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Field descriptions |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Runs** | 3 per document, per value |

| Dataset | Field descriptions | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| RealKIE | Field names only | — | — | — |
| RealKIE | Short descriptions | — | — | — |
| RealKIE | Descriptions with location, exclusions and format | — | — | — |
| Fake-W2 | Field names only | — | — | — |
| Fake-W2 | Short descriptions | — | — | — |
| Fake-W2 | Descriptions with location, exclusions and format | — | — | — |


#### EX-05: Do few-shot examples help extraction?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Few-shot examples |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Runs** | 3 per document, per value |

| Dataset | Few-shot examples | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| RealKIE | None (default) | — | — | — |
| RealKIE | One example per class | — | — | — |
| RealKIE | Three examples per class | — | — | — |
| Fake-W2 | None (default) | — | — | — |
| Fake-W2 | One example per class | — | — | — |
| Fake-W2 | Three examples per class | — | — | — |


#### EX-06: Does sending page images to the extraction model help?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Page images in the extraction prompt |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise) |
| **Runs** | 3 per document, per value |

| Dataset | Page images in the extraction prompt | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| RealKIE | Text and images (default) | — | — | — |
| RealKIE | Text only | — | — | — |
| Fake-W2 | Text and images (default) | — | — | — |
| Fake-W2 | Text only | — | — | — |
| ConfBench | Text and images (default) | — | — | — |
| ConfBench | Text only | — | — | — |


#### EX-07: Does deterministic table parsing help agentic extraction?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.agentic.table_parsing.enabled` |
| **Everything else** | Agentic (Advanced) extraction; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [Synthetic statement, 3,200 rows (66 pages)](datasets.md#synthetic-statement-3200-rows-66-pages) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.agentic.table_parsing.enabled` | List recall | List precision | Extraction tokens per page | Median seconds per document |
|---|---|---|---|---|---|
| Statement, 400 rows | On (default) | — | — | — | — |
| Statement, 400 rows | Off | — | — | — | — |
| Statement, 800 rows | On (default) | — | — | — | — |
| Statement, 800 rows | Off | — | — | — | — |
| Statement, 3,200 rows | On (default) | — | — | — | — |
| Statement, 3,200 rows | Off | — | — | — | — |


#### EX-08: What shard size should agentic extraction use?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Pages per shard (`max_pages_per_shard`) |
| **Everything else** | Agentic (Advanced) extraction; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [Synthetic statement, 3,200 rows (66 pages)](datasets.md#synthetic-statement-3200-rows-66-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Pages per shard (`max_pages_per_shard`) | List recall | List precision | Extraction tokens per page | Median seconds per document |
|---|---|---|---|---|---|
| Statement, 800 rows | 5 (default) | — | — | — | — |
| Statement, 800 rows | 3 | — | — | — | — |
| Statement, 800 rows | 10 | — | — | — | — |
| Statement, 800 rows | 25 | — | — | — | — |
| Statement, 3,200 rows | 5 (default) | — | — | — | — |
| Statement, 3,200 rows | 3 | — | — | — | — |
| Statement, 3,200 rows | 10 | — | — | — | — |
| Statement, 3,200 rows | 25 | — | — | — | — |


#### EX-09: Should agentic extraction load page images only when it needs them?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.agentic.table_parsing.lazy_images` |
| **Everything else** | Agentic (Advanced) extraction; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.agentic.table_parsing.lazy_images` | List recall | List precision | Extraction tokens per page |
|---|---|---|---|---|
| Statement, 400 rows | On (default) | — | — | — |
| Statement, 400 rows | Off | — | — | — |
| Statement, 800 rows | On (default) | — | — | — |
| Statement, 800 rows | Off | — | — | — |

| Dataset | `extraction.agentic.table_parsing.lazy_images` | Weighted accuracy | Extraction tokens per page |
|---|---|---|---|
| RealKIE | On (default) | — | — |
| RealKIE | Off | — | — |


#### EX-10: Do schema validation and value coercion improve the output?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.validation` and `extraction.coercion` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.validation` and `extraction.coercion` | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| Fake-W2 | Both on (default) | — | — | — |
| Fake-W2 | Coercion off | — | — | — |
| Fake-W2 | Validation off | — | — | — |
| RealKIE | Both on (default) | — | — | — |
| RealKIE | Coercion off | — | — | — |
| RealKIE | Validation off | — | — | — |


#### EX-11: Should I retry failed fields with a stronger model?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.validation.fail_action` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.validation.fail_action` | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| Fake-W2 | warn (default) | — | — | — |
| Fake-W2 | escalate | — | — | — |
| Fake-W2 | reject | — | — | — |
| RealKIE | warn (default) | — | — | — |
| RealKIE | escalate | — | — | — |
| RealKIE | reject | — | — | — |


#### EX-12: Does structured output make single-pass extraction more accurate?

**Answer:** Either; structured output guarantees valid JSON at no cost. With Claude Sonnet 5.5, structured output scored the same as the schema in the prompt on RealKIE invoices (0.82 against 0.80, within the noise) and on W-2 forms (0.92 both), at the same cost. With Claude Sonnet 5 the forced tool lowered W-2 accuracy, and it is not available on Claude Sonnet 5.5.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-211217, RealKIE-FCC-Verified-20261113-211218, RealKIE-FCC-Verified-20261113-211216, RealKIE-FCC-Verified-20261113-211535, RealKIE-FCC-Verified-20261113-211526, RealKIE-FCC-Verified-20261113-211543, RealKIE-FCC-Verified-20261113-211823, RealKIE-FCC-Verified-20261113-211839, RealKIE-FCC-Verified-20261113-211824, RealKIE-FCC-Verified-20261116-145708, RealKIE-FCC-Verified-20261116-145709, RealKIE-FCC-Verified-20261116-145710, RealKIE-FCC-Verified-20261116-145719, RealKIE-FCC-Verified-20261116-145720, RealKIE-FCC-Verified-20261116-145721, Fake-W2-Tax-Forms-20261113-212143, Fake-W2-Tax-Forms-20261113-212145, Fake-W2-Tax-Forms-20261113-212202, Fake-W2-Tax-Forms-20261113-212313, Fake-W2-Tax-Forms-20261113-212311, Fake-W2-Tax-Forms-20261113-212326, Fake-W2-Tax-Forms-20261113-212439, Fake-W2-Tax-Forms-20261113-212441, Fake-W2-Tax-Forms-20261113-212442, Fake-W2-Tax-Forms-20261116-145709, Fake-W2-Tax-Forms-20261116-145719, Fake-W2-Tax-Forms-20261116-145721, Fake-W2-Tax-Forms-20261116-145720, Fake-W2-Tax-Forms-20261116-145722, Fake-W2-Tax-Forms-20261116-145723, realkie-fcc-long-20261113-213052, realkie-fcc-long-20261113-213055, realkie-fcc-long-20261113-213057, realkie-fcc-long-20261113-213053, realkie-fcc-long-20261113-213054, realkie-fcc-long-20261113-213056, realkie-fcc-long-20261113-213058, realkie-fcc-long-20261113-213059, realkie-fcc-long-20261113-213104`.*

| | |
|---|---|
| **Changes** | Schema enforcement (`extraction.schema_enforcement.mode`) |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Schema enforcement (`extraction.schema_enforcement.mode`) | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs cheapest |
|---|---|---|---|---|---|---|---|---|
| RealKIE | Off: schema in the prompt (default) | 0.81 ± 0.06 | 0.05 ± 0 | 7,055 ± 6.46 | 21,131 ± 246 | $145 ± $1.25 | 41 ± 6.93 | no measurable gain |
| RealKIE | Off: schema in the prompt · Claude Sonnet 5.5 | 0.8 ± 0.07 | 0.02 ± 0.03 | 379,267 ± 372,270 | 1,120,275 ± 1,099,609 | $133 ± $0.78 | 47 ± 4.33 | — |
| RealKIE | Structured output · Claude Sonnet 5.5 | 0.82 ± 0.07 | 0.02 ± 0.03 | 7,243 ± 12 | 20,662 ± 425 | $134 ± $0.32 | 44 ± 2.61 | no measurable gain |
| Fake-W2 | Off: schema in the prompt (default) | 0.96 ± 0.01 | 0.15 ± 0.06 | 6,901 ± 11 | 15,276 ± 40 | $34.0 ± $0.93 | 27 ± 1.78 | $0.08 per error prevented |
| Fake-W2 | Off: schema in the prompt · Claude Sonnet 5.5 | 0.92 ± 0.02 | 0.3 ± 0 | 49,568 ± 85,629 | 110,899 ± 191,505 | $30.6 ± $0.30 | 22 ± 0.61 | — |
| Fake-W2 | Structured output · Claude Sonnet 5.5 | 0.92 ± 0.02 | 0.32 ± 0.03 | 6,760 ± 7.86 | 15,137 ± 23 | $30.5 ± $0.31 | 22 ± 1.04 | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*


#### EX-13: How should I extract sections that hold several records?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Multi-instance sections |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic file holding two statements](datasets.md#synthetic-file-holding-two-statements); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Runs** | 3 per document, per value |

| Dataset | Multi-instance sections | Weighted accuracy | Extraction tokens per page |
|---|---|---|---|
| Fake-W2 | Off (default) | — | — |
| Fake-W2 | On | — | — |


#### EX-14: How accurate is extraction across many document formats?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Document format |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [OmniAI-OCR-Benchmark, all 9 formats (293 documents)](datasets.md#omniai-ocr-benchmark-all-9-formats-293-documents) |
| **Runs** | 3 per document, per value |

| Dataset | Document format | Weighted accuracy | Documents with every field right | Extraction tokens per page |
|---|---|---|---|---|
| OmniAI | Each of the 9 OmniAI formats | — | — | — |


#### EX-15: How much do splitting errors cost extraction in a packet?

**Answer:** Not yet measured.

Needs a dataset we haven't built yet: Packets with section and field ground truth (to be built).

| | |
|---|---|
| **Changes** | Granularity and strategy |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Packets with section and field ground truth (to be built)](datasets.md#packets-with-section-and-field-ground-truth-to-be-built) |
| **Runs** | 3 per document, per value |

| Dataset | Granularity and strategy | Boundary accuracy | Weighted accuracy | Total tokens per page |
|---|---|---|---|---|
| Packets with fields | Page by page, one call (default) | — | — | — |
| Packets with fields | Holistic, one call | — | — | — |
| Packets with fields | Detect sections first, then classify | — | — | — |

<!-- END GENERATED ABLATIONS -->
