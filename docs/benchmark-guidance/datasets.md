---
title: "Benchmark Datasets"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Benchmark Datasets

These are the datasets behind the benchmarks on these pages. Each runs as a Test Studio
test set on the benchmark stack, starting from the configuration preset built for it.
[Benchmark Datasets](../idp-fundamentals/benchmark-datasets.md) in IDP Fundamentals
describes where each dataset comes from and how its ground truth was made.

<!-- BEGIN GENERATED DATASETS -->

## Datasets we run

### Synthetic statement, 100 rows (3 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bank-statement-sample`
- **Test Studio test set:** not on the benchmark stack yet

### Synthetic statement, 400 rows (9 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-400`, 5 documents per run
- **Used by:** [TR-01](index.md#tr-01-how-much-do-results-vary-between-identical-runs), [TR-02](index.md#tr-02-how-long-does-a-document-take-and-how-many-documents-an-hour), [DG-01](digitization.md#dg-01-does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-02](digitization.md#dg-02-if-i-read-pages-with-a-vision-model-which-model), [DG-04](digitization.md#dg-04-what-resolution-should-pages-be-rendered-at), [DG-06](digitization.md#dg-06-which-textract-features-do-i-need), [SC-10](splitting-and-classification.md#sc-10-if-every-file-is-one-document-should-i-turn-splitting-off), [EX-02](extraction.md#ex-02-which-extraction-model-should-i-use), [EX-03](extraction.md#ex-03-does-more-reasoning-effort-improve-extraction), [EX-07](extraction.md#ex-07-does-deterministic-table-parsing-help-agentic-extraction), [EX-09](extraction.md#ex-09-should-agentic-extraction-load-page-images-only-when-it-needs-them), [CF-01](confidence-assessment.md#cf-01-can-i-route-human-review-by-confidence), [CF-07](confidence-assessment.md#cf-07-how-should-long-lists-be-scored), [TU-01](token-use.md#tu-01-where-do-the-tokens-go), [TU-02](token-use.md#tu-02-how-much-does-prompt-caching-save-in-extraction), [TU-05](token-use.md#tu-05-how-many-tokens-does-each-extraction-model-use-for-the-same-document)

### Synthetic statement, 800 rows (17 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-800`, 5 documents per run
- **Used by:** [SC-10](splitting-and-classification.md#sc-10-if-every-file-is-one-document-should-i-turn-splitting-off), [EX-07](extraction.md#ex-07-does-deterministic-table-parsing-help-agentic-extraction), [EX-08](extraction.md#ex-08-what-shard-size-should-agentic-extraction-use), [EX-09](extraction.md#ex-09-should-agentic-extraction-load-page-images-only-when-it-needs-them), [CF-03](confidence-assessment.md#cf-03-separate-scoring-pass-or-integrated-into-extraction), [CF-07](confidence-assessment.md#cf-07-how-should-long-lists-be-scored)

### Synthetic statement, 1,600 rows (33 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-1600`, 5 documents per run
- **Used by:** [CF-07](confidence-assessment.md#cf-07-how-should-long-lists-be-scored)

### Synthetic statement, 3,200 rows (66 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bank-statement-sample`
- **Test Studio test set:** not on the benchmark stack yet
- **Used by:** [EX-07](extraction.md#ex-07-does-deterministic-table-parsing-help-agentic-extraction), [EX-08](extraction.md#ex-08-what-shard-size-should-agentic-extraction-use)

### Synthetic file holding two statements

- **About the dataset:** [Synthetic file holding two statements](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bank-statement-sample`
- **Test Studio test set:** not on the benchmark stack yet
- **Used by:** [EX-13](extraction.md#ex-13-how-should-i-extract-sections-that-hold-several-records)

### RealKIE-FCC-Verified (20 invoices)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-verified`, 20 documents per run
- **Used by:** [TR-01](index.md#tr-01-how-much-do-results-vary-between-identical-runs), [TR-02](index.md#tr-02-how-long-does-a-document-take-and-how-many-documents-an-hour), [DG-01](digitization.md#dg-01-does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-02](digitization.md#dg-02-if-i-read-pages-with-a-vision-model-which-model), [DG-03](digitization.md#dg-03-does-a-specialized-ocr-model-beat-textract), [DG-04](digitization.md#dg-04-what-resolution-should-pages-be-rendered-at), [DG-05](digitization.md#dg-05-does-image-preprocessing-help-on-poor-scans), [DG-06](digitization.md#dg-06-which-textract-features-do-i-need), [SC-10](splitting-and-classification.md#sc-10-if-every-file-is-one-document-should-i-turn-splitting-off), [EX-01](extraction.md#ex-01-when-is-agentic-extraction-worth-it), [EX-02](extraction.md#ex-02-which-extraction-model-should-i-use), [EX-03](extraction.md#ex-03-does-more-reasoning-effort-improve-extraction), [EX-04](extraction.md#ex-04-how-much-do-field-descriptions-matter), [EX-05](extraction.md#ex-05-do-few-shot-examples-help-extraction), [EX-06](extraction.md#ex-06-does-sending-page-images-to-the-extraction-model-help), [EX-09](extraction.md#ex-09-should-agentic-extraction-load-page-images-only-when-it-needs-them), [EX-10](extraction.md#ex-10-do-schema-validation-and-value-coercion-improve-the-output), [EX-11](extraction.md#ex-11-should-i-retry-failed-fields-with-a-stronger-model), [EX-12](extraction.md#ex-12-does-structured-output-make-single-pass-extraction-more-accurate), [CF-01](confidence-assessment.md#cf-01-can-i-route-human-review-by-confidence), [CF-02](confidence-assessment.md#cf-02-which-scoring-model-should-i-use), [CF-03](confidence-assessment.md#cf-03-separate-scoring-pass-or-integrated-into-extraction), [CF-04](confidence-assessment.md#cf-04-what-confidence-threshold-should-i-set), [TU-01](token-use.md#tu-01-where-do-the-tokens-go), [TU-02](token-use.md#tu-02-how-much-does-prompt-caching-save-in-extraction), [TU-03](token-use.md#tu-03-what-do-page-images-cost-in-tokens-step-by-step), [TU-04](token-use.md#tu-04-what-is-the-cheapest-configuration-that-keeps-accuracy), [TU-05](token-use.md#tu-05-how-many-tokens-does-each-extraction-model-use-for-the-same-document)

### RealKIE-FCC-Verified, all 75 invoices

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-verified`, 75 documents per run
- **Used by:** [EX-01](extraction.md#ex-01-when-is-agentic-extraction-worth-it)

### RealKIE-FCC-Verified, long invoices (19 invoices, 8 to 33 pages)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-long`, 19 documents per run
- **Used by:** [EX-01](extraction.md#ex-01-when-is-agentic-extraction-worth-it), [EX-12](extraction.md#ex-12-does-structured-output-make-single-pass-extraction-more-accurate)

### RealKIE-FCC-Verified, short invoices (24 invoices, 1 or 2 pages)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-short`, 24 documents per run
- **Used by:** [EX-01](extraction.md#ex-01-when-is-agentic-extraction-worth-it)

### ConfBench (RealKIE invoices with scan noise)

- **About the dataset:** [ConfBench](../extensions/confbench-testset.md)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** not on the benchmark stack yet
- **Used by:** [DG-01](digitization.md#dg-01-does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-02](digitization.md#dg-02-if-i-read-pages-with-a-vision-model-which-model), [DG-03](digitization.md#dg-03-does-a-specialized-ocr-model-beat-textract), [DG-04](digitization.md#dg-04-what-resolution-should-pages-be-rendered-at), [DG-05](digitization.md#dg-05-does-image-preprocessing-help-on-poor-scans), [DG-07](digitization.md#dg-07-how-much-accuracy-does-scan-quality-cost), [EX-06](extraction.md#ex-06-does-sending-page-images-to-the-extraction-model-help), [CF-01](confidence-assessment.md#cf-01-can-i-route-human-review-by-confidence), [CF-02](confidence-assessment.md#cf-02-which-scoring-model-should-i-use), [CF-03](confidence-assessment.md#cf-03-separate-scoring-pass-or-integrated-into-extraction), [CF-04](confidence-assessment.md#cf-04-what-confidence-threshold-should-i-set), [CF-06](confidence-assessment.md#cf-06-does-the-scoring-model-need-page-images)

### OmniAI-OCR-Benchmark, bank checks (20)

- **About the dataset:** [OmniAI-OCR-Benchmark](../idp-fundamentals/benchmark-datasets.md#omniai-ocr-benchmark)
- **Preset:** `ocr-benchmark`
- **Test Studio test set:** `ocr-benchmark`, 20 documents per run
- **Used by:** [DG-01](digitization.md#dg-01-does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-03](digitization.md#dg-03-does-a-specialized-ocr-model-beat-textract)

### OmniAI-OCR-Benchmark, all 9 formats (293 documents)

- **About the dataset:** [OmniAI-OCR-Benchmark](../idp-fundamentals/benchmark-datasets.md#omniai-ocr-benchmark)
- **Preset:** `ocr-benchmark`
- **Test Studio test set:** `ocr-benchmark`, 293 documents per run
- **Used by:** [EX-14](extraction.md#ex-14-how-accurate-is-extraction-across-many-document-formats)

### Fake-W2-Tax-Forms (W-2 forms, 45 fields)

- **About the dataset:** [Fake-W2-Tax-Forms](../idp-fundamentals/benchmark-datasets.md#fake-w2-tax-forms)
- **Preset:** `fake-w2`
- **Test Studio test set:** `fake-w2`, 20 documents per run
- **Used by:** [DG-06](digitization.md#dg-06-which-textract-features-do-i-need), [EX-01](extraction.md#ex-01-when-is-agentic-extraction-worth-it), [EX-02](extraction.md#ex-02-which-extraction-model-should-i-use), [EX-03](extraction.md#ex-03-does-more-reasoning-effort-improve-extraction), [EX-04](extraction.md#ex-04-how-much-do-field-descriptions-matter), [EX-05](extraction.md#ex-05-do-few-shot-examples-help-extraction), [EX-06](extraction.md#ex-06-does-sending-page-images-to-the-extraction-model-help), [EX-10](extraction.md#ex-10-do-schema-validation-and-value-coercion-improve-the-output), [EX-11](extraction.md#ex-11-should-i-retry-failed-fields-with-a-stronger-model), [EX-12](extraction.md#ex-12-does-structured-output-make-single-pass-extraction-more-accurate), [EX-13](extraction.md#ex-13-how-should-i-extract-sections-that-hold-several-records), [CF-02](confidence-assessment.md#cf-02-which-scoring-model-should-i-use), [CF-03](confidence-assessment.md#cf-03-separate-scoring-pass-or-integrated-into-extraction), [CF-06](confidence-assessment.md#cf-06-does-the-scoring-model-need-page-images), [TU-04](token-use.md#tu-04-what-is-the-cheapest-configuration-that-keeps-accuracy)

### DocSplit-Poly-Seq (20 packets)

- **About the dataset:** [DocSplit-Poly-Seq](../idp-fundamentals/benchmark-datasets.md#docsplit-poly-seq)
- **Preset:** `docsplit`
- **Test Studio test set:** `docsplit`, 20 documents per run
- **Used by:** [TR-01](index.md#tr-01-how-much-do-results-vary-between-identical-runs), [SC-11](splitting-and-classification.md#sc-11-does-holistic-classification-beat-page-by-page-classification), [SC-02](splitting-and-classification.md#sc-02-do-i-need-page-images-in-the-classification-prompt), [SC-03](splitting-and-classification.md#sc-03-which-classification-model-should-i-use), [SC-04](splitting-and-classification.md#sc-04-how-long-a-packet-can-holistic-classification-handle), [SC-05](splitting-and-classification.md#sc-05-does-neighboring-page-context-help-page-by-page-classification), [SC-06](splitting-and-classification.md#sc-06-do-few-shot-examples-help-classification), [SC-07](splitting-and-classification.md#sc-07-how-much-do-class-descriptions-matter), [SC-08](splitting-and-classification.md#sc-08-does-accuracy-drop-as-i-add-document-classes), [SC-09](splitting-and-classification.md#sc-09-how-small-can-classification-images-be), [CF-05](confidence-assessment.md#cf-05-does-classification-confidence-flag-the-wrong-document-class), [TU-01](token-use.md#tu-01-where-do-the-tokens-go), [TU-02](token-use.md#tu-02-how-much-does-prompt-caching-save-in-extraction), [TU-03](token-use.md#tu-03-what-do-page-images-cost-in-tokens-step-by-step)

## Datasets we haven't built yet

### Long packets, 30-60 pages (to be built)

- **Used by:** [SC-04](splitting-and-classification.md#sc-04-how-long-a-packet-can-holistic-classification-handle)

### Same-class documents back to back (to be built)

- **Used by:** [SC-05](splitting-and-classification.md#sc-05-does-neighboring-page-context-help-page-by-page-classification)

### Packets with section and field ground truth (to be built)

- **Used by:** [EX-15](extraction.md#ex-15-how-much-do-splitting-errors-cost-extraction-in-a-packet)

### Handwritten forms (to be built)

- **Used by:** [DG-08](digitization.md#dg-08-which-reader-handles-handwriting-best)

### Documents with expert rule verdicts (to be built)

- **Used by:** [RV-01](rule-validation.md#rv-01-how-accurate-are-rule-verdicts), [RV-02](rule-validation.md#rv-02-language-model-or-z3-for-exact-rules), [RV-03](rule-validation.md#rv-03-are-rules-drafted-by-policy-discovery-as-good-as-hand-written-rules), [RV-04](rule-validation.md#rv-04-how-large-should-rule-validation-chunks-be)

<!-- END GENERATED DATASETS -->
