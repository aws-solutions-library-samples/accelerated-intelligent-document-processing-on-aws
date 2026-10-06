Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Unified Configuration Presets

This directory contains configuration presets for the GenAI IDP Accelerator. Each preset defines document classes, extraction schemas, and processing options.

## Processing Modes

The unified architecture supports two processing modes, controlled by the `use_bda` flag in each configuration:

- **`use_bda: false`** (default) — **Pipeline mode**: Uses Amazon Textract for OCR, then Bedrock LLM for classification, extraction, assessment, and summarization step by step.
- **`use_bda: true`** — **BDA mode**: Uses Bedrock Data Automation for end-to-end processing.

All presets default to pipeline mode (`use_bda: false`). To switch to BDA mode, simply set `use_bda: true` in your configuration — no other schema changes are needed.

## Configuration Structure

Configurations leverage **system defaults** for standard settings. A minimal config only needs:

```yaml
use_bda: false
notes: "Description of the configuration"

classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: DocumentType
    type: object
    x-aws-idp-document-type: DocumentType
    description: "Document description"
    properties:
      field_name:
        type: string
        description: "Field description"
```

**Override only what differs from defaults.** For example, to change the classification method:
```yaml
classification:
  classificationMethod: textbasedHolisticClassification
```

## OCR Backend Selection

The pipeline mode supports multiple OCR backends, each with different implications for the assessment feature:

### Textract Backend (Default - Recommended)
- **Best for**: Production workflows, when assessment is enabled
- **Assessment Impact**: ✅ Full assessment capability with granular confidence scores
- **Text Confidence Data**: Rich confidence information for each text block
- **Cost**: Standard Textract pricing

### Bedrock Backend (LLM-based OCR)
- **Best for**: Challenging documents where traditional OCR fails
- **Assessment Impact**: ❌ Assessment disabled - no confidence data available
- **Text Confidence Data**: Empty (no confidence scores from LLM OCR)
- **Cost**: Bedrock LLM inference costs

### None Backend (Image-only)
- **Best for**: Custom OCR integration, image-only workflows
- **Assessment Impact**: ❌ Assessment disabled - no OCR text available
- **Text Confidence Data**: Empty
- **Cost**: No OCR costs

> ⚠️ **Assessment Recommendation**: Use Textract backend (default) when assessment functionality is required. Bedrock and None backends eliminate assessment capability due to lack of confidence data.

## Adding Configurations

To add a new configuration:

1. Create a new directory with a descriptive name
2. Include a `config.yaml` file with the appropriate settings
3. Add a README.md file using the template from `../TEMPLATE_README.md`
4. Include sample documents in a `samples/` directory

See the main [README.md](../README.md) for more detailed instructions on creating and contributing configurations.

## Available Configurations

| Configuration | Description | Special Features |
|---------------|-------------|------------------|
| [bank-statement-sample](./bank-statement-sample/) | Bank statement processing with transaction extraction | Text-based holistic classification, large-list confidence batching |
| [docsplit](./docsplit/) | DocSplit document classification benchmark (16 classes) | Based on RVL-CDIP |
| [healthcare-multisection-package](./healthcare-multisection-package/) | Healthcare multi-section document processing | Multi-section document support |
| [lending-package-sample](./lending-package-sample/) | Lending package processing (payslips, IDs, bank checks, W2s) | 6 document classes |
| [lending-package-sample-govcloud](./lending-package-sample-govcloud/) | GovCloud-compatible lending package processing | |
| [ocr-benchmark](./ocr-benchmark/) | OCR benchmarking configuration | |
| [realkie-fcc-verified](./realkie-fcc-verified/) | FCC invoices (the RealKIE-FCC-Verified test set) | One section per file (`sectionSplitting: disabled`) |
| [rule-validation](./rule-validation/) | Rule validation configuration | Custom validation rules |
| [rvl-cdip](./rvl-cdip/) | RVL-CDIP document classification benchmark | 16 document classes |
| [rvl-cdip-with-few-shot-examples](./rvl-cdip-with-few-shot-examples/) | RVL-CDIP with few-shot learning examples | Custom prompts with `{FEW_SHOT_EXAMPLES}` |
