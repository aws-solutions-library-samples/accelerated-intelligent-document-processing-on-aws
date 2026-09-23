# IDP SDK

Python SDK for programmatic access to IDP Accelerator capabilities.

## Installation

Run from the repository root. The SDK requires `idp_common` by name, and that name
on public PyPI belongs to an unrelated party, so both packages go in a single
command and both come from a path:

```bash
# From local development
pip install -e ./lib/idp_common_pkg -e ./lib/idp_sdk

# Or with uv
uv pip install -e ./lib/idp_common_pkg -e ./lib/idp_sdk
```

`make setup` (or `make setup-venv`) installs the SDK together with every other
first-party package in one pass. See
[Installing First-Party Packages Safely](../../docs/dependency-confusion.md).

## Quick Start

```python
from idp_sdk import IDPClient

# Initialize client with stack name
client = IDPClient(stack_name="my-idp-stack", region="us-west-2")

# Process documents from a directory
result = client.batch.process(directory="./documents/")
print(f"Batch ID: {result.batch_id}")
print(f"Queued: {result.documents_queued} documents")

# Check processing status
status = client.batch.get_status(batch_id=result.batch_id)
print(f"Completed: {status.completed}/{status.total}")

# Download results
client.batch.download_results(
    batch_id=result.batch_id,
    output_dir="./results"
)

# Reprocess documents from a specific step
reprocess_result = client.batch.reprocess(
    step="extraction",
    batch_id=result.batch_id
)
print(f"Requeued: {reprocess_result.documents_queued} documents")
```

## Configuration Management

The SDK manages **Configuration Profiles** — named configurations, each with its
own revision history.

> `config_profile=` is the current keyword. `config_version=` is the former name
> and is still accepted on every method that took it, so existing code keeps
> working; passing both with different values raises `ValueError`. See
> [configuration-profiles.md](../../docs/configuration-profiles.md).

```python
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-idp-stack")

# Upload configuration to a specific profile
client.config.upload(
    config_file="config.yaml",
    config_profile="production-v2",
    description="Updated model settings for new document types"
)

# Download a specific configuration profile
client.config.download(
    config_profile="production-v2",
    output="downloaded-config.yaml"
)

# Validate configuration
validation = client.config.validate(config_file="config.yaml")
if validation.valid:
    print("Configuration is valid")

# Process documents using a specific profile
result = client.batch.process(
    directory="./documents/",
    config_profile="production-v2"
)
```

## Stack-Independent Operations

Some operations don't require a deployed stack:

```python
from idp_sdk import IDPClient

client = IDPClient()  # No stack required

# Generate manifest from directory
manifest_result = client.manifest.generate(
    directory="./documents/",
    output="manifest.csv"
)

# Create configuration template
config_result = client.config.create(
    features="min",
    pattern="pattern-2",
    output="config.yaml"
)

# Validate configuration
validation = client.config.validate(config_file="./config.yaml")
if not validation.valid:
    print(f"Errors: {validation.errors}")
```

## Operation Namespaces

The SDK organizes functionality into 9 operation namespaces:

- **batch**: Process multiple documents, check status, rerun from specific steps
- **document**: Process and manage individual documents
- **config**: Create, validate, upload, and download configurations
- **manifest**: Generate and validate document manifests
- **stack**: Deploy and manage CloudFormation stacks
- **evaluation**: Compare results against baseline data
- **assessment**: Analyze extraction quality and confidence scores
- **search**: Query processed documents with natural language
- **testing**: Performance and load testing

## Result models

Every operation returns a typed result object: a Pydantic model for the document,
batch, stack and config surfaces, and a dataclass for the evaluation and search
ones. They live in `idp_sdk/models/` and are re-exported from the top-level
package. `docs/idp-sdk.md` documents the fields per operation; two properties of
the set are worth knowing before you add or change one.

**A result model's fields are exactly what its operation can supply.** A field the
operation never populates reads as a measured `None` to a caller, which is worse
than its absence — a `total_count` that was always `None` is indistinguishable
from an empty table. So `DocumentListResult.count` is the size of the page it
carries and there is no total (a DynamoDB scan reports none), and `BaselineInfo`
leaves `created_date` unset from `list_baselines` because an S3 prefix listing
does not return one. If a field cannot be filled from the processor's response,
either extend the processor to supply it or leave the field out.

**A mismatch between a model and its call site is a type error, not a test
failure.** For the dataclasses it raises `TypeError` at construction; for the
Pydantic models an unknown keyword is *silently ignored*, which is how
`DocumentInfo(batch_id=...)` dropped every document's batch id without anything
failing. Both shapes are caught statically: `reportCallIssue` is an **error** in
`pyrightconfig.json`, so `make typecheck` fails on a constructor call that does
not match its model. Run it after changing either side — a unit test that asserts
on the mock's call arguments cannot see this class of defect, so the tests in
`tests/unit/test_search_operations.py`,
`tests/unit/test_document_list_operation.py` and
`tests/unit/test_evaluation_operations.py` build the real result object from a
stubbed processor response instead.

**A score is `Optional[float]`, and `None` never means zero.** Every metric the
evaluation surface returns can be absent, and the distinction carries information a
`0.0` would destroy: a section the pipeline excluded from evaluation records no
scores, a stack whose evaluations have not run reports no documents, and a query
matching nothing is not a query that scored zero. So callers format these through a
guard rather than directly — `f"{metrics.avg_accuracy:.1%}"` raises `TypeError` on
`None`. The same rule is why `SearchResult.confidence` is `None` for an empty
answer.

Three things follow for the evaluation surface specifically.

`get_report` reads `<document key>/evaluation/results.json` from the output bucket.
The key comes from `idp_common.evaluation.contract.evaluation_results_key` — it is
**imported, not restated**, because the evaluation service and the aggregation
Lambda import the same helper, so a copy here could drift and leave this reader
looking for an object nothing writes. Its field vocabulary (`accuracy`,
`precision`, `recall`, `f1_score`, per-attribute comparisons) tracks that file.

`get_metrics` averages **documents** for its four top-level scores and aggregates
**sections** in `by_document_class`, because a document class is a property of a
section rather than of a document. Each metric carries its own denominator, so a
document or section that reported no score does not dilute the others.

⚠️ **`get_metrics(document_class=…)` returns `None` for all four top-level
averages.** They are whole-document figures and cannot answer a question about one
class of section; returning one anyway would be a real number measuring something
the caller did not ask for. The class-scoped answer is
`by_document_class[<class>]`, which carries all four scores. The filter is echoed
back on the result as `document_class`, which is what distinguishes this `None`
from "nothing reported that metric".

## Buckets the CLI creates

Two S3 buckets are created imperatively (outside CloudFormation) and are
hardened with the same `EnforceSSLOnly` bucket policy the stack's own buckets
carry — deny `s3:*` when `aws:SecureTransport` is false, on the bucket and its
objects:

| Bucket | Created by | Purpose |
|---|---|---|
| `<basename>-<region>` (default `idp-accelerator-artifacts-<account>-<region>`) | `idp-cli publish` | Build artifacts, templates, Lambda zips |
| `idp-cli-config-<account>-<region>-<suffix>` | `idp-cli deploy` | Staging a `--custom-config` upload (30-day lifecycle) |

The policy is merged additively — your own statements survive, and a stale
`EnforceSSLOnly` is replaced rather than duplicated, so re-running is
idempotent. It is applied to **pre-existing** buckets too (unlike Block Public
Access, which is never modified on a bucket the CLI didn't create, so a manual
remediation can't be reverted); there it's best-effort, warning and continuing
if the account restricts `s3:PutBucketPolicy`. ARNs use the region's real
partition, so GovCloud gets `arn:aws-us-gov:`.

To supply your own pre-hardened bucket (KMS CMK, access logging, tags), pass
`--bucket-basename` — see
[Enterprise artifact bucket hardening](../../docs/deployment-private-network.md).

## Common Patterns

### Batch Processing with Monitoring

```python
client = IDPClient(stack_name="my-stack")

# Start batch processing
result = client.batch.process(directory="./invoices/")
print(f"Started batch: {result.batch_id}")

# Poll for status
import time
while True:
    status = client.batch.get_status(batch_id=result.batch_id)
    print(f"Progress: {status.completed}/{status.total}")
    if status.all_complete:
        break
    time.sleep(5)
```

### Reprocessing Documents

```python
# Reprocess from classification step
reprocess = client.batch.reprocess(
    step="classification",
    batch_id="my-batch-id"
)
print(f"Requeued {reprocess.documents_queued} documents")

# Or reprocess specific documents
reprocess = client.batch.reprocess(
    step="extraction",
    document_ids=["doc1.pdf", "doc2.pdf"]
)
```

### Configuration Versioning

```python
# Create and upload a new configuration profile
client.config.upload(
    config_file="config.yaml",
    config_profile="v2.0",
    description="Updated extraction rules"
)

# Process with a specific profile
result = client.batch.process(
    directory="./docs/",
    config_profile="v2.0"
)
```

## Documentation

See [docs/idp-sdk.md](../../docs/idp-sdk.md) for complete API reference.

## Examples

- [basic_processing.py](examples/basic_processing.py) - Basic document processing workflow
- [config_operations.py](examples/config_operations.py) - Configuration management with versioning
- [manifest_operations.py](examples/manifest_operations.py) - Manifest generation and validation
- [workflow_control.py](examples/workflow_control.py) - Batch monitoring and reprocessing
- [lambda_function.py](examples/lambda_function.py) - Lambda function integration example
