Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# RealKIE-FCC-Verified Configuration

This directory contains the default (starting point) configuration for the FCC Invoices in the RealKIE-FCC-Verified dataset used to benchmark the GenAI IDP Accelerator. This configuration is specifically designed for processing the FCC invoice samples using Test Studio.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false). Set use_bda: true for BDA mode.

## Section Splitting

Every file in the RealKIE-FCC-Verified test set is one invoice, and its ground truth is one `Invoice` section spanning every page. Under the default strategy, `llm_determined`, page-level classification asks the model about every page of a multi-page file, and the model sometimes takes a page in the middle of an invoice for the start of a new document. The invoice is then split into several sections, each extracted on its own. Test Studio lists such a file as a classification error and scores only its first section against the whole invoice's ground truth.

Two configurations for this test set therefore set `classification.sectionSplitting: disabled`, which puts all of a file's pages, in page order, into one section: the stack-managed `realkie-fcc-verified` profile (`config_library/managed_config/realkie-fcc-verified/config.yaml`), which Test Studio selects for this test set, and the 1S-TopK reference configuration in this directory (`config-1s-topk-with-ocr-image.yaml`). With `Invoice` as the only class, classification then makes no model call: every page is assigned `Invoice` with confidence 1.0.

This directory's `config.yaml` does not set `classification.sectionSplitting`. A stack deployed with `ConfigurationPreset=realkie-fcc-verified` rebuilds its `default` profile from this file on every update. `default` is usually the active profile, and a profile created by importing a configuration or uploading one under a new name is built on it, so a value set here would reach that profile, and every profile built on it afterwards, the next time such a stack is upgraded. Importing `realkie-fcc-verified` from the configuration library also reads this file. To treat each file as one invoice in a profile created from it, set **Section splitting** to `disabled` in the profile's classification settings.

The setting applies to pipeline mode; with `use_bda: true`, BDA determines the sections. Do not use `disabled` for files that hold several documents: it merges them into one section.

## Validation Level

**Level**: 2 - Minimal Testing

- **Testing Evidence**: This configuration has been lightly tested with the RealKIE-FCC-Verified Dataset. 
- **Known Limitations**: Performance may vary - consider this configuration a starting point. We welcome Pull Requests to improve the accuracy.
