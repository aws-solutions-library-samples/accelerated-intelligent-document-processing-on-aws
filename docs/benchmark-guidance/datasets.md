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
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-100`, 5 documents per run
- **Used by:** [EX-08](extraction.md#does-single-pass-extraction-return-every-row-of-a-long-list)

### Synthetic statement, 400 rows (9 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-400`, 5 documents per run
- **Used by:** [EX-08](extraction.md#does-single-pass-extraction-return-every-row-of-a-long-list)

### Synthetic statement, 800 rows (17 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-800`, 5 documents per run
- **Used by:** [EX-08](extraction.md#does-single-pass-extraction-return-every-row-of-a-long-list)

### Synthetic statement, 1,600 rows (33 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-1600`, 5 documents per run
- **Used by:** [EX-08](extraction.md#does-single-pass-extraction-return-every-row-of-a-long-list)

### Synthetic statement, 3,200 rows (66 pages)

- **About the dataset:** [Synthetic statement](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bench-synth-statements`
- **Test Studio test set:** `synth-statements-3200`, 5 documents per run
- **Used by:** [EX-08](extraction.md#does-single-pass-extraction-return-every-row-of-a-long-list)

### Synthetic file holding two statements

- **About the dataset:** [Synthetic file holding two statements](../idp-fundamentals/benchmark-datasets.md#synthetic-bank-statements)
- **Preset:** `bank-statement-sample`
- **Test Studio test set:** not on the benchmark stack yet

### RealKIE-FCC-Verified (20 invoices)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-verified`, 20 documents per run
- **Used by:** [TR-01](index.md#planned) (planned), [DG-01](digitization.md#does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-02](digitization.md#planned) (planned), [DG-03](digitization.md#planned) (planned), [DG-04](digitization.md#what-resolution-should-pages-be-rendered-at), [DG-05](digitization.md#planned) (planned), [DG-06](digitization.md#which-textract-features-do-i-need), [SC-10](splitting-and-classification.md#planned) (planned), [EX-02](extraction.md#which-extraction-model-should-i-use), [EX-03](extraction.md#does-more-reasoning-effort-improve-extraction), [EX-04](extraction.md#planned) (planned), [EX-05](extraction.md#planned) (planned), [EX-06](extraction.md#what-is-the-impact-of-document-modality), [EX-10](extraction.md#do-schema-validation-and-value-coercion-improve-the-output), [EX-11](extraction.md#should-i-retry-failed-fields-with-a-stronger-model), [EX-12](extraction.md#does-schema-enforcement-improve-accuracy-or-cost), [CF-01](confidence-assessment.md#planned) (planned), [CF-02](confidence-assessment.md#which-scoring-model-should-i-use), [CF-03](confidence-assessment.md#separate-scoring-pass-or-integrated-into-extraction), [CF-04](confidence-assessment.md#planned) (planned), [TU-02](token-use.md#planned) (planned), [TU-03](token-use.md#planned) (planned), [TU-04](token-use.md#what-is-the-cheapest-configuration-that-keeps-accuracy), [TU-05](token-use.md#planned) (planned), [CF-08](confidence-assessment.md#can-a-decision-model-score-confidence)

### RealKIE-FCC-Verified, all 75 invoices

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-verified`, 75 documents per run
- **Used by:** [EX-01](extraction.md#when-is-agentic-extraction-worth-it)

### RealKIE-FCC-Verified, long invoices (19 invoices, 8 to 33 pages)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-long`, 19 documents per run
- **Used by:** [EX-07](extraction.md#does-deterministic-table-parsing-help-agentic-extraction)

### RealKIE-FCC-Verified, short invoices (24 invoices, 1 or 2 pages)

- **About the dataset:** [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** `realkie-fcc-short`, 24 documents per run

### ConfBench (RealKIE invoices with scan noise)

- **About the dataset:** [ConfBench](../extensions/confbench-testset.md)
- **Preset:** `realkie-fcc-verified`
- **Test Studio test set:** not on the benchmark stack yet
- **Used by:** [DG-02](digitization.md#planned) (planned), [DG-03](digitization.md#planned) (planned), [DG-05](digitization.md#planned) (planned), [DG-07](digitization.md#planned) (planned), [CF-01](confidence-assessment.md#planned) (planned), [CF-04](confidence-assessment.md#planned) (planned), [CF-06](confidence-assessment.md#planned) (planned)

### OmniAI-OCR-Benchmark, bank checks (20)

- **About the dataset:** [OmniAI-OCR-Benchmark](../idp-fundamentals/benchmark-datasets.md#omniai-ocr-benchmark)
- **Preset:** `ocr-benchmark`
- **Test Studio test set:** `ocr-benchmark`, 20 documents per run
- **Used by:** [DG-01](digitization.md#does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-03](digitization.md#planned) (planned), [EX-06](extraction.md#what-is-the-impact-of-document-modality)

### OmniAI-OCR-Benchmark, all 9 formats (293 documents)

- **About the dataset:** [OmniAI-OCR-Benchmark](../idp-fundamentals/benchmark-datasets.md#omniai-ocr-benchmark)
- **Preset:** `ocr-benchmark`
- **Test Studio test set:** `ocr-benchmark`, 293 documents per run
- **Used by:** [TU-05](token-use.md#planned) (planned)

### Fake-W2-Tax-Forms (W-2 forms, 45 fields)

- **About the dataset:** [Fake-W2-Tax-Forms](../idp-fundamentals/benchmark-datasets.md#fake-w2-tax-forms)
- **Preset:** `fake-w2`
- **Test Studio test set:** `fake-w2`, 20 documents per run
- **Used by:** [DG-06](digitization.md#which-textract-features-do-i-need), [EX-02](extraction.md#which-extraction-model-should-i-use), [EX-03](extraction.md#does-more-reasoning-effort-improve-extraction), [EX-04](extraction.md#planned) (planned), [EX-05](extraction.md#planned) (planned), [EX-06](extraction.md#what-is-the-impact-of-document-modality), [EX-11](extraction.md#should-i-retry-failed-fields-with-a-stronger-model), [EX-12](extraction.md#does-schema-enforcement-improve-accuracy-or-cost), [CF-03](confidence-assessment.md#separate-scoring-pass-or-integrated-into-extraction), [CF-06](confidence-assessment.md#planned) (planned), [TU-04](token-use.md#what-is-the-cheapest-configuration-that-keeps-accuracy)

### DocSplit-Poly-Seq (20 packets)

- **About the dataset:** [DocSplit-Poly-Seq](../idp-fundamentals/benchmark-datasets.md#docsplit-poly-seq)
- **Preset:** `docsplit`
- **Test Studio test set:** `docsplit`, 20 documents per run
- **Used by:** [TR-01](index.md#planned) (planned), [SC-11](splitting-and-classification.md#does-holistic-classification-beat-page-by-page-classification), [SC-06](splitting-and-classification.md#planned) (planned), [SC-07](splitting-and-classification.md#planned) (planned), [SC-08](splitting-and-classification.md#planned) (planned), [SC-09](splitting-and-classification.md#planned) (planned), [CF-05](confidence-assessment.md#planned) (planned), [TU-02](token-use.md#planned) (planned), [TU-03](token-use.md#planned) (planned)

### DocSplit-Poly-Seq (100 packets)

- **About the dataset:** [DocSplit-Poly-Seq](../idp-fundamentals/benchmark-datasets.md#docsplit-poly-seq)
- **Preset:** `docsplit`
- **Test Studio test set:** `docsplit`, 100 documents per run
- **Used by:** [SC-02](splitting-and-classification.md#do-i-need-page-images-in-the-classification-prompt), [SC-12](splitting-and-classification.md#can-a-decision-model-classify-the-sections), [SC-13](splitting-and-classification.md#which-way-of-splitting-and-classifying-should-i-use), [SC-14](splitting-and-classification.md#do-long-packets-change-which-approach-to-use)

### DocSplit same-class runs (60 packets)

- **About the dataset:** 60 packets we built from the pages of DocSplit packets 101 to 500, 879 pages in all. Each packet holds one run of documents of the same class back to back: 40 runs of two and 20 of three, over all 13 classes. DocSplit reuses a small pool of documents, so many also appear in packets 1 to 100.
- **Preset:** `docsplit`
- **Test Studio test set:** `docsplit-sameclass`, 60 documents per run
- **Used by:** [SC-13](splitting-and-classification.md#which-way-of-splitting-and-classifying-should-i-use)

### DocSplit long packets (40 packets, 30 to 60 pages)

- **About the dataset:** 40 packets we built from the pages of DocSplit packets 101 to 500, 1,815 pages and 508 documents in all, 10 packets in each of 30 to 37, 38 to 45, 46 to 53 and 54 to 60 pages. As in DocSplit, no two neighbouring documents share a class. DocSplit reuses a small pool of documents, so many also appear in packets 1 to 100.
- **Preset:** `docsplit`
- **Test Studio test set:** `docsplit-long`, 40 documents per run
- **Used by:** [SC-04](splitting-and-classification.md#how-long-a-packet-can-holistic-classification-handle), [SC-14](splitting-and-classification.md#do-long-packets-change-which-approach-to-use)

## Datasets we haven't built yet

### Packets with section and field ground truth (to be built)

- **Used by:** [EX-13](extraction.md#planned) (planned), [EX-15](extraction.md#planned) (planned)

### Handwritten forms (to be built)

- **Used by:** [DG-08](digitization.md#planned) (planned)

<!-- END GENERATED DATASETS -->
