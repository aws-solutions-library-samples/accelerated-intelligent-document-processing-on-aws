# IDP SDK

Python SDK for programmatic access to IDP Accelerator capabilities.

## Installation

```bash
# From local development
pip install -e ./lib/idp_sdk

# Or with uv
uv pip install -e ./lib/idp_sdk
```

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
