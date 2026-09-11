---
title: "IDP SDK Documentation"
---

# IDP SDK Documentation

The IDP SDK provides programmatic Python access to all IDP Accelerator capabilities with a clean, namespaced API.

## Installation

```bash
# Install from local development
# Recommended: install everything at once
make setup-venv
source .venv/bin/activate

# Or install just the SDK with pip/uv
uv pip install -e ./lib/idp_sdk
```

## Quick Start

> **Argument naming:** the named configuration entity is a **Configuration
> Profile**, selected with `config_profile=`. The former keyword,
> `config_version=`, is still accepted everywhere it used to be — both set the
> same value, and existing code does not need to change. Passing both with
> *different* values raises `ValueError` rather than silently picking one. Use
> `config_profile=` in new code. `config_revision=` is unrelated: it selects a
> *revision within* a profile. See
> [configuration-profiles.md](configuration-profiles.md#terminology-which-word-means-what).

### Using the Public Interface

The SDK is designed around a single entry point: `IDPClient`. Always import from the top-level `idp_sdk` package — this is the stable public interface. Avoid importing directly from internal submodules (e.g., `idp_sdk._core`, `idp_sdk.operations`, `idp_sdk.models.*`) as these are private implementation details and may change without notice.

```python
# Correct: use the public interface
from idp_sdk import IDPClient, BatchProcessResult, RerunStep, IDPError

# Avoid: importing from private/internal modules
# from idp_sdk.operations.batch import BatchOperation  # private
# from idp_sdk._core.batch_processor import BatchProcessor  # private
```

All response models, enums, and exceptions you need are exported directly from `idp_sdk` — see the [Response Models](#response-models) section for the complete list.

```python
from idp_sdk import IDPClient

# Create client with stack configuration
client = IDPClient(stack_name="my-idp-stack", region="us-west-2")

# Upload and process a single document
result = client.document.process(file_path="./invoice.pdf")
print(f"Document ID: {result.document_id}, Status: {result.status}")

# Process a batch of documents
batch_result = client.batch.process(source="./documents/")
print(f"Batch: {batch_result.batch_id}, Queued: {batch_result.queued}")

# Check status
status = client.batch.get_status(batch_id=batch_result.batch_id)
print(f"Progress: {status.completed}/{status.total}")
```

## Architecture

The SDK follows a namespaced operation pattern for better organization:

```python
client = IDPClient(stack_name="my-stack")

# Stack operations
client.stack.deploy(...)
client.stack.delete(...)
client.stack.get_resources()
client.stack.exists()
client.stack.get_status()
client.stack.check_in_progress()
client.stack.monitor(...)
client.stack.cancel_update()
client.stack.wait_for_stable_state()
client.stack.get_failure_analysis()
client.stack.cleanup_orphaned(...)
client.stack.get_bucket_info()

# Batch operations (multiple documents)
client.batch.process(...)
client.batch.reprocess(...)
client.batch.get_status(...)
client.batch.get_document_ids(...)
client.batch.get_results(...)
client.batch.get_confidence(...)
client.batch.list()
client.batch.download_results(...)
client.batch.download_sources(...)
client.batch.delete_documents(...)
client.batch.stop_workflows()

# Document operations (single document)
client.document.process(...)
client.document.get_status(...)
client.document.get_metadata(...)
client.document.list(...)
client.document.download_results(...)
client.document.download_source(...)
client.document.reprocess(...)
client.document.delete(...)

# Discovery operations (schema generation)
client.discovery.run(...)
client.discovery.run_batch(...)
client.discovery.run_multi_doc(...)
client.discovery.run_multi_section(...)
client.discovery.auto_detect_sections(...)

# Evaluation operations (baseline comparison)
client.evaluation.create_baseline(...)
client.evaluation.use_as_baseline(...)
client.evaluation.get_report(...)
client.evaluation.get_metrics(...)
client.evaluation.list_baselines(...)
client.evaluation.delete_baseline(...)

# Assessment operations (quality metrics)
client.assessment.get_confidence(...)
client.assessment.get_geometry(...)
client.assessment.get_metrics(...)

# Search operations (knowledge base)
client.search.query(...)

# Configuration operations
client.config.create(...)
client.config.validate(...)
client.config.upload(...)
client.config.download(...)
client.config.list()
client.config.revisions(...)
client.config.activate(...)
client.config.delete(...)
client.config.sync_bda(...)

# Manifest operations
client.manifest.generate(...)
client.manifest.validate(...)

# Testing operations
client.testing.load_test(...)
```

## Client Initialization

```python
from idp_sdk import IDPClient

# With stack name (for stack-dependent operations)
client = IDPClient(stack_name="my-stack", region="us-west-2")

# Without stack (for stack-independent operations)
client = IDPClient()

# Stack can be set later
client.stack_name = "new-stack"
```

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `stack_name` | str | No | CloudFormation stack name |
| `region` | str | No | AWS region (defaults to boto3 default) |

---

## Document Operations

Operations for processing individual documents.

### document.process()

Process a single document (upload and queue for processing).

**Parameters:**
- `file_path` (str, required): Path to local file to upload
- `document_id` (str, optional): Custom document ID (defaults to filename without extension)
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentUploadResult` with `document_id`, `status`, and `timestamp`

```python
result = client.document.process(
    file_path="/path/to/invoice.pdf",
    document_id="custom-id"  # Optional
)

print(f"Document ID: {result.document_id}")
print(f"Status: {result.status}")  # "queued"
print(f"Timestamp: {result.timestamp}")
```

### document.get_status()

Get processing status for a single document.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key format: batch-id/filename)
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentStatus` with processing information including status, duration, pages, sections, and errors

```python
status = client.document.get_status(document_id="batch-123/invoice.pdf")

print(f"Status: {status.status.value}")
print(f"Pages: {status.num_pages}")
print(f"Duration: {status.duration_seconds}s")
```

### document.download_results()

Download processing results (processed outputs) from OutputBucket for a single document.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `output_dir` (str, required): Local directory to save results
- `file_types` (list[str], optional): File types to download - "pages", "sections", "summary", "evaluation" (defaults to all)
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentDownloadResult` with `document_id`, `files_downloaded`, and `output_dir`

```python
result = client.document.download_results(
    document_id="batch-123/invoice.pdf",
    output_dir="./results",
    file_types=["pages", "sections", "summary"]  # Optional
)

print(f"Downloaded {result.files_downloaded} files")
```

### document.download_source()

Download original document source file from InputBucket.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `output_path` (str, required): Local file path to save document
- `stack_name` (str, optional): Stack name override

**Returns:** `str` - Local file path where document was saved

```python
file_path = client.document.download_source(
    document_id="batch-123/invoice.pdf",
    output_path="./downloads/invoice.pdf"
)

print(f"Downloaded to: {file_path}")
```

### document.reprocess()

Reprocess a single document from a specific pipeline step.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `step` (str or RerunStep, required): Pipeline step to reprocess from (e.g., "classification", "extraction", RerunStep.EXTRACTION)
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentReprocessResult` with `document_id`, `step`, and `queued` status

```python
from idp_sdk import RerunStep

result = client.document.reprocess(
    document_id="batch-123/invoice.pdf",
    step=RerunStep.EXTRACTION
)

print(f"Queued: {result.queued}")
```

### document.delete()

Permanently delete a single document and all associated data from InputBucket, OutputBucket, and DynamoDB.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `stack_name` (str, optional): Stack name override
- `dry_run` (bool, optional): If True, simulate deletion without actually deleting (default: False)

**Returns:** `DocumentDeletionResult` with `success`, `object_key`, `deleted` (dict of deleted items), and `errors`

```python
result = client.document.delete(
    document_id="batch-123/invoice.pdf",
    dry_run=False
)

print(f"Success: {result.success}")
print(f"Deleted: {result.deleted}")
```

### document.list()

List processed documents with pagination support.

**Parameters:**
- `limit` (int, optional): Maximum number of documents to return (default: 100)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentListResult` with `documents` (list of DocumentInfo), `count`, and optional `next_token`

```python
# List documents
result = client.document.list(limit=50)

for doc in result.documents:
    print(f"{doc.document_id}: {doc.status}")

# Pagination
if result.next_token:
    next_page = client.document.list(limit=50, next_token=result.next_token)
```

### document.get_metadata()

Get extracted metadata and fields for a processed document.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `section_id` (int, optional): Section number (default: 1)
- `stack_name` (str, optional): Stack name override

**Returns:** `DocumentMetadata` with `document_id`, `section_id`, `document_class`, `fields`, `confidence`, `page_count`, and `metadata`

```python
metadata = client.document.get_metadata(document_id="batch-123/invoice.pdf")

print(f"Document Type: {metadata.document_class}")
print(f"Confidence: {metadata.confidence:.2%}")
print(f"Fields:")
for field_name, field_value in metadata.fields.items():
    print(f"  {field_name}: {field_value}")

# Access specific fields
invoice_number = metadata.fields.get("invoice_number")
total_amount = metadata.fields.get("total_amount")
```

---

## Batch Operations

Operations for processing multiple documents.

### batch.process()

Process multiple documents through the IDP pipeline.

**Parameters:**
- `source` (str, optional): Auto-detected source - directory path, manifest file, or S3 URI
- `manifest` (str, optional): Path to manifest CSV file
- `directory` (str, optional): Local directory path
- `s3_uri` (str, optional): S3 URI (s3://bucket/prefix/)
- `test_set` (str, optional): Test set identifier
- `batch_id` (str, optional): Custom batch ID
- `batch_prefix` (str, optional): Batch ID prefix (default: "sdk-batch")
- `file_pattern` (str, optional): File pattern for filtering (default: "*.pdf")
- `recursive` (bool, optional): Recursively process subdirectories (default: True)
- `number_of_files` (int, optional): Limit number of files to process
- `config_path` (str, optional): Path to custom configuration file
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to use for processing
- `context` (str, optional): Context for test set processing
- `stack_name` (str, optional): Stack name override

**Returns:** `BatchProcessResult` with `batch_id`, `document_ids`, `queued`, `uploaded`, `failed`, `baselines_uploaded`, `source`, `output_prefix`, and `timestamp`

```python
# From directory
result = client.batch.process(source="./documents/")

# From manifest
result = client.batch.process(source="./manifest.csv")

# From S3
result = client.batch.process(source="s3://bucket/path/")

# With options
result = client.batch.process(
    source="./documents/",
    batch_prefix="my-batch",
    file_pattern="*.pdf",
    recursive=True,
    number_of_files=10,
    config_path="./config.yaml",
    config_profile="v2"
)

print(f"Batch ID: {result.batch_id}")
print(f"Documents queued: {result.queued}")
```

### batch.get_status()

Get processing status for all documents in a batch.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `stack_name` (str, optional): Stack name override

**Returns:** `BatchStatus` with `batch_id`, `documents` (list of DocumentStatus), `total`, `completed`, `failed`, `in_progress`, `queued`, `success_rate`, and `all_complete`

```python
status = client.batch.get_status(batch_id="batch-20250123-123456")

print(f"Total: {status.total}")
print(f"Completed: {status.completed}")
print(f"Failed: {status.failed}")
print(f"Success Rate: {status.success_rate:.1%}")

for doc in status.documents:
    print(f"  {doc.document_id}: {doc.status.value}")
```

### batch.get_document_ids()

Get all document IDs belonging to a batch. Useful for pre-fetching a document count before a confirmation prompt without triggering the full reprocess pipeline.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `stack_name` (str, optional): Stack name override

**Returns:** `list[str]` - List of document object keys (S3 keys) in the batch

```python
doc_ids = client.batch.get_document_ids(batch_id="batch-20250123-123456")
print(f"Batch contains {len(doc_ids)} documents")
```

### batch.get_results()

Get extracted metadata and fields for all documents in a batch, with pagination support.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `section_id` (int, optional): Section number within documents (default: 1)
- `limit` (int, optional): Maximum documents to return per page (default: 10)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `dict` with `batch_id`, `section_id`, `count`, `total_in_batch`, `documents` (list of per-document result dicts), and optional `next_token`

```python
result = client.batch.get_results(batch_id="batch-20250123-123456", limit=20)

print(f"Showing {result['count']} of {result['total_in_batch']} documents")
for doc in result["documents"]:
    print(f"  {doc['document_id']}: {doc['document_class']} ({doc['status']})")

# Pagination
if result.get("next_token"):
    next_page = client.batch.get_results(
        batch_id="batch-20250123-123456",
        limit=20,
        next_token=result["next_token"]
    )
```

### batch.get_confidence()

Get confidence scores and quality metrics for all documents in a batch, with pagination support.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `section_id` (int, optional): Section number (default: 1)
- `limit` (int, optional): Maximum documents to return (default: 10)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `dict` with `batch_id`, `section_id`, `count`, `total_in_batch`, `documents` (list with per-document confidence attributes), and optional `next_token`

```python
result = client.batch.get_confidence(batch_id="batch-20250123-123456", limit=20)

for doc in result["documents"]:
    low_conf = [f for f, a in doc["attributes"].items()
                if not a.get("meets_threshold")]
    if low_conf:
        print(f"{doc['document_id']} needs review: {', '.join(low_conf)}")
```

### batch.list()

List recent batch processing jobs with pagination support.

**Parameters:**
- `limit` (int, optional): Maximum number of batches to return (default: 10)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `BatchListResult` with `batches` (list of BatchInfo), `count`, and optional `next_token`

```python
# List recent batches
result = client.batch.list(limit=20)

for batch in result.batches:
    print(f"{batch.batch_id}: {batch.queued} docs, {batch.timestamp}")

# Pagination
if result.next_token:
    next_page = client.batch.list(limit=20, next_token=result.next_token)
```

### batch.download_results()

Download processing results (processed outputs) from OutputBucket for all documents in a batch.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `output_dir` (str, required): Local directory to save results
- `file_types` (list[str], optional): File types to download - "pages", "sections", "summary", "evaluation", or "all" (default: ["all"])
- `stack_name` (str, optional): Stack name override

**Returns:** `BatchDownloadResult` with `files_downloaded`, `documents_downloaded`, and `output_dir`

```python
result = client.batch.download_results(
    batch_id="batch-20250123-123456",
    output_dir="./results",
    file_types=["summary", "sections"]
)

print(f"Downloaded {result.files_downloaded} files")
```

### batch.download_sources()

Download original source files from InputBucket for all documents in a batch.

**Parameters:**
- `batch_id` (str, required): Batch identifier
- `output_dir` (str, required): Local directory to save source files
- `stack_name` (str, optional): Stack name override

**Returns:** `BatchDownloadResult` with `files_downloaded`, `documents_downloaded`, and `output_dir`

```python
result = client.batch.download_sources(
    batch_id="batch-20250123-123456",
    output_dir="./source_files"
)

print(f"Downloaded {result.files_downloaded} source files")
```

### batch.delete_documents()

Permanently delete documents and their associated data from InputBucket, OutputBucket, and DynamoDB. Select documents by batch ID or wildcard pattern.

**Parameters:**
- `batch_id` (str, optional): Batch identifier (selects all docs containing this string)
- `pattern` (str, optional): Wildcard pattern to match document keys (e.g., `"batch-123/*.pdf"`, `"*invoice*"`)
- `status_filter` (str, optional): Filter by document status (e.g., "FAILED", "COMPLETED")
- `stack_name` (str, optional): Stack name override
- `dry_run` (bool, optional): If True, simulate deletion without actually deleting (default: False)
- `continue_on_error` (bool, optional): Continue deleting if one document fails (default: True)

**Note:** Must specify either `batch_id` or `pattern` (not both).

**Returns:** `BatchDeletionResult` with `success`, `deleted_count`, `failed_count`, `total_count`, `dry_run`, and `results` (list of DocumentDeletionResult)

```python
# Delete entire batch
result = client.batch.delete_documents(batch_id="batch-123")

# Delete with status filter
result = client.batch.delete_documents(
    batch_id="batch-123",
    status_filter="FAILED"
)

# Delete by wildcard pattern
result = client.batch.delete_documents(
    pattern="batch-123/*.pdf"
)

# Delete all failed invoices across batches
result = client.batch.delete_documents(
    pattern="*invoice*",
    status_filter="FAILED"
)

# Dry run
result = client.batch.delete_documents(
    batch_id="batch-123",
    dry_run=True
)

print(f"Deleted: {result.deleted_count}/{result.total_count}")
```

### batch.reprocess()

Reprocess existing documents from a specific pipeline step.

**Parameters:**
- `step` (str or RerunStep, required): Pipeline step to reprocess from (e.g., "classification", "extraction", RerunStep.EXTRACTION)
- `document_ids` (list[str], optional): Specific document IDs to reprocess
- `batch_id` (str, optional): Batch ID to reprocess all documents in batch
- `stack_name` (str, optional): Stack name override

**Note:** Must specify either `document_ids` or `batch_id`

**Returns:** `BatchReprocessResult` with `documents_queued`, `documents_failed`, `failed_documents`, and `step`

```python
from idp_sdk import RerunStep

# Reprocess batch
result = client.batch.reprocess(
    step=RerunStep.EXTRACTION,
    batch_id="batch-20250123-123456"
)

# Reprocess specific documents
result = client.batch.reprocess(
    step="classification",
    document_ids=["batch/doc1.pdf", "batch/doc2.pdf"]
)

print(f"Queued: {result.documents_queued}")
```

### batch.stop_workflows()

Stop all running Step Functions workflows and purge the SQS queue.

**Parameters:**
- `stack_name` (str, optional): Stack name override
- `skip_purge` (bool, optional): Skip purging the SQS queue (default: False)
- `skip_stop` (bool, optional): Skip stopping executions (default: False)

**Returns:** `StopWorkflowsResult` with `executions_stopped` (`ExecutionsStoppedResult`), `documents_aborted` (`DocumentsAbortedResult`), and `queue_purged`

```python
result = client.batch.stop_workflows()

print(f"Queue purged: {result.queue_purged}")
if result.executions_stopped:
    print(f"Executions stopped: {result.executions_stopped.total_stopped}")
if result.documents_aborted:
    print(f"Documents aborted: {result.documents_aborted.documents_aborted}")
```

---

## Evaluation Operations

Operations for baseline comparison and accuracy measurement.

### evaluation.create_baseline()

Create evaluation baseline for a document to enable automated accuracy testing.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `baseline_data` (dict, required): Expected extraction results with sections and fields
- `metadata` (dict, optional): Optional metadata (created_by, purpose, etc.)
- `stack_name` (str, optional): Stack name override

**Returns:** `dict` with baseline creation result

```python
baseline = {
    "sections": [
        {
            "section_id": 1,
            "document_class": "invoice",
            "fields": {
                "invoice_number": "INV-12345",
                "total_amount": "1250.00",
                "invoice_date": "2024-01-15"
            }
        }
    ]
}

result = client.evaluation.create_baseline(
    document_id="test-invoice-001.pdf",
    baseline_data=baseline,
    metadata={"created_by": "qa_team"}
)
```

### evaluation.use_as_baseline()

Promote a processed document's output to the evaluation baseline — the
programmatic equivalent of the web UI's **Use as Evaluation Baseline** button.
Copies the document's output into the evaluation baseline bucket and sets its
`EvaluationStatus` to `BASELINE_AVAILABLE`. Runs synchronously.

Unlike `create_baseline()` (which writes a baseline you construct yourself from
a `baseline_data` dict), this captures an already-processed document's own
output as the baseline.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key) of a processed document
- `stack_name` (str, optional): Stack name override

**Returns:** `UseAsBaselineResult` with `document_id`, `files_copied`, `evaluation_status`, and `timestamp`

**Raises:** `IDPResourceNotFoundError` if the document has no output (e.g. not finished processing); `IDPProcessingError` if the copy fails.

```python
result = client.evaluation.use_as_baseline(
    document_id="loan-12345/package.pdf"
)
print(f"Copied {result.files_copied} files; status={result.evaluation_status}")
```

### evaluation.get_report()

Get evaluation report comparing extraction results to baseline.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `section_id` (int, optional): Section number (default: 1)
- `stack_name` (str, optional): Stack name override

**Returns:** `EvaluationReport` with `document_id`, `section_id`, `accuracy`, `field_results`, and `summary`

```python
report = client.evaluation.get_report(document_id="test-invoice-001.pdf")

print(f"Accuracy: {report.accuracy:.1%}")

for field, result in report.field_results.items():
    if result['match']:
        print(f"✓ {field}: {result['extracted']}")
    else:
        print(f"✗ {field}: expected '{result['expected']}', got '{result['extracted']}'")
```

### evaluation.get_metrics()

Get aggregated evaluation metrics across multiple documents.

**Parameters:**
- `start_date` (str, optional): Filter by start date (ISO format: "2024-01-15")
- `end_date` (str, optional): Filter by end date (ISO format: "2024-01-31")
- `document_class` (str, optional): Filter by document type (e.g., "invoice")
- `batch_id` (str, optional): Filter by batch identifier
- `stack_name` (str, optional): Stack name override

**Returns:** `EvaluationMetrics` with `total_evaluations`, `average_accuracy`, and `by_document_class`

```python
metrics = client.evaluation.get_metrics(
    start_date="2024-01-01",
    end_date="2024-01-31"
)

print(f"Total evaluations: {metrics.total_evaluations}")
print(f"Average accuracy: {metrics.average_accuracy:.1%}")

for doc_class, accuracy in metrics.by_document_class.items():
    print(f"{doc_class}: {accuracy:.1%}")
```

### evaluation.list_baselines()

List evaluation baselines with pagination support.

**Parameters:**
- `limit` (int, optional): Maximum number of baselines to return (default: 100)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `EvaluationBaselineListResult` with `baselines`, `count`, and optional `next_token`

```python
result = client.evaluation.list_baselines(limit=50)

for baseline in result.baselines:
    print(f"{baseline['document_id']}: {baseline['created_at']}")

if result.next_token:
    next_page = client.evaluation.list_baselines(limit=50, next_token=result.next_token)
```

### evaluation.delete_baseline()

Delete evaluation baseline for a document.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `stack_name` (str, optional): Stack name override

**Returns:** `dict` with deletion result

```python
result = client.evaluation.delete_baseline(document_id="test-invoice-001.pdf")
print(f"Deleted: {result['success']}")
```

---

## Assessment Operations

Operations for quality metrics and confidence scoring.

### assessment.get_confidence()

Get confidence scores for all extracted fields in a document section.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `section_id` (int, optional): Section number (default: 1)
- `stack_name` (str, optional): Stack name override

**Returns:** `AssessmentConfidenceResult` with `document_id`, `section_id`, and `attributes` (dict of AssessmentFieldConfidence)

**AssessmentFieldConfidence fields:**
- `confidence`: Float 0.0-1.0 (e.g., 0.95 = 95% confident)
- `confidence_threshold`: Minimum acceptable confidence from config
- `meets_threshold`: Boolean indicating if confidence is acceptable
- `reason`: Explanation for the confidence level

```python
confidence = client.assessment.get_confidence(document_id="batch-001/invoice.pdf")

# Check if any fields need review
low_conf_fields = [
    field for field, attr in confidence.attributes.items()
    if not attr.meets_threshold
]

if low_conf_fields:
    print(f"Review needed for: {', '.join(low_conf_fields)}")

# Get confidence for specific field
total_amount = confidence.attributes.get("total_amount")
if total_amount:
    print(f"Total amount confidence: {total_amount.confidence:.2%}")
    print(f"Meets threshold: {total_amount.meets_threshold}")
```

### assessment.get_geometry()

Get bounding box coordinates for all extracted fields in a document section.

**Parameters:**
- `document_id` (str, required): Document identifier (S3 key)
- `section_id` (int, optional): Section number (default: 1)
- `stack_name` (str, optional): Stack name override

**Returns:** `AssessmentGeometryResult` with `document_id`, `section_id`, and `attributes` (dict of AssessmentFieldGeometry)

**AssessmentFieldGeometry fields:**
- `page`: Page number where field was found (1-indexed)
- `bbox`: Normalized bounding box [left, top, width, height] (0.0-1.0)
- `bounding_box`: Absolute pixel coordinates {Left, Top, Width, Height}

```python
geometry = client.assessment.get_geometry(document_id="batch-001/invoice.pdf")

# Highlight fields in document viewer
for field_name, geo in geometry.attributes.items():
    print(f"{field_name} found on page {geo.page}")
    # Draw rectangle at normalized coordinates
    draw_highlight(
        page=geo.page,
        left=geo.bbox[0],
        top=geo.bbox[1],
        width=geo.bbox[2],
        height=geo.bbox[3]
    )
```

### assessment.get_metrics()

Get aggregated quality metrics across multiple documents.

**Parameters:**
- `start_date` (str, optional): Filter by start date (ISO format: "2024-01-15")
- `end_date` (str, optional): Filter by end date (ISO format: "2024-01-31")
- `document_class` (str, optional): Filter by document type (e.g., "invoice")
- `batch_id` (str, optional): Filter by batch identifier
- `stack_name` (str, optional): Stack name override

**Returns:** `dict` with aggregated metrics

```python
metrics = client.assessment.get_metrics(
    start_date="2024-01-15",
    end_date="2024-01-15"
)

print(f"Processed: {metrics['total_documents']} documents")
print(f"Avg confidence: {metrics['average_confidence']:.2%}")
print(f"SLA compliance: {metrics['threshold_compliance']:.2%}")
```

---

## Search Operations

Operations for knowledge base queries and semantic search.

### search.query()

Query knowledge base with natural language questions.

**Parameters:**
- `question` (str, required): Natural language question
- `document_ids` (list[str], optional): Limit search to specific documents
- `limit` (int, optional): Maximum number of results to return (default: 10)
- `next_token` (str, optional): Pagination token from previous request
- `stack_name` (str, optional): Stack name override

**Returns:** `SearchResult` with `answer`, `confidence`, `citations`, and optional `next_token`

```python
# Ask a question
result = client.search.query(
    question="What is the total amount on invoice INV-12345?"
)

print(f"Answer: {result.answer}")
print(f"Confidence: {result.confidence:.1%}")

for citation in result.citations:
    print(f"Source: {citation.document.document_id}")
    print(f"Page: {citation.document.page}")
    print(f"Text: {citation.text}")

# Search within specific documents
result = client.search.query(
    question="What is the vendor name?",
    document_ids=["batch-001/invoice1.pdf", "batch-001/invoice2.pdf"]
)
```

---

## Stack Operations

Operations for deploying and managing IDP stacks.

### stack.deploy()

Deploy or update an IDP CloudFormation stack.

**Parameters:**
- `stack_name` (str, optional): CloudFormation stack name (uses client default if not provided)
- `admin_email` (str, optional): Admin user email — required for new stacks
- `template_url` (str, optional): URL to CloudFormation template in S3
- `template_path` (str, optional): Local path to CloudFormation template file
- `from_code` (str, optional): Path to project root for building from source
- `custom_config` (str, optional): Path to local config file or S3 URI
- `max_concurrent` (int, optional): Maximum concurrent workflows
- `log_level` (str, optional): Logging level (DEBUG, INFO, WARN, ERROR)
- `enable_hitl` (bool, optional): Enable Human-in-the-Loop
- `parameters` (dict, optional): Additional CloudFormation parameters
- `wait` (bool, optional): Wait for operation to complete (default: True)
- `no_rollback` (bool, optional): Disable rollback on failure (default: False)
- `role_arn` (str, optional): CloudFormation service role ARN

**Returns:** `StackDeploymentResult` with `success`, `operation`, `status`, `stack_name`, `stack_id`, `outputs`, and `error`

```python
from idp_sdk import Pattern

result = client.stack.deploy(
    stack_name="my-new-stack",
    admin_email="admin@example.com",
    max_concurrent=100,
    wait=True
)

if result.success:
    print(f"Stack deployed: {result.stack_name}")
    print(f"Outputs: {result.outputs}")
```

### stack.delete()

Delete an IDP CloudFormation stack.

**Parameters:**
- `stack_name` (str, optional): CloudFormation stack name (uses client default if not provided)
- `empty_buckets` (bool, optional): Empty S3 buckets before deletion (default: False)
- `force_delete_all` (bool, optional): Force delete all retained resources after deletion (default: False)
- `wait` (bool, optional): Wait for deletion to complete (default: True)

**Returns:** `StackDeletionResult` with `success`, `status`, `stack_name`, `stack_id`, `error`, and `cleanup_result`

```python
result = client.stack.delete(
    empty_buckets=True,
    force_delete_all=False,
    wait=True
)

print(f"Status: {result.status}")
```

### stack.get_resources()

Get stack resource information.

**Returns:** `StackResources` with bucket names, ARNs, and other resource identifiers

```python
resources = client.stack.get_resources()

print(f"Input Bucket: {resources.input_bucket}")
print(f"Output Bucket: {resources.output_bucket}")
print(f"Queue URL: {resources.document_queue_url}")
```

### stack.exists()

Check whether a CloudFormation stack exists.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `bool` — True if the stack exists, False otherwise

```python
if client.stack.exists():
    print("Stack is deployed")
else:
    print("Stack not found")
```

### stack.get_status()

Get the current CloudFormation status of a stack.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `str` or `None` — CloudFormation status string (e.g., `"UPDATE_IN_PROGRESS"`), or `None` if the stack does not exist

```python
status = client.stack.get_status()
print(f"Stack status: {status}")
```

### stack.check_in_progress()

Check whether a CloudFormation stack has an operation currently in progress.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `StackOperationInProgress` if an operation is in progress, `None` otherwise. The `operation` field is one of `"CREATE"`, `"UPDATE"`, or `"DELETE"`.

```python
in_progress = client.stack.check_in_progress()
if in_progress:
    print(f"Operation in progress: {in_progress.operation} ({in_progress.status})")
```

### stack.monitor()

Monitor a CloudFormation stack operation until it reaches a terminal state. Blocks until the operation completes or fails.

**Parameters:**
- `stack_name` (str, optional): Stack name override
- `operation` (str, optional): Operation type being monitored: `"CREATE"`, `"UPDATE"`, or `"DELETE"` (default: `"UPDATE"`)
- `poll_interval_seconds` (int, optional): Seconds between CloudFormation API polls (default: 10)

**Returns:** `StackMonitorResult` with `success`, `operation`, `status`, `stack_name`, `outputs`, and `error`

```python
result = client.stack.monitor(operation="UPDATE")
if result.success:
    print(f"Operation complete: {result.status}")
else:
    print(f"Operation failed: {result.error}")
```

### stack.cancel_update()

Cancel an in-progress stack update. Only valid when the stack is in `UPDATE_IN_PROGRESS` status.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `CancelUpdateResult` with `success`, `message`, and `error`

```python
result = client.stack.cancel_update()
if result.success:
    print("Update cancelled successfully")
```

### stack.wait_for_stable_state()

Wait for a CloudFormation stack to reach a stable (non-transitional) state. Useful before triggering an operation on a stack that may be in a transitional state.

**Parameters:**
- `stack_name` (str, optional): Stack name override
- `timeout_seconds` (int, optional): Maximum seconds to wait (default: 1200)
- `poll_interval_seconds` (int, optional): Seconds between polls (default: 10)

**Returns:** `StackStableStateResult` with `success`, `status`, and `message`

```python
result = client.stack.wait_for_stable_state()
if result.success:
    print(f"Stack is stable: {result.status}")
```

### stack.get_failure_analysis()

Analyze a CloudFormation deployment failure. Recursively collects failed events from the main stack and all nested stacks, identifies root causes vs. cascade failures.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `FailureAnalysis` with `stack_name`, `root_causes` (list of `FailureCause`), and `all_failures` (list of `FailureCause`)

```python
analysis = client.stack.get_failure_analysis()
print(f"Root causes ({len(analysis.root_causes)}):")
for cause in analysis.root_causes:
    print(f"  {cause.resource}: {cause.reason}")
```

### stack.cleanup_orphaned()

Remove residual AWS resources left behind from deleted IDP stacks. Identifies orphaned CloudFront distributions, CloudWatch log groups, AppSync APIs, IAM policies, S3 buckets, DynamoDB tables, and more.

**Parameters:**
- `dry_run` (bool, optional): Preview changes without making them (default: False)
- `auto_approve` (bool, optional): Auto-approve all deletions (default: False)
- `regions` (list[str], optional): AWS regions to check (default: us-east-1, us-west-2, eu-central-1)
- `profile` (str, optional): AWS profile name (default: None)

**Returns:** `OrphanedResourceCleanupResult` with `results` (dict), `has_errors`, and `has_disabled`

```python
# Preview what would be cleaned up
result = client.stack.cleanup_orphaned(dry_run=True)
print(f"Has errors: {result.has_errors}")
```

### stack.get_bucket_info()

Get information about S3 buckets associated with a CloudFormation stack.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `list[BucketInfo]` — one `BucketInfo` per S3 bucket, with `logical_id`, `bucket_name`, `object_count`, `total_size`, and `size_display`

```python
buckets = client.stack.get_bucket_info()
for bucket in buckets:
    print(f"{bucket.logical_id}: {bucket.bucket_name} ({bucket.size_display}, {bucket.object_count} objects)")
```

---

## Publish Operations

Operations for building and publishing IDP CloudFormation artifacts to S3. This namespace consolidates what was historically done by the standalone `publish.py` and `scripts/generate_govcloud_template.py` scripts.

Accessed via `client.publish.*`.

### publish.build()

Build and publish IDP CloudFormation artifacts (SAM templates, Lambda functions, Lambda layers, UI, container images) to S3. Optionally also generates a **headless** (no-UI) template variant.

This is the programmatic equivalent of `idp-cli publish` — see [Headless Deployment](./headless-deployment.md) and [IDP CLI — publish](./idp-cli.md#publish) for end-to-end examples.

**Parameters:**
- `source_dir` (str, required): Path to the IDP project root directory
- `bucket` (str, optional): S3 bucket basename for artifacts. If omitted, auto-generated as `idp-accelerator-artifacts-{account_id}`. The region is appended automatically.
- `prefix` (str, optional): S3 key prefix for artifacts (default: `"idp-cli"`)
- `region` (str, optional): AWS region. Falls back to the client's region, then `AWS_DEFAULT_REGION`.
- `headless` (bool, optional): If `True`, also generate a **headless (no-UI) template variant**. For GovCloud regions (`us-gov-*`), GovCloud configuration defaults are applied automatically (ARN partition, GovCloud-compatible Bedrock models, `lending-package-sample-govcloud` preset). Default: `False`.
- `public` (bool, optional): If `True`, make S3 artifacts publicly readable. Default: `False`.
- `max_workers` (int, optional): Maximum concurrent build workers. Default: auto-detect.
- `clean_build` (bool, optional): If `True`, delete all checksum files to force a full rebuild. Default: `False`.
- `no_validate` (bool, optional): If `True`, skip CloudFormation template validation. Default: `False`.
- `verbose` (bool, optional): If `True`, enable verbose build output. Default: `False`.
- `lint` (bool, optional): If `True`, enable ruff linting and cfn-lint. Default: `True`.

**Returns:** `PublishResult` with:
- `success` (bool)
- `template_path` (str, optional): Local path to the built standard template
- `template_url` (str, optional): S3 URL of the uploaded standard template
- `headless_template_path` (str, optional): Local path to the built headless template (only when `headless=True`)
- `headless_template_url` (str, optional): S3 URL of the uploaded headless template (only when `headless=True`)
- `bucket` (str, optional): S3 bucket used (region-suffixed)
- `prefix` (str, optional): S3 prefix used
- `version` (str, optional): Version string from the project's `VERSION` file
- `error` (str, optional): Error message if the build failed

**Raises:**
- `IDPConfigurationError`: If the source directory is invalid or region cannot be determined
- `IDPStackError`: If the build pipeline fails

```python
from idp_sdk import IDPClient

client = IDPClient(region="us-east-1")

# Standard build — produces idp-main.yaml
result = client.publish.build(source_dir=".", region="us-east-1")
if result.success:
    print("Template:", result.template_url)

# Build both standard and headless variants
result = client.publish.build(
    source_dir=".",
    region="us-east-1",
    headless=True,
)
if result.success:
    print("Standard  :", result.template_url)
    print("Headless  :", result.headless_template_url)

# Build for GovCloud WITH the Web UI (govcloud=True removes CloudFront, keeps the UI)
result = client.publish.build(
    source_dir=".",
    region="us-gov-west-1",
    govcloud=True,
)
if result.success:
    print("GovCloud  :", result.govcloud_template_url)

# Or build a headless (no-UI) variant for GovCloud (GovCloud config
# defaults — ARN partitions, GovCloud preset — auto-applied for us-gov-*)
result = client.publish.build(
    source_dir=".",
    region="us-gov-west-1",
    headless=True,
)
```

### publish.transform_template_headless()

Transform an existing (already-built) CloudFormation template into a **headless** variant by removing UI, AppSync, Cognito, WAF, agent, HITL, knowledge-base, and Test Studio resources. Useful when you already have an `idp-main.yaml` and want to produce the headless variant without rebuilding.

**Parameters:**
- `source_template` (str, required): Path to the source CloudFormation YAML template
- `output_path` (str, optional): Path to write the headless template. If omitted, appends `-headless` to the source filename.
- `update_govcloud_config` (bool, optional): If `True`, additionally update the configuration maps for GovCloud (ARN partition rewrite, GovCloud-compatible Bedrock models, `lending-package-sample-govcloud` preset). Default: `False`.
- `verbose` (bool, optional): If `True`, enable verbose logging. Default: `False`.

**Returns:** `TemplateTransformResult` with:
- `success` (bool)
- `input_path` (str): Path to the source template
- `output_path` (str, optional): Path to the transformed headless template
- `error` (str, optional): Error message if transformation failed

```python
# Transform an already-built template for commercial headless deployment
result = client.publish.transform_template_headless(
    source_template="./.aws-sam/idp-main.yaml",
    output_path="./.aws-sam/idp-headless.yaml",
)

# Same, but apply GovCloud defaults (for GovCloud deployment)
result = client.publish.transform_template_headless(
    source_template="./.aws-sam/idp-main.yaml",
    output_path="./.aws-sam/idp-headless-govcloud.yaml",
    update_govcloud_config=True,
)

if result.success:
    print("Headless template written to:", result.output_path)
else:
    print("Error:", result.error)
```

### publish.print_deployment_urls()

Print deployment URLs (S3 template URL + 1-click CloudFormation launch link) to stdout. If a headless template URL is provided, also prints the headless variant. Uses the correct console domain for the partition (`aws.amazon.com` vs `amazonaws-us-gov.com`).

**Parameters:**
- `template_url` (str, required): S3 URL of the main template
- `region` (str, required): AWS region
- `headless_template_url` (str, optional): S3 URL of the headless template
- `stack_name` (str, optional): Default stack name for the 1-click launch URL (default: `"IDP"`)

**Returns:** `None` (prints to stdout)

```python
client.publish.print_deployment_urls(
    template_url=result.template_url,
    region="us-east-1",
    headless_template_url=result.headless_template_url,
    stack_name="MyStack",
)
```

### Deploying a Headless Stack Programmatically

To build a headless template and deploy it in one flow, combine `publish.build(headless=True)` with `stack.deploy(template_path=...)`:

```python
from idp_sdk import IDPClient

client = IDPClient(region="us-east-1", stack_name="my-idp-headless")

# Build both variants
publish_result = client.publish.build(
    source_dir=".",
    region="us-east-1",
    headless=True,
)
if not publish_result.success:
    raise SystemExit(f"Build failed: {publish_result.error}")

# Deploy the headless variant (local path) or the headless URL
deploy_result = client.stack.deploy(
    template_path=publish_result.headless_template_path,   # or template_url=publish_result.headless_template_url
    wait=True,
)
print("Deployed:", deploy_result.stack_name, deploy_result.status)
```

For GovCloud, use `region="us-gov-west-1"` with either `govcloud=True` (full Web UI, CloudFront removed) or `headless=True` (no UI). See the [GovCloud Deployment Guide](./govcloud-deployment.md).

---

## Configuration Operations

Operations for managing IDP configurations.

### config.create()

Generate an IDP configuration template.

**Parameters:**
- `features` (str, optional): Feature set to include — `"min"`, `"core"`, `"all"`, or comma-separated (default: `"min"`)
- `pattern` (str, optional): Pattern to use — `"pattern-1"` or `"pattern-2"` (default: `"pattern-2"`)
- `output` (str, optional): Output file path
- `include_prompts` (bool, optional): Include prompt templates (default: False)
- `include_comments` (bool, optional): Include explanatory comments (default: True)

**Returns:** `ConfigCreateResult` with `yaml_content` and `output_path`

```python
result = client.config.create(
    features="min",           # min, core, all, or comma-separated
    pattern="pattern-2",
    output="config.yaml",
    include_prompts=False,
    include_comments=True
)

print(result.yaml_content)
```

### config.validate()

Validate a configuration file against system defaults.

**Parameters:**
- `config_file` (str, required): Path to the configuration file
- `pattern` (str, optional): Pattern to validate against (default: `"pattern-2"`)
- `show_merged` (bool, optional): Include merged configuration in result (default: False)
- `strict` (bool, optional): Report deprecated/unknown fields as errors (default: False)

**Returns:** `ConfigValidationResult` with `valid`, `errors`, `warnings`, `deprecated_fields`, `unknown_fields`, and optional `merged_config`

```python
result = client.config.validate(
    config_file="./config.yaml",
    pattern="pattern-2"
)

if result.valid:
    print("Configuration is valid")
else:
    for error in result.errors:
        print(f"Error: {error}")

for warning in result.warnings:
    print(f"Warning: {warning}")
```

### config.upload()

Upload configuration to a deployed stack.

**Parameters:**
- `config_file` (str, required): Path to the YAML or JSON configuration file
- `config_profile` (alias: `config_version`) (str, required): Configuration profile to upload to (e.g., `"default"`, `"v1"`, `"production"`). If the profile doesn't exist, it will be created automatically.
- `stack_name` (str, optional): Stack name override
- `validate` (bool, optional): Validate configuration before uploading (default: `True`)
- `pattern` (str, optional): Pattern to validate against (default: `"pattern-2"`)
- `description` (str, optional): Description for the configuration version
- `created_by` (str, optional): Recorded as the revision's author and shown as **By** in the revision history. The Web UI derives this from the caller's Cognito identity; an SDK caller has none, so it defaults to `system` — set it to something that identifies your automation if you want its saves attributable.
- `revision_notes` (str, optional): What this upload changed, recorded on the revision and shown as **Notes** in the revision history. Distinct from `description`, which belongs to the profile and is overwritten by every save.

**Returns:** `ConfigUploadResult` with `success`, `version`, `version_created`, `revision`, and `error`

`revision` is the revision number this upload produced — pass it as
`config_revision=` to pin processing to exactly what was just uploaded. It is
`None` when the stack has no revision history. A save that changed nothing cuts
no new revision, so `revision` is then the one already current, which is still
the right value to pin.

```python
# Upload to the default version
result = client.config.upload(
    config_file="./my-config.yaml",
    config_profile="default",
    validate=True
)

if result.success:
    print("Configuration uploaded")

# Create a new named version
result = client.config.upload(
    config_file="./my-config.yaml",
    config_profile="v2",
    description="Updated extraction rules"
)

if result.success:
    if result.version_created:
        print(f"New version created: {result.version}")
    else:
        print(f"Version updated: {result.version}")

# Score exactly what was uploaded, rather than "whatever that profile holds now"
result = client.config.upload(
    config_file="./attempt.yaml",
    config_profile="tuning-run-42",
    description="raised topK to 20",
)
client.batch.process(
    directory="./docs/",
    config_profile="tuning-run-42",
    config_revision=result.revision,
)
```

### config.download()

Download configuration from a deployed stack.

**Parameters:**
- `stack_name` (str, optional): Stack name override
- `output` (str, optional): Output file path
- `format` (str, optional): Format type — `"full"` or `"minimal"` (default: `"full"`)
- `pattern` (str, optional): Pattern override (auto-detected if not provided)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to download (default: active version)
- `config_revision` (int, optional): Download an exact **revision** of that profile instead of its current configuration

**Returns:** `ConfigDownloadResult` with `config`, `yaml_content`, `output_path`, and `revision`

**Raises:** `IDPResourceNotFoundError` if the requested revision is no longer
retained. It does not fall back to the profile's current configuration — that
would hand back a *different* configuration under the name you asked for, and it
would look like a success.

```python
result = client.config.download(
    output="downloaded-config.yaml",
    format="minimal"  # "full" or "minimal"
)

print(result.yaml_content)

# Retrieve exactly what an earlier run used
earlier = client.config.download(config_profile="lending", config_revision=7)
```

### config.list()

List all configuration versions in a deployed stack.

**Parameters:**
- `stack_name` (str, optional): Stack name override

**Returns:** `ConfigListResult` with `versions` (list of `ConfigVersionInfo`) and `count`

**ConfigVersionInfo fields:** `version_name`, `is_active`, `created_at`, `updated_at`, `description`, `latest_revision`, `published_revision`

`published_revision` is the revision each profile's configuration currently
reflects — the one to pin. It equals `latest_revision` except while a restore is in
flight. Both are `None` for a profile with no history.

```python
result = client.config.list()

print(f"Found {result.count} profiles:")
for version in result.versions:
    status = " (ACTIVE)" if version.is_active else ""
    at = f" @r{version.published_revision}" if version.published_revision else ""
    print(f"  - {version.version_name}{status}{at}")
```

### config.revisions()

Revision history of one Configuration Profile, newest first.

Every save of a profile cuts an immutable revision. This returns the ones still
retained — the last 20, plus anything labeled, pinned by a test run, or currently
in use. See [configuration-profiles.md](configuration-profiles.md#revision-history).

**Parameters:**
- `config_profile` (alias: `config_version`) (str, required): Profile whose history to list
- `stack_name` (str, optional): Stack name override

**Returns:** `ConfigRevisionListResult` with `profile`, `revisions` (list of `ConfigRevisionInfo`), and `count`

**ConfigRevisionInfo fields:** `revision`, `created_at`, `created_by`, `label`, `notes`, `size_bytes`, `published`, `pinned`, `class_fingerprint`

`published` marks the revision the profile currently reflects. `pinned` means a
test run scored against it, which exempts it from retention pruning. Two revisions
with the same `class_fingerprint` extract the same fields, so their accuracy
numbers are directly comparable.

> **Comparing fingerprints across the v0.6.7 boundary.** The hash was made stable
> against a DynamoDB round-trip in v0.6.7, so a revision cut before that upgrade can
> carry a different `class_fingerprint` than the same classes hash to now. Read a
> mismatch where one side predates the upgrade as *unknown*, not as *changed*; two
> revisions cut on v0.6.7 or later compare exactly.

```python
history = client.config.revisions(config_profile="lending")

for rev in history.revisions:
    marker = " <- current" if rev.published else ""
    print(f"  r{rev.revision} by {rev.created_by}: {rev.notes or ''}{marker}")

# An empty list means no history: an older deployment, or a profile untouched
# since the stack was upgraded.
```

**Iterating on one profile instead of many.** `upload()` returning `revision`,
`revisions()`, and `config_revision=` on `download()` / `batch.process()` together
let an automated tuning loop keep **one** profile and track its attempts as
revisions. Naming a new profile per attempt also works, but every one of them then
appears in the profile pickers and access-control scope lists of the whole
deployment.

### config.activate()

Activate a configuration version. If the configuration uses BDA (`use_bda=True`), a BDA blueprint sync is performed before activation.

**Parameters:**
- `config_profile` (alias: `config_version`) (str, required): Configuration profile to activate
- `stack_name` (str, optional): Stack name override

**Returns:** `ConfigActivateResult` with `success`, `activated_version`, `bda_synced`, `bda_classes_synced`, `bda_classes_failed`, and `error`

```python
result = client.config.activate("v2")

if result.success:
    print(f"Activated version: {result.activated_version}")
    if result.bda_synced:
        print(f"BDA synced: {result.bda_classes_synced} classes")
else:
    print(f"Failed to activate: {result.error}")
```

### config.delete()

Delete a configuration version.

**Parameters:**
- `config_profile` (alias: `config_version`) (str, required): Configuration profile to delete
- `stack_name` (str, optional): Stack name override

**Returns:** `ConfigDeleteResult` with `success`, `deleted_version`, and `error`

```python
result = client.config.delete("old-version")

if result.success:
    print(f"Deleted version: {result.deleted_version}")
else:
    print(f"Failed to delete: {result.error}")
```

**Note:** Cannot delete `"default"` or currently active versions.

### config.sync_bda()

Synchronize IDP document class schemas with BDA (Bedrock Data Automation) blueprints.

**Parameters:**
- `direction` (str, optional): Sync direction — `"bidirectional"` (default), `"bda_to_idp"`, or `"idp_to_bda"`
- `mode` (str, optional): Sync mode — `"replace"` (default, full alignment) or `"merge"` (additive)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to sync (default: active version)
- `stack_name` (str, optional): Stack name override

**Returns:** `ConfigSyncBdaResult` with `success`, `direction`, `mode`, `classes_synced`, `classes_failed`, `processed_classes`, and `error`

```python
# Bidirectional sync (default)
result = client.config.sync_bda()

# Import BDA blueprints to IDP (merge mode)
result = client.config.sync_bda(
    direction="bda_to_idp",
    mode="merge"
)

# Push IDP classes to BDA
result = client.config.sync_bda(
    direction="idp_to_bda",
    config_profile="v2"
)

if result.success:
    print(f"Synced {result.classes_synced} classes")
    for cls in result.processed_classes:
        print(f"  • {cls}")
else:
    print(f"Sync failed: {result.error}")
```

---

## Discovery Operations

Discover document class schemas from sample documents using Amazon Bedrock.

**Two modes:**
- **Stack-connected** (with `stack_name`): Uses the stack's discovery config from DynamoDB, saves discovered schema to config
- **Local** (without `stack_name`): Uses system default Bedrock settings, returns schema without saving

### discovery.run()

Analyze a document to generate a JSON Schema definition for a document class.

**Parameters:**
- `document_path` (str, required): Local path to document file (PDF, PNG, JPG, TIFF)
- `ground_truth_path` (str, optional): Path to JSON ground truth file
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to save to (stack mode only)
- `stack_name` (str, optional): Stack name override
- `page_range` (str, optional): Page range to extract from a PDF (e.g., "1-3")
- `class_name_hint` (str, optional): Hint for the document class name (LLM uses this as `$id`)
- `auto_detect` (bool, optional): If True, auto-detect section boundaries and discover each section. Returns `DiscoveryBatchResult`.
- `model_id` (str, optional): Override the Bedrock model ID used for discovery (e.g., `us.anthropic.claude-opus-4-6-v1`). When `None` (default), the discovery model from the stack config (stack mode) or system defaults (local mode) is used. Applies to with-ground-truth, without-ground-truth, and `auto_detect` modes.

**Returns:** `DiscoveryResult` with `status`, `document_class`, `json_schema`, `config_version`, `document_path`, `page_range`, and `error`. When `auto_detect=True`, returns `DiscoveryBatchResult`.

```python
# Local mode — no stack needed
client = IDPClient()
result = client.discovery.run("./invoice.pdf")
print(json.dumps(result.json_schema, indent=2))

# Stack mode — uses stack config, saves schema
client = IDPClient(stack_name="my-stack")
result = client.discovery.run("./w2-form.pdf")

# With ground truth for better accuracy
result = client.discovery.run(
    "./invoice.pdf",
    ground_truth_path="./invoice-expected.json"
)

# With class name hint
result = client.discovery.run(
    "./form.pdf",
    class_name_hint="W2 Tax Form"
)

# Discover specific page range from a PDF
result = client.discovery.run(
    "./lending_package.pdf",
    page_range="3-5",
    class_name_hint="W2 Form"
)

# Auto-detect sections and discover each
batch_result = client.discovery.run(
    "./lending_package.pdf",
    auto_detect=True,
    config_profile="v2"
)
for r in batch_result.results:
    print(f"{r.document_class} (pages {r.page_range}): {r.status}")

# Save to specific config version
result = client.discovery.run(
    "./form.pdf",
    config_profile="v2"
)

# Override the Bedrock model (e.g. use Claude Opus instead of the default)
result = client.discovery.run(
    "./invoice.pdf",
    ground_truth_path="./invoice.json",
    model_id="us.anthropic.claude-opus-4-6-v1",
)
```

### discovery.auto_detect_sections()

Detect document section boundaries in a multi-page PDF using LLM analysis.

**Parameters:**
- `document_path` (str, required): Local path to a PDF document
- `stack_name` (str, optional): Stack name override
- `model_id` (str, optional): Override the Bedrock model ID used for section detection. When `None` (default), the configured `auto_split.model_id` is used.

**Returns:** `AutoDetectResult` with `status`, `sections` (list of `AutoDetectSection`), `document_path`, and `error`

**AutoDetectSection fields:** `start` (int), `end` (int), `type` (str, optional)

```python
# Detect section boundaries
result = client.discovery.auto_detect_sections("./lending_package.pdf")

if result.status == "SUCCESS":
    for section in result.sections:
        print(f"Pages {section.start}-{section.end}: {section.type}")
    # Output:
    # Pages 1-2: Cover Letter
    # Pages 3-5: W2 Form
    # Pages 6-8: Bank Statement
```

### discovery.run_multi_section()

Discover multiple document classes from page ranges in a single PDF.

**Parameters:**
- `document_path` (str, required): Local path to a multi-page PDF
- `page_ranges` (list, required): List of dicts with `start` (int), `end` (int), and optional `label` (str)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to save to
- `stack_name` (str, optional): Stack name override
- `model_id` (str, optional): Override the Bedrock model ID used for discovery. Applied to every per-range discovery call.

**Returns:** `DiscoveryBatchResult` with one result per page range

```python
# Discover specific page ranges
result = client.discovery.run_multi_section(
    "./lending_package.pdf",
    page_ranges=[
        {"start": 1, "end": 2, "label": "Cover Letter"},
        {"start": 3, "end": 5, "label": "W2 Form"},
        {"start": 6, "end": 8, "label": "Bank Statement"},
    ],
    config_profile="v2"
)

print(f"Discovered {result.succeeded}/{result.total} sections")
for r in result.results:
    print(f"  Pages {r.page_range}: {r.document_class} ({r.status})")
```

### discovery.run_multi_doc()

Discover document classes from a collection of documents using embedding-based clustering and agentic analysis. Unlike `run()` (which analyzes one document at a time), this method analyzes a directory of mixed documents to automatically identify document types, cluster similar documents, and generate JSON Schemas for each discovered class.

**Requires:** `pip install -e "lib/idp_common_pkg[multi_document_discovery]"`

**Note:** Requires at least **2 documents per expected class**. Clusters with fewer than 2 documents are filtered as noise. For discovering schemas from individual documents, use `discovery.run()` instead.

**Parameters:**
- `document_dir` (str, optional): Directory path containing documents to analyze (recursive scan)
- `document_paths` (list[str], optional): List of individual document file paths
- `embedding_model_id` (str, optional): Bedrock embedding model ID (default: `us.cohere.embed-v4:0`)
- `analysis_model_id` (str, optional): Bedrock LLM for cluster analysis (default: `us.anthropic.claude-sonnet-4-6`)
- `output_dir` (str, optional): Directory to write individual JSON schema files per discovered class
- `save_to_config` (bool, optional): Save discovered schemas to the stack's configuration (default: False)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to save schemas to (required with `save_to_config`)
- `progress_callback` (callable, optional): Callback function for pipeline progress updates
- `region` (str, optional): AWS region

**Returns:** `MultiDocDiscoveryResult` with `status` (SUCCESS/PARTIAL/FAILED), `discovered_classes` (list of `DiscoveredClassResult`), `reflection_report`, `total_documents`, `total_clusters`, `noise_documents`, `config_version`, and `error`

**DiscoveredClassResult fields:** `cluster_id`, `classification`, `json_schema`, `document_count`, `sample_doc_ids`, `error`

```python
# Basic usage — discover from a directory
client = IDPClient()
result = client.discovery.run_multi_doc(document_dir="./samples/")

print(f"Status: {result.status}")
print(f"Documents: {result.total_documents} → Clusters: {result.total_clusters}")

for dc in result.discovered_classes:
    if not dc.error:
        print(f"  Cluster {dc.cluster_id}: {dc.classification} ({dc.document_count} docs)")

# Save schemas to output directory
result = client.discovery.run_multi_doc(
    document_dir="./samples/",
    output_dir="./schemas/"
)

# Save to stack configuration
client = IDPClient(stack_name="my-stack")
result = client.discovery.run_multi_doc(
    document_dir="./samples/",
    save_to_config=True,
    config_profile="v2"
)

# With explicit document paths
result = client.discovery.run_multi_doc(
    document_paths=["./doc1.pdf", "./doc2.png", "./doc3.jpg"]
)

# With progress callback
def on_progress(step, data=None):
    print(f"  [{step}] {data}")

result = client.discovery.run_multi_doc(
    document_dir="./samples/",
    progress_callback=on_progress
)

# Print reflection report
if result.reflection_report:
    print(result.reflection_report)
```

**Note:** If the required dependencies are not installed, this method returns a `MultiDocDiscoveryResult` with `status="FAILED"` and an error message instructing the user to install `idp-common[multi_document_discovery]`, rather than raising an exception.

### discovery.run_batch()

Run discovery on multiple documents sequentially. Ground truth paths are auto-matched to documents by position.

**Parameters:**
- `document_paths` (list, required): List of local file paths
- `ground_truth_paths` (list, optional): Parallel list of ground truth paths (use `None` for docs without ground truth)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to save to
- `stack_name` (str, optional): Stack name override
- `model_id` (str, optional): Override the Bedrock model ID used for discovery. Applied to every document in the batch.

**Returns:** `DiscoveryBatchResult` with `total`, `succeeded`, `failed`, and `results` (list of `DiscoveryResult`)

```python
# Batch without ground truth
result = client.discovery.run_batch([
    "./invoice.pdf",
    "./w2-form.pdf",
    "./paystub.png",
])
print(f"Succeeded: {result.succeeded}/{result.total}")

# Batch with selective ground truth (matched by position)
result = client.discovery.run_batch(
    ["./invoice.pdf", "./w2.pdf"],
    ground_truth_paths=[None, "./w2.json"],
)
```

---

## Manifest Operations

Operations for manifest generation and validation.

### manifest.generate()

Generate a manifest file from a directory or S3 URI.

```python
result = client.manifest.generate(
    directory="./documents/",
    baseline_dir="./baselines/",
    output="manifest.csv",
    file_pattern="*.pdf",
    recursive=True
)

print(f"Documents: {result.document_count}")
print(f"Baselines matched: {result.baselines_matched}")
```

### manifest.validate()

Validate a manifest file.

```python
result = client.manifest.validate(manifest_path="./manifest.csv")

if result.valid:
    print(f"Valid manifest with {result.document_count} documents")
else:
    print(f"Invalid: {result.error}")
```

---

## Testing Operations

Operations for load testing, Test Studio evaluation results, and performance validation.

### testing.load_test()

Run load testing by copying files to the input bucket.

**Parameters:**
- `source_file` (str, required): Source file to copy
- `stack_name` (str, optional): Stack name override
- `rate` (int, optional): Files per minute for constant load (default: 100)
- `duration` (int, optional): Duration in minutes (default: 1)
- `schedule_file` (str, optional): Optional schedule file for variable load
- `dest_prefix` (str, optional): Destination prefix in S3 (default: `"load-test"`)
- `config_profile` (alias: `config_version`) (str, optional): Configuration profile to tag files with

**Returns:** `LoadTestResult` with `success`, `total_files`, `duration_minutes`, and `error`

```python
result = client.testing.load_test(
    source_file="./sample.pdf",
    rate=100,              # Files per minute
    duration=5,            # Duration in minutes
    dest_prefix="load-test"
)

print(f"Total files: {result.total_files}")
print(f"Success: {result.success}")
```

### testing.get_test_result()

Get Test Studio evaluation results for a specific test run.

**Parameters:**
- `test_run_id` (str, required): Test run identifier
- `stack_name` (str, optional): Stack name override
- `wait` (bool, optional): Wait for test run to complete if still in progress (default: False)
- `timeout` (int, optional): Maximum wait time in seconds (default: 300)
- `poll_interval` (int, optional): Polling interval in seconds (default: 5)

**Returns:** `TestRunResult` with evaluation metrics

```python
# Get result immediately (may be evaluating)
result = client.testing.get_test_result(
    test_run_id="Fake-W2-Tax-Forms-20260410-173735"
)

# Wait for evaluation to complete
result = client.testing.get_test_result(
    test_run_id="Fake-W2-Tax-Forms-20260410-173735",
    wait=True,
    timeout=900
)

print(f"Status: {result.status}")
print(f"Overall Accuracy: {result.overall_accuracy:.2%}")
print(f"Precision: {result.accuracy_breakdown['precision']:.2%}")
print(f"Recall: {result.accuracy_breakdown['recall']:.2%}")
print(f"F1 Score: {result.accuracy_breakdown['f1_score']:.2%}")
print(f"Total Cost: ${result.total_cost:.2f}")
```

### testing.compare_test_runs()

Compare multiple Test Studio evaluation runs.

**Parameters:**
- `test_run_ids` (list[str], required): List of test run identifiers to compare (minimum 2)
- `stack_name` (str, optional): Stack name override

**Returns:** `TestComparisonResult` with metrics for each test run

```python
result = client.testing.compare_test_runs(
    test_run_ids=[
        "Fake-W2-Tax-Forms-20260410-173735",
        "Fake-W2-Tax-Forms-20260409-191545"
    ]
)

for test_run_id, metrics in result.metrics.items():
    print(f"\nTest Run: {test_run_id}")
    print(f"  Accuracy: {metrics['overallAccuracy']:.2%}")
    print(f"  Completed: {metrics['completedFiles']}/{metrics['filesCount']}")
    print(f"  Cost: ${metrics['totalCost']:.2f}")
```

---

## Response Models

All operations return typed Pydantic models. Import them from the top-level `idp_sdk` package:

```python
from idp_sdk import (
    # Document models
    DocumentUploadResult,
    DocumentStatus,
    DocumentDownloadResult,
    DocumentReprocessResult,
    DocumentRerunResult,
    DocumentDeletionResult,
    DocumentMetadata,
    DocumentInfo,
    DocumentListResult,

    # Batch models
    BatchResult,
    BatchProcessResult,
    BatchStatus,
    BatchInfo,
    BatchListResult,
    BatchReprocessResult,
    BatchRerunResult,
    BatchDownloadResult,
    BatchDeletionResult,

    # Evaluation models
    EvaluationReport,
    EvaluationMetrics,
    EvaluationBaselineListResult,
    BaselineResult,
    BaselineInfo,
    FieldComparison,
    DeleteResult,

    # Assessment models
    AssessmentConfidenceResult,
    AssessmentFieldConfidence,
    AssessmentGeometryResult,
    AssessmentFieldGeometry,
    AssessmentMetrics,

    # Search models
    SearchResult,
    SearchCitation,
    SearchDocumentReference,

    # Stack models
    StackDeploymentResult,
    StackDeletionResult,
    StackResources,
    StackOperationInProgress,
    StackMonitorResult,
    StackStableStateResult,
    FailureCause,
    FailureAnalysis,
    BucketInfo,
    CancelUpdateResult,
    OrphanedResourceCleanupResult,

    # Config models
    ConfigCreateResult,
    ConfigValidationResult,
    ConfigUploadResult,
    ConfigDownloadResult,
    ConfigActivateResult,
    ConfigVersionInfo,
    ConfigListResult,
    ConfigDeleteResult,

    # Discovery models
    DiscoveryResult,
    DiscoveryBatchResult,

    # Manifest models
    ManifestDocument,
    ManifestResult,
    ManifestValidationResult,

    # Testing models
    StopWorkflowsResult,
    ExecutionsStoppedResult,
    DocumentsAbortedResult,
    LoadTestResult,
    TestRunResult,
    TestComparisonResult,

    # Enums
    DocumentState,
    Pattern,
    RerunStep,
    StackState,

    # Exceptions
    IDPError,
    IDPConfigurationError,
    IDPStackError,
    IDPProcessingError,
    IDPResourceNotFoundError,
    IDPValidationError,
    IDPTimeoutError,
)
```

---

## Error Handling

```python
from idp_sdk import IDPClient, IDPProcessingError, IDPResourceNotFoundError

client = IDPClient(stack_name="my-stack")

try:
    result = client.document.process(file_path="./invoice.pdf")
except IDPProcessingError as e:
    print(f"Processing error: {e}")
except IDPResourceNotFoundError as e:
    print(f"Resource not found: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

---

## Complete Example

```python
from idp_sdk import IDPClient, RerunStep
import time

# Initialize client
client = IDPClient(stack_name="my-idp-stack", region="us-west-2")

# Upload single document
doc_result = client.document.process(file_path="./invoice.pdf")
doc_id = doc_result.document_id

# Monitor document processing
while True:
    status = client.document.get_status(document_id=doc_id)
    print(f"Status: {status.status.value}")

    if status.status.value in ["COMPLETED", "FAILED"]:
        break

    time.sleep(5)

# Download results if successful
if status.status.value == "COMPLETED":
    client.document.download_results(
        document_id=doc_id,
        output_dir="./results"
    )
    print("Results downloaded successfully")

# Process a batch
batch_result = client.batch.process(source="./documents/")
batch_id = batch_result.batch_id

# Monitor batch progress
while True:
    batch_status = client.batch.get_status(batch_id=batch_id)
    print(f"Progress: {batch_status.completed}/{batch_status.total}")

    if batch_status.all_complete:
        break

    time.sleep(10)

# Download batch results
client.batch.download_results(
    batch_id=batch_id,
    output_dir="./batch_results"
)

print(f"Batch complete! Success rate: {batch_status.success_rate:.1%}")
```

---

## See Also

- [IDP CLI Documentation](./idp-cli.md) - Command-line interface
- [SDK Examples](../lib/idp_sdk/examples/) - Code examples
- [API Reference](../lib/idp_sdk/README.md) - Detailed API documentation
