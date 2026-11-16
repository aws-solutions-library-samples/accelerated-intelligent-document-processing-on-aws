---
title: "Confidence Assessment Benchmarks"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Confidence Assessment Benchmarks

Confidence assessment gives every extracted value a score, so a person reviews the values
most likely to be wrong instead of every value.
[Confidence Assessment](../idp-fundamentals/confidence-assessment.md) explains how it works.

## How to improve confidence on your documents

**Your definitions.** The scoring model reads each field's description when it judges a
value. Say what a right value looks like. Mark the fields you can't do without as required:
an empty required field goes to review even though it has no score. See
[Improving confidence](../idp-fundamentals/confidence-assessment.md#improving-confidence-once-your-pipeline-is-running).

**Data quality.** Poor scans cause more extraction mistakes, and catching those mistakes is
the job confidence does. We measure it on [ConfBench](../extensions/confbench-testset.md),
which adds scan noise to real invoices.

**Settings.**

| Setting | What it changes | Questions |
|---|---|---|
| Scoring mode | A separate scoring pass, scores inside the extraction call, or none | [CF-03](#separate-scoring-pass-or-integrated-into-extraction) |
| Scoring model | The model that scores each value | [CF-02](#which-scoring-model-should-i-use) |
| Confidence threshold | The score below which a value goes to review | [CF-01](#can-i-route-human-review-by-confidence), [CF-04](#what-confidence-threshold-should-i-set) |
| Page images | Whether the scoring model sees the pages | [CF-06](#does-the-scoring-model-need-page-images) |
| List batch size | How many list rows the scoring model scores in one request | [CF-07](#how-should-long-lists-be-scored) |

## Questions

<!-- BEGIN GENERATED FAQ -->

### Can I route human review by confidence?

**Answer:** Not yet measured. [Details: CF-01](#cf-01-can-i-route-human-review-by-confidence)

### Which scoring model should I use?

**Answer:** Claude Haiku 5.5, or Claude Sonnet 5.5 for the most accuracy on invoices. On W-2 forms both caught every error when a reviewer checks the 30% of values with the lowest confidence (AUROC 0.98 and 0.99), at about the default's cost for Claude Haiku 5.5. On RealKIE invoices Claude Sonnet 5.5 caught 87% of errors that way (AUROC 0.92) against 66% for Claude Haiku 5.5 (0.81), for about $104 more per 1,000 invoices. The default, Amazon Nova Lite, caught 14% to 19%: fewer than reviewing values at random (30%). [Details: CF-02](#cf-02-which-scoring-model-should-i-use)

![Claude Haiku 5.5 catches errors; the default doesn't: AUROC and errors caught reviewing 30% of values against cost per 1,000 documents, one point per scoring model](../../images/benchmark-guidance/CF-02-fields.svg)

*AUROC and errors caught reviewing 30% of values against cost per 1,000 documents, one point per scoring model. Bands show ±2 standard errors. Dashed lines mark coin flip (50%, no better than chance) and random review (what a review of a random 30% of values catches).* N = 20 documents × 3 runs. Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). [Details: CF-02](#cf-02-which-scoring-model-should-i-use). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### Separate scoring pass or integrated into extraction?

**Answer:** Separate, with a scoring model that works, or off. On W-2 forms integrated scoring separated right from wrong values better than the default separate pass with Amazon Nova Lite (AUROC 0.76 against 0.47) and used 36% fewer tokens per page, but it lowered extraction accuracy from 0.96 to 0.92. Turning confidence off changed no extracted value and cut tokens per page by 55% to 65%. On RealKIE invoices integrated ran as a separate pass, because in single-pass extraction the accelerator scores a class with list fields separately. For confidence you can act on, use a separate pass with Claude Haiku 5.5 or Claude Sonnet 5 (see CF-02). [Details: CF-03](#cf-03-separate-scoring-pass-or-integrated-into-extraction)

![Integrated scoring catches more W-2 errors but lowers accuracy: weighted accuracy and errors caught reviewing 30% of values against cost per 1,000 documents, one point per confidence mode](../../images/benchmark-guidance/CF-03-fields.svg)

*Weighted accuracy and errors caught reviewing 30% of values against cost per 1,000 documents, one point per confidence mode. Bands show ±2 standard errors. The dashed line marks random review (what a review of a random 30% of values catches).* N = 20 documents × 3 runs. Datasets: [RealKIE](datasets.md#realkie-fcc-verified-20-invoices), [Fake-W2](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields). [Details: CF-03](#cf-03-separate-scoring-pass-or-integrated-into-extraction). Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13.

### What confidence threshold should I set?

**Answer:** Not yet measured. [Details: CF-04](#cf-04-what-confidence-threshold-should-i-set)

### Does classification confidence flag the wrong document class?

**Answer:** Not yet measured. [Details: CF-05](#cf-05-does-classification-confidence-flag-the-wrong-document-class)

### Does the scoring model need page images?

**Answer:** Not yet measured. [Details: CF-06](#cf-06-does-the-scoring-model-need-page-images)

### How should long lists be scored?

**Answer:** Not yet measured. [Details: CF-07](#cf-07-how-should-long-lists-be-scored)

<!-- END GENERATED FAQ -->

## Metrics on this page

- *AUROC:* the chance that a wrong value scores lower than a right one. 0.5 is a coin flip.
- *Calibration error:* the gap between the scores and the measured accuracy.
- *Values below threshold:* the share of values the threshold sends to review.
- *Errors below threshold:* the share of wrong values the threshold sends to review.
- *Errors caught reviewing 30% of values:* the share of wrong values a reviewer finds by
  checking the 30% of values with the lowest scores. Checking 30% picked at random finds
  about 30%, which the charts draw as a dashed line labeled "random review".

[Benchmark Datasets](datasets.md) describes each dataset.

## Benchmark details

<!-- BEGIN GENERATED ABLATIONS -->

#### CF-01: Can I route human review by confidence?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Scoring model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages) |
| **Runs** | 3 per document, per value |

| Dataset | Scoring model | AUROC | Calibration error | Values below threshold | Errors below threshold |
|---|---|---|---|---|---|
| ConfBench | Amazon Nova Lite (default) | — | — | — | — |
| ConfBench | Claude Sonnet 5 | — | — | — | — |
| RealKIE | Amazon Nova Lite (default) | — | — | — | — |
| RealKIE | Claude Sonnet 5 | — | — | — | — |

| Dataset | Scoring model | AUROC | Calibration error | Values below threshold | Errors below threshold |
|---|---|---|---|---|---|
| Statement, 400 rows | Amazon Nova Lite (default) | — | — | — | — |
| Statement, 400 rows | Claude Sonnet 5 | — | — | — | — |


#### CF-02: Which scoring model should I use?

**Answer:** Claude Haiku 5.5, or Claude Sonnet 5.5 for the most accuracy on invoices. On W-2 forms both caught every error when a reviewer checks the 30% of values with the lowest confidence (AUROC 0.98 and 0.99), at about the default's cost for Claude Haiku 5.5. On RealKIE invoices Claude Sonnet 5.5 caught 87% of errors that way (AUROC 0.92) against 66% for Claude Haiku 5.5 (0.81), for about $104 more per 1,000 invoices. The default, Amazon Nova Lite, caught 14% to 19%: fewer than reviewing values at random (30%).

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-181724, RealKIE-FCC-Verified-20261113-181722, RealKIE-FCC-Verified-20261113-181723, RealKIE-FCC-Verified-20261113-182034, RealKIE-FCC-Verified-20261113-182041, RealKIE-FCC-Verified-20261113-182113, RealKIE-FCC-Verified-20261113-182352, RealKIE-FCC-Verified-20261113-182430, RealKIE-FCC-Verified-20261113-182432, RealKIE-FCC-Verified-20261113-182712, RealKIE-FCC-Verified-20261113-182736, RealKIE-FCC-Verified-20261113-182750, RealKIE-FCC-Verified-20261116-145733, RealKIE-FCC-Verified-20261116-145735, RealKIE-FCC-Verified-20261116-145734, Fake-W2-Tax-Forms-20261113-213105, Fake-W2-Tax-Forms-20261113-213106, Fake-W2-Tax-Forms-20261113-213107, Fake-W2-Tax-Forms-20261113-213108, Fake-W2-Tax-Forms-20261113-213109, Fake-W2-Tax-Forms-20261113-213110, Fake-W2-Tax-Forms-20261113-213111, Fake-W2-Tax-Forms-20261113-213117, Fake-W2-Tax-Forms-20261113-213119, Fake-W2-Tax-Forms-20261113-213118, Fake-W2-Tax-Forms-20261113-213120, Fake-W2-Tax-Forms-20261113-213121, Fake-W2-Tax-Forms-20261116-145735, Fake-W2-Tax-Forms-20261116-145733, Fake-W2-Tax-Forms-20261116-145734`.*

| | |
|---|---|
| **Changes** | Scoring model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: Claude Sonnet 5, single-pass · Confidence: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Scoring model | AUROC | Errors caught reviewing 30% of values | Calibration error | Values below threshold | Errors below threshold | Scoring tokens per page | Cost per 1,000 documents (USD) |
|---|---|---|---|---|---|---|---|---|
| ConfBench | Amazon Nova Lite (default) | — | — | — | — | — | — | — |
| ConfBench | Amazon Nova 2 Lite | — | — | — | — | — | — | — |
| ConfBench | Claude Haiku 5.5 | — | — | — | — | — | — | — |
| ConfBench | Claude Sonnet 5.5 | — | — | — | — | — | — | — |
| RealKIE | Amazon Nova Lite (default) | 0.42 ± 0.07 | 0.14 ± 0.11 | 0.17 ± 0.02 | — | — | 14,079 ± 166 | $143 |
| RealKIE | Amazon Nova 2 Lite | 0.61 ± 0.13 | 0.46 ± 0.13 | 0.18 ± 0.05 | — | — | 5,290 ± 331 | $139 |
| RealKIE | Claude Haiku 5.5 | 0.81 ± 0.07 | 0.66 ± 0.19 | 0.1 ± 0.03 | — | — | 13,193 ± 1,006 | $140 |
| RealKIE | Claude Sonnet 5.5 | 0.92 ± 0.01 | 0.87 ± 0.06 | 0.11 ± 0.04 | — | — | 12,244 ± 318 | $245 ± $0.92 |
| Fake-W2 | Amazon Nova Lite (default) | 0.47 ± 0.06 | 0.18 ± 0.07 | 0.04 ± 0.01 | — | — | 61,579 ± 106,479 | $32.3 ± $0.10 |
| Fake-W2 | Amazon Nova 2 Lite | 0.88 ± 0.09 | 0.78 ± 0.19 | 0.02 ± 0.01 | — | — | 83,245 ± 77,145 | $34.9 ± $0.07 |
| Fake-W2 | Claude Haiku 5.5 | 0.98 ± 0 | 1 ± 0 | 0.06 ± 0 | — | — | 215,723 ± 1,227 | $34.1 ± $0.30 |
| Fake-W2 | Claude Sonnet 5.5 | 0.99 ± 0 | 1 ± 0 | 0.02 ± 0.01 | — | — | 8,720 ± 25 | $61.9 ± $0.29 |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### CF-03: Separate scoring pass or integrated into extraction?

**Answer:** Separate, with a scoring model that works, or off. On W-2 forms integrated scoring separated right from wrong values better than the default separate pass with Amazon Nova Lite (AUROC 0.76 against 0.47) and used 36% fewer tokens per page, but it lowered extraction accuracy from 0.96 to 0.92. Turning confidence off changed no extracted value and cut tokens per page by 55% to 65%. On RealKIE invoices integrated ran as a separate pass, because in single-pass extraction the accelerator scores a class with list fields separately. For confidence you can act on, use a separate pass with Claude Haiku 5.5 or Claude Sonnet 5 (see CF-02).

*Measured on release 0.6.15.dev1, November 2026, us-east-1. Source: `Test Studio runs RealKIE-FCC-Verified-20261113-202145, RealKIE-FCC-Verified-20261113-202146, RealKIE-FCC-Verified-20261113-202147, RealKIE-FCC-Verified-20261113-202440, RealKIE-FCC-Verified-20261113-202454, RealKIE-FCC-Verified-20261113-202607, RealKIE-FCC-Verified-20261113-202740, RealKIE-FCC-Verified-20261113-202800, RealKIE-FCC-Verified-20261113-202905, Fake-W2-Tax-Forms-20261113-203253, Fake-W2-Tax-Forms-20261113-203254, Fake-W2-Tax-Forms-20261113-203255, Fake-W2-Tax-Forms-20261113-203428, Fake-W2-Tax-Forms-20261113-203420, Fake-W2-Tax-Forms-20261113-203440, Fake-W2-Tax-Forms-20261113-203545, Fake-W2-Tax-Forms-20261113-203554, Fake-W2-Tax-Forms-20261113-203604`.*

| | |
|---|---|
| **Changes** | Confidence mode |
| **Everything else** | Single-pass (Simple) extraction; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Models** | OCR: Amazon Textract (Tables, Layout, Signatures) · Extraction: Claude Sonnet 5, single-pass · Confidence: varies (the setting under test). |
| **Runs** | 3 per document, per value; N = 20 documents × 3 runs |

| Dataset | Confidence mode | Weighted accuracy | AUROC | Errors caught reviewing 30% of values | Scoring tokens per page | Cost per 1,000 documents (USD) | Median seconds per document | MCoE vs Off |
|---|---|---|---|---|---|---|---|---|
| ConfBench | Separate (default) | — | — | — | — | — | — | — |
| ConfBench | Integrated | — | — | — | — | — | — | — |
| ConfBench | Off | — | — | — | — | — | — | — |
| RealKIE | Separate (default) | 0.81 ± 0.07 | 0.42 ± 0.02 | 0.18 ± 0.03 | 13,240 ± 1,063 | $137 | 36 ± 4.47 | no measurable gain |
| RealKIE | Integrated | 0.81 ± 0.06 | 0.41 ± 0.04 | 0.18 ± 0.09 | 13,206 ± 600 | $135 | 32 ± 3.84 | no measurable gain |
| RealKIE | Off | 0.8 ± 0.06 | — | — | — | $134 | 23 ± 0.76 | — |
| Fake-W2 | Separate (default) | 0.96 ± 0.02 | 0.47 ± 0.03 | 0.13 ± 0.05 | 8,319 ± 1.49 | $35.3 | 28 ± 0.71 | no measurable gain |
| Fake-W2 | Integrated | 0.92 ± 0.02 | 0.76 ± 0.01 | 0.47 ± 0.05 | — | $61.5 | 33 ± 0.67 | — |
| Fake-W2 | Off | 0.96 ± 0.02 | — | — | — | $31.8 | 17 ± 0.23 | — |

| Dataset | Confidence mode | List recall | AUROC | Scoring tokens per page | Cost per 1,000 documents (USD) | Median seconds per document |
|---|---|---|---|---|---|---|
| Statement, 800 rows | Separate (default) | — | — | — | — | — |
| Statement, 800 rows | Integrated | — | — | — | — | — |
| Statement, 800 rows | Off | — | — | — | — | — |

*Costs use on-demand prices for us-east-1 from [config_library/pricing.yaml](../../config_library/pricing.yaml) as of 2026-11-13. Amazon Bedrock and Amazon Textract only; Lambda and other AWS charges are not included.*

*Costs come from each value's first, cold-cache run: repeat runs reuse the prompt cache the first run wrote, which a document processed once would not.*


#### CF-04: What confidence threshold should I set?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Confidence threshold |
| **Everything else** | Computed from the CF-02 runs, per scoring model; the rest as each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [RealKIE-FCC-Verified (20 invoices)](datasets.md#realkie-fcc-verified-20-invoices) |
| **Runs** | None: computed from the CF-02 runs |

| Dataset | Confidence threshold | Values below threshold | Errors below threshold |
|---|---|---|---|
| ConfBench | 0.5 | — | — |
| ConfBench | 0.7 | — | — |
| ConfBench | 0.8 (default) | — | — |
| ConfBench | 0.9 | — | — |
| ConfBench | 0.95 | — | — |
| RealKIE | 0.5 | — | — |
| RealKIE | 0.7 | — | — |
| RealKIE | 0.8 (default) | — | — |
| RealKIE | 0.9 | — | — |
| RealKIE | 0.95 | — | — |


#### CF-05: Does classification confidence flag the wrong document class?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Classification model |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [DocSplit-Poly-Seq (20 packets)](datasets.md#docsplit-poly-seq-20-packets) |
| **Runs** | 3 per document, per value |

| Dataset | Classification model | Label accuracy | AUROC | Values below threshold | Errors below threshold | Classification tokens per page |
|---|---|---|---|---|---|---|
| DocSplit | Amazon Nova 2 Lite (default) | — | — | — | — | — |
| DocSplit | Claude Haiku 5.5 | — | — | — | — | — |
| DocSplit | Claude Sonnet 5 | — | — | — | — | — |


#### CF-06: Does the scoring model need page images?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | Page images in the scoring prompt |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [ConfBench (RealKIE invoices with scan noise)](datasets.md#confbench-realkie-invoices-with-scan-noise); [Fake-W2-Tax-Forms (W-2 forms, 45 fields)](datasets.md#fake-w2-tax-forms-w-2-forms-45-fields) |
| **Runs** | 3 per document, per value |

| Dataset | Page images in the scoring prompt | AUROC | Calibration error | Scoring tokens per page |
|---|---|---|---|---|
| ConfBench | Text and images (default) | — | — | — |
| ConfBench | Text only | — | — | — |
| Fake-W2 | Text and images (default) | — | — | — |
| Fake-W2 | Text only | — | — | — |


#### CF-07: How should long lists be scored?

**Answer:** Not yet measured.

| | |
|---|---|
| **Changes** | `extraction.confidence.list_batch_size` |
| **Everything else** | As each dataset's [preset](index.md#how-we-run-a-benchmark) sets it |
| **Datasets** | [Synthetic statement, 400 rows (9 pages)](datasets.md#synthetic-statement-400-rows-9-pages); [Synthetic statement, 800 rows (17 pages)](datasets.md#synthetic-statement-800-rows-17-pages); [Synthetic statement, 1,600 rows (33 pages)](datasets.md#synthetic-statement-1600-rows-33-pages) |
| **Runs** | 3 per document, per value |

| Dataset | `extraction.confidence.list_batch_size` | List recall | AUROC | Scoring tokens per page | Median seconds per document |
|---|---|---|---|---|---|
| Statement, 400 rows | 25 (default) | — | — | — | — |
| Statement, 400 rows | 12 | — | — | — | — |
| Statement, 400 rows | 50 | — | — | — | — |
| Statement, 800 rows | 25 (default) | — | — | — | — |
| Statement, 800 rows | 12 | — | — | — | — |
| Statement, 800 rows | 50 | — | — | — | — |
| Statement, 1,600 rows | 25 (default) | — | — | — | — |
| Statement, 1,600 rows | 12 | — | — | — | — |
| Statement, 1,600 rows | 50 | — | — | — | — |

<!-- END GENERATED ABLATIONS -->
