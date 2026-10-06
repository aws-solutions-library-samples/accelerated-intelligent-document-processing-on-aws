Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# RealKIE-FCC-Verified Configuration

This directory contains the default (starting point) configuration for the FCC Invoices in the RealKIE-FCC-Verified dataset used to benchmark the GenAI IDP Accelerator. This configuration is specifically designed for processing the FCC invoice samples using Test Studio.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false). Set use_bda: true for BDA mode.

## Section Splitting

Every file in the RealKIE-FCC-Verified test set is one invoice, and its ground truth is one `Invoice` section spanning every page. This configuration therefore sets `classification.sectionSplitting: disabled`, which puts all of a file's pages, in page order, into one section. With `Invoice` as the only class, classification then makes no model call: every page is assigned `Invoice` with confidence 1.0. Test Studio's classification and splitting metrics on this test set are therefore 1.0 by construction, and a run measures extraction.

Under the default, `llm_determined`, page-level classification asks the model about every page of a multi-page file, and the model sometimes takes a page in the middle of an invoice for the start of a new document. The invoice is then split into several sections, each extracted on its own. Test Studio lists such a file as a classification error and scores only its first section against the whole invoice's ground truth.

The 1S-TopK reference configuration in this directory (`config-1s-topk-with-ocr-image.yaml`) and the stack-managed `realkie-fcc-verified` profile (`config_library/managed_config/realkie-fcc-verified/config.yaml`) set the same value. The setting applies to pipeline mode; with `use_bda: true`, BDA determines the sections. Do not carry it over to files that hold several documents: `disabled` merges them into one section.

## Validation Level

**Level**: 2 - Minimal Testing

- **Testing Evidence**: This configuration has been lightly tested with the RealKIE-FCC-Verified Dataset. 
- **Known Limitations**: Performance may vary - consider this configuration a starting point. We welcome Pull Requests to improve the accuracy.
