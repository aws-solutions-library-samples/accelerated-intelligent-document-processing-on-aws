Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Common Package

This package contains common utilities and services for the GenAI IDP Accelerator patterns.

## 📑 Documentation Structure

This README provides a high-level overview of the package. For detailed documentation:

- [**Core Data Models**](idp_common/README.md): Central document model, compression support, and key classes
- [**OCR**](idp_common/ocr/README.md): Document text extraction using AWS Textract
- [**Classification**](idp_common/classification/README.md): Document type identification using LLMs or SageMaker
- [**Extraction**](idp_common/extraction/README.md): Structured information extraction from documents
- [**Evaluation**](idp_common/evaluation/README.md): Accuracy measurement against ground truth
- [**Summarization**](idp_common/summarization/README.md): Document summary generation
- [**DynamoDB**](idp_common/dynamodb/README.md): Document tracking storage in the TrackingTable, plus the [document service factory](idp_common/docs_service_README.md) most Lambdas call
- [**Reporting**](idp_common/reporting/README.md): Analytics data storage and management
- [**BDA**](idp_common/bda/README.md): Integration with Bedrock Data Automation

## ✨ Components

### Core Services

- **Core Data Model**: Central document processing pipeline structure ([models.py](idp_common/models.py))
- **OCR**: Text extraction using AWS Textract
- **Classification**: Document type identification using LLMs and SageMaker/UDOP
- **Extraction**: Structured field extraction using LLMs
- **Evaluation**: Results comparison against ground truth
- **Summarization**: Document summary generation
- **DynamoDB**: Document tracking writes/reads against the TrackingTable, reached through the `docs_service` factory
- **API adapter**: `idp_common.api_adapter`, which normalizes an API Gateway request into the resolver event shape for the UI's REST dispatcher
- **Reporting**: Analytics data storage
- **Discovery**: Automated document class schema generation using LLMs

### AWS Service Clients

- Bedrock client with retry logic
- S3 client operations
- CloudWatch metrics
- DynamoDB client for TrackingTable operations

### Configuration

- DynamoDB-based configuration management
- Support for default and custom configuration merging

## 🚀 Getting Started

### Installation

> ⚠️ **Always install from a local checkout — never by bare name.**
> `idp_common` is first-party: it lives in this repo and is **not published to
> PyPI**. The name `idp-common` on public PyPI is registered by an unrelated
> third party, so `pip install idp_common` installs *someone else's package*.
> Every command below therefore uses a path (`./` or `-e`). See
> [Installing First-Party Packages Safely](../../docs/dependency-confusion.md).

To minimize Lambda package size, install only the components you need. Run these
from the repository root:

```bash
# Install core functionality only (minimal dependencies)
pip install -e "lib/idp_common_pkg[core]"

# Install with specific component support
pip install -e "lib/idp_common_pkg[ocr]"
pip install -e "lib/idp_common_pkg[classification]"
pip install -e "lib/idp_common_pkg[extraction]"
pip install -e "lib/idp_common_pkg[evaluation]"
pip install -e "lib/idp_common_pkg[reporting]"
pip install -e "lib/idp_common_pkg[docs_service]"
pip install -e "lib/idp_common_pkg[image]"

# Install everything
pip install -e "lib/idp_common_pkg[all]"

# Install multiple components
pip install -e "lib/idp_common_pkg[ocr,classification]"
```

To install every first-party package at once (the usual setup), use `make setup`
— it installs them in a single pip invocation so the sibling names resolve
locally rather than from an index.

For Lambda functions, specify only the required components in requirements.txt:

```
../../lib/idp_common_pkg[extraction]
```

### Basic Usage

```python
from idp_common import get_config
from idp_common.models import Document
from idp_common import ocr, classification, extraction, evaluation, reporting
from idp_common.docs_service import create_document_service

# Get configuration (merged from Default and Custom records in the DynamoDb Configuration Table)
cfg = get_config()

# Create a document object
document = Document(
    input_bucket="my-bucket",
    input_key="my-document.pdf",
    output_bucket="output-bucket"
)

# OCR Processing
ocr_service = ocr.OcrService()
document = ocr_service.process_document(document)

# Document Classification
classification_service = classification.ClassificationService(config=cfg)
document = classification_service.classify_document(document)

# Field Extraction for a section
extraction_service = extraction.ExtractionService(config=cfg)
document = extraction_service.process_document_section(document, section_id="section-1")

# Evaluate extraction results
expected_document = Document.from_s3(bucket="baseline-bucket", input_key=document.input_key)
evaluation_service = evaluation.EvaluationService(config=cfg)
document = evaluation_service.evaluate_document(document, expected_document)

# Save evaluation results to reporting storage
reporter = reporting.SaveReportingData("reporting-bucket")
reporter.save(document, data_to_save=["evaluation_results"])

# Record document state in the TrackingTable (DynamoDB)
document_service = create_document_service()
updated_document = document_service.update_document(document)
```

## 📦 Handling Large Documents

The Document model includes automatic compression support for documents exceeding Step Functions payload limits (256KB):

```python
# Handle input - automatically detects and decompresses if needed
document = Document.load_document(
    event_data=event["document"], 
    working_bucket=working_bucket, 
    logger=logger
)

# Process document...
# (your processing logic here)

# Prepare output - automatically compresses if document is large
response = {
    "document": document.serialize_document(
        working_bucket=working_bucket, 
        step_name="classification", 
        logger=logger
    )
}
```

See the [Core Data Models documentation](idp_common/README.md) for more details on document compression features.

## ⚙️ Configuration

The configuration module retrieves and merges configuration from DynamoDB:

```python
# Get configuration with default table name from CONFIGURATION_TABLE_NAME environment variable
config = get_config()

# Or specify a table name explicitly
config = get_config(table_name="my-config-table")
```

## 🧪 Testing

```bash
# Install the package with test dependencies
pip install -e ".[test]"

# Run all tests
pytest

# Run with coverage report
pytest --cov=idp_common --cov-report=term-missing

# Run tests and generate reports
pytest --junitxml=test-results.xml --cov=idp_common --cov-report=xml:coverage.xml

# Run tests in parallel
pytest -xvs
```

## 📝 Development Notes

This package uses a unified Document-based approach across all services:

1. All services accept and return Document objects
2. Each service updates the Document with its results
3. Results are properly encapsulated in the Document model
4. Large results (like extraction attributes) are stored in S3 with only URIs in the Document

Key benefits:
- Consistency across all services
- Simplified data flow in serverless functions
- Better resource usage with the focused document pattern
- Improved maintainability with standardized interfaces
