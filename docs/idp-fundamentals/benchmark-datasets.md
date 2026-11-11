---
title: "Benchmark Datasets"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Benchmark Datasets

These are the datasets behind the "What we measured" sections. Each one measures one or more
problem sets, and every stack loads the public ones into Test Studio, so you can run the same
comparisons in your own account.

| Dataset | Kind | Splitting and classification | Extraction | Confidence | In Test Studio |
|---|---|---|---|---|---|
| [Synthetic bank statements](#synthetic-bank-statements) | Generated, exact answers | Document boundaries | **Long lists** | **Field scores** | Generate with the benchmark suite |
| [RealKIE-FCC-Verified](#realkie-fcc-verified) | Real invoices | — | **Yes** | Partly | Yes |
| [DocSplit-Poly-Seq](#docsplit-poly-seq) | Real packets | **Yes** | — | **Type scores** | Yes |
| [OmniAI-OCR-Benchmark](#omniai-ocr-benchmark) | Real images | — | Single-page images | — | Yes |
| [Fake-W2-Tax-Forms](#fake-w2-tax-forms) | Synthetic forms | — | Ready to run | — | Yes |

These datasets do not yet cover handwriting, identity documents, long narrative text or rule
validation. For those, build a test set from your own documents; see
[Measure with ground truth](index.md#4-measure-with-ground-truth).

## Synthetic bank statements

- **Source:** the benchmark suite generates these statements
  (`benchmarks/corpus/generators/bank_statement.py`). The generator is deterministic, so it
  produces the same documents every time.
- **Size:** from 5 rows on 1 page to 3,200 rows on 66 pages, plus variants with wide tables,
  several separate lists, long descriptions, and files that hold two statements.
- **Ground truth:** exact. Every transaction row carries a unique tag, so the scorer knows
  exactly which rows came back and checks each row's date and amount.
- **Measures:** extraction completeness and accuracy as documents grow, field-confidence
  calibration and ranking, and document boundaries in one- and two-statement files.
- **Scope:** clean, regular tables of one document type. Extraction rarely gets a value
  wrong on them, which makes them ideal for measuring completeness and too clean for
  measuring how confidence ranks errors on hard documents.

## RealKIE-FCC-Verified

- **Source:**
  [amazon-agi/RealKIE-FCC-Verified](https://huggingface.co/datasets/amazon-agi/RealKIE-FCC-Verified):
  invoices for broadcast advertising, taken from US Federal Communications Commission filings.
- **Size:** 75 invoices. The benchmark uses the first 20 (81 pages).
- **Ground truth:** human-verified values for header fields (agency, advertiser, totals,
  payment terms, commission) and a list of line items. Stickler scores it as weighted
  accuracy.
- **Measures:** extraction from real documents with real layout variation.
- **Scope:** one document type. With 20 documents, treat differences under about 5 points as
  within the noise.
- **In Test Studio:** [RealKIE-FCC-Verified](../test-studio.md#realkie-fcc-verified).

## DocSplit-Poly-Seq

- **Source:** [amazon/doc_split](https://huggingface.co/datasets/amazon/doc_split): packets
  built from the pages of [RVL-CDIP-N-MP](https://huggingface.co/datasets/jordyvl/rvl_cdip_n_mp).
- **Size:** 500 packets of 5–20 pages, each joining whole documents of different types,
  across 13 types in all. The benchmark uses the first 20 packets (298 pages).
- **Ground truth:** the type of every page and the pages of every document.
- **Measures:** page typing, document boundaries, and document-type confidence.
- **Scope:** it has no field values, so it measures splitting but not extraction. Neighboring
  documents in a packet are always of different types.
- **In Test Studio:** [DocSplit-Poly-Seq](../test-studio.md#docsplit-poly-seq).

## OmniAI-OCR-Benchmark

- **Source:** [getomni-ai/ocr-benchmark](https://huggingface.co/datasets/getomni-ai/ocr-benchmark).
- **Size:** 293 single-page images in 9 document formats.
- **Ground truth:** structured values for each image.
- **Measures, as we use it:** the benchmark takes the first 20 images, which are all bank
  checks, so our results measure extraction from one kind of single-page image. The full test
  set covers nine formats and suits a broader comparison in your own account.
- **In Test Studio:** [OmniAI-OCR-Benchmark](../test-studio.md#omniai-ocr-benchmark).

## Fake-W2-Tax-Forms

- **Source:**
  [singhsays/fake-w2-us-tax-form-dataset](https://huggingface.co/datasets/singhsays/fake-w2-us-tax-form-dataset):
  synthetic US W-2 forms with fake personal data.
- **Size:** 2,000 form images with 45 ground-truth fields each.
- **Measures:** extraction from fixed-field forms. It is not yet part of our benchmark runs,
  and it is ready to run in Test Studio today.
- **In Test Studio:** [Fake-W2-Tax-Forms](../test-studio.md#fake-w2-tax-forms).

## Run them yourself

Every stack loads the public datasets above into Test Studio, each with a matching
configuration profile. See
[Pre-Deployed Test Sets](../test-studio.md#pre-deployed-test-sets). To run the full benchmark
matrices and score them the way these pages do, see [Benchmarking](../benchmarking/index.md).
