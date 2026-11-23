---
title: "Benchmark Datasets"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Benchmark Datasets

A **problem set** is one step of document processing, such as extraction. A **dataset** is
a collection of files with known correct answers, called **ground truth**, that measures
how well one or more problem sets work. The two do not line up one to one: one dataset can
measure several problem sets, and a problem set can draw on several datasets. This page
describes the datasets behind the benchmark results on the other pages in this section.

Two terms recur below. A **document class** is a kind of document you define, such as
*Invoice*. A **section** is one document inside a file: a run of consecutive pages with one
document class.

Every stack loads the public datasets into [Test Studio](../test-studio.md) as test sets,
so you can run the same comparisons in your own account.

| Dataset | Files | Ground truth | Problem sets it measures | In Test Studio |
|---|---|---|---|---|
| [Synthetic bank statements](#synthetic-bank-statements) | Generated statements, 1 to 66 pages | Every row and value, exact | Splitting (one or two statements per file), extraction of long lists, field confidence | Generate with the benchmark suite |
| [RealKIE-FCC-Verified](#realkie-fcc-verified) | Real invoices | Header fields and line items | Digitization, extraction | Yes |
| [DocSplit-Poly-Seq](#docsplit-poly-seq) | Real multi-document packets | Document class of every page, page range of every section | Splitting and classification, document class confidence | Yes |
| [OmniAI-OCR-Benchmark](#omniai-ocr-benchmark) | Real single-page images | Structured values for each image | Digitization, extraction (bank checks only in our runs) | Yes |
| [Fake-W2-Tax-Forms](#fake-w2-tax-forms) | Synthetic W-2 forms | 45 fields per form | None yet; ready to run | Yes |

None of these datasets measures extraction from handwriting, identity documents or long
narrative text, and none measures rule validation. For those, build a test set from your own documents; see
[Measure with ground truth](index.md#4-measure-with-ground-truth).

## Synthetic bank statements

- **Source:** the benchmark suite generates the statements with
  `benchmarks/corpus/generators/bank_statement.py`. The generator uses no randomness, so it
  produces the same files every time.
- **Size:** from 5 transaction rows on 1 page to 3,200 rows on 66 pages, plus variants with
  wide tables, several separate lists, long descriptions, OCR-like noise, a header repeated
  on every page, and two statements in one file. `benchmarks/matrices/doc_matrix.yaml` lists
  every variant.
- **Ground truth:** exact. Every transaction row carries a unique tag, so the scorer knows
  which rows came back and checks the values in each one.
- **Measures:** how completely and accurately extraction returns long lists as files grow,
  how well field confidence separates right values from wrong ones, and how many sections
  the accelerator finds in one- and two-statement files.
- **How far it carries:** clean, regular tables of one document class. Extraction rarely
  misreads a value on them, which makes them ideal for measuring completeness and too clean
  for measuring how confidence ranks errors on hard documents.

## RealKIE-FCC-Verified

- **Source:**
  [amazon-agi/RealKIE-FCC-Verified](https://huggingface.co/datasets/amazon-agi/RealKIE-FCC-Verified):
  invoices for broadcast advertising, from US Federal Communications Commission filings.
- **Size:** 75 invoices. The benchmark uses the first 20, which hold 81 pages.
- **Ground truth:** values for the header fields (agency, advertiser, gross total, payment
  terms, agency commission, net amount due) and a list of line items. The accelerator's
  evaluation scores them as weighted accuracy.
- **Measures:** extraction, and the effect of the OCR choice on it, on real layouts.
- **How far it carries:** one document class, and one section per file. With 20 files,
  treat differences under about 5 points as noise.
- **In Test Studio:** [RealKIE-FCC-Verified](../test-studio.md#realkie-fcc-verified).

## DocSplit-Poly-Seq

- **Source:** [amazon/doc_split](https://huggingface.co/datasets/amazon/doc_split): packets
  built from the pages of [RVL-CDIP-N-MP](https://huggingface.co/datasets/jordyvl/rvl_cdip_n_mp).
- **Size:** 500 packets of 5 to 20 pages, 7,330 pages in all, across 13 document classes.
  Each packet joins whole documents end to end. The benchmark uses the first 20 packets,
  which hold 298 pages.
- **Ground truth:** the document class of every page and the page range of every section.
- **Measures:** page classification, section boundaries, and document class confidence.
- **How far it carries:** the packets carry no field values, so the dataset measures
  splitting and classification but not extraction. No document class appears twice in a
  packet, so the dataset does not test runs of same-class documents, such as several pay
  stubs in a row.
- **In Test Studio:** [DocSplit-Poly-Seq](../test-studio.md#docsplit-poly-seq).

## OmniAI-OCR-Benchmark

- **Source:** [getomni-ai/ocr-benchmark](https://huggingface.co/datasets/getomni-ai/ocr-benchmark).
- **Size:** the Test Studio test set holds 293 single-page images in 9 document classes,
  such as bank checks, leases and shift schedules. The benchmark uses the first 20 images,
  and all 20 are bank checks.
- **Ground truth:** structured values for each image.
- **Measures:** in our runs, digitization and extraction on one kind of single-page image,
  the bank check.
- **How far it carries:** our results describe bank checks only. The full test set covers
  nine document classes and suits a broader comparison in your own account.
- **In Test Studio:** [OmniAI-OCR-Benchmark](../test-studio.md#omniai-ocr-benchmark).

## Fake-W2-Tax-Forms

- **Source:**
  [singhsays/fake-w2-us-tax-form-dataset](https://huggingface.co/datasets/singhsays/fake-w2-us-tax-form-dataset):
  synthetic US W-2 forms filled with fake personal data.
- **Size:** 2,000 form images.
- **Ground truth:** 45 fields per form, covering every W-2 box.
- **Measures:** extraction from fixed-field forms. Our benchmark runs do not use the dataset
  yet, and you can run it in Test Studio today.
- **In Test Studio:** [Fake-W2-Tax-Forms](../test-studio.md#fake-w2-tax-forms).

## Run the datasets yourself

Every stack loads the public datasets into Test Studio, each with a matching configuration
profile that Test Studio selects for you. See
[Pre-Deployed Test Sets](../test-studio.md#pre-deployed-test-sets). To generate the synthetic
statements and run the full benchmark matrices the way these pages do, see
[Benchmarking](../benchmarking/index.md).
