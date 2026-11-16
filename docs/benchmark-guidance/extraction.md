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
Poor scans and handwriting give it less to read, and long tables need a mode built for them.

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

**Answer:** When a document's lists run past about 150 rows. On RealKIE invoices with 182 or more line items, single-pass got almost none right (line-item score 0.00 to 0.50, and 0 on the invoices with 309, 392 and 1,490 items), while agentic extraction got 0.32 to 0.90. On synthetic bank statements, agentic extraction returned every one of 1,600 rows exactly right on all 15 runs of the 1,600-row statements, where single-pass returned 3%; at 800 rows it was 1.00 against 0.82, and at 400 both were perfect. Below about 150 rows single-pass did better and cost less: across all 20 RealKIE invoices its line-item score was 0.61 against 0.45. Agentic extraction is slow on very long lists: about 39 minutes for a 1,600-row statement, past Test Studio's time limit, so Test Studio reports those runs as failed although every document finished. [Details: EX-01](#ex-01-when-is-agentic-extraction-worth-it)

![List score against rows in the list (ground truth) for each Extraction mode](../../images/benchmark-guidance/EX-01-by-size-synth-100.svg)

*List score per document, against rows in the list (ground truth). One point per document per extraction mode, on a log scale; the line is that value's running median over log-spaced bins of the x axis. A list score is Stickler's score for the list: matched rows over the larger of the true and returned row counts.* Datasets: [Statement, 400 rows](datasets.md#synthetic-statement-400-rows-9-pages), [Statement, 800 rows](datasets.md#synthetic-statement-800-rows-17-pages), [Statement, 1,600 rows](datasets.md#synthetic-statement-1600-rows-33-pages). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Confidence: Off. Extraction by dataset: Statement, 400 rows — varies (the setting under test); Statement, 800 rows — varies (the setting under test); Statement, 1,600 rows — Claude Sonnet 5, single-pass. N = 15 documents × 2–3 runs.

![List score against rows in the list (ground truth) for each Extraction mode](../../images/benchmark-guidance/EX-01-by-size-realkie.svg)

*List score per document, against rows in the list (ground truth). One point per document per extraction mode, on a log scale; the line is that value's running median over log-spaced bins of the x axis. A list score is Stickler's score for the list: matched rows over the larger of the true and returned row counts.* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [RealKIE, short](datasets.md#realkie-fcc-verified-short-invoices-24-invoices-1-or-2-pages), [RealKIE, long](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 44 documents × 3 runs.

![List recall against cost per 1,000 documents (USD) for each Extraction mode](../../images/benchmark-guidance/EX-01-list.svg)

*List recall against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [Statement, 400 rows](datasets.md#synthetic-statement-400-rows-9-pages), [Statement, 800 rows](datasets.md#synthetic-statement-800-rows-17-pages), [Statement, 1,600 rows](datasets.md#synthetic-statement-1600-rows-33-pages). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Confidence: Off. Extraction by dataset: Statement, 400 rows — varies (the setting under test); Statement, 800 rows — varies (the setting under test); Statement, 1,600 rows — Claude Sonnet 5, single-pass. N: Statement, 400 rows — 5 documents × 3 runs; Statement, 800 rows — 5 documents × 2–3 runs; Statement, 1,600 rows — 5 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Which extraction model should I use?

**Answer:** On W-2 forms, Claude Opus 5, Claude Opus 5.5, OpenAI GPT-6 Astra and xAI Grok 4.6 were nearly perfect (0.999), 0.04 above the default, Claude Sonnet 5 (0.96), and Claude Haiku 5.5 scored 0.98; the Amazon Nova models scored 0.06 to 0.16 below the default. On RealKIE invoices no model scored measurably different from the default (0.74 to 0.84). Each model family counts tokens differently, so compare models on cost rather than tokens. xAI Grok 4.6 took about three times as long per document. [Details: EX-02](#ex-02-which-extraction-model-should-i-use)

![Weighted accuracy against cost per 1,000 documents (USD) for each Extraction model](../../images/benchmark-guidance/EX-02-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Color and shape mark the model family, and each family has its own chart on the same axes, with the other families' values as gray dots. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N: RealKIE — 20 documents × 2–3 runs; Fake-W2 — 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Does more reasoning effort improve extraction?

**Answer:** A little, for few tokens. High effort raised accuracy from 0.79 to 0.83 on RealKIE invoices and from 0.95 to 0.97 on W-2 forms, for 2 to 5% more total tokens per page and a few seconds per document. Both gains are near the edge of the noise on 20 documents, so confirm on your own test set before you change the default. [Details: EX-03](#ex-03-does-more-reasoning-effort-improve-extraction)

![Weighted accuracy against cost per 1,000 documents (USD) for each `extraction.reasoning_effort`](../../images/benchmark-guidance/EX-03-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N = 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

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

**Answer:** Structured output, or leave it off; avoid the forced tool. Structured output scored the same as the default on W-2 forms (0.96) and slightly higher on RealKIE invoices (0.84 against 0.81, at the edge of the noise), and it was a little cheaper and faster. The forced tool lowered accuracy on W-2 forms (0.89 against 0.96) and got no W-2 fully right. [Details: EX-12](#ex-12-does-structured-output-make-single-pass-extraction-more-accurate)

![Weighted accuracy against cost per 1,000 documents (USD) for each Schema enforcement (`extraction.schema_enforcement.mode`)](../../images/benchmark-guidance/EX-12-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields), [RealKIE, long](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N: RealKIE — 20 documents × 3 runs; Fake-W2 — 20 documents × 3 runs; RealKIE, long — 19 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

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

**Answer:** When a document's lists run past about 150 rows. On RealKIE invoices with 182 or more line items, single-pass got almost none right (line-item score 0.00 to 0.50, and 0 on the invoices with 309, 392 and 1,490 items), while agentic extraction got 0.32 to 0.90. On synthetic bank statements, agentic extraction returned every one of 1,600 rows exactly right on all 15 runs of the 1,600-row statements, where single-pass returned 3%; at 800 rows it was 1.00 against 0.82, and at 400 both were perfect. Below about 150 rows single-pass did better and cost less: across all 20 RealKIE invoices its line-item score was 0.61 against 0.45. Agentic extraction is slow on very long lists: about 39 minutes for a 1,600-row statement, past Test Studio's time limit, so Test Studio reports those runs as failed although every document finished.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-173220, RealKIE-FCC-Verified-20261113-173644, RealKIE-FCC-Verified-20261113-173641, RealKIE-FCC-Verified-20261113-173219, RealKIE-FCC-Verified-20261113-173642, RealKIE-FCC-Verified-20261113-173643, Fake-W2-Tax-Forms-20261113-181721, Fake-W2-Tax-Forms-20261113-181722, Fake-W2-Tax-Forms-20261113-181723, Fake-W2-Tax-Forms-20261113-181901, Fake-W2-Tax-Forms-20261113-182002, Fake-W2-Tax-Forms-20261113-182011, realkie-fcc-short-20261113-201254, realkie-fcc-short-20261113-201252, realkie-fcc-short-20261113-201253, realkie-fcc-short-20261113-201713, realkie-fcc-short-20261113-201703, realkie-fcc-short-20261113-201714, realkie-fcc-long-20261113-201929, realkie-fcc-long-20261113-201928, realkie-fcc-long-20261113-202020, realkie-fcc-long-20261113-202336, realkie-fcc-long-20261113-202804, realkie-fcc-long-20261113-202644, synth-statements-400-20261113-231559, synth-statements-400-20261113-231602, synth-statements-400-20261113-231603, synth-statements-400-20261113-231600, synth-statements-400-20261113-231601, synth-statements-400-20261113-231604, synth-statements-800-20261113-231613, synth-statements-800-20261113-231615, synth-statements-800-20261113-231617, synth-statements-800-20261113-231614, synth-statements-800-20261113-231616, synth-statements-1600-20261113-231626, synth-statements-1600-20261113-231629, synth-statements-1600-20261113-231631 (3 failed documents left out of the scores)`.*

| | |
|---|---|
| **Changes** | Extraction mode |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic statement, 100 rows (3 pages)](datasets.md#synthetic-statement-100-rows-3-pages); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [Synthetic statement, 1,600 rows (33 pages)](datasets.md#synthetic-statement-1600-rows-33-pages); [Synthetic statement, 3,200 rows (66 pages)](datasets.md#synthetic-statement-3200-rows-66-pages); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [RealKIE-FCC-Verified, short invoices (24 invoices, 1 or 2 pages)](datasets.md#realkie-fcc-verified-short-invoices-24-invoices-1-or-2-pages); [RealKIE-FCC-Verified, long invoices (19 invoices, 8 to 33 pages)](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed). Extraction by dataset: Statement, 400 rows — varies (the setting under test); Statement, 800 rows — varies (the setting under test); Statement, 1,600 rows — Claude Sonnet 5, single-pass; RealKIE — varies (the setting under test); RealKIE, short — varies (the setting under test); RealKIE, long — varies (the setting under test); Fake-W2 — varies (the setting under test). Confidence by dataset: Statement, 400 rows — Off; Statement, 800 rows — Off; Statement, 1,600 rows — Off; RealKIE — Amazon Nova Lite, escalating to Claude Sonnet 5; RealKIE, short — Amazon Nova Lite, escalating to Claude Sonnet 5; RealKIE, long — Amazon Nova Lite, escalating to Claude Sonnet 5; Fake-W2 — Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N: Statement, 400 rows — 5 documents × 3 runs; Statement, 800 rows — 5 documents × 2–3 runs; Statement, 1,600 rows — 5 documents × 3 runs; RealKIE — 20 documents × 3 runs; RealKIE, short — 24 documents × 3 runs; RealKIE, long — 18–19 documents × 3 runs, 1 failed document per run left out; Fake-W2 — 20 documents × 3 runs |

| Dataset | Extraction mode | List recall | List precision | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Single-pass (Simple, default) |
|---|---|---|---|---|---|---|---|---|
| Statement, 100 rows | Single-pass (Simple, default) | — | — | — | — | — | — | — |
| Statement, 100 rows | Agentic (Advanced) | — | — | — | — | — | — | — |
| Statement, 400 rows | Single-pass (Simple, default) | 1 ± 0 | 1 ± 0 | 8,839 ± 2.07 | 10,987 ± 12 | $545 ± $1.16 | 337 ± 24 | — |
| Statement, 400 rows | Agentic (Advanced) | 1 ± 0 | 1 ± 0 | 24,778 ± 4.31 | 26,928 ± 25 | $546 ± $23.6 | 241 ± 25 | — |
| Statement, 800 rows | Single-pass (Simple, default) | 0.82 ± 0.01 | 1 ± 0 | 8,568 ± 33 | 10,623 ± 17 | $957 ± $4.36 | 762 ± 39 | — |
| Statement, 800 rows | Agentic (Advanced) | 1 ± 0 | 1 ± 0 | 26,297 ± 0.66 | 28,335 ± 10 | $1,036 ± $25.7 | 779 ± 81 | $0.45 per error prevented |
| Statement, 1,600 rows | Single-pass (Simple, default) | 0.03 ± 0.01 | 1 ± 0 | 6,033 ± 17 | 8,069 ± 13 | $1,127 ± $6.63 | 366 ± 169 | — |
| Statement, 1,600 rows | Agentic (Advanced) | — | — | — | — | — | — | — |
| Statement, 3,200 rows | Single-pass (Simple, default) | — | — | — | — | — | — | — |
| Statement, 3,200 rows | Agentic (Advanced) | — | — | — | — | — | — | — |

![Weighted accuracy against cost per 1,000 documents (USD) for each Extraction mode](../../images/benchmark-guidance/EX-01-fields.svg)

*Weighted accuracy against cost per 1,000 documents (USD). Each point is one value of the setting; shaded bands span two standard errors either side. Up and to the left is better. Lines run from the cheapest value to each more accurate one, labeled with the marginal cost of error: the extra cost per error prevented ([Marginal Cost of Error](marginal-cost-of-error.md)).* Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [RealKIE, short](datasets.md#realkie-fcc-verified-short-invoices-24-invoices-1-or-2-pages), [RealKIE, long](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). Models: OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. N: RealKIE — 20 documents × 3 runs; RealKIE, short — 24 documents × 3 runs; RealKIE, long — 18–19 documents × 3 runs, 1 failed document per run left out; Fake-W2 — 20 documents × 3 runs. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

| Dataset | Extraction mode | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Single-pass (Simple, default) |
|---|---|---|---|---|---|---|---|
| RealKIE | Single-pass (Simple, default) | 0.8 ± 0.06 | 7,042 ± 14 | 20,208 ± 339 | $145 | 58 ± 37 | — |
| RealKIE | Agentic (Advanced) | 0.8 ± 0.08 | 15,368 ± 597 | 20,843 ± 516 | $169 | 65 ± 29 | — |
| RealKIE, short | Single-pass (Simple, default) | 0.82 ± 0.06 | 8,593 ± 29 | 19,612 ± 253 | $52.8 | 22 ± 0.65 | — |
| RealKIE, short | Agentic (Advanced) | 0.82 ± 0.06 | 34,802 ± 2,925 | 41,424 ± 2,846 | $79.2 | 28 ± 1.63 | — |
| RealKIE, long | Single-pass (Simple, default) | 0.77 ± 0.08 | 7,322 ± 39 | 34,597 ± 5,531 | $404 | 105 ± 21 | — |
| RealKIE, long | Agentic (Advanced) | 0.8 ± 0.08 | 66,260 ± 5,944 | 85,090 ± 5,546 | $1,273 | 101 ± 6.87 | no measurable gain |
| Fake-W2 | Single-pass (Simple, default) | 0.95 ± 0.02 | 6,876 ± 2.3 | 15,192 ± 2.09 | $33.0 | 38 ± 1.14 | — |
| Fake-W2 | Agentic (Advanced) | 0.97 ± 0.01 | 46,661 ± 84 | 51,789 ± 84 | $61.4 | 30 ± 0.46 | no measurable gain |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### EX-02: Which extraction model should I use?

**Answer:** On W-2 forms, Claude Opus 5, Claude Opus 5.5, OpenAI GPT-6 Astra and xAI Grok 4.6 were nearly perfect (0.999), 0.04 above the default, Claude Sonnet 5 (0.96), and Claude Haiku 5.5 scored 0.98; the Amazon Nova models scored 0.06 to 0.16 below the default. On RealKIE invoices no model scored measurably different from the default (0.74 to 0.84). Each model family counts tokens differently, so compare models on cost rather than tokens. xAI Grok 4.6 took about three times as long per document.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-183955, RealKIE-FCC-Verified-20261113-183954, RealKIE-FCC-Verified-20261113-183956, RealKIE-FCC-Verified-20261113-184302, RealKIE-FCC-Verified-20261113-184254, RealKIE-FCC-Verified-20261113-184301, RealKIE-FCC-Verified-20261113-184609, RealKIE-FCC-Verified-20261113-184643, RealKIE-FCC-Verified-20261113-184918, RealKIE-FCC-Verified-20261113-190709, RealKIE-FCC-Verified-20261113-192401, RealKIE-FCC-Verified-20261113-192310, RealKIE-FCC-Verified-20261113-192341, RealKIE-FCC-Verified-20261113-192701, RealKIE-FCC-Verified-20261113-192729, RealKIE-FCC-Verified-20261113-192743, RealKIE-FCC-Verified-20261113-194402, RealKIE-FCC-Verified-20261113-194401, RealKIE-FCC-Verified-20261113-194404, RealKIE-FCC-Verified-20261113-195220, RealKIE-FCC-Verified-20261113-195217, RealKIE-FCC-Verified-20261113-195219, RealKIE-FCC-Verified-20261113-195617, RealKIE-FCC-Verified-20261113-195659, RealKIE-FCC-Verified-20261113-195740, RealKIE-FCC-Verified-20261113-195936, RealKIE-FCC-Verified-20261113-195957, RealKIE-FCC-Verified-20261113-200106, RealKIE-FCC-Verified-20261113-200235, RealKIE-FCC-Verified-20261113-200733, RealKIE-FCC-Verified-20261113-200820, RealKIE-FCC-Verified-20261113-200702, Fake-W2-Tax-Forms-20261113-201217, Fake-W2-Tax-Forms-20261113-201204, Fake-W2-Tax-Forms-20261113-201242, Fake-W2-Tax-Forms-20261113-201354, Fake-W2-Tax-Forms-20261113-201419, Fake-W2-Tax-Forms-20261113-201509, Fake-W2-Tax-Forms-20261113-201521, Fake-W2-Tax-Forms-20261113-201543, Fake-W2-Tax-Forms-20261113-201623, Fake-W2-Tax-Forms-20261113-201638, Fake-W2-Tax-Forms-20261113-201728, Fake-W2-Tax-Forms-20261113-201737, Fake-W2-Tax-Forms-20261113-201754, Fake-W2-Tax-Forms-20261113-201833, Fake-W2-Tax-Forms-20261113-201841, Fake-W2-Tax-Forms-20261113-201858, Fake-W2-Tax-Forms-20261113-201947, Fake-W2-Tax-Forms-20261113-202015, Fake-W2-Tax-Forms-20261113-202016, Fake-W2-Tax-Forms-20261113-202114, Fake-W2-Tax-Forms-20261113-202132, Fake-W2-Tax-Forms-20261113-202129, Fake-W2-Tax-Forms-20261113-202220, Fake-W2-Tax-Forms-20261113-202242, Fake-W2-Tax-Forms-20261113-202246, Fake-W2-Tax-Forms-20261113-202327, Fake-W2-Tax-Forms-20261113-202346, Fake-W2-Tax-Forms-20261113-202412, Fake-W2-Tax-Forms-20261113-202453, Fake-W2-Tax-Forms-20261113-202510, Fake-W2-Tax-Forms-20261113-202644, Fake-W2-Tax-Forms-20261113-202609, Fake-W2-Tax-Forms-20261113-202529`.*

| | |
|---|---|
| **Changes** | Extraction model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: varies (the setting under test) · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 2–3 runs; Fake-W2 — 20 documents × 3 runs |

| Dataset | Extraction model | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Amazon Nova Lite |
|---|---|---|---|---|---|---|---|
| RealKIE | Claude Sonnet 5 (default) | 0.8 ± 0.06 | 7,041 ± 13 | 20,556 ± 1,144 | $143 | 44 ± 3.91 | no measurable gain |
| RealKIE | Amazon Nova Lite | 0.74 ± 0.07 | 3,710 ± 68 | 25,004 ± 5,956 | $66.2 | 41 ± 1.28 | — |
| RealKIE | Amazon Nova Pro | 0.78 ± 0.07 | 3,468 ± 28 | 14,516 ± 443 | $75.2 | 41 ± 1.99 | no measurable gain |
| RealKIE | Amazon Nova 2 Lite | 0.78 ± 0.09 | 1,862 ± 2.65 | 17,517 ± 1,681 | $74.8 | 56 ± 16 | no measurable gain |
| RealKIE | Claude Haiku 5.5 | 0.79 ± 0.08 | 7,345 ± 109 | 22,012 ± 1,166 | $73.9 | 58 ± 28 | no measurable gain |
| RealKIE | Claude Sonnet 4.6 | 0.8 ± 0.08 | 3,413 ± 0.71 | 18,579 ± 164 | $131 | 57 ± 9.64 | no measurable gain |
| RealKIE | Claude Sonnet 5.5 | 0.8 ± 0.07 | 6,991 ± 1.4 | 20,214 ± 751 | $134 | 63 ± 4.58 | no measurable gain |
| RealKIE | Claude Opus 5 | 0.81 ± 0.07 | 7,050 ± 69 | 21,389 ± 1,270 | $235 | 61 ± 22 | no measurable gain |
| RealKIE | Claude Opus 5.5 | 0.82 ± 0.07 | 7,046 ± 49 | 22,110 ± 1,257 | $198 | 43 ± 12 | no measurable gain |
| RealKIE | OpenAI GPT-6 Astra | 0.84 ± 0.07 | 9,884 ± 3 | 25,815 ± 16 | $661 | 62 ± 24 | $5.53 per error prevented |
| RealKIE | xAI Grok 4.6 | 0.84 ± 0.07 | 3,713 ± 6.42 | 17,552 ± 823 | $97.1 | 87 ± 41 | $0.31 per error prevented |
| Fake-W2 | Claude Sonnet 5 (default) | 0.96 ± 0.02 | 6,877 ± 2.89 | 15,195 ± 2.28 | $33.0 | 29 ± 6.1 | $0.19 per error prevented |
| Fake-W2 | Amazon Nova Lite | 0.87 ± 0.03 | 6,682 ± 1.23 | 14,993 ± 6.52 | $15.8 | 23 ± 0.9 | — |
| Fake-W2 | Amazon Nova Pro | 0.9 ± 0.02 | 6,684 ± 1.91 | 15,005 ± 7.56 | $20.7 | 24 ± 0.24 | no measurable gain |
| Fake-W2 | Amazon Nova 2 Lite | 0.8 ± 0.04 | 4,643 ± 0.25 | 12,980 ± 2.25 | $17.7 | 19 ± 0.17 | — |
| Fake-W2 | Claude Haiku 5.5 | 0.98 ± 0.01 | 7,889 ± 31 | 16,210 ± 35 | $16.9 | 24 ± 0.65 | $0.01 per error prevented |
| Fake-W2 | Claude Sonnet 4.6 | 0.95 ± 0.03 | 5,773 ± 0.2 | 14,091 ± 0.97 | $38.9 | 23 ± 0.79 | $0.29 per error prevented |
| Fake-W2 | Claude Sonnet 5.5 | 0.92 ± 0.03 | 6,743 ± 4.47 | 15,067 ± 3.14 | $31.6 | 21 ± 0.84 | $0.29 per error prevented |
| Fake-W2 | Claude Opus 5 | 1 ± 0 | 6,826 ± 7.99 | 15,140 ± 9.49 | $58.7 | 25 ± 1.12 | $0.33 per error prevented |
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
| Statement, 400 rows | Claude Sonnet 4.6 | — | — | — | — | — | — |
| Statement, 400 rows | Claude Sonnet 5.5 | — | — | — | — | — | — |
| Statement, 400 rows | Claude Opus 5 | — | — | — | — | — | — |
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
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
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

**Answer:** Structured output, or leave it off; avoid the forced tool. Structured output scored the same as the default on W-2 forms (0.96) and slightly higher on RealKIE invoices (0.84 against 0.81, at the edge of the noise), and it was a little cheaper and faster. The forced tool lowered accuracy on W-2 forms (0.89 against 0.96) and got no W-2 fully right.

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-211217, RealKIE-FCC-Verified-20261113-211218, RealKIE-FCC-Verified-20261113-211216, RealKIE-FCC-Verified-20261113-211535, RealKIE-FCC-Verified-20261113-211526, RealKIE-FCC-Verified-20261113-211543, RealKIE-FCC-Verified-20261113-211823, RealKIE-FCC-Verified-20261113-211839, RealKIE-FCC-Verified-20261113-211824, Fake-W2-Tax-Forms-20261113-212143, Fake-W2-Tax-Forms-20261113-212145, Fake-W2-Tax-Forms-20261113-212202, Fake-W2-Tax-Forms-20261113-212313, Fake-W2-Tax-Forms-20261113-212311, Fake-W2-Tax-Forms-20261113-212326, Fake-W2-Tax-Forms-20261113-212439, Fake-W2-Tax-Forms-20261113-212441, Fake-W2-Tax-Forms-20261113-212442, realkie-fcc-long-20261113-213052, realkie-fcc-long-20261113-213055, realkie-fcc-long-20261113-213057, realkie-fcc-long-20261113-213053, realkie-fcc-long-20261113-213054, realkie-fcc-long-20261113-213056, realkie-fcc-long-20261113-213058, realkie-fcc-long-20261113-213059, realkie-fcc-long-20261113-213104`.*

| | |
|---|---|
| **Changes** | Schema enforcement (`extraction.schema_enforcement.mode`) |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [RealKIE-FCC-Verified, long invoices (19 invoices, 8 to 33 pages)](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Classification: Amazon Nova 2 Lite, page by page (no calls billed) · Extraction: Claude Sonnet 5, single-pass · Confidence: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 3 runs; Fake-W2 — 20 documents × 3 runs; RealKIE, long — 19 documents × 3 runs |

| Dataset | Schema enforcement (`extraction.schema_enforcement.mode`) | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs cheapest |
|---|---|---|---|---|---|---|---|---|
| RealKIE | Off: schema in the prompt (default) | 0.81 ± 0.06 | 0.05 ± 0 | 7,055 ± 6.46 | 21,131 ± 246 | $145 ± $1.25 | 41 ± 6.93 | — |
| RealKIE | Structured output | 0.84 ± 0.07 | 0.08 ± 0.03 | 7,252 ± 0.28 | 20,383 ± 957 | $135 ± $0.74 | 31 ± 3.49 | — |
| RealKIE | Forced tool | 0.82 ± 0.07 | 0.07 ± 0.03 | 7,335 ± 3.28 | 21,270 ± 1,326 | $139 ± $4.94 | 36 ± 3.36 | — |
| Fake-W2 | Off: schema in the prompt (default) | 0.96 ± 0.01 | 0.15 ± 0.06 | 6,901 ± 11 | 15,276 ± 40 | $34.0 ± $0.93 | 27 ± 1.78 | — |
| Fake-W2 | Structured output | 0.96 ± 0.01 | 0.22 ± 0.03 | 6,905 ± 14 | 15,272 ± 31 | $32.4 ± $0.16 | 25 ± 0.52 | — |
| Fake-W2 | Forced tool | 0.89 ± 0.01 | 0 ± 0 | 9,962 ± 0.52 | 18,313 ± 4.9 | $42.5 ± $1.26 | 29 ± 1.45 | — |
| RealKIE, long | Off: schema in the prompt (default) | 0.79 ± 0.08 | 0.04 ± 0.04 | 1,043,012 ± 1,035,840 | 5,119,416 ± 5,222,129 | $448 ± $9.63 | 149 ± 2.5 | no measurable gain |
| RealKIE, long | Structured output | 0.81 ± 0.06 | 0 ± 0 | 1,059,084 ± 1,051,665 | 5,225,387 ± 5,184,786 | $404 ± $8.84 | 142 ± 15 | no measurable gain |
| RealKIE, long | Forced tool | 0.77 ± 0.06 | 0 ± 0 | 1,059,792 ± 1,059,793 | 5,044,236 ± 5,045,061 | $401 ± $9.30 | 99 ± 6.03 | — |

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
