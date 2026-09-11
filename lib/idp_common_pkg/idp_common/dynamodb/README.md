# DynamoDB Module - Direct DynamoDB Integration

The `dynamodb` module provides direct DynamoDB integration for the IDP Common package, allowing Lambda functions to interact with the TrackingTable without going through AppSync GraphQL API.

## Overview

This module is designed to replace AppSync dependencies in Lambda functions while maintaining the same functionality and data structures. It provides:

- Direct DynamoDB operations using boto3
- Document CRUD operations matching AppSync schema
- Transaction support for atomic operations
- Error handling and logging
- TTL support for document expiration

## Key Components

### DynamoDBClient

Low-level client for DynamoDB operations:
- `put_item()` - Insert items
- `update_item()` - Update existing items
- `get_item()` - Retrieve items by key
- `transact_write_items()` - Execute transactions
- `scan()` - Scan table with filters
- `query()` - Query with key conditions

### DocumentDynamoDBService

High-level service for document operations:
- `create_document()` - Create new documents with list partitioning
- `update_document()` - Update existing documents
- `get_document()` - Retrieve documents by object key
- `list_documents()` - List documents with date filtering
- `list_documents_date_hour()` - List by specific date/hour
- `list_documents_date_shard()` - List by date/shard
- `calculate_ttl()` - Generate TTL timestamps

#### Document run (version) records

Each successful processing run of a document is recorded as an immutable "run"
item so prior results can be retained and compared (see the user-facing
[Document Versions](../../../../docs/document-versions.md) guide and the
companion `idp_common.document_versions` module, which pins each run's output
S3 object versions into a manifest):

- `create_document_run(document, run_id, manifest_uri, file_count, expires_after)`
  - Writes `PK = doc#<key>`, `SK = run#<run_id>` and increments a
    `VersionCount` counter on the doc item.
- `list_document_runs(object_key)` - All runs for a document, newest first.
- `get_document_run(object_key, run_id)` - A single run record.
- `delete_document_run(object_key, run_id)` - Remove a run record and decrement
  `VersionCount` (S3 artifact cleanup is the caller's responsibility via
  `document_versions.delete_run_artifacts`).

**Purging output before a re-run:**

```python
delete_current_output_objects(
    s3_client, output_bucket: str, input_key: str,
    subprefixes: Sequence[str] | None = None,
) -> int   # number of objects deleted
```

Deletes the *current* output artifacts for a document key. Used by the queue
sender and the reprocess resolver so a re-upload sharing a filename cannot
inherit the previous document's results: OCR's retry-safe recovery reads
`pages/`, and without the purge it would reinstate stale output (issue #719).

⚠️ **`subprefixes` is a safety scope, and it defaults to `None` — meaning
unscoped.** Both shipped callers pass `("pages/",)` deliberately: scoping to the
one subprefix OCR recovery actually reads makes it impossible for an upload of
`foo` to remove a nested document at `foo/bar.pdf/*`. A new caller that omits it
gets the broad delete, so pass it explicitly.

Failures are swallowed so ingest still proceeds, and emit the
`StaleOutputPurgeFailed` metric — alarmed, because the alternative is silent stale
extraction.

> **GSI safety.** Run items deliberately omit the `ItemType` and
> `InitialEventTime` attributes so they never enter the `TypeDateIndex` GSI
> (which keys on both), and `VersionCount` is **not** in that GSI's INCLUDE
> projection. Document versioning therefore required **no GSI schema change** —
> GSIs cannot be modified without replacing/rehydrating the index.

### JobDynamoDBService

Service for managing batch job records (used by the `/jobs` REST API):
- `create_job_record()` - Create a job metadata record with optional TTL and metadata
- `get_job_record()` - Retrieve a job record by job ID (Files map values returned as Status enum)
- `update_job_files()` - Replace the entire Files map on a job record
- `update_file_status()` - Update a single file's status within a job record

## Usage

### Basic Usage

```python
from idp_common.dynamodb import DocumentDynamoDBService
from idp_common.models import Document, Status

# Initialize service (uses TRACKING_TABLE env var)
service = DocumentDynamoDBService()

# Create a document
document = Document(
    input_key="my-document.pdf",
    status=Status.QUEUED,
    queued_time="2024-01-01T12:00:00Z"
)

# Create in DynamoDB
service.create_document(document)

# Update document
document.status = Status.PROCESSING
updated_doc = service.update_document(document)

# Retrieve document
retrieved_doc = service.get_document("my-document.pdf")
```

### Advanced Usage

```python
# Custom client configuration
from idp_common.dynamodb import DynamoDBClient, DocumentDynamoDBService

client = DynamoDBClient(
    table_name="my-tracking-table",
    region="us-east-1"
)
service = DocumentDynamoDBService(dynamodb_client=client)

# Create with TTL
ttl = service.calculate_ttl(days=30)
service.create_document(document, expires_after=ttl)

# List documents with filtering
results = service.list_documents(
    start_date_time="2024-01-01T00:00:00Z",
    end_date_time="2024-01-31T23:59:59Z",
    limit=100
)

# Query by date and hour
hourly_docs = service.list_documents_date_hour(
    date="2024-01-01",
    hour=14
)
```

### Job Operations

```python
from idp_common.job_service import create_job_service
from idp_common.models import Status

# Initialize service (uses TRACKING_TABLE env var)
job_service = create_job_service()

# Create a job record
job_service.create_job_record(
    job_id="a1b2c3d4",
    metadata={"source": "api"}
)

# Get job record (Files values returned as Status enum)
record = job_service.get_job_record("a1b2c3d4")

# Update a single file's status
job_service.update_file_status("a1b2c3d4", "doc.pdf", Status.COMPLETED)

# Replace entire Files map
job_service.update_job_files("a1b2c3d4", {
    "doc_a.pdf": Status.COMPLETED,
    "doc_b.pdf": Status.IN_PROGRESS,
})
```

## Data Structure Compatibility

The module maintains full compatibility with the existing AppSync schema:

### Document Table Structure
- **PK**: `doc#{ObjectKey}` - Primary partition key
- **SK**: `none` - Sort key (always "none" for documents)
- **ObjectKey**: Document identifier
- **ObjectStatus**: Document processing status
- **Pages**: List of page objects with Id, Class, ImageUri, TextUri
- **Sections**: List of section objects with Id, PageIds, Class, OutputJSONUri
- **Metering**: JSON string of metering data
- **TTL**: ExpiresAfter timestamp

### List Partition Structure
- **PK**: `list#{date}#s#{shard}` - Time-based partition key
- **SK**: `ts#{timestamp}#id#{ObjectKey}` - Sort key for chronological ordering
- **ObjectKey**: Document identifier
- **QueuedTime**: When document was queued

### Job Record Structure
- **PK**: `job#{job_id}` - Primary partition key
- **SK**: `metadata` - Sort key (always "metadata" for job records)
- **Files**: Map of filenames to status values (e.g., `{"doc.pdf": "COMPLETED"}`)
- **CreatedAt**: ISO 8601 timestamp of job creation
- **UpdatedAt**: ISO 8601 timestamp of last update
- **Metadata**: Optional JSON string of job metadata
- **ExpiresAfter**: Optional TTL timestamp

## Error Handling

The module provides comprehensive error handling:

```python
from idp_common.dynamodb import DynamoDBError

try:
    service.create_document(document)
except DynamoDBError as e:
    logger.error(f"DynamoDB operation failed: {e}")
    logger.error(f"Error code: {e.error_code}")
```

## Environment Variables

The module uses these environment variables:

- `TRACKING_TABLE` - DynamoDB table name
- `AWS_REGION` - AWS region

## Migration from AppSync

To migrate from AppSync to direct DynamoDB:

1. Replace imports:
   ```python
   # Old
   from idp_common.appsync import DocumentAppSyncService
   
   # New
   from idp_common.dynamodb import DocumentDynamoDBService
   ```

2. Update service initialization:
   ```python
   # Old
   service = DocumentAppSyncService(api_url=appsync_url)
   
   # New
   service = DocumentDynamoDBService(table_name=table_name)
   ```

3. Method calls remain the same:
   ```python
   # These work with both services
   service.create_document(document)
   service.update_document(document)
   service.get_document(object_key)
   ```

## Performance Considerations

- **Transactions**: Document creation uses DynamoDB transactions for consistency
- **Sharding**: List partitions are sharded by time to distribute load
- **Pagination**: All list operations support pagination via `exclusive_start_key`
- **Filtering**: Date-based filtering uses efficient query operations when possible

## Logging

The module provides detailed logging at DEBUG and INFO levels:
- Operation success/failure
- Performance metrics
- Data conversion warnings
- Error details with context

## Testing

The module is designed to be easily testable with mocked DynamoDB clients:

```python
from unittest.mock import Mock
from idp_common.dynamodb import DocumentDynamoDBService

# Mock client for testing
mock_client = Mock()
service = DocumentDynamoDBService(dynamodb_client=mock_client)

# Test operations
service.create_document(test_document)
mock_client.transact_write_items.assert_called_once()
```
