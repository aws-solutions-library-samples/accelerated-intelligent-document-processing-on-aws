---
title: "Pattern 2: Bedrock Classification and Extraction"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Pattern 2: Bedrock Classification and Extraction

> **⚠️ DEPRECATED**: Pattern 2 has been superseded by the **Unified Pattern**, which combines both BDA and pipeline processing modes into a single deployment. The `use_bda` configuration flag (set via the UI) controls whether documents are processed via BDA or the step-by-step pipeline. See [architecture.md](./architecture.md) for details on the unified architecture.
>
> This document is retained as a reference for pipeline-specific concepts (OCR, classification, extraction, assessment, rule validation) that still apply when `use_bda: false` is set in the unified pattern (the default mode).

This pattern implements an intelligent document processing workflow that uses Amazon Bedrock with Nova or Claude models for both page classification/grouping and information extraction.

For the current architecture diagram, see the unified architecture in [architecture.md](./architecture.md).

## Table of Contents

- [Architecture Overview](#architecture-overview)
  - [State Machine Workflow](#state-machine-workflow)
  - [Lambda Functions](#lambda-functions)
    - [OCR Function](#ocr-function)
    - [Classification Function](#classification-function)
    - [Extraction Function](#extraction-function)
    - [ProcessResults Function](#processresults-function)
  - [Human-in-the-Loop (HITL)](#human-in-the-loop-hitl)
  - [Monitoring and Metrics](#monitoring-and-metrics)
    - [Performance Metrics](#performance-metrics)
    - [Error Tracking](#error-tracking)
    - [Lambda Function Metrics](#lambda-function-metrics)
  - [Template Outputs](#template-outputs)
  - [Configuration](#configuration)
- [Bedrock OCR Feature](#bedrock-ocr-feature)
  - [Overview](#overview)
  - [Configuration](#configuration-1)
  - [Enabling Bedrock OCR](#enabling-bedrock-ocr)
  - [Benefits](#benefits)
  - [Cost Considerations](#cost-considerations)
- [Customizing Classification](#customizing-classification)
- [Few Shot Example Feature](#few-shot-example-feature)
- [Customizing Extraction](#customizing-extraction)
- [Assessment Feature](#assessment-feature)
- [Testing](#testing)
- [Best Practices](#best-practices)

## Architecture Overview

The workflow consists of three main processing steps with an optional assessment step:
1. OCR processing using Amazon Textract
2. Document classification using Claude via Amazon Bedrock (with two available methods):
   - Page-level classification: Classifies individual pages and groups them
   - Holistic packet classification: Analyzes multi-document packets to identify document boundaries
3. Field extraction using Claude via Amazon Bedrock
4. **Assessment** (optional): Confidence evaluation of extraction results using LLMs

### State Machine Workflow

The Step Functions state machine (`workflow.asl.json`) orchestrates the following flow:

```
OCRStep → ClassificationStep → ProcessPageGroups (Map State for Extraction) → ProcessResultsStep
```

Each step includes comprehensive retry logic for handling transient errors:
- Initial retry after 2 seconds
- Exponential backoff with rate of 2
- Maximum of 8-10 retry attempts depending on the step

### Lambda Functions

#### OCR Function
- **Purpose**: Processes input PDFs using Amazon Textract or Amazon Bedrock
- **Key Features**:
  - Supports two OCR backends:
    - Amazon Textract (default)
    - Amazon Bedrock LLMs (Claude, Nova)
  - Concurrent page processing with ThreadPoolExecutor
  - **Configurable Image Processing**: Enhanced image resizing with aspect-ratio preservation
  - **Configurable DPI**: Adjustable DPI for PDF-to-image conversion
  - **Dual Image Strategy**: Stores original high-DPI images while using resized images for OCR processing
  - **Smart Resizing**: Only downsizes images when necessary (scale factor < 1.0)
  - Image preprocessing and optimization
  - Comprehensive error handling and retries
  - Detailed metrics tracking
- **Input**:
  ```json
  {
    "execution_arn": "<ARN>",
    "output_bucket": "<BUCKET>",
    "input": {
      "detail": {
        "bucket": { "name": "<BUCKET>" },
        "object": { "key": "<KEY>" }
      }
    }
  }
  ```
- **Output**:
  ```json
  {
    "metadata": {
      "input_bucket": "<BUCKET>",
      "object_key": "<KEY>",
      "output_bucket": "<BUCKET>",
      "output_prefix": "<PREFIX>",
      "num_pages": "<NUMBER OF PAGES>"
    },
    "pages": {
      "<PAGE_NUMBER>": {
        "rawTextUri": "<S3_URI>",
        "parsedTextUri": "<S3_URI>",
        "imageUri": "<S3_URI>"
      }
    }
  }
  ```

#### Classification Function
- **Purpose**: Classifies pages or document packets using Claude via Bedrock and segments into sections
- **Key Features**:
  - Two classification methods:
    - Page-level classification (multimodalPageLevelClassification)
    - Holistic packet classification (textbasedHolisticClassification)
  - RVL-CDIP dataset categories for classification
  - Concurrent page processing
  - Automatic image resizing and optimization
  - Robust error handling with exponential backoff
  - **Few shot example support for improved accuracy**
- **Input**: Output from OCR function
- **Output**:
  ```json
  {
    "metadata": "<FROM_OCR>",
    "sections": [
      {
        "id": "<GROUP_ID>",
        "class": "<CLASS>",
        "pages": [...]
      }
    ]
  }
  ```

#### Extraction Function
- **Purpose**: Extracts fields using Claude via Bedrock
- **Key Features**:
  - Document class-specific attribute extraction
  - Configurable extraction attributes
  - Comprehensive error handling
  - Token usage tracking
  - **Few shot example support for improved accuracy**
- **Input**: Individual section from Classification output
- **Output**:
  ```json
  {
    "section": {
      "id": "<ID>",
      "class": "<CLASS>",
      "page_ids": ["<PAGEID>", ...],
      "outputJSONUri": "<S3_URI>"
    },
    "pages": [...]
  }
  ```

#### ProcessResults Function
- **Purpose**: Consolidates results from all sections
- **Output**: Standardized format for GenAIIDP parent stack:
  ```json
  {
    "Sections": [{
      "Id": "<ID>",
      "PageIds": ["<PAGEID>", ...],
      "Class": "<CLASS>",
      "OutputJSONUri": "<S3_URI>"
    }],
    "Pages": [{
      "Id": "<ID>",
      "Class": "<CLASS>",
      "TextUri": "<S3_URI>",
      "ImageUri": "<S3_URI>"
    }],
    "PageCount": "<TOTAL_PAGES>"
  }
  ```

### Human-in-the-Loop (HITL)

Pattern-2 supports Human-in-the-Loop (HITL) review capabilities using Amazon SageMaker Augmented AI (A2I). This feature allows human reviewers to validate and correct extracted information when the system's confidence falls below a specified threshold.

**Pattern-2 Specific Configuration:**
- `EnableHITL`: Boolean parameter to enable/disable the HITL feature
- `IsPattern2HITLEnabled`: Boolean parameter specific to Pattern-2 HITL enablement
- `Pattern2 - Existing Private Workforce ARN`: Optional parameter to use existing private workforce

For comprehensive HITL documentation including workflow details, configuration steps, best practices, and troubleshooting, see the [Human-in-the-Loop Review Guide](./human-review.md).

### Monitoring and Metrics

The pattern includes a comprehensive CloudWatch dashboard with:

#### Performance Metrics
- Document and page throughput
- Token usage (input/output/total)
- Bedrock request statistics
- Processing latencies
- Throttling and retry metrics

#### Error Tracking
- Lambda function errors
- Long-running invocations
- Classification/extraction failures
- Throttling events

#### Lambda Function Metrics
- Duration
- Memory usage
- Error rates
- Concurrent executions

### Template Outputs

The pattern exports these outputs to the parent stack:

- `StateMachineName`: Name of Step Functions state machine
- `StateMachineArn`: ARN of Step Functions state machine
- `StateMachineLogGroup`: CloudWatch log group for state machine
- `DashboardName`: Name of pattern-specific dashboard
- `DashboardArn`: ARN of pattern-specific dashboard

### Configuration

**Stack Deployment Parameters:**
- `ClassificationMethod`: Classification methodology to use (options: 'multimodalPageLevelClassification' or 'textbasedHolisticClassification')
- **Summarization**: Control summarization via configuration file `summarization.enabled` property (replaces `IsSummarizationEnabled` parameter)
- `ConfigurationDefaultS3Uri`: Optional S3 URI to custom configuration (uses default configuration if not specified)
- `MaxConcurrentWorkflows`: Workflow concurrency limit
- `LogRetentionDays`: CloudWatch log retention period
- `ExecutionTimeThresholdMs`: Latency threshold for alerts

**Configuration Management:**
- Model selection is now handled through configuration files rather than CloudFormation parameters
- Configuration supports multiple presets per pattern (e.g., default, checkboxed_attributes_extraction, medical_records_summarization, few_shot_example)
- Configuration can be updated through the Web UI without stack redeployment
- Model choices are constrained through enum constraints in the configuration schema

## Bedrock OCR Feature

### Overview

Pattern 2 now supports Amazon Bedrock LLMs (Claude, Nova) as an alternative OCR backend alongside the traditional Amazon Textract service. This feature enables multimodal document processing where large language models can extract text from document images using their vision capabilities.

### Configuration

Bedrock OCR is configured through the pattern's configuration files. The OCR backend can be selected using the `backend` parameter:

```yaml
ocr:
  backend: "bedrock"  # Options: "textract", "bedrock", "none"
  model_id: "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
  system_prompt: "You are an expert OCR system. Extract all text from the provided image accurately, preserving layout where possible."
  task_prompt: "Extract all text from this document image. Preserve the layout, including paragraphs, tables, and formatting."
```

### Enabling Bedrock OCR

To use Bedrock OCR:

1. **Set the backend**: Configure `backend: "bedrock"` in your OCR configuration
2. **Choose a model**: Select from supported vision-capable models:
   - `us.amazon.nova-lite-v1:0`
   - `us.amazon.nova-pro-v1:0` 
   - `us.amazon.nova-premier-v1:0`
   - `us.amazon.nova-2-lite-v1:0`
   - `us.anthropic.claude-3-haiku-20240307-v1:0`
   - `us.anthropic.claude-haiku-4-5-20251001-v1:0`
   - `us.anthropic.claude-3-5-sonnet-20241022-v2:0`
   - `us.anthropic.claude-3-7-sonnet-20250219-v1:0`
   - `us.anthropic.claude-sonnet-4-20250514-v1:0`
   - `us.anthropic.claude-sonnet-4-5-20250929-v1:0`
   - `us.anthropic.claude-sonnet-4-6`
   - `us.anthropic.claude-sonnet-4-6:1m`
   - `us.anthropic.claude-opus-4-20250514-v1:0`
   - `us.anthropic.claude-opus-4-1-20250805-v1:0`
   - `us.anthropic.claude-opus-4-5-20251101-v1:0`
   - `us.anthropic.claude-opus-4-6-v1`
   - `us.anthropic.claude-opus-4-6-v1:1m`
   - `us.anthropic.claude-opus-4-7`
   - `us.anthropic.claude-opus-4-7:1m`
   - `us.anthropic.claude-opus-4-8`
   - `us.anthropic.claude-opus-4-8:1m`
   - `us.anthropic.claude-opus-5`
   - `us.anthropic.claude-opus-5:1m`
   - `eu.amazon.nova-lite-v1:0`
   - `eu.amazon.nova-pro-v1:0`
   - `eu.amazon.nova-2-lite-v1:0`
   - `eu.anthropic.claude-3-haiku-20240307-v1:0`
   - `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
   - `eu.anthropic.claude-3-5-sonnet-20241022-v2:0`
   - `eu.anthropic.claude-3-7-sonnet-20250219-v1:0`
   - `eu.anthropic.claude-sonnet-4-20250514-v1:0`
   - `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`
   - `eu.anthropic.claude-sonnet-4-6`
   - `eu.anthropic.claude-sonnet-4-6:1m`
   - `eu.anthropic.claude-opus-4-5-20251101-v1:0`
   - `eu.anthropic.claude-opus-4-6-v1`
   - `eu.anthropic.claude-opus-4-6-v1:1m`
   - `eu.anthropic.claude-opus-4-7`
   - `eu.anthropic.claude-opus-4-7:1m`
   - `eu.anthropic.claude-opus-4-8`
   - `eu.anthropic.claude-opus-4-8:1m`
   - `eu.anthropic.claude-opus-5`
   - `eu.anthropic.claude-opus-5:1m`
   - `qwen.qwen3-vl-235b-a22b`
   - `global.amazon.nova-2-lite-v1:0`
   - `global.anthropic.claude-haiku-4-5-20251001-v1:0`
   - `global.anthropic.claude-sonnet-4-5-20250929-v1:0`
   - `global.anthropic.claude-sonnet-4-6`
   - `global.anthropic.claude-sonnet-4-6:1m`
   - `global.anthropic.claude-opus-4-5-20251101-v1:0`
   - `global.anthropic.claude-opus-4-6-v1`
   - `global.anthropic.claude-opus-4-6-v1:1m`
   - `global.anthropic.claude-opus-4-7`
   - `global.anthropic.claude-opus-4-7:1m`
   - `global.anthropic.claude-opus-4-8`
   - `global.anthropic.claude-opus-4-8:1m`
   - `global.anthropic.claude-opus-5`
   - `global.anthropic.claude-opus-5:1m`

3. **Configure prompts**: Customize system and task prompts for your specific use case
4. **Deploy**: The configuration can be updated through the Web UI without stack redeployment

### Benefits

**Advantages of Bedrock OCR:**
- **Multimodal Understanding**: LLMs can understand both visual layout and textual content
- **Context Awareness**: Better handling of complex document structures and relationships
- **Flexibility**: Customizable prompts for domain-specific terminology and formats
- **Advanced Reasoning**: Can handle challenging cases like handwritten text, poor quality scans, or complex layouts
- **Unified Processing**: Same models used for OCR, classification, and extraction provide consistency

**When to Use Bedrock OCR:**
- Documents with complex layouts or mixed content types
- Handwritten or low-quality documents where Textract struggles
- Domain-specific documents requiring contextual understanding
- When you want unified processing across the entire pipeline
- For experimental or specialized use cases requiring prompt customization

### Cost Considerations

**Bedrock OCR Pricing:**
- Charged per input/output token rather than per page
- Typically higher cost per page than Textract for standard documents
- Cost varies significantly by model (Nova Lite < Nova Pro < Claude models)
- Image tokens are more expensive than text tokens

**Cost Optimization Tips:**
1. **Model Selection**: Use Nova Lite for cost-sensitive applications, Claude for quality-critical use cases
2. **Image Preprocessing**: Enable image resizing and preprocessing to reduce token consumption
3. **Prompt Optimization**: Use concise, focused prompts to minimize token usage
4. **Hybrid Approach**: Use Textract for standard documents, Bedrock for complex cases
5. **Batch Processing**: Process multiple pages efficiently with concurrent processing

## OCR Configuration

The OCR service in Pattern 2 supports enhanced image processing capabilities for optimal text extraction:

### DPI Configuration

Configure DPI (Dots Per Inch) for PDF-to-image conversion:

```python
# Example OCR service initialization with custom DPI
ocr_service = OcrService(
    dpi=400,  # Higher DPI for better quality (default: 300)
    resize_config={
        'target_width': 1200,
        'target_height': 1600
    }
)
```

### Image Resizing Configuration

The OCR service supports optional image resizing for processing optimization:

```yaml
# OCR configuration example
ocr:
  image:
    resize_config:
      target_width: 951   # Target width for processing
      target_height: 1268 # Target height for processing
    preprocessing: true   # Enable adaptive binarization preprocessing
```

### OCR Image Processing Features

- **Configurable DPI**: Higher DPI (400+) for better quality, standard DPI (300) for balanced performance
- **Dual Image Strategy**: 
  - Stores original high-DPI images in S3 for archival and downstream processing
  - Uses resized images for OCR processing to optimize performance
- **Aspect Ratio Preservation**: Images are resized proportionally without distortion
- **Smart Scaling**: Only downsizes images when necessary (scale factor < 1.0)
- **Image Preprocessing**: Optional adaptive binarization to improve OCR accuracy on challenging documents
- **Enhanced Logging**: Detailed logging for DPI and resize operations

### Image Preprocessing Configuration

The OCR service supports optional adaptive binarization preprocessing to improve OCR accuracy on challenging documents:

```yaml
# Enable preprocessing for improved OCR accuracy
ocr:
  image:
    preprocessing: true  # Enable adaptive binarization
```

**Adaptive Binarization Benefits:**
- **Improved OCR Accuracy**: Significantly enhances text extraction on documents with uneven lighting, shadows, or low contrast
- **Background Noise Reduction**: Removes background gradients and noise that can interfere with OCR
- **Enhanced Edge Detection**: Sharpens text boundaries for better character recognition
- **Robust Processing**: Handles challenging document conditions like poor scans or faded text

**When to Enable Preprocessing:**
- Documents with uneven lighting or shadows
- Low contrast text or faded documents
- Scanned documents with background noise
- Handwritten or mixed content documents
- When standard OCR accuracy is insufficient

### Configuration Benefits

- **Quality Control**: Higher DPI settings improve OCR accuracy for complex documents
- **Performance Optimization**: Resized images reduce processing time and memory usage
- **Storage Efficiency**: Dual strategy balances quality preservation with processing efficiency
- **Preprocessing Enhancement**: Adaptive binarization improves OCR accuracy on challenging documents
- **Flexibility**: Runtime configuration allows adjustment without code changes
- **Backward Compatibility**: Default values maintain existing behavior

### Best Practices for OCR

1. **DPI Selection**:
   - Use 300 DPI for standard documents
   - Use 400+ DPI for documents with small text or complex layouts
   - Consider processing costs when using higher DPI settings

2. **Image Resizing**:
   - Enable resizing for large documents to improve processing speed
   - Maintain aspect ratios to preserve text readability
   - Test different dimensions based on document types

3. **Performance Tuning**:
   - Monitor processing times and adjust DPI/resize settings accordingly
   - Use concurrent processing for multi-page documents
   - Balance quality requirements with processing costs

## Customizing Classification

The pattern supports two different classification methods:

1. **Page-Level Classification (multimodalPageLevelClassification)**: This is the default method that classifies each page independently based on its visual layout and textual content. It outputs a simple JSON format with a single class label per page.

2. **Holistic Packet Classification (textbasedHolisticClassification)**: This method examines the document as a whole to identify boundaries between different document types within a multi-document packet. It can detect logical document boundaries and identifies document types in the context of the whole document. This is especially useful for packets where individual pages may not be clearly classifiable on their own. It outputs a JSON format that identifies document segments with start and end page numbers.

You can select which method to use by setting the `ClassificationMethod` parameter when deploying the stack.

The classification system uses RVL-CDIP dataset categories and can be customized through the configuration files. Classification models and prompts are now managed through the configuration library rather than CloudFormation parameters.

Available categories:
- letter
- form
- email
- handwritten
- advertisement
- scientific_report
- scientific_publication
- specification
- file_folder
- news_article
- budget
- invoice
- presentation
- questionnaire
- resume
- memo

## Few Shot Example Feature

Pattern 2 supports few shot learning through example-based prompting to significantly improve classification and extraction accuracy. This feature allows you to provide concrete examples of documents with their expected classifications and attribute extractions.

### Overview

Few shot examples work by including reference documents with known classifications and expected attribute values in the prompts sent to the AI model. This helps the model understand the expected format and accuracy requirements for your specific use case.

### Configuration

Few shot examples are configured using JSON Schema format in the configuration files in the `config_library/unified/` directory. The `few_shot_example` configuration demonstrates how to set up examples:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Letter
    x-aws-idp-document-type: Letter
    type: object
    description: "A formal written correspondence..."
    properties:
      SenderName:
        type: string
        description: "The name of the person who wrote the letter..."
    x-aws-idp-examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "SenderName": "Will E. Clark",
              "SenderAddress": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              "RecipientName": "The Honorable Wendell H. Ford"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Email
    x-aws-idp-document-type: Email
    type: object
    description: "A digital message with email headers..."
    x-aws-idp-examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'Email'"
        name: "Email1"
        x-aws-idp-attributes-prompt: |
          expected attributes are: 
             "FromAddress": "Kelahan, Ben",
             "ToAddress": "TI New York: 'TI Minnesota",
             "Subject": "FW: Morning Team Notes 4/20"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/email1.jpg"
```

### Benefits

Using few shot examples provides several advantages:

1. **Improved Accuracy**: Models perform better when given concrete examples
2. **Consistent Formatting**: Examples help ensure consistent output structure  
3. **Domain Adaptation**: Examples help models understand domain-specific terminology
4. **Reduced Hallucination**: Examples reduce the likelihood of made-up data
5. **Better Edge Case Handling**: Examples can demonstrate how to handle unusual cases

### Integration with Template Prompts

The few shot examples are automatically integrated into the classification and extraction prompts using the `{FEW_SHOT_EXAMPLES}` placeholder. You can also use the `{DOCUMENT_IMAGE}` placeholder for precise image positioning:

**Standard Template with Text Only:**
```python
# In classification task_prompt
task_prompt: |
  Classify this document into exactly one of these categories:
  {CLASS_NAMES_AND_DESCRIPTIONS}
  
  <few_shot_examples>
  {FEW_SHOT_EXAMPLES}
  </few_shot_examples>
  
  <document_ocr_data>
  {DOCUMENT_TEXT}
  </document_ocr_data>

# In extraction task_prompt  
task_prompt: |
  Extract attributes from this document.
  
  <few_shot_examples>
  {FEW_SHOT_EXAMPLES}
  </few_shot_examples>
  
  
  <document_ocr_data>
  {DOCUMENT_TEXT}
  </document_ocr_data>
```

**Enhanced Template with Image Placement:**
```python
# In classification task_prompt with image positioning
task_prompt: |
  Classify this document into exactly one of these categories:
  {CLASS_NAMES_AND_DESCRIPTIONS}
  
  <few_shot_examples>
  {FEW_SHOT_EXAMPLES}
  </few_shot_examples>
  
  Now examine this new document:
  {DOCUMENT_IMAGE}
  
  <document_ocr_data>
  {DOCUMENT_TEXT}
  </document_ocr_data>
  
  Classification:

# In extraction task_prompt with image positioning
task_prompt: |
  Extract attributes from this {DOCUMENT_CLASS} document:
  {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
  
  <few_shot_examples>
  {FEW_SHOT_EXAMPLES}
  </few_shot_examples>
  
  Analyze this document image:
  {DOCUMENT_IMAGE}
  
  <document_ocr_data>
  {DOCUMENT_TEXT}
  </document_ocr_data>
  
  Extract as JSON:
```

### Available Template Placeholders

Pattern 2 supports several placeholders for building dynamic prompts:

- **`{CLASS_NAMES_AND_DESCRIPTIONS}`**: List of document classes and their descriptions
- **`{FEW_SHOT_EXAMPLES}`**: Examples from the configuration (class-specific for extraction, all classes for classification)
- **`{DOCUMENT_TEXT}`**: OCR-extracted text content from the document
- **`{DOCUMENT_IMAGE}`**: Document image(s) positioned at specific locations in the prompt
- **`{DOCUMENT_CLASS}`**: The classified document type (used in extraction prompts)
- **`{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`**: List of attributes to extract with their descriptions

**Image Placement Benefits:**
- **Visual Context**: Position images where they provide maximum context for the task
- **Multimodal Understanding**: Help models correlate visual and textual information effectively
- **Flexible Design**: Create prompts that flow naturally between different content types
- **Enhanced Accuracy**: Strategic image placement can improve both classification and extraction performance

### Using Few Shot Examples

To use few shot examples in your deployment:

1. **Use the example configuration**: Deploy with `ConfigurationDefaultS3Uri` pointing to `config_library/unified/few_shot_example/config.yaml`
2. **Create custom examples**: Copy the example configuration and modify it with your own document examples
3. **Provide example images**: Place example document images in the appropriate directory and reference them in the `x-aws-idp-image-path` field

### Best Practices

1. **Quality over Quantity**: Use 1-3 high-quality examples per document class
2. **Representative Examples**: Choose examples that represent typical documents in your use case
3. **Clear Attribution**: Ensure examples clearly show expected attribute extractions
4. **Diverse Coverage**: Include examples that cover different variations and edge cases

## Customizing Extraction

The extraction system can be customized through the configuration files rather than CloudFormation parameters:

1. **Attribute Definitions**: 
   - Define attributes per document class in the `classes` section of the configuration
   - Specify descriptions for each attribute
   - Configure the format and structure

2. **Extraction Prompts**:
   - Customize system behavior through the `system_prompt` in configuration
   - Add domain expertise and guidance in the `task_prompt`
   - Modify output formatting requirements

3. **Model Selection**:
   - Model selection is handled through enum constraints in the configuration
   - Available models are defined in the configuration schema
   - Changes can be made through the Web UI without redeployment

Example attribute definition from the configuration using JSON Schema format:
```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: A commercial document issued by a seller to a buyer relating to a sale
    properties:
      InvoiceNumber:
        type: string
        description: The unique identifier for the invoice. Look for 'invoice no', 'invoice #', or 'bill number', typically near the top of the document.
      InvoiceDate:
        type: string
        description: The date when the invoice was issued. May be labeled as 'date', 'invoice date', or 'billing date'.
      TotalAmount:
        type: string
        description: The final amount to be paid including all charges. Look for 'total', 'grand total', or 'amount due', typically the last figure on the invoice.
```

## Confidence (Assessment)

As of **config v0.6**, per-field **confidence** and **geometry** are **outputs of
extraction**, not a separate downstream stage. Their settings live under
`extraction.confidence.*` and `extraction.geometry.*` (there is no top-level
`assessment.{model, geometry_mode, ...}` block anymore). This section is a Pattern
2 (Pipeline mode) summary; the full reference is
[Extraction & Confidence](./extraction-and-confidence.md).

### Overview

Confidence assessment produces, for each extracted attribute:
- **Confidence Scores**: Per-attribute confidence ratings (0.0-1.0)
- **Explanatory Reasoning**: Human-readable explanations for each confidence score
- **UI Integration**: Automatic display in the web interface visual editor
- **Cost Optimization**: Can be disabled entirely for zero LLM cost
- **Large-list batching**: Long list fields (hundreds of rows) are scored in sequential batches so every row is covered

### Confidence modes

`extraction.confidence.mode` controls *where* per-field confidence is produced:

- **`separate`** *(default)* — on the Simple (non-agentic) path, confidence runs
  as the standalone Assessment step; on the Advanced (agentic) path, it runs as a
  second inference inside each extraction shard and the standalone step auto-skips.
- **`integrated`** — a single extraction inference returns values **and** inline
  confidence together (works on **both** the simple and agentic paths); the
  service splits the envelope, enriches with thresholds, and emits
  `explainability_info`, so the standalone Assessment step auto-skips.
- **`off`** — no confidence scoring (equivalent to `enabled: false`); zero LLM cost.

```yaml
extraction:
  confidence:
    enabled: true             # false disables confidence entirely (zero LLM cost)
    mode: separate            # off | separate (default) | integrated
    model: us.amazon.nova-lite-v1:0
    temperature: 0.0
    list_batch_size: 25       # rows per assessment batch for large lists
    system_prompt: "You are an expert document analyst..."
    task_prompt: |
      Assess the confidence of extraction results for this {DOCUMENT_CLASS} document.
      Extraction Results: {EXTRACTION_RESULTS}
      Attributes: {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
      Document Text: {DOCUMENT_TEXT}
      OCR Confidence: {OCR_TEXT_CONFIDENCE}
      {DOCUMENT_IMAGE}
```

**Behavior when disabled** (`enabled: false` or `mode: off`): the assessment
Lambda is still invoked (minimal overhead) but returns immediately with logging
"Assessment is disabled via configuration" — no LLM calls, no S3 operations.
Defaults to enabled when the property is missing.

> **Migration note:** The previous `IsAssessmentEnabled` CloudFormation parameter
> has been removed in favor of this configuration-based control.

### Large lists (`list_batch_size`)

For documents with large lists (bank statements with hundreds of transactions,
line-item tables, brokerage holdings), the standalone Assessment step **batches
large lists automatically**: it slices the largest list field into
`extraction.confidence.list_batch_size` chunks (default **25**), assesses each
chunk sequentially with the shared scalars/context, then reconciles the results
so **every** list cell gets its own confidence and bounding box. A bounded
missing-row retry re-scores any rows the model dropped, so coverage reaches 100%.
Lower `list_batch_size` if a model still struggles to enumerate a full chunk;
raise it to reduce the number of inference calls.

> **Granular assessment is retired.** The former "granular assessment" service
> (a thread-pool fan-out with DynamoDB caching, formerly configured under
> `assessment.granular` / `extraction.confidence.granular`) has been **retired and
> deleted**. Large-list batching is its full replacement. Any leftover
> `granular.*` keys still validate but are ignored — no config edit required. See
> [Granular Assessment Retirement](./migration-granular-retirement.md).

### For large documents, prefer Advanced (agentic) extraction

For very large or complex documents, prefer **Advanced (agentic) extraction** —
it shards both extraction and confidence assessment and produced the
best-calibrated confidence in testing. Simple + `separate` remains fully viable
for large lists via the batching above.

### Comprehensive Documentation

For detailed information about confidence and geometry configuration, output
formats, confidence thresholds, UI integration, cost optimization, and
troubleshooting, see [Extraction & Confidence](./extraction-and-confidence.md).
That guide covers:
- Complete configuration examples and placeholders
- Attribute types and assessment formats (simple, group, list)
- Confidence threshold configuration and UI integration
- Confidence modes (`off` / `separate` / `integrated`) and geometry modes
- Large-list batching and the missing-row retry
- Cost optimization strategies and token reduction techniques
- Multimodal assessment with image processing
- Best practices

## Best Practices

1. **Configuration Management**:
   - Use the configuration library for different use cases (default, medical_records, few_shot_example)
   - Test configuration changes thoroughly before production deployment
   - Leverage the Web UI for configuration updates without redeployment

2. **Throttling Management**:
   - Implement exponential backoff with jitter
   - Configure appropriate retry limits
   - Monitor throttling metrics

3. **Error Handling**:
   - Comprehensive error logging
   - Graceful degradation
   - Clear error messages

4. **Performance Optimization**:
   - Concurrent processing where appropriate
   - Image optimization
   - Resource pooling

5. **Monitoring**:
   - Detailed CloudWatch metrics
   - Performance dashboards
   - Error tracking

6. **Security**:
   - KMS encryption
   - Least privilege IAM roles
   - Secure configuration management

7. **Few Shot Examples**:
   - Use high-quality, representative examples
   - Include examples for all document classes you expect to process
   - Regularly review and update examples based on real-world performance
   - Test configurations with examples before production deployment

8. **Image Processing Optimization**:
   - Configure appropriate image dimensions for each service based on document complexity
   - Use higher DPI (400+) for OCR when processing documents with small text or complex layouts
   - Balance image quality with processing performance and costs
   - Test different image configurations with your specific document types
   - Monitor memory usage and processing times when adjusting image settings
   - Leverage the dual image strategy in OCR to preserve quality while optimizing processing