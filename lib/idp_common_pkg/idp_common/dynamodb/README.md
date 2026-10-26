# DynamoDB Module - Direct DynamoDB Integration

The `dynamodb` module provides direct DynamoDB integration for the IDP Common package. It is **the only** path by which Lambda functions read and write document tracking state: they interact with the TrackingTable through this module, either directly or through the [document service factory](../docs_service_README.md), which always returns `DocumentDynamoDBService`. AWS AppSync was removed from the solution — see [AppSync → REST API Migration](../../../../docs/migration-appsync-to-rest.md).

## Overview

The module provides:

- Direct DynamoDB operations using boto3
- Document CRUD operations against the TrackingTable
- Transaction support for atomic operations
- Error handling and logging
- TTL support for document expiration

## Key Components

### DynamoDBClient

Low-level client for DynamoDB operations. Always pass `table_name` explicitly when
the table is not the TrackingTable: the constructor falls back to the
`TRACKING_TABLE` environment variable.

| Method | What it does |
|---|---|
| `put_item(item, condition_expression=None, *, expression_attribute_names=None, expression_attribute_values=None)` | Insert or replace an item, optionally conditionally |
| `update_item(key, update_expression, ..., condition_expression=None)` | Update (upsert) an item |
| `get_item(key, *, consistent_read=False, projection_expression=None, expression_attribute_names=None)` | Retrieve one item by key, or `None` |
| `delete_item(key, *, condition_expression=None, expression_attribute_names=None, expression_attribute_values=None, return_values=None)` | Delete an item, optionally conditionally |
| `batch_get_items(keys, *, consistent_read=False, projection_expression=None, expression_attribute_names=None, max_attempts=6)` | Get many items: de-duplicated, chunked at 100, `UnprocessedKeys` retried |
| `batch_write_items(*, put_items=(), delete_keys=(), max_attempts=6)` | Put and/or delete many items: deletes de-duplicated, chunked at 25, `UnprocessedItems` retried |
| `transact_write_items(transact_items)` | Execute a transaction (`Put`, `Update`, `Delete`, `ConditionCheck`) |
| `scan(...)` | One page of a scan, with optional filter |
| `scan_all(...)` | Scan every page, following `LastEvaluatedKey` |
| `query(key_condition_expression, expression_attribute_names=None, expression_attribute_values=None, limit=None, exclusive_start_key=None, *, index_name=None, scan_index_forward=None, filter_expression=None, projection_expression=None, select=None, consistent_read=None)` | One page of a query |
| `query_pages(key_condition_expression, *, ..., page_size=None, exclusive_start_key=None, max_pages=None)` | Yield raw query responses, following `LastEvaluatedKey` until it is absent or `max_pages` is reached |
| `query_count(key_condition_expression, *, ..., index_name=None, filter_expression=None, consistent_read=None)` | Sum `Count` over every page with `Select=COUNT` |

The keyword-only options on `query`, `get_item`, `delete_item` and `put_item` are
sent to DynamoDB only when set, so existing positional calls behave as before.

**A filtered query must paginate.** `Limit` bounds the items DynamoDB *examines*, not
the items that match, so one page of a filtered query can hold fewer matches than
exist, or none. Iterate `query_pages` (or use `query_count`) instead of reading a
single `query` response.

**`batch_get_items` raises instead of returning a partial result.** Keys DynamoDB
leaves in `UnprocessedKeys` are re-submitted with exponential backoff and full
jitter; when `max_attempts` runs out the call raises
`DynamoDBUnprocessedKeysError`, a `DynamoDBError` with
`error_code == "UnprocessedKeys"` whose `items` holds the rows read before it gave
up, for a caller that can use a partial answer on purpose. `batch_write_items` does the same with
`error_code == "UnprocessedItems"`. A batch of up to 100 keys with nothing
unprocessed behaves as it always did.

**Transactions report which item cancelled them.** A cancelled
`transact_write_items` raises `DynamoDBTransactionCanceledError`, a `DynamoDBError`
subclass carrying `cancellation_reasons` (one entry per transact item, in request
order) and `reason_code(index)`, which returns the code for one item (`"None"` when it
did not cause the cancellation or the index is out of range,
`"ConditionalCheckFailed"` for a false condition). `ConditionCheck` items get the
table name injected like the other operation types, and the caller's list is never
mutated.

**Expected condition failures log at DEBUG.** Conditions are routine control flow
(create-if-absent, optimistic concurrency, dedupe), so a
`ConditionalCheckFailedException` from `put_item`, `update_item` or `delete_item`,
and a transaction cancelled only with `None` / `ConditionalCheckFailed` reasons, are
logged at DEBUG. They still raise. Every other `ClientError` logs at ERROR.

### `calculate_ttl`

`idp_common.dynamodb.calculate_ttl(days, *, now=None) -> int` returns the epoch
seconds `days` after `now` (default: the current UTC time; a naive `now` is treated
as UTC), the value a DynamoDB TTL attribute holds. `days` may be fractional.
`DocumentDynamoDBService.calculate_ttl(days)` delegates to it.

`put_item()` and `update_item()` both take an optional `condition_expression`,
evaluated by DynamoDB against the item as it exists at write time and surfaced as a
`DynamoDBError` with `error_code == "ConditionalCheckFailedException"`. **Pass one
whenever the value you are writing was computed from a value you read.** Without it
the write is unconditional, so two callers that read the same state both succeed and
the loser's contribution is gone with nothing raising — the read cannot serve as its
own guard, because nothing ties it to the write. `attribute_exists(<key>)` is the
narrowest useful form: `update_item` is an upsert, so with no condition an item
deleted between the read and the write is silently re-created holding only the
attributes the expression sets.

**Prefer a targeted `update_item` over a `put_item` that re-lists the attributes to
keep.** `put_item` replaces the whole item, so any attribute absent from the dict is
deleted; a hand-maintained list of attributes to carry forward fails silently and
permanently the first time somebody adds a field and does not extend it.
`scripts/tests/test_config_head_writers.py` enforces this for the configuration
profile head record, which is where it went wrong.

### DocumentDynamoDBService

High-level service for document operations:
- `create_document()` - Create new documents with list partitioning
- `update_document(document, expected_status=None, unless_status=None)` - Update
  existing documents. With `expected_status`, the write lands only if the stored
  `ObjectStatus` is still that value; with `unless_status`, it lands unless the
  stored `ObjectStatus` is that value (a record with no status, or no record, is
  written as without it). Either way a refusal raises `DynamoDBError` with error
  code `ConditionalCheckFailedException`, and the two cannot be combined. The
  queue processor writes RUNNING after `StartExecution` with
  `unless_status=ABORTED`, and stops the execution when that is refused
- `get_document(object_key, consistent_read=False)` - Retrieve documents by
  object key. The default read is eventually consistent, so pass
  `consistent_read=True` when deciding from a write that has just landed
- `claim_for_execution(object_key)` - A conditional write that changes nothing
  and returns False if the document is ABORTED or has no tracking record. The
  queue processor calls it immediately before starting an execution, so a
  QUEUED document aborted after the processor read it is not started
- `list_documents()` - List documents with date filtering
- `list_documents_date_hour()` - List by specific date/hour
- `list_documents_date_shard()` - List by date/shard
- `calculate_ttl()` - Generate TTL timestamps (delegates to the module-level `calculate_ttl`)

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

The attribute names this module writes still match the field names in the
retained GraphQL contract at `nested/api-resolvers/src/api/schema.graphql`. That
file is no longer served by a live GraphQL service: it is now a typed contract
used for UI type codegen (`src/ui/codegen.config.mjs` reads it) and as the
authoritative baseline for the REST dispatcher's input-shape validation and
per-resolver RBAC checks. Keep the names below in sync with it — a mismatch
shows up as a missing field in the UI, not as a schema error.

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

## Historical: migrating code written before AppSync was removed

> **This section is a historical note, not a live choice.** AppSync is gone from
> the solution and `idp_common.appsync` no longer exists, so there is no "AppSync
> vs DynamoDB" decision to make — direct DynamoDB is the only option. The steps
> below apply only if you are porting old code (an out-of-tree fork, an old
> branch, or a stale copy of a resolver) that still imports the deleted module;
> those imports fail with an `ImportError`. New code should call
> `create_document_service()` from [`docs_service`](../docs_service_README.md).

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

3. Method names are unchanged, so call sites do not need edits:
   ```python
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
