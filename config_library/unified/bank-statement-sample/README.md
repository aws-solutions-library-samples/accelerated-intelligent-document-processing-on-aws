Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Bank Statement Sample Configuration

This directory contains a specialized configuration for processing bank statements using the GenAI IDP Accelerator. This configuration demonstrates the new nested attribute support including simple attributes, group attributes, and list attributes.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova/Claude) for classification, extraction, assessment, and summarization. Set use_bda: true for BDA mode.

## Validation Level

**Level**: 0 - Modest Testing

- **Testing Evidence**: This configuration has been tested with the provided sample document `samples/bank-statement-multipage.pdf`. It demonstrates accurate extraction of account information, address details, and individual transaction records. See [notebook examples](../../../notebooks/usecase-specific-examples/multi-page-bank-statement)
- **Production Usage**: This configuration serves as a reference implementation for financial document processing workflows
- **Known Limitations**: Will require adjustments for specialized financial institutions with unique layouts

## Overview

The bank statement sample configuration is designed to handle multi-page bank account statements with complex nested data structures. This configuration showcases the full capabilities of the GenAI IDP Accelerator's nested attribute support.

The configuration processes bank statements to extract:

- Account-level information (simple attributes)
- Customer address details (group attributes) 
- Individual transaction records (list attributes)

## Key Components

### Document Classes

The configuration defines 1 document class with comprehensive nested attributes:

- **Bank Statement**: Monthly bank account statement
  - **Simple Attributes**: Account Number, Statement Period
  - **Group Attributes**: Account Holder Address (Street Number, Street Name, City, State, ZIP Code)
  - **List Attributes**: Transactions (Date, Description, Amount for each transaction)

### Attribute Types Demonstration

This configuration demonstrates all three attribute types supported by the GenAI IDP Accelerator:

#### 1. Simple Attributes
- **Account Number**: Primary account identifier (EXACT evaluation)
- **Statement Period**: Statement period like "January 2024" (FUZZY evaluation with 0.8 threshold)

#### 2. Group Attributes  
- **Account Holder Address**: Nested object structure containing:
  - **Street Number**: House or building number (FUZZY evaluation with 0.9 threshold)
  - **Street Name**: Name of the street (FUZZY evaluation with 0.8 threshold)
  - **City**: City name (FUZZY evaluation with 0.9 threshold)
  - **State**: State abbreviation (EXACT evaluation)
  - **ZIP Code**: 5 or 9 digit postal code (EXACT evaluation)

#### 3. List Attributes
- **Transactions**: Array of transaction records, each containing:
  - **Date**: Transaction date in ISO-8601 format (YYYY-MM-DD), matching the field's `format: date` (DATE evaluation, which is format-tolerant)
  - **Description**: Transaction description or merchant name (SEMANTIC evaluation with 0.7 threshold)
  - **Amount**: Transaction amount with positive/negative values (NUMERIC_EXACT evaluation)

### Classification Settings

- **Model**: Amazon Nova Pro
- **Method**: Text-based holistic classification
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The classification component analyzes the entire document package to identify bank statement sections and page boundaries.

### Extraction Settings

- **Model**: Amazon Nova Pro  
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Document Image Support**: Uses `{DOCUMENT_IMAGE}` placeholder for multimodal extraction

The extraction component processes each bank statement section to extract structured data including nested address information and transaction lists.

### Assessment Settings

- **Model**: Claude 3.7 Sonnet
- **Default Confidence Threshold**: 0.9
- **Temperature**: 0 (deterministic outputs)

The assessment component evaluates extraction confidence for each attribute, including nested structures, with individual confidence thresholds per attribute type.

### Evaluation Settings

- **Model**: Claude 3 Haiku (for LLM evaluations)
- **Evaluation Methods**: 
  - EXACT: For account numbers, state abbreviations, ZIP codes
  - FUZZY: For names, addresses, dates (with configurable thresholds)
  - SEMANTIC: For transaction descriptions  
  - NUMERIC_EXACT: For transaction amounts

## Key Differences from Default Configuration

### 1. Nested Attribute Support

Unlike the default configuration which only uses simple attributes, this configuration demonstrates:
- **Group attributes** for structured address information
- **List attributes** for variable-length transaction records
- **Mixed evaluation methods** tailored to each attribute type

### 2. Financial Document Specialization

- Optimized prompts for bank statement processing
- Specialized evaluation methods for financial data (NUMERIC_EXACT for amounts)
- Higher confidence thresholds for critical financial information

### 3. Multimodal Enhancement

- Uses `{DOCUMENT_IMAGE}` placeholder for improved extraction accuracy
- Combines text and visual analysis for complex document layouts
- Enhanced assessment capabilities with image-based confidence evaluation

## Sample Documents

This configuration includes the following sample document:

- `samples/bank-statement-multipage.pdf`: A multi-page bank statement demonstrating account information, customer address, and multiple transaction records across several pages

## Performance Metrics

Based on testing with the provided sample document:

| Metric | Value | Notes |
|--------|-------|-------|
| Classification Accuracy | >95% | Accurate identification of bank statement sections |
| Simple Attribute Accuracy | >90% | Account numbers and periods extracted reliably |
| Group Attribute Accuracy | >85% | Address components extracted with high fidelity |
| List Attribute Accuracy | >90% | Transaction details extracted accurately per item |

## Usage Instructions

To use this bank statement configuration:

1. **Deploy with Sample**: Upload `samples/bank-statement-multipage.pdf` to test the configuration
2. **Review Results**: Examine the extracted nested data structure in the UI
3. **Evaluate Performance**: Use the evaluation framework to compare against baseline results
4. **Customize**: Modify attribute definitions for your specific bank statement formats

## Customization Guidance

### Adding New Attributes

To add new simple attributes:
```yaml
- name: "Routing Number"
  description: "Bank routing number"
  attributeType: simple
  evaluation_method: EXACT
```

To extend the address group:
```yaml
groupAttributes:
  - name: "Country"
    description: "Country name"
    evaluation_method: EXACT
```

To add new transaction fields:
```yaml
itemAttributes:
  - name: "Category"
    description: "Transaction category"
    evaluation_method: SEMANTIC
    evaluation_threshold: '0.8'
```

### Modifying Evaluation Methods

Adjust evaluation methods and thresholds based on your accuracy requirements:
- Use `EXACT` for critical identifiers
- Use `FUZZY` with high thresholds (0.8-0.9) for names and addresses  
- Use `SEMANTIC` for descriptive fields
- Use `NUMERIC_EXACT` for financial amounts

## Contributors

- GenAI IDP Accelerator Team

## Version History

- v1.0 (2025-06-19): Initial release demonstrating nested attribute support for bank statements
