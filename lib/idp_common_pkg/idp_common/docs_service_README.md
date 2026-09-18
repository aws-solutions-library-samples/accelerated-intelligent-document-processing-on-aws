# Document Service Factory

The `docs_service` module is a thin factory in front of the document tracking
service. It has exactly one backend: **DynamoDB**. `create_service()` always
returns a [`DocumentDynamoDBService`](dynamodb/README.md) writing to the
TrackingTable.

The factory exists because dozens of Lambda handlers call
`create_document_service()` rather than constructing the DynamoDB service
themselves, and because it once selected between two backends. AWS AppSync has
since been removed from the solution entirely — the web UI talks to an API
Gateway REST API, and backend workers write the TrackingTable directly instead of
publishing GraphQL mutations. See
[AppSync → REST API Migration](../../../docs/migration-appsync-to-rest.md) for
why.

## What survives only as a back-compat shim

Several parts of this module's surface are deliberately inert. They are kept so
existing call sites keep importing and running without edits; none of them
changes behavior:

| Surface | Behavior today |
|---|---|
| `create_service(mode=...)` / `create_document_service(mode=...)` | The `mode` argument is accepted and **ignored**. DynamoDB is always used. |
| `api_url=` keyword | Accepted and **dropped** (`kwargs.pop("api_url", None)`) before the DynamoDB service is constructed. It was the AppSync endpoint. |
| `is_appsync_mode()` / `DocumentServiceFactory.is_appsync_mode()` | Always returns `False`. |
| `is_dynamodb_mode()` / `DocumentServiceFactory.is_dynamodb_mode()` | Always returns `True`. |
| `get_document_tracking_mode()` / `get_current_mode()` | Always returns `"dynamodb"`. |
| `DOCUMENT_TRACKING_MODE` env var | Still set to `dynamodb` on the pattern Lambdas, but **the code never reads it**. Setting it to anything else has no effect. |
| `SUPPORTED_MODES`, `DEFAULT_MODE`, `DYNAMODB_MODE` | Retained module constants; `SUPPORTED_MODES == ["dynamodb"]` and `DEFAULT_MODE == "dynamodb"`. |

If you are auditing this module, the constant `False`/`True` returns above are
intentional, not a bug — removing them would break callers that still branch on
them.

## Key Components

### DocumentServiceFactory

```python
from idp_common.docs_service import DocumentServiceFactory

# Create the DynamoDB-backed service
service = DocumentServiceFactory.create_service()

# Extra keyword arguments are forwarded to DocumentDynamoDBService
service = DocumentServiceFactory.create_service(table_name="my-tracking-table")
```

`create_service()` forwards `**kwargs` to `DocumentDynamoDBService`, so
`dynamodb_client=` and `table_name=` both work. With neither, the service builds
its own client from the `TRACKING_TABLE` environment variable.

### Convenience Functions

```python
from idp_common.docs_service import (
    create_document_service,
    get_document_tracking_mode,
    is_dynamodb_mode,
)

service = create_document_service()

assert get_document_tracking_mode() == "dynamodb"
assert is_dynamodb_mode() is True
```

## Environment Configuration

The factory itself reads no environment variables. The service it returns reads:

- `TRACKING_TABLE` — DynamoDB TrackingTable name (used when no explicit
  `table_name`/`dynamodb_client` is passed)
- `AWS_REGION` — region for the DynamoDB client

## Installation

The extra that carries this module's dependencies is `docs_service`, and it is
the one every Lambda `requirements.txt` in this repo uses:

```
../../lib/idp_common_pkg[classification,docs_service]
```

```bash
pip install -e "lib/idp_common_pkg[core,docs_service]"
```

There is also a vestigial `appsync` extra in `pyproject.toml`. It installs
`requests` for a module that no longer exists, no `requirements.txt` references
it, and you should not use it.

## Usage Patterns

### Basic Usage

```python
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status

service = create_document_service()

document = Document(
    input_key="my-document.pdf",
    status=Status.QUEUED,
    queued_time="2024-01-01T12:00:00Z"
)

service.create_document(document)
service.update_document(document)
retrieved_doc = service.get_document("my-document.pdf")
```

### Lambda Function Integration

This is how the pattern Lambdas use it — see
`patterns/unified/src/classification_function/index.py` for a live example:

```python
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status

def lambda_handler(event, context):
    document = Document.load_document(
        event_data=event["document"],
        working_bucket=working_bucket,
        logger=logger,
    )

    document.status = Status.CLASSIFYING
    document.workflow_execution_arn = event.get("execution_arn")

    document_service = create_document_service()
    document_service.update_document(document)

    return {"statusCode": 200}
```

### Testing

Because there is a single backend, a test only needs to assert the concrete
class (or inject a mocked DynamoDB client):

```python
from unittest.mock import Mock

from idp_common.docs_service import create_document_service
from idp_common.dynamodb import DynamoDBClient


def test_factory_returns_dynamodb_service():
    service = create_document_service(table_name="test-table")
    assert service.__class__.__name__ == "DocumentDynamoDBService"


def test_with_mocked_client():
    service = create_document_service(dynamodb_client=Mock(spec=DynamoDBClient))
    service.create_document(test_document)
    service.client.transact_write_items.assert_called_once()
```

A legacy `mode=` argument in an older test is harmless — it is ignored — so
`create_document_service(mode="dynamodb")` and
`create_document_service(mode="appsync")` return the same DynamoDB service.

## Service Interface

The returned `DocumentDynamoDBService` provides:

- `create_document(document, expires_after=None) -> Optional[str]`
- `update_document(document) -> Document`
- `get_document(object_key) -> Optional[Document]`
- `batch_get_documents(object_keys) -> List[Dict[str, Any]]`
- `list_documents(...) -> Dict[str, Any]`
- `list_documents_date_hour(...) -> Dict[str, Any]`
- `list_documents_date_shard(...) -> Dict[str, Any]`
- `update_document_status(...)`
- `update_document_section(...)`
- `create_document_run(...)` / `list_document_runs(...)` /
  `get_document_run(...)` / `delete_document_run(...)`
- `calculate_ttl(days=30) -> int`

See [the DynamoDB module README](dynamodb/README.md) for the full contract,
including the document-run (version) records and the table's key structure.

## Error Handling

`create_service()` no longer raises on an unrecognized mode — there is no mode to
get wrong. Errors come from the DynamoDB service itself, as `DynamoDBError`:

```python
from idp_common.docs_service import create_document_service
from idp_common.dynamodb import DynamoDBError

service = create_document_service()

try:
    service.create_document(document)
except DynamoDBError as e:
    logger.error(f"Document creation failed: {e} (code={e.error_code})")
```

## Migration Guide

### From direct DynamoDB usage

Both forms are supported; the factory is preferred so construction stays in one
place:

```python
# Direct
from idp_common.dynamodb import DocumentDynamoDBService
service = DocumentDynamoDBService(table_name=table_name)

# Via the factory (equivalent)
from idp_common.docs_service import create_document_service
service = create_document_service(table_name=table_name)
```

### From code written before AppSync was removed

Old call sites that imported `idp_common.appsync` will fail with an
`ImportError` — that package is gone, along with `DocumentAppSyncService`.
Replace them with the factory and drop the endpoint argument:

```python
# Before AppSync removal (no longer importable)
from idp_common.appsync import DocumentAppSyncService
service = DocumentAppSyncService(api_url=appsync_url)

# Now
from idp_common.docs_service import create_document_service
service = create_document_service()
```

Call sites that merely pass `mode=` or `api_url=` into this factory do **not**
need to change — those arguments are ignored (see the shim table above).

## Best Practices

1. **Use the factory** (`create_document_service()`) rather than constructing
   `DocumentDynamoDBService` in each handler, so the construction point stays
   single.
2. **Do not pass `mode=` or `api_url=`** in new code; they are ignored.
3. **Set `TRACKING_TABLE`** in the Lambda environment (the pattern templates do)
   instead of hardcoding a table name.
4. **Handle `DynamoDBError`** around writes, and let it propagate where a failed
   status write should fail the step.

## Examples

See `idp_common/dynamodb/example.py` for runnable usage examples covering basic
service creation, factory usage, and document operations with pages and
sections.
