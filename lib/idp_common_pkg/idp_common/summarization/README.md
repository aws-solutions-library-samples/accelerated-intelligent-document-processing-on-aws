Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Summarization Service

The Summarization Service module provides functionality for generating summaries of documents using LLMs through AWS Bedrock.

## Overview

The Summarization Service processes document text through a Bedrock LLM to extract key information and present it in flexible formats. The service is designed to integrate with the IDP pipeline to enable automatic document summarization with support for any JSON structure returned by LLMs. It dynamically adapts to whatever fields are returned in the JSON response from the model.

## Main Components

### DocumentSummary

The `DocumentSummary` class provides a flexible container for any JSON structure returned by LLMs:

```python
@dataclass
class DocumentSummary:
    """Flexible model for document summary results that can handle any JSON structure."""
    
    content: Dict[str, Any]
    """The raw content from the summarization result, containing any fields the LLM returned."""
    
    metadata: Dict[str, Any] = field(default_factory=dict)
    """Optional metadata about the summarization process."""
    
    def __getitem__(self, key: str) -> Any:
        """Allow dictionary-like access to summary fields."""
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a summary field with an optional default value."""
    
    def keys(self) -> List[str]:
        """Get a list of available keys in the summary."""
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
```

### DocumentSummarizationResult

The `DocumentSummarizationResult` class contains the comprehensive summarization results:

```python
@dataclass
class DocumentSummarizationResult:
    """Comprehensive summarization result for a document."""
    document_id: str
    summary: DocumentSummary
    execution_time: float = 0.0
    output_uri: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        
    def to_markdown(self) -> str:
        """Convert summarization results to markdown format."""
```

### SummarizationService

The `SummarizationService` class handles the core summarization functionality:

```python
class SummarizationService:
    """Service for summarizing documents using various backends."""
    
    def __init__(
        self,
        region: str = None,
        config: Dict[str, Any] = None,
        backend: str = "bedrock"
    ):
        # Initialize service with region, config and backend
        
    def process_text(self, text: str) -> DocumentSummary:
        # Process raw text to generate a summary with flexible structure
        
    def process_document_section(
        self,
        document: Document,
        section_id: str
    ) -> Document:
        # Process a specific section of a document and update the Document object with the summary
        # Stores summary results in S3 and updates section.attributes with URIs
        
    def process_document(
        self, 
        document: Document, 
        store_results: bool = True
    ) -> Document:
        # Process a Document object and update it with summary information
        # Automatically detects whether to use section-based or whole document summarization
        # store_results parameter controls whether to create and store the markdown report
```

## Usage Examples

### Summarizing Text

```python
from idp_common.summarization.service import SummarizationService

# Initialize service with config
summarization_service = SummarizationService(config=config)

# Summarize text with flexible format
text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit..."
summary = summarization_service.process_text(text)

# Access fields directly using dictionary-like syntax
for key in summary.keys():
    print(f"{key}: {summary[key]}")

# Or using get() with optional default value
overview = summary.get('overview', 'No overview available')
key_points = summary.get('key_points', [])
```

### Summarizing a Document Section

```python
from idp_common.models import Document
from idp_common.summarization.service import SummarizationService
import json

# Initialize service
summarization_service = SummarizationService(config=config)

# Load document
document = Document.from_s3("your-bucket", "your-document-key")

# Process a specific section
section_id = "section-1"
document = summarization_service.process_document_section(document, section_id)

# Find the processed section
section = next((s for s in document.sections if s.section_id == section_id), None)

if section and section.attributes and 'summary_uri' in section.attributes:
    # Access the summary from S3
    from idp_common import s3
    summary_uri = section.attributes['summary_uri']
    summary_content = s3.get_json_content(summary_uri)
    
    # Print the summary content
    print(f"Summary for section {section_id} ({section.classification}):")
    print(json.dumps(summary_content, indent=2))
    
    # Access the markdown version
    markdown_uri = section.attributes['summary_md_uri']
    markdown_content = s3.get_text_content(markdown_uri)
    print(f"\nMarkdown Summary:\n{markdown_content[:500]}...")
else:
    print(f"No summary available for section {section_id}")
```

## Configuration

The service requires configuration with the following structure:

```json
{
  "summarization": {
    "model": "us.amazon.nova-pro-v1:0",
    "temperature": 0,
    "top_k": 5,
    "system_prompt": "You are an expert document summarizer. Create a balanced summary that captures key points.",
    "task_prompt": "Summarize the following document:\n\n{DOCUMENT_TEXT}\n\nProvide your summary in JSON format with the following fields:\n- 'brief_summary': A brief 1-2 sentence overview\n- 'detailed_summary': A comprehensive summary with key points\n\nEnsure the response is valid JSON."
  }
}
```

The service can handle any JSON structure returned by the model. You can use any field names in your prompt template:

```json
{
  "summarization": {
    "task_prompt": "Summarize the following document:\n\n{DOCUMENT_TEXT}\n\nProvide your summary in JSON format with the following fields:\n- 'overview': A brief 1-2 sentence overview\n- 'key_points': A list of the most important points\n- 'sections': A dictionary of section titles and summaries\n- 'entities': Important entities mentioned in the document\n\nEnsure all JSON fields are properly formatted and the response is valid JSON."
  }
}
```

Important considerations for the prompt template:
1. Always request a valid JSON response format
2. Specify the exact fields you want to include
3. Include any formatting or style instructions directly in the prompt
4. Fields can have any names and be nested as needed

## Integration with Document Pipeline

The Summarization Service integrates with the IDP pipeline by:

1. Reading all available document page text
2. Combining the text with page markers
3. Sending the text to Bedrock LLM for summarization
4. Parsing the JSON response from the LLM with any structure
5. Creating a `DocumentSummary` with the parsed JSON content
6. Creating a `DocumentSummarizationResult` object with results and timing information
7. Optionally generating a markdown summary report and storing it in S3 (when `store_results=True`)
8. Updating the document with:
   - `summarization_result`: Complete result object with summary, timing, and URI
   - `summary_report_uri`: S3 URI to the markdown report (only when `store_results=True`)

### Flexible Structure Handling

The main advantage of this service is that it can work with any JSON structure returned by the LLM:

1. You can specify any JSON structure in your prompt template
2. The service preserves the exact structure returned by the model
3. The markdown report dynamically creates sections based on the JSON keys

### Summary Report

When `store_results=True` (the default), the service generates a markdown summary report that is stored in S3 at the location:
```
s3://{output_bucket}/{document.input_key}/summary/summary.md
```

The report dynamically creates sections based on the JSON keys returned:

```markdown
# Document Summary: doc-123

## Overview
This is a brief overview of the document.

## Key Points
- Point 1
- Point 2
- Point 3

## Sections
### Introduction
Content about the introduction

### Main Content
Content about the main points

## Entities
- Entity 1
- Entity 2

Execution time: 1.25 seconds
```

Special formatting is applied based on the data type:
- Lists are formatted as bullet points
- Dictionaries are formatted as nested sections
- Strings are presented as-is

The report is generated using the `to_markdown()` method of the `DocumentSummarizationResult` class and can be accessed through `document.summary_report_uri` or `document.summarization_result.output_uri`.

When `store_results=False`, the document is still updated with the summary information and the `summarization_result` object, but no markdown report is generated or stored in S3.

## Document Summarization Approaches

The `process_document` method now supports two different approaches to document summarization:

### 1. Section-Based Summarization

When a document has defined sections, the service will:

```python
from idp_common.models import Document
from idp_common.summarization.service import SummarizationService

# Initialize service
summarization_service = SummarizationService(config=config)

# Load document with sections already defined
document = Document.from_dict(document_data)

# Process document - will use section-based approach automatically
document = summarization_service.process_document(document)

# Access the combined summary report
print(f"Summary Report URI: {document.summary_report_uri}")

# Access individual section summaries
for section in document.sections:
    if section.attributes and 'summary_uri' in section.attributes:
        print(f"Section {section.section_id} summary: {section.attributes['summary_uri']}")
```

This approach:
1. Processes each section separately using `process_document_section`
2. Stores individual section summaries in S3
3. Combines all section summaries into a comprehensive document summary
4. Generates a markdown report with all section summaries

The combined markdown report will include all section summaries in a structured format:

```markdown
# Document Summary: doc-123

This summary combines results from all document sections.

## Section Summaries

# Section: introduction

[Introduction section summary content]

# Section: financial_data

[Financial data section summary content]

Total execution time: 10.25 seconds
```

### 2. Whole Document Summarization

When a document has no defined sections, the service automatically falls back to summarizing the entire document at once:

```python
from idp_common.models import Document
from idp_common.summarization.service import SummarizationService

# Initialize service
summarization_service = SummarizationService(config=config)

# Load document without sections
document = Document.from_dict(document_data)
document.sections = []  # No sections defined

# Process document - will use whole document approach automatically
document = summarization_service.process_document(document)

# Access the summary report
print(f"Summary Report URI: {document.summary_report_uri}")
```

This approach:
1. Combines text from all pages
2. Generates a single summary for the entire document
3. Stores the summary in S3

The markdown report will follow the standard format based on the JSON fields returned by the model.

### Summarizing a Document

```python
from idp_common.models import Document
from idp_common.summarization.service import SummarizationService

# Initialize service
summarization_service = SummarizationService(config=config)

# Load document
document = Document.from_dict(document_data)

# Process document and store markdown results in S3 (default)
# Will automatically use section-based or whole document approach
document = summarization_service.process_document(document)

# Access summary through the result object
summary = document.summarization_result.summary
print(f"Available fields: {summary.keys()}")

# Access any field directly
for field in summary.keys():
    print(f"{field}: {summary[field]}")

# Access execution info and report URI
print(f"Execution Time: {document.summarization_result.execution_time:.2f} seconds")
print(f"Summary Report URI: {document.summary_report_uri}")

# Process document without storing results in S3
document = summarization_service.process_document(document, store_results=False)

# Document has summarization_result but no summary_report_uri
print(f"Has summarization_result: {document.summarization_result is not None}")
print(f"Has summary_report_uri: {document.summary_report_uri is not None}")
```

## Section-Level Summarization

The `process_document_section` method allows you to generate summaries for specific sections of a document. This is particularly useful for multi-class documents where different sections may require different types of summaries.

### How It Works

1. **Input**: Takes a Document object and a section_id
2. **Processing**:
   - Validates the document and finds the specified section
   - Extracts text from all pages in the section
   - Generates a summary using the Bedrock LLM
   - Stores the summary in S3 in both JSON and Markdown formats
3. **Output**: 
   - Updates the section's attributes with links to the summary files
   - Returns the updated Document object

### Key Features

- **Section-specific processing**: Focuses only on the pages in the specified section
- **Attribute initialization**: Safely initializes `section.attributes` to an empty dictionary if it's `None`
- **Dual format storage**: Stores both JSON and Markdown versions of the summary
- **Error handling**: Gracefully handles errors and updates the document's error list

### Storage Locations

For a section with ID `section-id`, the summaries are stored at:
- JSON: `s3://{output_bucket}/{document.input_key}/sections/{section_id}/summary.json`
- Markdown: `s3://{output_bucket}/{document.input_key}/sections/{section_id}/summary.md`

### Section Attributes

After processing, the section's attributes will contain:
- `summary_uri`: S3 URI for the JSON summary
- `summary_md_uri`: S3 URI for the Markdown summary

### Processing Multiple Sections

You can process multiple sections sequentially:

```python
# Process all sections in a document
for section in document.sections:
    document = summarization_service.process_document_section(
        document=document,
        section_id=section.section_id
    )
```

Or process them in parallel for better performance:

```python
from concurrent.futures import ThreadPoolExecutor

def process_section(section_id):
    return summarization_service.process_document_section(
        document=document.copy(),  # Create a copy to avoid concurrency issues
        section_id=section_id
    )

# Process sections in parallel
with ThreadPoolExecutor(max_workers=4) as executor:
    section_ids = [section.section_id for section in document.sections]
    results = list(executor.map(process_section, section_ids))
    
    # Merge results if needed
    # (This is a simplified example - you would need to merge the results properly)
    for result_doc in results:
        # Update the original document with section results
        for section in result_doc.sections:
            # Find matching section in original document
            orig_section = next((s for s in document.sections if s.section_id == section.section_id), None)
            if orig_section and section.attributes:
                if orig_section.attributes is None:
                    orig_section.attributes = {}
                orig_section.attributes.update(section.attributes)
```
