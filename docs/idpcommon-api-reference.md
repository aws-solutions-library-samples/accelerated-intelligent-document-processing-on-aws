---
title: "idp_common API Reference"
---

# idp_common API Reference

The `idp_common` library (`lib/idp_common_pkg/`) is the core shared Python package that powers all document processing in the IDP Accelerator. It provides modular services for each step of the pipeline, a central `Document` data model, and utility functions for S3, Bedrock, DynamoDB, and more.

## Installation

```bash
# Minimal installation
pip install -e "lib/idp_common_pkg[core]"

# Install specific modules
pip install -e "lib/idp_common_pkg[ocr]"
pip install -e "lib/idp_common_pkg[classification]"
pip install -e "lib/idp_common_pkg[extraction]"
pip install -e "lib/idp_common_pkg[evaluation]"

# Install multi-document discovery (includes scikit-learn, scipy, numpy, strands-agents)
pip install -e "lib/idp_common_pkg[multi_document_discovery]"

# Install everything
pip install -e "lib/idp_common_pkg[all]"
```

## Quick Start

```python
from idp_common import get_config, Document, Page, Section, Status

# Load configuration from DynamoDB
config = get_config()

# Create a document
document = Document(
    id="doc-123",
    input_bucket="my-bucket",
    input_key="documents/sample.pdf",
    output_bucket="output-bucket"
)
```

## Core Data Model

### Document

The `Document` dataclass is the central data structure passed through the entire processing pipeline. Each processing step enriches it with additional data.

**Key fields:**

| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Document identifier (typically the S3 key) |
| `input_bucket` / `input_key` | `str` | Source document location in S3 |
| `output_bucket` | `str` | S3 bucket for processing outputs |
| `status` | `Status` | Current processing stage (see Status enum below) |
| `num_pages` | `int` | Number of pages in the document |
| `pages` | `Dict[str, Page]` | Page data keyed by page ID |
| `sections` | `List[Section]` | Logical document sections (grouped by classification) |
| `summary_report_uri` | `str` | S3 URI to the summarization output |
| `config_version` | `str` | Configuration version used for processing |
| `metering` | `Dict` | Token usage and cost tracking data |
| `evaluation_results_uri` | `str` | S3 URI to evaluation results |
| `rule_validation_result` | `RuleValidationResult` | Rule validation output |
| `hitl_status` | `str` | Human-in-the-loop review status |
| `confidence_alert_count` | `int` | Number of low-confidence fields |
| `errors` | `List[str]` | Processing error messages |

**Key methods:**

| Method | Description |
|--------|-------------|
| `Document.from_dict(data)` | Create from a dictionary |
| `Document.from_json(json_str)` | Create from a JSON string |
| `Document.from_s3(bucket, key)` | Create from baseline files in S3 |
| `Document.from_s3_event(event, bucket)` | Create from an S3 EventBridge event |
| `Document.load_document(event_data, bucket)` | Handle compressed or uncompressed Lambda input |
| `document.serialize_document(bucket, step)` | Prepare output with automatic compression |
| `document.to_dict()` / `document.to_json()` | Serialize to dict or JSON |

### Page

Represents a single page in a document.

| Field | Type | Description |
|-------|------|-------------|
| `page_id` | `str` | Page identifier |
| `image_uri` | `str` | S3 URI to page image (JPG) |
| `raw_text_uri` | `str` | S3 URI to raw Textract JSON response |
| `parsed_text_uri` | `str` | S3 URI to parsed text (markdown) |
| `text_confidence_uri` | `str` | S3 URI to confidence data for assessment |
| `classification` | `str` | Classified document type for this page |
| `confidence` | `float` | Classification confidence score |

### Section

Represents a logical group of pages with the same document class.

| Field | Type | Description |
|-------|------|-------------|
| `section_id` | `str` | Section identifier |
| `classification` | `str` | Document type for this section |
| `page_ids` | `List[str]` | List of page IDs in this section |
| `extraction_result_uri` | `str` | S3 URI to extraction results |
| `attributes` | `Dict` | Extracted attribute values |
| `confidence_threshold_alerts` | `List[Dict]` | Low-confidence field alerts |

### Status Enum

```
QUEUED → RUNNING → PREPROCESSING → OCR → CLASSIFYING → EXTRACTING → ASSESSING →
HITL_IN_PROGRESS → SUMMARIZING → RULE_VALIDATION →
RULE_VALIDATION_ORCHESTRATOR → EVALUATING → COMPLETED
```

(`PREPROCESSING` appears only while a preprocessing hook runs — e.g. PII
redaction; it is skipped when no hook is registered.)

Also: `POSTPROCESSING`, `FAILED`, `ABORTED`, `REDACTED_SUPERSEDED` (a
redact-copy-and-stop original superseded by its redacted copy)

## Processing Modules

### OCR (`idp_common.ocr`)

Converts PDF documents to machine-readable text using Amazon Textract.

```python
from idp_common.ocr.service import OcrService

service = OcrService(region="us-east-1", config=config)
document = service.process_document(document)
```

**Features:** Layout analysis, table extraction, signature detection, configurable DPI.

### Classification (`idp_common.classification`)

Identifies document types and creates logical section boundaries.

```python
from idp_common.classification import ClassificationService

service = ClassificationService(region="us-east-1", config=config)
document = service.classify_document(document)
```

**Features:** Multimodal page-level classification, text-based holistic classification, BIO-like sequence segmentation, regex-based classification, DynamoDB caching, few-shot examples, section splitting strategies (`disabled`, `page`, `llm_determined`).

### Extraction (`idp_common.extraction`)

Extracts structured data fields from document sections using LLMs.

```python
from idp_common.extraction.service import ExtractionService

service = ExtractionService(region="us-east-1", config=config)
document = service.process_document_section(document, section_id="1")
```

**Features:** Simple/group/list attribute types, multimodal extraction (text + images), few-shot examples, class-specific filtering, JSON Schema validation.

### Assessment (`idp_common.assessment`)

Evaluates confidence of extraction results with optional bounding box localization.

```python
from idp_common.assessment.service import AssessmentService

service = AssessmentService(region="us-east-1", config=config)
document = service.process_document_section(document, section_id="1")
```

**Features:** Per-attribute confidence scores (0.0-1.0), confidence reasoning, optional bounding box coordinates, configurable confidence thresholds.

### Summarization (`idp_common.summarization`)

Creates human-readable document summaries with citations.

```python
from idp_common.summarization.service import SummarizationService

service = SummarizationService(region="us-east-1", config=config)
document = service.process_document(document)
```

**Features:** Markdown formatting, page citations, structured references section.

### Evaluation (`idp_common.evaluation`)

Compares processing results against ground truth for accuracy assessment.

```python
from idp_common.evaluation.service import EvaluationService

service = EvaluationService(region="us-east-1", config=config)
document = service.evaluate_document(actual_document, expected_document)
```

**Features:** Multiple comparison methods (EXACT, FUZZY, LEVENSHTEIN, SEMANTIC, NUMERIC_EXACT, DATE, LLM, HUNGARIAN), per-attribute and per-document metrics, visual evaluation reports.

### Rule Validation (`idp_common.rule_validation`)

Validates extracted data against business rules using a two-step LLM approach.

```python
from idp_common.rule_validation import RuleValidationService, RuleValidationOrchestratorService

# Step 1: Fact extraction per section
service = RuleValidationService(region="us-east-1", config=config)
document = service.validate_document(document)

# Step 2: Orchestrated consolidation
orchestrator = RuleValidationOrchestratorService(config=config)
document = orchestrator.consolidate_and_save(document, config=config, multiple_sections=True)
```

**Features:** Concurrent rule processing, page-aware chunking, customizable recommendation options, JSON and Markdown output.

## Infrastructure Modules

### BDA (`idp_common.bda`)

Integration with Amazon Bedrock Data Automation for end-to-end document processing.

**Key classes:** `BdaInvocationService` (invoke BDA projects), `BdaBlueprintService` (manage BDA blueprints and schema conversion).

### Discovery (`idp_common.discovery`)

Automatic document class and schema discovery using LLMs. Includes single-document discovery (`ClassesDiscovery`) and multi-document collection discovery (`MultiDocumentDiscovery`).

#### ClassesDiscovery — Single-Document Discovery

Analyzes a single document to identify its type and generate a JSON Schema.

```python
from idp_common.discovery.classes_discovery import ClassesDiscovery

discovery = ClassesDiscovery(input_bucket="bucket", input_prefix="doc.pdf", region="us-east-1")
result = discovery.discovery_classes_with_document(
    input_bucket="bucket", input_prefix="doc.pdf", save_to_config=False
)
```

**Features:** JSON Schema generation, auto-detect section boundaries, page range selection, ground truth comparison.

#### MultiDocumentDiscovery — Multi-Document Collection Discovery

Discovers document classes from a collection of documents using an embedding-based clustering pipeline: **embed → cluster → analyze → reflect**. Supports both S3-based processing (for Lambda/Step Functions) and local file processing (for CLI/SDK).

> **Requires extra dependencies:** `pip install -e "lib/idp_common_pkg[multi_document_discovery]"` or `make setup` from the project root. This installs scikit-learn, scipy, numpy, strands-agents, and pypdfium2.

> **Minimum 2 documents per class:** Clusters with fewer than 2 documents are filtered as noise. Ensure you provide at least 2 documents for each expected document type.

**Supported file types:** `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.webp`

##### Initialization

```python
from idp_common.discovery.multi_document_discovery import MultiDocumentDiscovery

discovery = MultiDocumentDiscovery(
    region="us-east-1",
    config={
        "embedding_model_id": "us.cohere.embed-v4:0",        # Bedrock embedding model
        "analysis_model_id": "us.anthropic.claude-sonnet-4-6",  # Strands agent model
        "max_documents": 500,                                  # Safety limit
        "min_cluster_size": 2,                                 # Minimum docs per cluster
        "num_sample_documents": 3,                             # Samples per cluster for analysis
        "max_concurrent_embeddings": 5,                        # Parallel embedding calls
        "max_concurrent_clusters": 3,                          # Parallel cluster analysis
        "max_sample_size": 5,                                  # Max images sent to agent
    },
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `region` | `str` | `"us-east-1"` | AWS region for Bedrock calls |
| `config` | `Dict` | `{}` | Discovery configuration (from `IDPConfig.discovery.multi_document`) |
| `bedrock_client` | `BedrockClient` | `None` | Optional pre-configured Bedrock client |

##### Internal Services

`MultiDocumentDiscovery` composes three specialized services:

| Service | Class | Purpose |
|---------|-------|---------|
| Embedding | `EmbeddingService` | Generates vector embeddings for document images via Bedrock |
| Clustering | `ClusteringService` | KMeans clustering with silhouette analysis (scikit-learn) |
| Analysis | `DiscoveryAgent` | Strands agent with Claude for cluster analysis and JSON Schema generation |

##### Local Pipeline (CLI/SDK)

The local pipeline processes documents from the local filesystem — no AWS infrastructure required beyond Bedrock model access.

**`run_local_pipeline()`** — Main entry point for local discovery

```python
result = discovery.run_local_pipeline(
    document_dir="/path/to/documents/",   # Scan directory recursively
    # document_paths=["/path/a.pdf", "/path/b.png"],  # OR explicit file list
    config_version="v1",                  # Optional: save results to DynamoDB config
    progress_callback=my_callback,        # Optional: callable(step_name, step_data)
)

print(f"Found {result.num_clusters} clusters from {result.total_documents} documents")
print(f"Successful schemas: {result.num_successful_schemas}")
print(result.reflection_report)

for cls in result.discovered_classes:
    print(f"  {cls['classification']} — {cls['document_count']} docs")
    print(f"  Schema keys: {list(cls['json_schema']['properties'].keys())}")
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `document_dir` | `str` | One of `document_dir` or `document_paths` | Directory to scan recursively |
| `document_paths` | `List[str]` | One of `document_dir` or `document_paths` | Explicit list of file paths |
| `config_version` | `str` | No | Config version to save discovered classes to |
| `progress_callback` | `Callable[[str, Any], None]` | No | Progress updates callback |

**Pipeline steps:**
1. **List** — Scan directory or validate explicit paths
2. **Embed** — Render PDFs to images (pypdfium2), generate embeddings via Bedrock
3. **Cluster** — KMeans with automatic K selection via silhouette analysis
4. **Analyze** — Strands agent examines sample images per cluster, generates classification + JSON Schema
5. **Reflect** — Agent produces a Markdown report reviewing all discovered classes
6. **Save** — (Optional) Merge schemas into a DynamoDB configuration version

**`list_local_documents()`** — Scan for supported files

```python
paths = discovery.list_local_documents(
    document_dir="/path/to/documents/",  # Recursive scan
    max_documents=500,                   # Safety limit
)
# Returns: ["/abs/path/invoice1.pdf", "/abs/path/w2.png", ...]
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `document_dir` | `str` | One of dir/paths | Directory to scan recursively |
| `document_paths` | `List[str]` | One of dir/paths | Explicit file paths to validate |
| `max_documents` | `int` | No | Override safety limit (default: 500) |

**`generate_embeddings_local()`** — Generate embeddings from local files

```python
embedding_result = discovery.generate_embeddings_local(
    file_paths=paths,
    progress_callback=lambda done, total: print(f"{done}/{total}"),
)
# embedding_result.embeddings — numpy array (N × embedding_dim)
# embedding_result.valid_keys — file paths that succeeded
# embedding_result.failed_keys — file paths that failed
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_paths` | `List[str]` | Yes | Local file paths |
| `progress_callback` | `Callable[[int, int], None]` | No | Progress callback `(done, total)` |

##### S3 Pipeline (Lambda/Step Functions)

The S3 pipeline processes documents stored in Amazon S3. Designed for use with Step Functions orchestration (Lambda handlers call individual steps) or as a single high-level call.

**`run_full_pipeline()`** — End-to-end S3 pipeline

```python
result = discovery.run_full_pipeline(
    bucket="my-bucket",
    prefix="documents/batch-001/",
    config_version="v1",                  # Optional: save to DynamoDB config
    progress_callback=my_callback,        # Optional
)
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `bucket` | `str` | Yes | S3 bucket containing documents |
| `prefix` | `str` | Yes | S3 key prefix to scan |
| `config_version` | `str` | No | Config version to save discovered classes to |
| `progress_callback` | `Callable[[str, Any], None]` | No | Progress updates callback |

**Step-by-step methods** (for Step Functions Map state integration):

```python
# Step 1: List documents in S3
s3_keys = discovery.list_documents(bucket="my-bucket", prefix="docs/", max_documents=500)

# Step 2: Generate embeddings
embedding_result = discovery.generate_embeddings(
    bucket="my-bucket", s3_keys=s3_keys, progress_callback=cb
)

# Step 3: Cluster
cluster_result = discovery.cluster_documents(embedding_result)

# Step 4: Load images for analysis
images = discovery._load_images_for_analysis(bucket="my-bucket", s3_keys=embedding_result.valid_keys)

# Step 5: Analyze each cluster (suitable for Step Functions Map iteration)
for cluster_id in range(cluster_result.num_clusters):
    discovered_class = discovery.analyze_cluster(cluster_id, cluster_result, images)

# Step 6: Generate reflection report
report = discovery.reflect(discovered_classes)

# Step 7: Save to config (optional)
saved = discovery.save_to_config(discovered_classes, config_version="v1",
                                  input_bucket="my-bucket", input_prefix="docs/")
```

| Method | Description |
|--------|-------------|
| `list_documents(bucket, prefix, max_documents)` | List supported files in S3 |
| `generate_embeddings(bucket, s3_keys, progress_callback)` | Generate embeddings for S3 documents |
| `cluster_documents(embedding_result)` | Cluster documents based on embeddings |
| `analyze_cluster(cluster_id, cluster_result, images)` | Analyze a single cluster (returns `DiscoveredClass`) |
| `reflect(discovered_classes)` | Generate Markdown reflection report |
| `save_to_config(discovered_classes, config_version, input_bucket, input_prefix)` | Save to DynamoDB config |

##### Result Objects

**`MultiDocDiscoveryResult`** (dataclass)

| Field | Type | Description |
|-------|------|-------------|
| `discovered_classes` | `List[Dict]` | List of discovered classes as serializable dicts |
| `reflection_report` | `str` | Markdown reflection report |
| `total_documents` | `int` | Total documents processed |
| `num_clusters` | `int` | Number of clusters found |
| `num_failed_embeddings` | `int` | Documents that failed embedding |
| `num_successful_schemas` | `int` | Clusters with successful schema generation |
| `num_failed_schemas` | `int` | Clusters where schema generation failed |

Each entry in `discovered_classes` contains:

| Key | Type | Description |
|-----|------|-------------|
| `cluster_id` | `int` | Cluster identifier |
| `classification` | `str` | Discovered document type name |
| `json_schema` | `Dict` | Generated JSON Schema for extraction |
| `document_count` | `int` | Number of documents in the cluster |
| `sample_doc_ids` | `List[str]` | Sample document identifiers (file paths or S3 keys) |
| `error` | `str \| None` | Error message if analysis failed for this cluster |

##### Progress Callback

Both `run_local_pipeline()` and `run_full_pipeline()` accept a `progress_callback(step_name, step_data)` that receives updates at each pipeline stage:

| Step Name | Data | Description |
|-----------|------|-------------|
| `listing_documents` | `{dir, paths}` or `{bucket, prefix}` | Starting document scan |
| `documents_found` | `{count}` | Number of documents found |
| `generating_embeddings` | `{total}` | Starting embedding generation |
| `embedding_progress` | `{done, total}` | Per-document embedding progress |
| `embeddings_complete` | Serialized `EmbeddingResult` | All embeddings done |
| `clustering` | `{num_documents}` | Starting clustering |
| `clustering_complete` | Serialized `ClusterResult` | Clustering done |
| `analyzing_clusters` | `{total}` | Starting cluster analysis |
| `cluster_analysis_progress` | `{done, total, classification}` | Per-cluster progress |
| `reflecting` | — | Starting reflection |
| `saving_to_config` | `{version}` | Saving to DynamoDB (if requested) |
| `pipeline_complete` | Full result dict | Pipeline finished |

##### Integration with IDP SDK and CLI

The multi-document discovery pipeline is also accessible through higher-level interfaces:

```python
# Via IDP SDK
from idp_sdk import IDPClient

client = IDPClient()
result = client.discovery.run_multi_doc(
    document_dir="/path/to/documents/",
    progress_callback=my_callback,
)
```

```bash
# Via IDP CLI
idp-cli discover-multidoc --dir /path/to/documents/
```

See [IDP SDK Reference](idp-sdk.md) and [IDP CLI Reference](idp-cli.md) for full details.

### Schema (`idp_common.schema`)

Dynamic Pydantic v2 model generation from JSON Schema definitions.

```python
from idp_common.schema import create_pydantic_model_from_json_schema

Model = create_pydantic_model_from_json_schema(schema=schema_dict, class_label="Invoice")
validated = Model(**extracted_data)
```

### Configuration (`idp_common.config`)

Configuration management with system defaults and user overrides.

```python
from idp_common import get_config, IDPConfig

config = get_config()  # Load from DynamoDB or system defaults
```

**Key functions:** `get_config()`, `load_system_defaults(pattern)`, `merge_config_with_defaults()`, `create_config_template()`.

### Agents (`idp_common.agents`)

Conversational AI agent framework with specialized agents for analytics, error analysis, and code intelligence.

**Key components:** Agent factory/registry, Analytics agent, Error Analyzer agent, Code Intelligence agent, External MCP agent, Conversational orchestrator.

## Utility Modules

### Bedrock (`idp_common.bedrock`)

Utilities for invoking Amazon Bedrock LLMs with retry logic, prompt caching, and token tracking. `invoke_model()` works across Anthropic Claude, Amazon Nova, and **OpenAI GPT-5.x** models — the latter are transparently routed to the `bedrock-mantle` OpenAI Responses API (with a `reasoning_effort` parameter in place of temperature/top_p/top_k, and a `stream_responses_api()` generator for streaming). OpenAI models are not supported for agentic extraction or Discovery. See the [OpenAI GPT-5.x Models](./openai-models.md) guide and the [bedrock module README](../lib/idp_common_pkg/idp_common/bedrock/README.md) for details.

### Document Service (`idp_common.docs_service`)

Document state persistence to the DynamoDB TrackingTable. `create_document_service()` (or `DocumentServiceFactory.create_service()`) returns a `DocumentDynamoDBService`, which the pipeline uses to create and update document records. Tracking is DynamoDB-only, so the factory's `mode` argument and the legacy `api_url` keyword are accepted but ignored, and `get_document_tracking_mode()` always returns `"dynamodb"` — the factory shape is retained so pre-migration call sites keep working.

### DynamoDB (`idp_common.dynamodb`)

Document tracking, HITL state management, and configuration storage. Provides `DocumentDynamoDBService` (the document-state backend behind `docs_service`) and the lower-level `DynamoDBClient`.

### Reporting (`idp_common.reporting`)

Analytics data storage for AWS Glue/Athena reporting pipelines.

### S3 (`idp_common.s3`)

S3 read/write utilities: `get_text_content()`, `get_json_content()`, `write_content()`, `find_matching_files()`.

### Image (`idp_common.image`)

Image resizing, format conversion, and Bedrock attachment preparation: `resize_image()`, `prepare_image()`, `prepare_bedrock_image_attachment()`.

### Utils (`idp_common.utils`)

Common helpers: `build_s3_uri()`, `parse_s3_uri()`, `merge_metering_data()`, `extract_structured_data_from_text()`.

### Metrics (`idp_common.metrics`)

CloudWatch metrics publishing: `publish_metric()`, `record_duration()`.

## Detailed Module Documentation

Each module has its own detailed README with comprehensive usage examples:

| Module | Location |
|--------|----------|
| Core Models | [`lib/idp_common_pkg/idp_common/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/README.md) |
| Classification | [`lib/idp_common_pkg/idp_common/classification/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/classification/README.md) |
| Extraction | [`lib/idp_common_pkg/idp_common/extraction/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/extraction/README.md) |
| Assessment | [`lib/idp_common_pkg/idp_common/assessment/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/assessment/README.md) |
| Rule Validation | [`lib/idp_common_pkg/idp_common/rule_validation/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/rule_validation/README.md) |
| Discovery | [`lib/idp_common_pkg/idp_common/discovery/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/discovery/README.md) |
| Agents | [`lib/idp_common_pkg/idp_common/agents/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/agents/README.md) |
| BDA | [`lib/idp_common_pkg/idp_common/bda/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/bda/README.md) |
| OCR | [`lib/idp_common_pkg/idp_common/ocr/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/ocr/README.md) |
| Schema | [`lib/idp_common_pkg/idp_common/schema/README.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/main/lib/idp_common_pkg/idp_common/schema/README.md) |
