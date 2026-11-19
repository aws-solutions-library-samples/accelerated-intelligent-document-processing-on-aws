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

Two things matter more than any setting.

**Your definitions.** The model reads each field's description as its instruction. Ask for
the values your database needs, keep values that belong together (a line item's date, rate
and description) in one list, say what each field is and isn't ("the issue date, not the due
date"), and give the format you want. See
[Improving accuracy](../idp-fundamentals/extraction.md#improving-accuracy-once-your-pipeline-is-running).

**Your annotations.** A wrong annotation counts as an extraction error, and in our experience
annotation errors are more than half of what Test Studio reports. Check each reported error
and fix the annotation when the extraction was right. See
[Measure with ground truth](../idp-fundamentals/index.md#4-measure-with-ground-truth).

**The dataset behind most of these results.** Most extraction questions here are answered on [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified): real invoices with ground truth that people checked for every header field and every line item. We lean on it because its ground truth is accurate and its documents are real, varied layouts, not synthetic ones. It is one kind of document, broadcast advertising invoices, many of them with long lists of line items. If your documents have few or no line items, weigh the line-item results less and confirm on your own documents.

**Settings** are what we measure:

| Setting | What it changes | Questions |
|---|---|---|
| *Extraction mode* (`extraction.mode`) | Single-pass: one request per section (`simple`). Agentic: an agent that works through the section in shards (`advanced`) | [EX-01](#when-is-agentic-extraction-worth-it), [EX-07](#does-deterministic-table-parsing-help-agentic-extraction) |
| *Extraction model* (`extraction.model`) | The model that reads the section | [EX-02](#which-extraction-model-should-i-use) |
| Reasoning effort (`extraction.reasoning_effort`) | How long the model reasons before it answers | [EX-03](#does-more-reasoning-effort-improve-extraction) |
| Few-shot examples | Sample documents with their correct values, added to the prompt | [EX-05](#planned) |
| Page images | Whether the model sees the pages as well as the OCR text | [EX-06](#what-is-the-impact-of-document-modality) |
| Value coercion and schema validation | Type repair and schema checks on every record | [EX-10](#do-schema-validation-and-value-coercion-improve-the-output), [EX-11](#should-i-retry-failed-fields-with-a-stronger-model) |

## Questions

<details>
<summary>What the metrics mean</summary>

- *List recall:* the share of ground-truth list cells extracted correctly; a lost row
  lowers it.
- *List precision:* the share of extracted list cells that are correct; an invented or
  misaligned row lowers it.
- *Weighted accuracy:* Stickler's field-weighted score against ground truth.
- *Documents with every field right:* the share of documents with no wrong field.

Weighted accuracy counts a whole list as one field, so a lost row barely moves it. Read list recall for lists.

Full definitions: Stickler's [Thresholds and Metrics](https://awslabs.github.io/stickler/Getting-Started/thresholds-and-metrics/) and [Aggregate Metrics](https://awslabs.github.io/stickler/Advanced/aggregate-metrics/), and [Field Weighting](../evaluation.md#field-weighting-for-business-criticality) in the evaluation guide.

[Benchmark Datasets](datasets.md) describes each dataset.

</details>

<!-- BEGIN GENERATED FAQ -->

### When is agentic extraction worth it?

![Agentic extraction helps only on very long extractions, such as 100+ line items: line-item score per invoice against line items per invoice](../../images/benchmark-guidance/EX-01-by-size-realkie-all.svg)

*One point per invoice per extraction mode and model; each line is its running median.*

**Answer:** Single-pass is a good place to start for most documents. Agentic extraction may do better on exceptionally long or complex extractions, such as invoices or receipts with many line items. On [RealKIE](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified) invoices with more than about 100 line items, single-pass got almost no line items right, and agentic with Claude Haiku 5.5 got 44% to 79%. Below that, single-pass scored as well or better, for about the same cost or less. Agentic costs here are a floor: we can't price its calls over 100,000 tokens, which Claude Haiku 5.5 charges more for. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on each dataset's preset, not the baseline setup.*

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 62 Test Studio runs (12 failed documents left out of the scores), listed in `benchmarks/results/guidance/EX-01.json`.*

Single-pass scored as well as agentic or better up to 100 line items, and cost about the same there with the 5.5 models. Above 100 line items, agentic with Claude Haiku 5.5 scored as well as with Claude Sonnet 5.5 for at least $316 against $1,073 per 1,000 invoices; Claude Haiku 5.5's agentic calls over 100,000 tokens bill at its long-context rate and can't be priced from our metering, so its cost could be up to about two thirds of Claude Sonnet 5.5's. Only 5 RealKIE invoices have more than 100 line items, and Test Studio could not score the longest (1,490 items) because its evaluation step timed out.

| | |
|---|---|
| **Changes** | Extraction mode |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [RealKIE-FCC-Verified, all 75 invoices](datasets.md#realkie-fcc-verified-all-75-invoices) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: varies (the setting under test) · Scoring: Amazon Nova Lite, escalating to Claude Sonnet 5. |
| **Runs** | 3 per document, per value; N = 74–75 documents × 3 runs; a document that failed in a run is left out of that run |

*N = 70 invoices × 3 runs. Datasets: [RealKIE, all](datasets.md#realkie-fcc-verified-all-75-invoices). A line-item score is Stickler's score for the list: matched rows over the larger of the true and returned row counts.*

<table class="strata">
<caption>Claude Haiku 5.5</caption>
<thead>
<tr><th rowspan="2">Line items</th><th rowspan="2">Invoices</th><th colspan="2" class="group">Line-item score</th><th colspan="2" class="group">Weighted accuracy</th><th colspan="2" class="group">Cost per 1,000 invoices</th><th rowspan="2" class="group">Agentic costs</th></tr>
<tr><th class="group">Single‑pass</th><th>Agentic</th><th class="group">Single‑pass</th><th>Agentic</th><th class="group">Single‑pass</th><th>Agentic</th></tr>
</thead>
<tbody>
<tr><td>No line items</td><td>4</td><td class="group">—</td><td>—</td><td class="group">0.70</td><td>0.68</td><td class="group">$22.0</td><td>$43.2</td><td class="group">2.0×</td></tr>
<tr><td>1–20</td><td>41</td><td class="group"><strong>0.69</strong></td><td>0.57</td><td class="group">0.84</td><td>0.83</td><td class="group">$73.2</td><td>$69.6</td><td class="group">1.0×</td></tr>
<tr><td>21–100</td><td>25</td><td class="group">0.53</td><td>0.49</td><td class="group">0.80</td><td>0.79</td><td class="group">$104</td><td>$105</td><td class="group">1.0×</td></tr>
<tr><td>101–300</td><td>2</td><td class="group">0.02</td><td><strong>0.44</strong></td><td class="group">0.75</td><td>0.72</td><td class="group">$248</td><td>$238</td><td class="group">1.0×</td></tr>
<tr><td>More than 300</td><td>2</td><td class="group">0.00</td><td><strong>0.79</strong></td><td class="group">0.89</td><td>0.98</td><td class="group">$228</td><td>$316</td><td class="group">1.4×</td></tr>
<tr><td><strong>All invoices</strong></td><td>74</td><td class="group">0.57</td><td>0.52</td><td class="group">0.82</td><td>0.81</td><td class="group">$89.8</td><td>$91.3</td><td class="group">1.0×</td></tr>
</tbody>
</table>

<table class="strata">
<caption>Claude Sonnet 5.5</caption>
<thead>
<tr><th rowspan="2">Line items</th><th rowspan="2">Invoices</th><th colspan="2" class="group">Line-item score</th><th colspan="2" class="group">Weighted accuracy</th><th colspan="2" class="group">Cost per 1,000 invoices</th><th rowspan="2" class="group">Agentic costs</th></tr>
<tr><th class="group">Single‑pass</th><th>Agentic</th><th class="group">Single‑pass</th><th>Agentic</th><th class="group">Single‑pass</th><th>Agentic</th></tr>
</thead>
<tbody>
<tr><td>No line items</td><td>4</td><td class="group">—</td><td>—</td><td class="group">0.79</td><td>0.84</td><td class="group">$29.5</td><td>$33.9</td><td class="group">1.2×</td></tr>
<tr><td>1–20</td><td>41</td><td class="group">0.65</td><td>0.64</td><td class="group">0.84</td><td>0.86</td><td class="group">$130</td><td>$123</td><td class="group">0.9×</td></tr>
<tr><td>21–100</td><td>25</td><td class="group">0.51</td><td>0.52</td><td class="group">0.78</td><td>0.79</td><td class="group">$218</td><td>$198</td><td class="group">0.9×</td></tr>
<tr><td>101–300</td><td>2</td><td class="group">0.22</td><td>0.49</td><td class="group">0.81</td><td>0.72</td><td class="group">$544</td><td>$576</td><td class="group">1.1×</td></tr>
<tr><td>More than 300</td><td>2</td><td class="group">0.10</td><td><strong>0.77</strong></td><td class="group">0.90</td><td>0.98</td><td class="group">$581</td><td>$1,073</td><td class="group">1.8×</td></tr>
<tr><td><strong>All invoices</strong></td><td>74</td><td class="group">0.57</td><td>0.62</td><td class="group">0.82</td><td>0.84</td><td class="group">$178</td><td>$181</td><td class="group">1.0×</td></tr>
</tbody>
</table>


*Invoices grouped by the line items their ground truth holds. Each score and cost is a mean over the row's invoices; only invoices every run of every value scored are counted, each once. The line-item score is the share of rows matched: matched rows over the larger of the true and returned row counts. Cost is each invoice's own Amazon Bedrock and Amazon Textract metering, without the invoices left out, so it can be lower than the run cost in the table above, averaged over runs that share no prompt cache; Agentic costs is agentic's cost over single-pass's for the same model. No line-item score for invoices with no line items. Bold: the better mode, where the gap exceeds twice its standard error.* Datasets: [RealKIE, all](datasets.md#realkie-fcc-verified-all-75-invoices). N = 74 invoices × 3 runs.

Test Studio could not score 1 invoice for Agentic · Claude Haiku 5.5: its evaluation step timed out, although extraction finished (1,490 line items, 3 of 3 runs). Left out of every column.

Test Studio could not score 1 invoice for Agentic · Claude Sonnet 5.5: its evaluation step timed out, although extraction finished (1,490 line items, 3 of 3 runs). Left out of every column.

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

![Averaged over whole datasets, the two modes score within the noise: weighted accuracy against cost per 1,000 documents, one point per extraction mode and model](../../images/benchmark-guidance/EX-01-fields.svg)

*Bands show ±2 standard errors.*

| Dataset | Extraction mode | Weighted accuracy | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Agentic · Haiku 5.5 |
|---|---|---|---|---|---|---|---|
| RealKIE, all | Single-pass · Claude Haiku 5.5 | 0.82 ± 0.04 | 7,673 ± 11 | **26,263 ± 1,091** | $108 ± $7.45 | 52 ± 2.33 | no measurable gain |
| RealKIE, all | Agentic · Claude Haiku 5.5 | 0.81 ± 0.04 | 38,874 ± 1,241 | 50,089 ± 1,853 | $105 ± $1.92 | 60 ± 1.67 | — |
| RealKIE, all | Single-pass · Claude Sonnet 5.5 | 0.82 ± 0.04 | **7,428 ± 20** | 33,542 ± 1,190 | $200 ± $2.55 | 54 ± 9.18 | no measurable gain |
| RealKIE, all | Agentic · Claude Sonnet 5.5 | 0.84 ± 0.03 | 24,766 ± 3,108 | 37,144 ± 4,911 | $227 ± $46.1 | 59 ± 9.6 | no measurable gain |

Failed documents per run: Agentic · Claude Haiku 5.5: 1, 1, 1; Agentic · Claude Sonnet 5.5: 1, 1, 1.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Which extraction model should I use?

![Claude Haiku 5.5 scores as well as any model, for the least: weighted accuracy against cost per 1,000 documents, one point per extraction model](../../images/benchmark-guidance/EX-02-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Claude Haiku 5.5 is a good place to start. On RealKIE invoices no model got a measurably higher weighted accuracy, and Claude Haiku 5.5 cost the least: $23 per 1,000 invoices, against $59 to $601 for the other Claude, OpenAI and xAI models. OpenAI GPT-6 Astra got every field right on the most invoices, 13% against none for Claude Haiku 5.5, for $601. Test it on your own documents.

**The economics:** On RealKIE invoices, OpenAI GPT-6 Astra scored no measurably better than Claude Haiku 5.5, so the extra $578 per 1,000 documents buys nothing measurable.

| Instead of Haiku 5.5 | Extra cost per 1,000 documents | Extra documents right per 1,000 documents | [Cost per document it gets right](marginal-cost-of-error.md) |
|---|---|---|---|
| Nova 2 Lite | +$2.11 | no measurable difference | no measurable gain |
| Sonnet 5.5 | +$62.0 | no measurable difference | no measurable gain |
| Opus 5.5 | +$127 | no measurable difference | no measurable gain |
| GPT-6 Astra | +$578 | no measurable difference | no measurable gain |
| Grok 4.6 | +$36.2 | no measurable difference | no measurable gain |

*RealKIE invoices, N = 20 documents × 3 runs. A difference within twice the combined standard error is no measurable difference. Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18.*

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 36 Test Studio runs, listed in `benchmarks/results/guidance/EX-02.json`.*

On W-2 forms every Claude, OpenAI and xAI model scored 0.996 or better, and Amazon Nova 2 Lite 0.86. xAI Grok 4.6 took about 9 minutes per W-2 form, against 30 seconds for the others.

| | |
|---|---|
| **Changes** | Extraction model |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: varies (the setting under test) · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Extraction model | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Haiku 5.5 |
|---|---|---|---|---|---|---|---|---|
| RealKIE | Amazon Nova 2 Lite | 0.77 ± 0.07 | 0 ± 0 | **1,594 ± 44** | **14,461 ± 1,930** | $25.1 ± $0.69 | 52 ± 9.93 | no measurable gain |
| RealKIE | **Claude Haiku 5.5 (baseline)** | 0.8 ± 0.08 | 0 ± 0 | 6,769 ± 25 | 17,288 ± 424 | **$23.0 ± $0.85** | 50 ± 4.74 | — |
| RealKIE | Claude Sonnet 5.5 | 0.82 ± 0.07 | 0 ± 0 | 6,782 ± 49 | 17,675 ± 1,953 | $85.0 ± $2.37 | 42 ± 5.17 | no measurable gain |
| RealKIE | Claude Opus 5.5 | 0.83 ± 0.07 | 0.07 ± 0.03 | 6,901 ± 11 | 21,564 ± 1,184 | $150 ± $0.55 | 46 ± 6.84 | no measurable gain |
| RealKIE | OpenAI GPT-6 Astra | 0.85 ± 0.07 | **0.13 ± 0.03** | 9,577 ± 5.47 | 24,949 ± 1,996 | $601 ± $1.55 | 53 ± 3.61 | no measurable gain |
| RealKIE | xAI Grok 4.6 | 0.83 ± 0.07 | 0.03 ± 0.03 | 3,441 ± 21 | 18,649 ± 1,411 | $59.2 ± $1.21 | 64 ± 11 | no measurable gain |
| Fake-W2 | Amazon Nova 2 Lite | 0.86 ± 0.03 | 0 ± 0 | **4,306 ± 0.79** | 15,778 ± 110 | $8.51 ± $0.06 | 29 ± 0.3 | no measurable gain |
| Fake-W2 | **Claude Haiku 5.5 (baseline)** | 1 ± 0 | 0.8 ± 0.06 | 7,422 ± 61 | 18,400 ± 276 | **$7.42 ± $0.04** | 31 ± 0.69 | — |
| Fake-W2 | Claude Sonnet 5.5 | 1 ± 0 | 0.8 ± 0 | 6,256 ± 4.5 | 16,962 ± 18 | $20.7 ± $0.53 | **26 ± 0.71** | no measurable gain |
| Fake-W2 | Claude Opus 5.5 | 1 ± 0 | **0.88 ± 0.03** | 6,398 ± 1.87 | 17,206 ± 196 | $37.6 ± $2.12 | 30 ± 0.75 | no measurable gain |
| Fake-W2 | OpenAI GPT-6 Astra | 1 ± 0 | 0.83 ± 0.03 | 4,594 ± 1.1 | **15,372 ± 68** | $96.9 ± $0.04 | 30 ± 0.5 | no measurable gain |
| Fake-W2 | xAI Grok 4.6 | 1 ± 0 | 0.75 ± 0.06 | 5,704 ± 16 | 16,565 ± 316 | $22.0 ± $0.16 | 543 ± 184 | no measurable gain |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Does more reasoning effort improve extraction?

![More reasoning effort adds cost, not accuracy: weighted accuracy against cost per 1,000 documents, one point per reasoning effort](../../images/benchmark-guidance/EX-03-fields.svg)

*Bands show ±2 standard errors.*

*Fake-W2: every value scored 0.99 or higher, too easy to separate.*

**Answer:** Low effort is a good place to start. With Claude Sonnet 5.5, medium and high effort scored no better than low on RealKIE invoices or W-2 forms, and high cost more on both. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 18 Test Studio runs, listed in `benchmarks/results/guidance/EX-03.json`.*

| | |
|---|---|
| **Changes** | `extraction.reasoning_effort`, on Claude Sonnet 5.5 (Claude Haiku 5.5 takes no effort setting) |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: Claude Sonnet 5.5, single-pass · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | `extraction.reasoning_effort`, on Claude Sonnet 5.5 (Claude Haiku 5.5 takes no effort setting) | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Low (preset default) |
|---|---|---|---|---|---|---|---|---|
| RealKIE | **Low (preset default)** | 0.83 ± 0.07 | 0 ± 0 | **6,780 ± 49** | **17,946 ± 1,665** | $85.7 ± $2.36 | 38 ± 3.32 | — |
| RealKIE | Medium | 0.83 ± 0.07 | 0 ± 0 | 6,835 ± 7.46 | 20,982 ± 1,563 | $87.1 ± $0.34 | 38 ± 2.93 | no measurable gain |
| RealKIE | High | 0.82 ± 0.07 | 0 ± 0 | 6,914 ± 50 | 20,968 ± 1,355 | $90.5 ± $2.56 | 36 ± 3.22 | no measurable gain |
| Fake-W2 | **Low (preset default)** | 1 ± 0 | 0.78 ± 0.03 | 6,254 ± 4.5 | 17,042 ± 268 | $19.9 ± $0.08 | **26 ± 0.49** | — |
| Fake-W2 | Medium | 1 ± 0 | 0.8 ± 0 | 6,256 ± 8.92 | 17,019 ± 32 | $19.9 ± $0.08 | 27 ± 0.28 | — |
| Fake-W2 | High | 1 ± 0 | 0.8 ± 0 | 6,715 ± 62 | 17,522 ± 326 | $24.9 ± $0.71 | 30 ± 0.67 | — |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### What is the impact of document modality?

![Image and OCR text together keep forms and checks right: weighted accuracy and documents with every field right against cost per 1,000 documents, one point per what the extraction model reads](../../images/benchmark-guidance/EX-06-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Page image plus OCR text is a good place to start. On W-2 forms and bank checks, OCR text alone scored lowest (W-2s 0.91 against 0.997; checks 0.89 against 0.994), and the page image alone got every field right on fewer forms (50% of W-2s against 82%). On RealKIE invoices all three scored about the same (0.77 to 0.79). Our image-only runs still paid for OCR; skipping it would save about $10 per 1,000 pages. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 27 Test Studio runs (1 failed documents left out of the scores), listed in `benchmarks/results/guidance/EX-06.json`.*

| | |
|---|---|
| **Changes** | What the extraction model reads: the page image, the OCR text, or both |
| **Everything else** | OCR text from Amazon Bedrock Data Automation standard output; single-pass extraction on Claude Haiku 5.5; the rest as in the [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields); [OmniAI-OCR-Benchmark, bank checks (20)](datasets.md#omniai-ocr-benchmark-bank-checks-20) |
| **Models** | OCR: bda · Extraction: Claude Haiku 5.5, single-pass · Scoring: Claude Haiku 5.5. Classification by dataset: OmniAI, bank checks — Claude Haiku 5.5, holistic. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 3 runs; Fake-W2 — 20 documents × 3 runs; OmniAI, bank checks — 19–20 documents × 3 runs; a document that failed in a run is left out of that run |

| Dataset | What the extraction model reads: the page image, the OCR text, or both | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs cheapest |
|---|---|---|---|---|---|---|---|---|
| RealKIE | **Page image and OCR text (baseline)** | 0.79 ± 0.07 | 0 ± 0 | 7,167 ± 44 | 19,769 ± 2,401 | $51.0 ± $0.72 | 61 ± 2.4 | no measurable gain |
| RealKIE | OCR text only | 0.79 ± 0.07 | 0 ± 0 | **2,524 ± 57** | 15,365 ± 3,022 | $49.2 ± $1.13 | 68 ± 13 | no measurable gain |
| RealKIE | Page image only | 0.77 ± 0.07 | 0 ± 0 | 5,771 ± 45 | 15,308 ± 311 | $49.2 ± $0.54 | 61 ± 6.04 | — |
| Fake-W2 | **Page image and OCR text (baseline)** | 1 ± 0 | **0.82 ± 0.07** | 7,818 ± 8.75 | 18,545 ± 212 | $14.0 ± $0.16 | 39 ± 2.41 | no measurable gain |
| Fake-W2 | OCR text only | 0.91 ± 0.02 | 0.02 ± 0.03 | 7,991 ± 78 | 19,064 ± 90 | $14.3 ± $0.17 | 44 ± 8.78 | no measurable gain |
| Fake-W2 | Page image only | 0.99 ± 0 | 0.5 ± 0.06 | **6,292 ± 32** | **17,051 ± 346** | **$13.6 ± $0.05** | **35 ± 0.39** | — |
| OmniAI, bank checks | **Page image and OCR text (baseline)** | **0.99 ± 0** | 0.38 ± 0.07 | 8,086 ± 51 | 22,032 ± 877 | $14.0 ± $0.32 | 27 ± 0.63 | $0.01 |
| OmniAI, bank checks | OCR text only | 0.89 ± 0.08 | 0.02 ± 0.03 | **5,203 ± 90** | **18,785 ± 435** | $13.2 ± $0.18 | 26 ± 1.25 | — |
| OmniAI, bank checks | Page image only | 0.98 ± 0.01 | 0.3 ± 0.1 | 7,546 ± 190 | 21,146 ± 281 | $13.4 ± $0.22 | 27 ± 0.59 | same cost |

Failed documents per run: OmniAI, bank checks — OCR text only: 0, 0, 1.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Does deterministic table parsing help agentic extraction?

![Table parsing adds cost without adding accuracy: line-item score per document against rows in the list (ground truth)](../../images/benchmark-guidance/EX-07-by-size-realkie-long.svg)

*One point per document per table parsing, a tool that turns Textract's tables into rows by code instead of the model retyping them; each line is its running median.*

**Answer:** Off is a good place to start. On 18 long RealKIE invoices (8 to 33 pages), agentic extraction with table parsing on scored no better than with it off (line-item score 52% against 56%), cost more (at least $257 against $210 per 1,000 invoices) and took longer. Test it on your own long tables.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 6 Test Studio runs (6 failed documents left out of the scores), listed in `benchmarks/results/guidance/EX-07.json`.*

| | |
|---|---|
| **Changes** | Deterministic table parsing (`extraction.agentic.table_parsing.enabled`): a tool that turns Textract's tables into rows by code, instead of the model retyping them |
| **Everything else** | Agentic extraction on Claude Haiku 5.5, with Textract reading tables and layout (the parser reads Textract's tables); the rest as in the [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified, long invoices (19 invoices, 8 to 33 pages)](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages) |
| **Models** | OCR: Amazon Textract (Tables, Layout) · Extraction: Claude Haiku 5.5, agentic · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 18 documents × 3 runs; a document that failed in a run is left out of that run |

*N = 18 documents × 3 runs. Datasets: [RealKIE, long](datasets.md#realkie-fcc-verified-long-invoices-19-invoices-8-to-33-pages). A line-item score is Stickler's score for the list: matched rows over the larger of the true and returned row counts.*

| Dataset | Deterministic table parsing (`extraction.agentic.table_parsing.enabled`): a tool that turns Textract's tables into rows by code, instead of the model retyping them | Line-item score | Weighted accuracy | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Off |
|---|---|---|---|---|---|---|---|
| RealKIE, long | **On (baseline)** | — | 0.76 ± 0.1 | 96,578 ± 42,525 | $257 ± $11.3 | 336 ± 62 | no measurable gain |
| RealKIE, long | Off | — | 0.81 ± 0.08 | 60,188 ± 26,127 | **$210 ± $43.4** | **267 ± 26** | — |

Failed documents per run: On: 1, 1, 1; Off: 1, 1, 1.

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Do schema validation and value coercion improve the output?

![Validation and coercion change nothing measurable; keep them on: weighted accuracy against cost per 1,000 documents, one point per schema validation and value coercion, by extraction model](../../images/benchmark-guidance/EX-10-fields.svg)

*Bands show ±2 standard errors.*

**Answer:** Keep both on; they're a cheap safety net. Validation checks each value against the schema and records what fails. Coercion converts values to the schema's types, for example "$6.00" to 6.0. On RealKIE invoices, accuracy was the same with them on or off for every model, and they added up to 8% to the cost. Coercion changed no value, and validation flagged only Amazon Nova 2 Lite: 5 of 60 sections were missing a required field, which it records but doesn't fix.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 24 Test Studio runs, listed in `benchmarks/results/guidance/EX-10.json`.*

| | |
|---|---|
| **Changes** | `extraction.validation.enabled` and `extraction.coercion.enabled` together: validation checks the extracted values against the schema, and coercion repairs values of the wrong type or format ("$1,234.00" in a number field) by code; off leaves the model's output exactly as returned |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: varies (the setting under test) · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

**Weighted accuracy**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|---|
| Validation and coercion on | 0.81 ± 0.04 | 0.82 ± 0.03 | 0.83 ± 0.04 | 0.76 ± 0.04 |
| Validation and coercion off | 0.81 ± 0.04 | 0.83 ± 0.03 | 0.82 ± 0.04 | 0.75 ± 0.04 |

**Documents with every field right**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|---|
| Validation and coercion on | 0.00 | 0.00 | 0.10 | 0.00 |
| Validation and coercion off | 0.00 | 0.00 | 0.07 ± 0.02 | 0.00 |

**Median seconds per document**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|---|
| Validation and coercion on | 50.89 ± 4.88 | 50.96 ± 3.34 | 45.91 ± 2.46 | 44.92 ± 1.15 |
| Validation and coercion off | 38.09 ± 3.85 | 35.74 ± 0.65 | 39.86 ± 0.10 | 36.31 ± 0.22 |

**Cost per 1,000 documents (USD)**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|---|
| Validation and coercion on | $24.1 ± $0.32 | $87.8 ± $1.35 | $153 ± $1.26 | $26.8 ± $0.60 |
| Validation and coercion off | **$22.9 ± $0.18** | $87.8 ± $0.19 | $149 ± $0.80 | $24.8 ± $0.15 |

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Validation and coercion on* with *Claude Haiku 5.5*. A bold figure beats every other in its table by more than twice the combined standard error.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Should I retry failed fields with a stronger model?

![Escalation never ran: validation never failed: weighted accuracy against cost per 1,000 documents, one point per what a validation failure does](../../images/benchmark-guidance/EX-11-fields.svg)

*Bands show ±2 standard errors.*

*Fake-W2: every value scored 0.99 or higher, too easy to separate.*

**Answer:** Warn, which only records validation failures, is a good place to start. We couldn't measure the retry: validation never failed on these documents, so escalating to Claude Sonnet 5.5 never ran. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 9 Test Studio runs, listed in `benchmarks/results/guidance/EX-11.json`.*

Differences in the table are run-to-run noise.

| | |
|---|---|
| **Changes** | `extraction.validation.fail_action`, escalating to Claude Sonnet 5.5 (`reject` only flags the section, so it cannot change a value) |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: varies (the setting under test) · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N: RealKIE — 20 documents × 3 runs; Fake-W2 — 20 documents × 1–2 runs |

| Dataset | `extraction.validation.fail_action`, escalating to Claude Sonnet 5.5 (`reject` only flags the section, so it cannot change a value) | Weighted accuracy | Documents with every field right | Extraction tokens per page | Total tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | [Cost per error prevented](marginal-cost-of-error.md) vs Warn (recommended) |
|---|---|---|---|---|---|---|---|---|
| RealKIE | **Warn (recommended)** | 0.8 ± 0.07 | 0 ± 0 | 6,780 ± 13 | 16,581 ± 601 | $22.3 ± $0.16 | 41 ± 5.57 | — |
| RealKIE | Escalate to Claude Sonnet 5.5 | 0.81 ± 0.07 | 0 ± 0 | 6,777 ± 13 | 17,193 ± 1,104 | $22.4 ± $0.25 | 46 ± 9.58 | no measurable gain |
| Fake-W2 | **Warn (recommended)** | 1 ± 0 | 0.82 ± 0.05 | **7,423 ± 0.85** | 18,494 ± 109 | **$7.41 ± $0.06** | 43 ± 8.99 | — |
| Fake-W2 | Escalate to Claude Sonnet 5.5 | 1 ± 0 | — | 7,481 ± 0 | **18,362 ± 0** | $7.59 | **34 ± 0** | — |

*A bold figure beats every other in its column, for that dataset, by more than twice the combined standard error. The bold name is the value the [baseline setup](index.md#how-we-run-a-benchmark) uses.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Does schema enforcement improve accuracy or cost?

![The forced tool costs W-2 accuracy; structured output matches the prompt: weighted accuracy against cost per 1,000 documents, one point per schema enforcement, by model](../../images/benchmark-guidance/EX-12-fields.svg)

*Bands show ±2 standard errors. Cost axes differ by dataset.*

**Answer:** Structured output, or the schema in the prompt, is a good place to start; structured output isn't in a release yet ([#1785](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1785)). Avoid the forced tool: with Claude Haiku 5.5 it lowered W-2 accuracy from 0.995 to 0.92. Test it on your own documents.

<details>
<summary>Details</summary>

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: 42 Test Studio runs, listed in `benchmarks/results/guidance/EX-12.json`.*

Structured output scored the same as the schema in the prompt on RealKIE invoices with Claude Haiku 5.5 and Claude Sonnet 5.5. On W-2 forms it never applied: the schema has 28 optional fields and structured output allows 12, so the accelerator fell back to the prompt.

| | |
|---|---|
| **Changes** | Schema enforcement (`extraction.schema_enforcement.mode`) |
| **Everything else** | The [baseline setup](index.md#how-we-run-a-benchmark) |
| **Datasets** | [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Layout) · Extraction: varies (the setting under test) · Scoring: Claude Haiku 5.5. |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

**RealKIE: weighted accuracy**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | 0.83 ± 0.03 | 0.82 ± 0.04 | 0.76 ± 0.04 |
| Forced tool | 0.82 ± 0.03 | — | 0.67 ± 0.05 |
| Structured output | 0.80 ± 0.04 | 0.83 ± 0.03 | — |

**RealKIE: documents with every field right**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | 0.03 ± 0.02 | 0.03 ± 0.02 | 0.00 |
| Forced tool | 0.00 | — | 0.00 |
| Structured output | 0.00 | 0.00 | — |

**RealKIE: cost per 1,000 documents (USD)**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | $25.6 ± $1.05 | $90.3 ± $1.70 | $24.3 ± $0.22 |
| Forced tool | $22.1 ± $0.09 | — | $22.0 ± $0.10 |
| Structured output | $22.0 ± $0.09 | $85.4 ± $1.37 | — |

**Fake-W2: weighted accuracy**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | 1.00 ± 0.00 | 1.00 ± 0.00 | 0.86 ± 0.01 |
| Forced tool | 0.92 ± 0.01 | — | 0.83 ± 0.01 |
| Structured output | 1.00 ± 0.00 | 1.00 ± 0.00 | — |

**Fake-W2: documents with every field right**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | 0.78 ± 0.02 | 0.80 | 0.00 |
| Forced tool | 0.02 ± 0.02 | — | 0.00 |
| Structured output | 0.75 ± 0.03 | 0.78 ± 0.02 | — |

**Fake-W2: cost per 1,000 documents (USD)**

| | Claude Haiku 5.5 | Claude Sonnet 5.5 | Amazon Nova 2 Lite |
|---|---|---|---|
| Schema in prompt | $7.61 ± $0.09 | $20.7 ± $0.13 | $8.51 ± $0.00 |
| Forced tool | $7.42 ± $0.04 | — | $8.80 ± $0.02 |
| Structured output | $7.51 ± $0.05 | $20.0 ± $0.05 | — |

*The [baseline setup](index.md#how-we-run-a-benchmark) is *Structured output* with *Claude Haiku 5.5*.*

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-18. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

</details>

### Planned

We haven't run these yet.

- How much do field descriptions matter? (EX-04)
- Do few-shot examples help extraction? (EX-05)
- How should I extract sections that hold several records? (EX-13)
- How much do splitting errors cost extraction in a packet? (EX-15)

<!-- END GENERATED FAQ -->
