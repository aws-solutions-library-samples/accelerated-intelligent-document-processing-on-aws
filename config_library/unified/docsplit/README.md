Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# DocSplit (RVL-CDIP) Configuration

This configuration is designed for the DocSplit-Poly-Seq test set and handles 13 diverse business and administrative document types.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova/Claude) for classification, extraction, assessment, and summarization. Set use_bda: true for BDA mode.

## Configuration Variants In This Directory

`config.yaml` is the preset. It is the only file the `docsplit` value of the
**Configuration Preset** stack parameter resolves to, and the only one the web UI's
*Import from library* offers. The other two files are the two arms of a
classification A/B benchmark, and they are installed by naming them explicitly —
`idp-cli config-upload -f <path>`, `idp-cli deploy --custom-config <path>`, or a
**Custom Configuration Path** S3 URI.

| File | `classification.model` | Purpose |
|---|---|---|
| `config.yaml` | preset default | The selectable preset |
| `docsplit_base_model_config.yaml` | `us.amazon.nova-2-lite-v1:0` | Base-model arm of the A/B |
| `docsplit_finedtuned_config.yaml` | `us.amazon.nova-2-lite-v1:0`, for you to replace | Fine-tuned arm of the A/B |

### Running the fine-tuned arm

`docsplit_finedtuned_config.yaml` exists to measure a **fine-tuned** Nova 2 Lite
against the base model, and a fine-tuned model is reached through a Bedrock
`custom-model-deployment` ARN. Such an ARN is account-scoped: it resolves only in the
account that owns the deployment, and from anywhere else Bedrock answers
`AccessDeniedException` — *"the provided resource ARN is from a different account"* —
so the classification stage fails outright rather than degrading. The file therefore
cannot ship a working one.

It ships with `classification.model` set to the base model, so it runs as-is and is
priced and limit-checked correctly. To run the fine-tuned arm:

1. Fine-tune `amazon.nova-2-lite-v1:0` on your own classification data.
2. Create a custom model deployment for the result and note its ARN.
3. Replace `classification.model` in your copy of the file with that ARN. The line is
   marked with a comment showing the shape:
   `arn:aws:bedrock:<region>:<your-account-id>:custom-model-deployment/<deployment-id>`.
4. Add a `bedrock/<that ARN>` entry to your pricing configuration if you want cost
   reporting for the run — a model with no pricing entry records a NULL cost rather
   than a wrong one, so the run's spend is reported as incomplete.

## Test Set Compatibility

**Compatible Test Set**: DocSplit-Poly-Seq

This configuration should be used with the DocSplit-Poly-Seq test set, which contains 500 multi-page packets with 13 document types. The test set is automatically deployed during stack deployment and is available in the Test Studio UI. See [docs/test-studio.md](../../../docs/test-studio.md) for details.

## Validation Level

**Level**: 2 - Comprehensive Testing

- **Testing Evidence**: This configuration has been tested with a diverse set of business documents across all supported document classes. It has shown consistent performance in classifying and extracting information from standard business documents.
- **Known Limitations**: Performance may vary with highly specialized document types or documents with complex layouts not represented in the test set.

## Overview

This configuration is designed to handle 13 diverse business and administrative document types, including:

- **letter**: Business and personal correspondence
- **form**: Administrative forms and applications
- **invoice**: Billing and financial documents
- **resume**: Professional resumes and CVs
- **scientific_publication**: Academic and research papers
- **memo**: Internal business memos
- **email**: Email communications
- **questionnaire**: Surveys and questionnaires
- **specification**: Technical specifications
- **budget**: Financial budgets and reports
- **news_article**: Newspaper and news articles
- **handwritten**: Handwritten notes and documents
- **language**: Non-English documents (e.g., Arabic documents)

It includes settings for document classification, information extraction, and document summarization using Amazon Bedrock models.

## Key Components

### Document Classes

The configuration defines 13 document classes, each with specific attributes to extract:

- **letter**: Extracts sender name and address
- **form**: Extracts form type and ID
- **invoice**: Extracts invoice number and date
- **resume**: Extracts full name and contact information
- **scientific_publication**: Extracts title and authors
- **memo**: Extracts memo date and sender
- **email**: Extracts from and to addresses
- **questionnaire**: Extracts form title and respondent information
- **specification**: Extracts product name and version
- **budget**: Extracts budget period and total amount
- **news_article**: Extracts article headline and publication date
- **handwritten**: Extracts document type and key content summary
- **language**: Extracts detected language and document type

### Classification Settings

- **Model**: Amazon Nova2 Lite
- **Method**: multimodalPageLevelClassification
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The classification component analyzes document content and structure to determine the document type and page boundaries within multi-page documents.

### Extraction Settings

- **Model**: Amazon Nova Pro
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The extraction component identifies and extracts specific attributes from each document based on its classified type.

### Summarization Settings

- **Model**: Amazon Nova Pro
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The summarization component creates concise summaries of documents with citations and hover functionality.

## Test Set

This configuration is designed to work with the **DocSplit-Poly-Seq** test set:

- **500 multi-page packets** containing 2-10 distinct documents each
- **7,330 total pages** across all packets
- **2,027 document sections** for classification and splitting evaluation
- **Automatic deployment** during stack creation
- **Complete ground truth** for evaluation of page-level classification and document splitting accuracy

The test set is automatically available in the Test Studio UI after stack deployment. See the [Test Studio documentation](../../../docs/test-studio.md) for usage instructions.

### Dataset Information

- **DocSplit Dataset**: https://huggingface.co/datasets/amazon/doc_split
- **Documents Source**: https://huggingface.co/datasets/jordyvl/rvl_cdip_n_mp

The DocSplit dataset uses documents sourced from the RVL-CDIP-N-MP dataset, which are combined into multi-page packets for document splitting and classification evaluation.

## How to Use

To use this default configuration:

1. **Direct Use**: Deploy the GenAI IDP Accelerator with this configuration without any modifications for general-purpose document processing.

2. **As a Template**: Copy this configuration to a new directory and modify it for your specific use case:
   ```bash
   cp -r config_library/unified/default config_library/unified/your_use_case_name
   ```

3. **For Testing**: Use this configuration as a baseline for comparing the performance of customized configurations.

## Common Customization Scenarios

### Adding New Document Classes

To add a new document class:

1. Add a new entry to the `classes` array in the configuration:
   ```json
   {
     "name": "your_class_name",
     "description": "Description of your document class",
     "attributes": [
       {
         "name": "attribute_name",
         "description": "Description of what to extract and where to find it"
       },
       // Add more attributes as needed
     ]
   }
   ```

2. Test the configuration with sample documents of the new class.

### Modifying Prompts

To adjust the behavior of classification, extraction, or summarization:

1. Modify the `system_prompt` or `task_prompt` in the respective section.
2. Keep the placeholders (e.g., `{DOCUMENT_TEXT}`, `{CLASS_NAMES_AND_DESCRIPTIONS}`) intact.
3. Test the modified prompts with representative documents.

### Changing Models

To use a different model:

1. Update the `model` field in the classification, extraction, or summarization section.
2. Adjust temperature and other parameters as needed for the new model.
3. Test the configuration with the new model to ensure compatibility.

## Performance Considerations

The default configuration is optimized for:

- **Accuracy**: Using temperature 0 for deterministic outputs
- **Generality**: Handling a wide range of document types

For specialized use cases, consider adjusting the configuration to focus on the specific document types and attributes relevant to your needs.

## Contributors

- GenAI IDP Accelerator Team
