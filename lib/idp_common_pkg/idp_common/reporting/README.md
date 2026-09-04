# Reporting Module

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Overview

The Reporting module provides comprehensive functionality for saving document processing data to reporting storage in structured formats for analytics and reporting purposes. It supports saving evaluation results, metering data, and document sections as Parquet files in S3, with automatic AWS Glue table creation and partition projection for efficient querying with Amazon Athena.

## Key Components

### Multi-instance sections: one row per DOCUMENT (#715)

A class flagged `x-aws-idp-multi-instance` produces
`inference_result = {"instances": [ … ]}`. Left alone, `_flatten_json_data`
`json.dumps`'s that list into a **single opaque `inference_result.instances`
column**: N columns collapse to 1 and every existing dashboard query against the
class's fields returns NULL.

So `_multi_instance_records` fans a wrapped section out into **one parquet row per
instance**, with a `record_index` column — the same column names a single-record
class produces, so existing queries keep working. Glue tables are
crawler-discovered, so there is no static column list to update.

Two things to know:

- ⚠️ `(document_id, section_id)` is **no longer unique** for such a table; the key
  is `(document_id, section_id, record_index)`. `COUNT(DISTINCT document_id)`
  undercounts documents of the class, because one input file can contain several.
  The analytics agent is told this for flagged classes.
- The decision is driven by **config**, not by the shape of the output, so a
  Designate-mode class that happens to name its own array `instances` keeps its
  existing reporting shape. An empty `instances` list falls back to the single-row
  path.

### SaveReportingData Class

The `SaveReportingData` class is the main component of the reporting module. It provides methods to save different types of document data to a reporting bucket in Parquet format, including automated cost calculation capabilities.

```python
from idp_common.reporting import SaveReportingData
from idp_common.models import Document

# Initialize the SaveReportingData class with a reporting bucket
reporter = SaveReportingData(reporting_bucket="my-reporting-bucket")

# Initialize with pricing configuration for cost calculation
config = {
    "pricing": [
        {
            "name": "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0",
            "units": [
                {"name": "inputTokens", "price": "3.0e-6"},
                {"name": "outputTokens", "price": "1.5e-5"}
            ]
        }
    ]
}
reporter = SaveReportingData(
    reporting_bucket="my-reporting-bucket",
    database_name="my-glue-database",  # Optional for Glue table creation
    config=config  # Optional for cost calculations
)

# Save specific data types for a document
results = reporter.save(document, data_to_save=["evaluation_results", "metering"])
```

#### Constructor Parameters

- **reporting_bucket** (str): S3 bucket name for storing reporting data
- **database_name** (str, optional): AWS Glue database name for automatic table creation and updates
- **config** (Dict[str, Any], optional): Configuration dictionary containing pricing data and other settings

When `config` is provided with pricing information, the system automatically calculates unit costs and estimated costs for all metering data.

## Features

- **Modular Design**: Each data type has its own processing method, making it easy to add support for new data types
- **Parquet Format**: Data is saved in Parquet format, which is optimized for analytics workloads
- **Hierarchical Storage**: Data is organized in a hierarchical structure by date/document with unique timestamped filenames
- **Flexible Schema**: Each data type has its own schema definition, allowing for specialized data structures
- **Dynamic Schema Inference**: Automatically infers schemas for document sections without predefined structures
- **Partition Projection**: Uses AWS Glue partition projection for efficient querying without MSCK REPAIR operations
- **Automatic Table Discovery**: AWS Glue Crawler automatically discovers new section types and creates tables
- **Unique File Naming**: Timestamp-based filenames prevent overwrites when documents are reprocessed
- **Error Handling**: Comprehensive error handling with detailed logging and graceful failure recovery
- **AWS Athena Ready**: All data is immediately queryable through Amazon Athena with optimized partition pruning
- **Cost Calculation**: Automated unit cost and estimated cost calculation for metering data based on configuration
- **Pricing Configuration**: Dynamic loading and caching of pricing data from configuration sources
- **Cost Analytics**: Enhanced metering data with cost information for detailed financial analysis

## Cost Calculation Features

The reporting module includes comprehensive cost calculation capabilities that automatically enhance metering data with cost information:

### Pricing Configuration

Pricing data is loaded from the configuration dictionary passed to the constructor. The configuration supports multiple services and units:

```python
config = {
    "pricing": [
        {
            "name": "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0",
            "units": [
                {"name": "inputTokens", "price": "3.0e-6"},
                {"name": "outputTokens", "price": "1.5e-5"},
                {"name": "cacheReadInputTokens", "price": "1.5e-6"},
                {"name": "cacheWriteInputTokens", "price": "1.875e-5"}
            ]
        },
        {
            "name": "textract/analyze_document", 
            "units": [
                {"name": "pages", "price": "0.0015"}
            ]
        },
        {
            "name": "bedrock/us.amazon.nova-pro-v1:0",
            "units": [
                {"name": "inputTokens", "price": "8.0e-7"},
                {"name": "outputTokens", "price": "3.2e-6"}
            ]
        }
    ]
}

# Initialize with cost calculation enabled
reporter = SaveReportingData(
    reporting_bucket="my-reporting-bucket",
    database_name="my-glue-database",
    config=config
)
```

### Cost Calculation Process

1. **Configuration Loading**: Pricing data is loaded from the config dictionary and cached for performance
2. **Service/Unit Matching**: System attempts exact match for service_api/unit combinations
3. **Fuzzy Matching**: If exact match fails, uses partial matching for common patterns
4. **Cost Calculation**: `estimated_cost = value × unit_cost` for each metering record
5. **Fallback Handling**: Missing pricing defaults to $0.0 with warning logs

### Enhanced Metering Schema

When cost calculation is enabled, metering records include additional fields:

| Field | Type | Description |
|-------|------|-------------|
| unit_cost | double | Cost per unit in USD (e.g., $0.000003 per token) |
| estimated_cost | double | Total calculated cost (value × unit_cost) |

### Cost Calculation Methods

The following methods support the cost calculation functionality:

#### `_get_pricing_from_config() -> Dict[str, Dict[str, float]]`
Loads and caches pricing data from the configuration dictionary.

**Returns**: Dictionary mapping service names to unit costs

**Example**:
```python
# Automatically called during metering data processing
pricing_map = reporter._get_pricing_from_config()
# Result: {
#   "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0": {
#     "inputTokens": 0.000003,
#     "outputTokens": 0.000015
#   }
# }
```

#### `_get_unit_cost(service_api: str, unit: str) -> float`
Retrieves the unit cost for a specific service API and unit combination.

**Parameters**:
- `service_api`: The service identifier (e.g., "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0")
- `unit`: The unit of measurement (e.g., "inputTokens", "pages")

**Returns**: Unit cost in USD, or 0.0 if not found

**Example**:
```python
# Get cost per input token for Claude
cost = reporter._get_unit_cost(
    "bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0", 
    "inputTokens"
)
# Result: 0.000003
```

#### `clear_pricing_cache()`
Clears the cached pricing data to force reload from configuration.

**Example**:
```python
# Clear cache after configuration update
reporter.clear_pricing_cache()
```

> **Glue table lifecycle**: the `metering` Glue table is now managed
> declaratively by CloudFormation (`AWS::Glue::Table` in `template.yaml`)
> with a `date`+`hour` partition-projection over
> `s3://<reporting-bucket>/metering/date=YYYY-MM-DD/hour=HH/`. The
> previously-documented in-code `_create_or_update_metering_glue_table`
> helper has been removed — `SaveReportingData` writes parquet under
> that same layout and the table becomes queryable in Athena
> automatically. For any additional custom tables (e.g. document-section
> tables, rule-validation tables) the class still creates Glue entries
> on-demand via `_create_or_update_glue_table` and
> `_create_or_update_rule_validation_glue_table`.

### Cost Analysis Examples

With cost calculation enabled, you can perform detailed cost analysis:

```python
from idp_common.reporting import SaveReportingData
from idp_common.models import Document

# Initialize with cost calculation
config = {"pricing": [...]}  # Your pricing configuration
reporter = SaveReportingData(
    reporting_bucket="my-reporting-bucket",
    config=config
)

# Process document with cost calculation
document = Document.from_dict(document_data)
results = reporter.save(document, data_to_save=["metering"])

# The metering data now includes:
# - unit_cost: Cost per token/page/operation
# - estimated_cost: Total cost for each operation
```

### Performance Considerations

- **Caching**: Pricing configuration is cached after first load to avoid repeated parsing
- **Lazy Loading**: Pricing data is only loaded when first metering record is processed
- **Memory Efficient**: Cache stores only processed pricing data, not raw configuration
- **Error Handling**: Invalid pricing entries are skipped with warning logs

### Cost Calculation Logging

The system provides detailed logging for cost calculation operations:

```
INFO - Found 15 pricing entries in configuration
INFO - Successfully loaded 45 pricing entries from configuration  
INFO - Using partial match for bedrock/claude-3/inputTokens: bedrock/us.anthropic.claude-3-sonnet-20240229-v1:0/inputTokens = $0.000003
WARNING - No unit cost mapping found for service_api='custom/service', unit='unknown_unit'. Using $0.0
```

## Supported Data Types

### Evaluation Results

The module supports saving document evaluation results, which include:

1. **Document-level metrics**: Overall accuracy, precision, recall, F1 score, etc.
2. **Section-level metrics**: Metrics for each document section
3. **Attribute-level details**: Detailed information about each attribute, including expected vs. actual values, match status, and confidence

```python
# Save evaluation results for a document
result = reporter.save_evaluation_results(document)
```

### Metering Data

The module supports saving document processing metering data for cost tracking and analytics:

```python
# Save metering data for a document
result = reporter.save_metering_data(document)
```

**Partitioning:** `save_metering_data` partitions rows by **write time**
(= document completion time, since the writer runs at workflow end) —
NOT by queue time. Every metering row lands under
`metering/date=<YYYY-MM-DD>/hour=<HH>/` reflecting the completion
timestamp.

**Columns:** `document_id`, `context`, `service_api`, `unit`, `value`,
`number_of_pages`, `unit_cost`, `estimated_cost`, `timestamp` (completion
time — same as partition), `initial_event_time` (original queue time,
preserved for consumers who need queue-time semantics), `config_version`.

**Semantic note for consumers of raw `metering`:** `WHERE date = 'X'`
means "docs completed on X", NOT "docs queued on X". Filter on
`initial_event_time` explicitly when you need queue-time semantics.

**Rollup tables (populated by the scheduled `DataMartRollupFunction`,
main stack):** for aggregate cost/volume queries over wide date ranges,
consumers should read from the pre-aggregated Athena tables rather
than raw metering:

- `metering_hourly` — hourly cost rollup, hour × config_version × service_api × unit (`sum_value`, `sum_cost`)
- `metering_daily` — daily cost rollup, day × config_version × service_api × unit (`sum_value`, `sum_cost`)
- `metering_docs_hourly` — hourly doc/pages rollup, hour × config_version (`n_docs`, `sum_pages`), aggregated via MAX-per-doc to avoid fanning `number_of_pages` out by service/unit rows
- `metering_docs_daily` — daily doc/pages rollup, day × config_version (`n_docs`, `sum_pages`), where `n_docs` is a doc-hours count (a doc processed across two hours counts twice)
- `control_plane_hourly` — per-Lambda cost attribution for control-plane infra

See [`docs/reporting-sql-layer.md`](../../../../../docs/reporting-sql-layer.md)
for the tier picker (`<2h → raw`, `2-24h → hourly`, `>24h → daily`),
the tagging model, and the migration path for the new `hour` partition
key.

### Document Sections

The module supports saving document section extraction results as Parquet files with dynamic schema inference. This functionality processes each section in the document, loads the extraction results from S3, and saves them in a structured, partitioned format suitable for analytics.

#### Key Features

- **Dynamic Schema Inference**: Automatically constructs PyArrow schemas from JSON data without requiring predefined schemas
- **Flexible Data Handling**: Supports various JSON structures (objects, arrays, primitives)
- **Nested JSON Flattening**: Converts nested objects to flat structure using dot notation
- **Partition Structure**: Organizes data with section_type and date-based partitioning
- **Error Resilience**: Continues processing other sections if individual sections fail
- **Comprehensive Logging**: Detailed logging for monitoring and debugging

#### Usage

```python
# Save document sections along with other data types
data_to_save = ["sections"]  # New option
results = reporter.save(document, data_to_save)

# Or combine with existing functionality
data_to_save = ["evaluation_results", "metering", "sections"]
results = reporter.save(document, data_to_save)
```

#### Requirements

For the sections functionality to work, your `Document` object must have:

1. **Sections**: A list of `Section` objects in `document.sections`
2. **Extraction Results**: Each section should have `extraction_result_uri` pointing to S3 JSON file
3. **Classification**: Each section should have a `classification` field for partitioning

#### Data Processing

**Schema Inference**: The method dynamically infers PyArrow schemas by analyzing the JSON data:
- **Strings**: Mapped to `pa.string()`
- **Integers**: Mapped to `pa.int64()`
- **Floats**: Mapped to `pa.float64()`
- **Booleans**: Mapped to `pa.bool_()`
- **Lists/Objects**: Converted to JSON strings and mapped to `pa.string()`
- **Mixed Types**: Defaults to `pa.string()`

**JSON Flattening**: Nested JSON structures are flattened using dot notation:

```json
// Input JSON
{
  "customer": {
    "name": "John Doe",
    "address": {
      "street": "123 Main St",
      "city": "Anytown"
    }
  },
  "items": ["item1", "item2"]
}

// Flattened Output
{
  "customer.name": "John Doe",
  "customer.address.street": "123 Main St", 
  "customer.address.city": "Anytown",
  "items": "[\"item1\", \"item2\"]"  // Arrays become JSON strings
}
```

**Metadata Fields**: Each record includes the following metadata fields:
- `section_id`: The unique identifier of the section
- `document_id`: The document identifier
- `section_classification`: The section's classification/type
- `section_confidence`: Confidence in the section's CLASSIFICATION (not its
  extracted fields). **Null when the classifier produced no score**, which is the
  default — see [classification confidence](../classification/README.md#classification-confidence-confidence-classification_reason).
  It previously carried a hardcoded 1.0 for every row, so historical partitions
  cannot be compared against new ones on this column
- `timestamp`: The timestamp when the document was processed

## Storage Structure

Data is stored in S3 with the following structure:

```
reporting-bucket/
├── evaluation_metrics/
│   ├── document_metrics/
│   │   └── date=YYYY-MM-DD/
│   │       ├── doc-id_20240115_143052_123_results.parquet
│   │       ├── doc-id_20240115_143127_456_results.parquet  # Same doc, reprocessed
│   │       └── another-doc-id_20240115_144001_789_results.parquet
│   ├── section_metrics/
│   │   └── date=YYYY-MM-DD/
│   │       ├── doc-id_20240115_143052_123_results.parquet
│   │       └── another-doc-id_20240115_144001_789_results.parquet
│   └── attribute_metrics/
│       └── date=YYYY-MM-DD/
│           ├── doc-id_20240115_143052_123_results.parquet
│           └── another-doc-id_20240115_144001_789_results.parquet
├── metering/
│   └── date=YYYY-MM-DD/
│       └── hour=HH/
│           ├── doc-id_20240115_143052_123_results.parquet
│           └── another-doc-id_20240115_144001_789_results.parquet
├── metering_hourly/
│   └── date=YYYY-MM-DD/
│       └── hour=HH/
│           └── <athena INSERT output>.parquet
├── metering_daily/
│   └── date=YYYY-MM-DD/
│       └── <athena INSERT output>.parquet
├── metering_docs_hourly/
│   └── date=YYYY-MM-DD/
│       └── hour=HH/
│           └── <athena INSERT output>.parquet
├── metering_docs_daily/
│   └── date=YYYY-MM-DD/
│       └── <athena INSERT output>.parquet
├── control_plane_hourly/
│   └── date=YYYY-MM-DD/
│       └── hour=HH/
│           └── data.parquet
└── document_sections/
    ├── invoice/
    │   └── date=YYYY-MM-DD/
    │       ├── doc-id_section_1.parquet
    │       └── doc-id_section_4.parquet
    ├── receipt/
    │   └── date=YYYY-MM-DD/
    │       └── doc-id_section_2.parquet
    └── bank_statement/
        └── date=YYYY-MM-DD/
            └── doc-id_section_3.parquet
```

### File Naming Convention

All files use a unique timestamp-based naming convention to prevent overwrites:
- **Format**: `{escaped_doc_id}_{timestamp}_results.parquet`
- **Timestamp**: `YYYYMMDD_HHMMSS_mmm` (includes milliseconds)
- **Example**: `invoice-123_20240115_143052_123_results.parquet`

This ensures that if the same document is processed multiple times in the same day, each processing attempt creates a separate file, preserving all data for analysis and debugging.

This structure is designed to be compatible with AWS Glue and Amazon Athena for analytics. The document sections are partitioned by `section_type` (classification) as the first partition level, followed by a single date-based partition using the format `YYYY-MM-DD`. Each file is uniquely named with the document ID and section ID to avoid conflicts, and the document ID is included as a column in the Parquet data for filtering and analysis.

### Partition Structure Benefits

The new single date partition structure provides several advantages:

- **Simplified Queries**: Natural date range queries like `WHERE date BETWEEN '2024-01-01' AND '2024-01-31'`
- **Efficient Pruning**: Athena can efficiently prune partitions based on date ranges
- **Cleaner Organization**: Single date partition is easier to understand and maintain
- **Better Performance**: Reduced partition overhead compared to three-level partitioning
- **Future-Proof**: Easier to extend and modify partition strategies

## AWS Glue Integration

The reporting module is designed to work seamlessly with AWS Glue and Amazon Athena:

### Automatic Table Creation

The reporting module provides two types of automatic table creation:

#### Predefined Tables (CloudFormation)
- **Evaluation Tables**: `document_evaluations`, `section_evaluations`, `attribute_evaluations`
- **Metering Table**: `metering`
- Created during stack deployment via CloudFormation

#### Dynamic Section Tables (Runtime)
When processing documents with new section types, the `SaveReportingData` class automatically:
- **Creates New Tables**: Generates a Glue table for each unique section type (e.g., `document_sections_invoice`, `document_sections_w2`)
- **Updates Schemas**: Adds new columns when new fields are detected in extraction results
- **Configures Partitions**: Sets up partition projection for efficient date-based queries
- **Normalizes Names**: Converts section types to lowercase for S3 path consistency (e.g., "W2" → "w2")

This automatic table creation eliminates manual table management and ensures data is immediately queryable in Athena.

### Partition Projection Configuration

All tables use AWS Glue partition projection to eliminate the need for `MSCK REPAIR TABLE` operations:

```yaml
# Example partition projection for date-based partitioning
projection.enabled: "true"
projection.date.type: "date"
projection.date.format: "yyyy-MM-dd"
projection.date.range: "2024-01-01,2030-12-31"
projection.date.interval: "1"
projection.date.interval.unit: "DAYS"
storage.location.template: "s3://bucket/path/date=${date}/"
```

### AWS Glue Crawler for Document Sections

The document sections crawler automatically:

- **Discovers New Section Types**: Creates tables for new document classifications
- **Updates Schemas**: Adapts to changes in extraction result structures
- **Configurable Schedule**: Runs manually, every 15 minutes, hourly, or daily
- **Partition Discovery**: Automatically discovers new date partitions

### Benefits

- **No Manual Maintenance**: Tables and partitions are automatically managed
- **Immediate Availability**: New data is queryable immediately without manual intervention
- **Optimized Performance**: Partition projection provides efficient query performance
- **Schema Evolution**: Automatic adaptation to changing data structures

## Extending the Module

To add support for a new data type:

1. Add a new method to the `SaveReportingData` class:

```python
def save_document_metadata(self, document: Document) -> Optional[Dict[str, Any]]:
    """
    Save document metadata to the reporting bucket.
    
    Args:
        document: Document object containing metadata
        
    Returns:
        Dict with status and message, or None if no metadata
    """
    # Define schema specific to document metadata
    metadata_schema = pa.schema([
        ('document_id', pa.string()),
        ('input_key', pa.string()),
        ('created_at', pa.timestamp('ms')),
        ('page_count', pa.int32()),
        ('file_size', pa.int64()),
        # Add other metadata fields as needed
    ])
    
    # Implementation for saving document metadata
    # ...
    
    return {
        'statusCode': 200,
        'body': "Successfully saved document metadata"
    }
```

2. Update the `save` method to call this new method when the data type is requested:

```python
if 'document_metadata' in data_to_save:
    logger.info("Processing document metadata")
    result = self.save_document_metadata(document)
    if result:
        results.append(result)
```

## Usage in Lambda Functions

The module is designed to be used in Lambda functions for saving document data to reporting storage:

```python
from idp_common.models import Document
from idp_common.reporting import SaveReportingData

def handler(event, context):
    # Extract parameters from the event
    document_dict = event.get('document')
    reporting_bucket = event.get('reporting_bucket')
    data_to_save = event.get('data_to_save', [])
    
    # Convert document dict to Document object
    document = Document.from_dict(document_dict)
    
    # Use the SaveReportingData class to save the data
    reporter = SaveReportingData(reporting_bucket)
    results = reporter.save(document, data_to_save)
    
    # Return success if all operations completed
    return {
        'statusCode': 200,
        'body': "Successfully saved data to reporting bucket"
    }
```

## Dependencies

The reporting module has the following dependencies:

- `boto3`: For AWS S3 operations
- `pyarrow`: For Parquet file creation and schema definition
- `idp_common.models`: For the Document data model

When deploying Lambda functions that use this module, ensure that these dependencies are included in the deployment package.
