# IDP SDK Examples

This directory contains example scripts demonstrating how to use the IDP SDK.

## Prerequisites

1. Install the IDP SDK in development mode:
   ```bash
   cd lib/idp_sdk
   pip install -e .
   ```

2. Configure AWS credentials with access to your IDP stack.

## Available Examples

### 1. Basic Processing (`basic_processing.py`)

**Requires: Deployed IDP stack**

Demonstrates the most common workflow: submit documents, monitor progress, download results.

```bash
# Process local directory
python basic_processing.py \
    --stack-name idp-stack-01 \
    --directory ./samples \
    --output-dir /tmp/results

# Limit files for testing
python basic_processing.py \
    --stack-name idp-stack-01 \
    --directory ./samples \
    --output-dir /tmp/results \
    --number-of-files 5
```

### 2. Manifest Operations (`manifest_operations.py`)

**Does NOT require a deployed stack**

Demonstrates manifest generation and validation.

```bash
# Generate manifest from directory
python manifest_operations.py --directory ./samples --output manifest.csv

# Generate with baselines for evaluation
python manifest_operations.py \
    --directory ./samples \
    --baseline-dir ./baselines \
    --output manifest.csv

# Validate existing manifest
python manifest_operations.py --validate-only ./manifest.csv
```

### 3. Configuration Operations (`config_operations.py`)

**Create/Validate: No stack required | Download/Upload: Requires stack**

Demonstrates configuration creation, validation, download, and upload.

```bash
# Create minimal configuration template
python config_operations.py create --features min --pattern pattern-2

# Create config with all features and save to file
python config_operations.py create --features all --output my-config.yaml

# Validate a configuration file
python config_operations.py validate my-config.yaml --pattern pattern-2

# Download config from deployed stack (minimal diff format)
python config_operations.py download --stack-name idp-stack-01 --format minimal --output current-config.yaml

# Download a specific version
python config_operations.py download --stack-name idp-stack-01 --config-profile v2 --output v2-config.yaml

# Upload config to deployed stack (creates version if it doesn't exist)
python config_operations.py upload my-config.yaml --stack-name idp-stack-01 --config-profile default

# Upload as a new named version with description
python config_operations.py upload my-config.yaml \
    --stack-name idp-stack-01 \
    --config-profile v2 \
    --description "Updated extraction rules"
```

### 4. Workflow Control (`workflow_control.py`)

**Requires: Deployed IDP stack**

Demonstrates workflow management: listing batches, getting status, reprocessing documents, stopping workflows.

```bash
# List recent batches
python workflow_control.py --stack-name idp-stack-01 list --limit 10

# Get batch status
python workflow_control.py --stack-name idp-stack-01 status --batch-id my-batch-123

# Get single document status
python workflow_control.py --stack-name idp-stack-01 status --document-id "batch/doc.pdf"

# Reprocess a batch from extraction step
python workflow_control.py --stack-name idp-stack-01 rerun --batch-id my-batch-123 --step extraction

# Reprocess specific documents
python workflow_control.py --stack-name idp-stack-01 rerun \
    --document-ids "batch/doc1.pdf" "batch/doc2.pdf" \
    --step classification

# Stop all running workflows
python workflow_control.py --stack-name idp-stack-01 stop

# Stop without purging the SQS queue
python workflow_control.py --stack-name idp-stack-01 stop --skip-purge

# Show stack resources
python workflow_control.py --stack-name idp-stack-01 resources
```

### 5. Lambda Function (`lambda_function.py`)

Example Lambda function that uses the SDK for document processing automation. See
`deploy_lambda_example.py` for a script that packages, deploys, and tests the function.

See `lambda_function.py` for full deployment instructions and IAM requirements.

## SDK Quick Reference

```python
from idp_sdk import IDPClient

# Create client with default stack
client = IDPClient(stack_name="my-stack", region="us-west-2")

# Or create client and specify stack per-operation
client = IDPClient()

# Batch operations (require stack)
result = client.batch.process(source="./documents/")
status = client.batch.get_status(batch_id=result.batch_id)
client.batch.download_results(batch_id=result.batch_id, output_dir="./results")

# Config operations (no stack required for create/validate)
config = client.config.create(features="min")
validation = client.config.validate(config_file="my-config.yaml")

# Manifest operations (no stack required)
manifest = client.manifest.generate(directory="./docs/")
```

## Common Patterns

### Wait for Processing to Complete

```python
import time
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-stack")
result = client.batch.process(source="./documents/")

# Poll until complete
while True:
    status = client.batch.get_status(batch_id=result.batch_id)
    print(f"Progress: {status.completed}/{status.total}")

    if status.all_complete:
        print(f"Done! Success rate: {status.success_rate:.1%}")
        break

    time.sleep(10)

# Download results
client.batch.download_results(batch_id=result.batch_id, output_dir="./results")
```

### Process with Custom Configuration

```python
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-stack")

# Upload custom config to a named version
client.config.upload(config_file="my-config.yaml", config_version="v2")

# Activate the version so the pipeline uses it
client.config.activate("v2")

# Then process documents (they will use the activated config)
result = client.batch.process(directory="./documents/")
```

### Error Handling

```python
from idp_sdk import (
    IDPClient,
    IDPConfigurationError,
    IDPProcessingError,
    IDPStackError,
    IDPResourceNotFoundError,
)

client = IDPClient(stack_name="my-stack")

try:
    result = client.batch.process(source="./documents/")
except IDPConfigurationError as e:
    print(f"Configuration error: {e}")
except IDPProcessingError as e:
    print(f"Processing error: {e}")
except IDPStackError as e:
    print(f"Stack error: {e}")
except IDPResourceNotFoundError as e:
    print(f"Resource not found: {e}")
```

## Available Methods

| Method | Requires Stack | Description |
|--------|----------------|-------------|
| **Batch** | | |
| `batch.process()` | Yes | Submit documents for processing (directory, manifest, S3, or test set) |
| `batch.get_status()` | Yes | Get processing status for all documents in a batch |
| `batch.get_document_ids()` | Yes | Get all document IDs in a batch |
| `batch.get_results()` | Yes | Get extracted metadata for all documents in a batch (paginated) |
| `batch.get_confidence()` | Yes | Get confidence scores for all documents in a batch (paginated) |
| `batch.list()` | Yes | List recent batch jobs |
| `batch.download_results()` | Yes | Download processing results from OutputBucket |
| `batch.download_sources()` | Yes | Download original source files from InputBucket |
| `batch.reprocess()` | Yes | Reprocess documents from a pipeline step |
| `batch.delete_documents()` | Yes | Permanently delete all documents in a batch |
| `batch.stop_workflows()` | Yes | Stop all running Step Functions and purge SQS queue |
| **Document** | | |
| `document.process()` | Yes | Upload and queue a single document |
| `document.get_status()` | Yes | Get processing status for a single document |
| `document.get_metadata()` | Yes | Get extracted fields and metadata for a document |
| `document.list()` | Yes | List processed documents with pagination |
| `document.download_results()` | Yes | Download processing results for a single document |
| `document.download_source()` | Yes | Download original source file for a document |
| `document.reprocess()` | Yes | Reprocess a single document from a pipeline step |
| `document.delete()` | Yes | Delete a document and all associated data |
| **Stack** | | |
| `stack.deploy()` | No* | Deploy or update a CloudFormation stack |
| `stack.delete()` | Yes | Delete a CloudFormation stack |
| `stack.get_resources()` | Yes | Get stack resource details (buckets, queues, etc.) |
| `stack.exists()` | Yes | Check whether a stack exists |
| `stack.get_status()` | Yes | Get current CloudFormation stack status |
| `stack.check_in_progress()` | Yes | Check if a stack operation is in progress |
| `stack.monitor()` | Yes | Monitor a stack operation until terminal state |
| `stack.cancel_update()` | Yes | Cancel an in-progress stack update |
| `stack.wait_for_stable_state()` | Yes | Wait for stack to reach a stable state |
| `stack.get_failure_analysis()` | Yes | Analyze a CloudFormation deployment failure |
| `stack.cleanup_orphaned()` | No | Remove orphaned resources from deleted stacks |
| `stack.get_bucket_info()` | Yes | Get S3 bucket sizes and object counts |
| **Configuration** | | |
| `config.create()` | No | Generate a configuration template |
| `config.validate()` | No | Validate a configuration file |
| `config.download()` | Yes | Download active (or versioned) configuration |
| `config.upload()` | Yes | Upload configuration to a version |
| `config.list()` | Yes | List all configuration versions |
| `config.activate()` | Yes | Activate a configuration version |
| `config.delete()` | Yes | Delete a configuration version |
| **Discovery** | | |
| `discovery.run()` | No† | Analyze a document and generate a JSON Schema |
| `discovery.run_batch()` | No† | Run discovery on multiple documents |
| **Evaluation** | | |
| `evaluation.create_baseline()` | Yes | Create evaluation baseline for a document |
| `evaluation.get_report()` | Yes | Get accuracy report comparing results to baseline |
| `evaluation.get_metrics()` | Yes | Get aggregated evaluation metrics |
| `evaluation.list_baselines()` | Yes | List evaluation baselines with pagination |
| `evaluation.delete_baseline()` | Yes | Delete an evaluation baseline |
| **Assessment** | | |
| `assessment.get_confidence()` | Yes | Get per-field confidence scores |
| `assessment.get_geometry()` | Yes | Get bounding box coordinates for extracted fields |
| `assessment.get_metrics()` | Yes | Get aggregated quality metrics |
| **Search** | | |
| `search.query()` | Yes | Query knowledge base with natural language |
| **Manifest** | | |
| `manifest.generate()` | No | Generate a manifest from a directory or S3 URI |
| `manifest.validate()` | No | Validate a manifest file |
| **Testing** | | |
| `testing.load_test()` | Yes | Run a load test against the pipeline |

\* `stack.deploy()` does not require an existing stack (creates new one).  
† `discovery.run()` and `discovery.run_batch()` operate in **local mode** (no stack, uses Bedrock directly) when no `stack_name` is set, or in **stack-connected mode** (saves schema to config) when a stack is available.
