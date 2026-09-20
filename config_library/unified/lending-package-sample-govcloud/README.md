Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Default-Lending Configuration (GovCloud)

This directory contains the default-lending GovCloud configuration for the GenAI IDP Accelerator. This configuration is specifically designed for processing lending and financial document packages commonly used in loan applications, underwriting, and financial verification processes in AWS GovCloud regions.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM (Nova/Claude) for classification, extraction, assessment, and summarization. Set use_bda: true for BDA mode.

## GovCloud Deployment

This configuration uses model IDs that are available in AWS GovCloud regions:
- **Amazon Nova Pro**: `amazon.nova-pro-v1:0`
- **Amazon Nova Lite**: `amazon.nova-lite-v1:0`
- **Anthropic Claude Sonnet 4.5**: `anthropic.claude-sonnet-4-5-20250929-v1:0`

## Validation Level

**Level**: 2 - Minimal Testing

- **Testing Evidence**: This configuration has been lightly tested with lending sample document including payslips, driver's licenses, bank statements, checks, W2 forms, and insurance applications. 
- **Known Limitations**: Performance may vary with non-standard document formats, heavily redacted financial documents, or documents with poor image quality that affect OCR accuracy.

## Overview

The default-lending configuration is designed to handle comprehensive lending document packages typically encountered in:

- **Loan Applications**: Personal and commercial lending
- **Mortgage Processing**: Home loan documentation
- **Credit Assessment**: Income and asset verification
- **Underwriting**: Risk assessment documentation
- **Compliance Verification**: Financial record validation

It includes specialized settings for document classification, detailed financial information extraction, and document summarization using Amazon Bedrock models optimized for financial document processing.

## Key Components

### Document Classes

The configuration defines 6 specialized lending document classes, each with comprehensive attributes for detailed financial data extraction:

- **Payslip**: Employee wage statements with detailed earnings, deductions, taxes, and year-to-date totals (21 simple attributes, 3 group attributes, 3 list attributes)
- **US-drivers-licenses**: Government-issued identification documents with personal information and driving privileges (7 simple attributes, 3 group attributes, 2 list attributes)
- **Bank-checks**: Written financial instruments with payment details and account information (11 simple attributes)
- **Bank-Statement**: Periodic financial reports with account activity and transaction details (8 simple attributes, 2 list attributes)
- **W2**: Annual tax documents with comprehensive wage and tax withholding information (2 simple attributes, 5 group attributes, 2 list attributes)
- **Homeowners-Insurance-Application**: Insurance coverage applications with detailed applicant and property information (10 simple attributes, 3 group attributes)

### Classification Settings

- **Model**: Amazon Nova Pro (`amazon.nova-pro-v1:0`)
- **Method**: Multimodal Page Level Classification
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **OCR Backend**: Amazon Textract with LAYOUT, TABLES, and SIGNATURES features

The classification component analyzes document content and structure to accurately identify lending document types and establish proper page boundaries within multi-document packages.

### Extraction Settings

- **Model**: Amazon Nova Pro (`amazon.nova-pro-v1:0`)
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Max Tokens**: 10,000 (increased for detailed financial data)

The extraction component performs comprehensive attribute extraction tailored to each lending document type, capturing critical financial information including:
- Detailed income and deduction breakdowns
- Personal identification information
- Account numbers and financial institution details
- Tax withholding and year-to-date totals
- Insurance coverage details and applicant information

### Assessment Settings

- **Model**: Amazon Nova Lite (`amazon.nova-lite-v1:0`)
- **Granular Assessment**: Enabled with parallel processing
- **Default Confidence Threshold**: 0.9
- **Max Workers**: 20 for improved performance

Enhanced confidence assessment ensures high accuracy for financial data extraction, critical for lending decisions.

### Summarization Settings

- **Model**: Anthropic Claude Sonnet 4.5 (`anthropic.claude-sonnet-4-5-20250929-v1:0`)
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The summarization component creates structured summaries of lending documents with proper citations, essential for loan documentation and compliance.

### Evaluation Settings

- **Model**: Anthropic Claude Sonnet 4.5 (`anthropic.claude-sonnet-4-5-20250929-v1:0`)
- **Temperature**: 0 (deterministic outputs)

## Sample Documents

This configuration is optimized for processing lending document packages that typically include:

- **Income Verification**: Payslips, W2 forms, tax returns
- **Identity Verification**: Driver's licenses, state IDs
- **Asset Verification**: Bank statements, investment accounts
- **Payment History**: Bank checks, payment records
- **Insurance Documentation**: Homeowner's insurance applications and policies

## How to Use

To use this default-lending GovCloud configuration:

1. **Direct Deployment**: Deploy the GenAI IDP Accelerator with this configuration for lending document processing workflows in GovCloud regions.

2. **Loan Processing Integration**: Integrate with existing loan origination systems for automated document processing and data extraction.

3. **Compliance Workflows**: Use for regulatory compliance documentation and audit trail generation.

4. **Custom Lending Workflows**: Adapt for specific lending scenarios.

## Common Customization Scenarios

### Adding New Financial Document Classes

To add additional lending document types (e.g., tax returns, employment verification letters):

1. Add a new entry to the `classes` array:
   ```yaml
   - name: tax_return
     description: Individual or business tax return documents containing income and deduction information
     attributes:
       - name: tax_year
         description: The tax year for which the return was filed. Look for 'Tax Year' or year designation at the top of the form.
       - name: filing_status
         description: The taxpayer's filing status such as Single, Married Filing Jointly, etc.
   ```

2. Test with representative tax return documents.

### Customizing Extraction Prompts for Compliance

For enhanced compliance and audit requirements:

1. Modify the extraction `task_prompt` to include compliance-specific instructions:
   ```yaml
   task_prompt: |
     Extract financial information with particular attention to:
     - Verification of income sources and amounts
     - Identification of any discrepancies or missing information
     - Compliance with lending regulatory requirements
   ```

### Adjusting Confidence Thresholds for Financial Data

For critical lending decisions, you may want higher confidence thresholds:

1. Update the `default_confidence_threshold` in the assessment section:
   ```yaml
   assessment:
     default_confidence_threshold: '0.95'  # Higher threshold for financial data
   ```

## Performance Considerations

The default-lending configuration is optimized for:

- **High Accuracy**: Temperature 0 and elevated confidence thresholds for reliable financial data extraction
- **Comprehensive Coverage**: Detailed attribute definitions covering all critical lending information
- **Compliance**: Structured outputs suitable for regulatory documentation and audit trails
- **Scalability**: Granular assessment with parallel processing for high-volume lending workflows

### Financial Data Specific Optimizations

- **OCR Enhancement**: Uses SIGNATURES feature to detect signed documents
- **Table Processing**: TABLES feature for structured financial data in statements
- **Layout Preservation**: LAYOUT feature maintains document structure for complex forms
- **Extended Token Limits**: 10,000 tokens for comprehensive financial document processing

## Security and Compliance Considerations

When processing lending documents in GovCloud:

- **Data Privacy**: Ensure compliance with financial privacy regulations (GLBA, CCPA, etc.)
- **FedRAMP Compliance**: GovCloud regions provide FedRAMP High authorization
- **Encryption**: Use encrypted storage and transmission for all financial documents
- **Access Controls**: Implement proper authentication and authorization
- **Audit Logging**: Maintain comprehensive logs for regulatory compliance
- **Data Retention**: Follow applicable data retention policies for financial records

## Integration Guidelines

### Loan Origination Systems (LOS)

This configuration can be integrated with popular LOS platforms:
- Automated document classification upon upload
- Real-time data extraction for loan application prefill
- Exception handling for documents requiring manual review

### Credit Decisioning

Extracted data can feed directly into credit decisioning engines:
- Income verification from payslips and W2s
- Asset verification from bank statements
- Identity verification from driver's licenses

## Contributors

- GenAI IDP Accelerator Team
- Lending Solutions Architecture Team
