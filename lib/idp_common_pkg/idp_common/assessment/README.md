Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Assessment Service for IDP Accelerator

This module provides assessment capabilities for evaluating document extraction confidence using LLMs within the IDP Accelerator project.

## Overview

The Assessment service is designed to assess the confidence and accuracy of extraction results by analyzing them against source documents using LLMs. It supports both text and image content analysis and provides detailed confidence scores and explanations for each extracted attribute.

## Features

- **LLM-powered confidence assessment** using Amazon Bedrock models
- **Multi-modal analysis** with support for both document text and images
- **Optimized token usage** with pre-generated text confidence data (80-90% reduction)
- **Structured confidence output** with scores and explanations per attribute
- **Prompt template support** with placeholder substitution
- **Image placeholder positioning** for precise multimodal prompt construction
- **Fallback mechanisms** for robust error handling
- **Metering integration** for usage tracking
- **Direct Document model integration**

## Usage Example

```python
from idp_common.assessment.service import AssessmentService
from idp_common.models import Document

# Initialize assessment service with configuration
assessment_service = AssessmentService(
    region="us-east-1",
    config=config_dict
)

# Process a single section
document = assessment_service.process_document_section(document, section_id="1")

# Or assess entire document
document = assessment_service.assess_document(document)

# Access assessment results in the extraction results
section = document.sections[0]
extraction_data = s3.get_json_content(section.extraction_result_uri)
assessment_info = extraction_data.get("explainability_info", {})

# Example assessment output:
# {
#   "vendor_name": {
#     "confidence": 0.95,
#     "confidence_reason": "Vendor name clearly visible in header with high OCR confidence"
#   },
#   "total_amount": {
#     "confidence": 0.87,
#     "confidence_reason": "Amount visible but OCR confidence slightly lower due to formatting"
#   }
# }
```

## Configuration

The assessment service uses configuration-driven prompts and model parameters:

```yaml
assessment:
  model: "anthropic.claude-3-5-sonnet-20241022-v2:0"
  temperature: 0
  top_k: 5
  top_p: 0.1
  max_tokens: 4096
  system_prompt: "You are an expert document analyst..."
  task_prompt: |
    Assess the confidence of extraction results for this {DOCUMENT_CLASS} document.
    
    Text Confidence Data:
    {OCR_TEXT_CONFIDENCE}
    
    Extraction Results:
    {EXTRACTION_RESULTS}
    
    Attributes Definition:
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    
    Document Images:
    {DOCUMENT_IMAGE}
    
    Respond with confidence assessments in JSON format.
```

## Prompt Template Placeholders

The assessment service supports the following placeholders in prompt templates:

### Standard Placeholders
- `{DOCUMENT_TEXT}` - Parsed document text (markdown format)
- `{DOCUMENT_CLASS}` - Document classification (e.g., "invoice", "contract")
- `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` - Formatted list of attributes to extract
- `{EXTRACTION_RESULTS}` - JSON of extraction results to assess

### OCR Confidence Data
- `{OCR_TEXT_CONFIDENCE}` - **NEW** - Optimized text confidence data with 80-90% token reduction

### Image Positioning
- `{DOCUMENT_IMAGE}` - Placeholder for precise image positioning in multimodal prompts

## Text Confidence Data Integration

The assessment service automatically uses pre-generated text confidence data when available, providing significant performance and cost benefits:

### Automatic Data Source Selection
1. **Primary**: Uses pre-generated `textConfidence.json` files from OCR processing
2. **Fallback**: Generates text confidence data on-demand from raw OCR for backward compatibility

### Token Usage Optimization
```python
# Traditional approach (high token usage)
prompt = f"OCR Data: {raw_textract_response}"  # ~50,000 tokens

# Optimized approach (low token usage)  
prompt = f"Text Confidence Data: {text_confidence_data}"  # ~5,000 tokens
```

### Data Format
The text confidence data provides essential information in a minimal format:

```json
{
  "page_count": 2,
  "text_blocks": [
    {
      "text": "INVOICE #12345",
      "confidence": 98.7
    },
    {
      "text": "Date: March 15, 2024",
      "confidence": 95.2
    }
  ]
}
```

## Multimodal Assessment

The service supports sophisticated multimodal prompts with precise image positioning:

### Image Placeholder Usage
```python
task_prompt = """
Analyze the extraction results for accuracy.

Extraction Results:
{EXTRACTION_RESULTS}

{DOCUMENT_IMAGE}

Based on the document image above and the OCR confidence data below, 
assess each extracted field:

{OCR_TEXT_CONFIDENCE}
"""
```

### Automatic Image Handling
- Supports both single and multiple document images
- Automatically limits to 20 images per Bedrock constraints
- Graceful fallback when images are unavailable

## Attribute Types and Assessment Formats

The assessment service supports three distinct attribute types, each requiring a specific assessment response format. The service automatically detects the attribute type from your document class configuration and handles the assessment processing accordingly.

### 1. Simple Attributes

For basic single-value extractions like dates, amounts, or names.

**Configuration Example:**
```yaml
attributes:
  - name: "InvoiceNumber"
    attributeType: "simple"  # or omit for default
    description: "The invoice number from the document"
  - name: "TotalAmount"
    attributeType: "simple"
    description: "The total amount due"
```

**Expected Assessment Response:**
```json
{
  "InvoiceNumber": {
    "confidence": 0.92,
    "confidence_reason": "Invoice number clearly visible in standard location"
  },
  "TotalAmount": {
    "confidence": 0.87,
    "confidence_reason": "Amount visible but OCR confidence slightly lower due to formatting"
  }
}
```

### 2. Group Attributes

For nested object structures with multiple related fields that are logically grouped together.

**Configuration Example:**
```yaml
attributes:
  - name: "VendorDetails"
    attributeType: "group"
    description: "Vendor contact information"
    groupAttributes:
      - name: "VendorName"
        description: "Name of the vendor company"
      - name: "VendorAddress"
        description: "Vendor's business address"
      - name: "VendorPhone"
        description: "Vendor's contact phone number"
```

**Expected Assessment Response:**
```json
{
  "VendorDetails": {
    "VendorName": {
      "confidence": 0.95,
      "confidence_reason": "Company name clearly printed in header"
    },
    "VendorAddress": {
      "confidence": 0.88,
      "confidence_reason": "Address visible with good OCR quality"
    },
    "VendorPhone": {
      "confidence": 0.82,
      "confidence_reason": "Phone number partially blurred but readable"
    }
  }
}
```

### 3. List Attributes

For arrays of items where each item has the same structure, such as line items, transactions, or entries.

**Configuration Example:**
```yaml
attributes:
  - name: "LineItems"
    attributeType: "list"
    description: "Individual line items on the invoice"
    listItemTemplate:
      itemDescription: "A single invoice line item"
      itemAttributes:
        - name: "Description"
          description: "Item description or service name"
        - name: "Quantity"
          description: "Number of items or hours"
        - name: "UnitPrice"
          description: "Price per unit"
        - name: "Total"
          description: "Line item total (quantity × unit price)"
```

**Expected Assessment Response:**
```json
{
  "LineItems": [
    {
      "Description": {
        "confidence": 0.94,
        "confidence_reason": "Service description clearly printed"
      },
      "Quantity": {
        "confidence": 0.91,
        "confidence_reason": "Quantity number easily readable"
      },
      "UnitPrice": {
        "confidence": 0.89,
        "confidence_reason": "Unit price in standard currency format"
      },
      "Total": {
        "confidence": 0.93,
        "confidence_reason": "Total amount calculation clearly visible"
      }
    },
    {
      "Description": {
        "confidence": 0.87,
        "confidence_reason": "Description text slightly compressed but readable"
      },
      "Quantity": {
        "confidence": 0.95,
        "confidence_reason": "Quantity clearly printed in quantity column"
      },
      "UnitPrice": {
        "confidence": 0.88,
        "confidence_reason": "Unit price readable with minor OCR uncertainty"
      },
      "Total": {
        "confidence": 0.92,
        "confidence_reason": "Line total properly formatted and clear"
      }
    }
  ]
}
```

### Service Processing Behavior

The assessment service automatically handles each attribute type differently:

**Simple Attributes:**
- Expects a single confidence assessment object
- Adds confidence threshold to the assessment data
- Creates alerts for low confidence scores

**Group Attributes:**
- Processes each sub-attribute within the group independently
- Applies confidence thresholds to each sub-attribute
- Creates individual alerts for each sub-attribute that falls below threshold

**List Attributes:**
- Processes each array item separately (individual assessment per list item)
- Applies the same confidence thresholds to all items in the list
- Creates alerts using array notation (e.g., "LineItems[0].Description", "LineItems[1].Total")
- **Important**: Does NOT create aggregate assessments - each item must be assessed individually

### Assessment Response Requirements

**Critical Guidelines:**

1. **Structure Matching**: Assessment response must exactly mirror the extraction result structure
2. **List Processing**: For list attributes, assess each array item individually, never as an aggregate
3. **Nested Consistency**: Group attributes require confidence assessments for all sub-attributes
4. **Individual Focus**: Each confidence assessment should evaluate a specific field, not summarize multiple fields

**Common Mistakes to Avoid:**

```json
// ❌ WRONG: Aggregate assessment for list
{
  "LineItems": {
    "confidence": 0.85,
    "confidence_reason": "Overall line items look good"
  }
}

// ✅ CORRECT: Individual item assessments
{
  "LineItems": [
    {
      "Description": {"confidence": 0.94, "confidence_reason": "..."},
      "Quantity": {"confidence": 0.91, "confidence_reason": "..."}
    },
    {
      "Description": {"confidence": 0.87, "confidence_reason": "..."},
      "Quantity": {"confidence": 0.95, "confidence_reason": "..."}
    }
  ]
}
```

## Complete Assessment Output Example

Here's a comprehensive example showing all three attribute types in a single assessment:

```json
{
  "inference_result": {
    "InvoiceNumber": "INV-12345",
    "VendorDetails": {
      "VendorName": "ACME Corporation",
      "VendorAddress": "123 Business St, City, ST 12345",
      "VendorPhone": "(555) 123-4567"
    },
    "LineItems": [
      {
        "Description": "Professional Services",
        "Quantity": "40",
        "UnitPrice": "$125.00",
        "Total": "$5,000.00"
      },
      {
        "Description": "Materials",
        "Quantity": "10",
        "UnitPrice": "$25.00", 
        "Total": "$250.00"
      }
    ]
  },
  "explainability_info": [
    {
      "InvoiceNumber": {
        "confidence": 0.92,
        "confidence_reason": "Invoice number clearly visible in standard header location",
        "confidence_threshold": 0.85
      },
      "VendorDetails": {
        "VendorName": {
          "confidence": 0.95,
          "confidence_reason": "Company name clearly printed in document header with high OCR confidence",
          "confidence_threshold": 0.90
        },
        "VendorAddress": {
          "confidence": 0.88,
          "confidence_reason": "Address visible with good OCR quality, standard formatting",
          "confidence_threshold": 0.80
        },
        "VendorPhone": {
          "confidence": 0.82,
          "confidence_reason": "Phone number readable but slightly compressed in layout",
          "confidence_threshold": 0.75
        }
      },
      "LineItems": [
        {
          "Description": {
            "confidence": 0.94,
            "confidence_reason": "Service description clearly printed in line item table",
            "confidence_threshold": 0.80
          },
          "Quantity": {
            "confidence": 0.91,
            "confidence_reason": "Quantity number clearly visible in quantity column",
            "confidence_threshold": 0.85
          },
          "UnitPrice": {
            "confidence": 0.89,
            "confidence_reason": "Unit price in standard currency format, well aligned",
            "confidence_threshold": 0.85
          },
          "Total": {
            "confidence": 0.93,
            "confidence_reason": "Total amount clearly calculated and displayed",
            "confidence_threshold": 0.85
          }
        },
        {
          "Description": {
            "confidence": 0.87,
            "confidence_reason": "Description text slightly compressed but fully readable",
            "confidence_threshold": 0.80
          },
          "Quantity": {
            "confidence": 0.95,
            "confidence_reason": "Quantity clearly printed with excellent OCR confidence",
            "confidence_threshold": 0.85
          },
          "UnitPrice": {
            "confidence": 0.88,
            "confidence_reason": "Unit price readable with standard formatting",
            "confidence_threshold": 0.85
          },
          "Total": {
            "confidence": 0.92,
            "confidence_reason": "Line total properly formatted and clearly visible",
            "confidence_threshold": 0.85
          }
        }
      ]
    }
  ],
  "metadata": {
    "assessment_time_seconds": 4.23,
    "assessment_parsing_succeeded": true
  }
}
```

## Error Handling and Fallbacks

The assessment service includes comprehensive error handling:

### Parsing Failures
- Automatic fallback to default confidence scores (0.5) when LLM response parsing fails
- Detailed error logging for troubleshooting
- Continued processing of other sections

### Data Source Fallbacks
- Primary: Pre-generated text confidence files
- Secondary: On-demand text confidence generation from raw OCR
- Tertiary: Graceful degradation without OCR confidence data

### Template Validation
- Validates required placeholders in prompt templates
- Fallback to default prompts when template validation fails
- Flexible placeholder enforcement for partial templates

## Integration Example

```python
import json
from idp_common.assessment.service import AssessmentService
from idp_common.models import Document
from idp_common import s3

def lambda_handler(event, context):
    # Initialize service
    assessment_service = AssessmentService(
        region=os.environ['AWS_REGION'],
        config=event.get('config', {})
    )
    
    # Get document from event
    document = Document.from_dict(event['document'])
    
    # Assess all sections in the document
    assessed_document = assessment_service.assess_document(document)
    
    # Return updated document
    return {
        'document': assessed_document.to_dict()
    }
```

## Best Practices

### Prompt Design
- Use `{OCR_TEXT_CONFIDENCE}` instead of raw OCR data for optimal token usage
- Position `{DOCUMENT_IMAGE}` strategically in multimodal prompts
- Include clear instructions for confidence scoring (0.0 to 1.0 scale)

### Configuration
- Set appropriate temperature (0 for deterministic assessment)
- Configure max_tokens based on expected response length
- Use system prompts to establish assessment criteria

### Performance
- Leverage pre-generated text confidence data for best performance
- Monitor assessment timing and token usage through metering data
- Consider image limits for large multi-page documents

## Service Classes

### AssessmentService

Main service class for document assessment:

```python
class AssessmentService:
    def __init__(self, region: str = None, config: Dict[str, Any] = None)
    
    def process_document_section(self, document: Document, section_id: str) -> Document
    def assess_document(self, document: Document) -> Document
    
    # Internal methods for text confidence data and prompt building
    def _get_text_confidence_data(self, page) -> str
    def _build_content_with_or_without_image_placeholder(...) -> List[Dict[str, Any]]
```

### Assessment Models

Data models for structured assessment results:

```python
@dataclass
class AttributeAssessment:
    confidence: float
    confidence_reason: str

@dataclass 
class AssessmentResult:
    attributes: Dict[str, AttributeAssessment]
    metadata: Dict[str, Any]
