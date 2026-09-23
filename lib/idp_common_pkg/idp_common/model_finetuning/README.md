# Model Fine-tuning Service

This module provides the `ModelFinetuningService` class for fine-tuning language models using Amazon Bedrock and creating provisioned throughput for the fine-tuned models.

## ModelFinetuningService Class

The `ModelFinetuningService` class is the core service for managing Nova model fine-tuning workflows programmatically.

### Initialization

```python
from idp_common.model_finetuning import ModelFinetuningService

# Initialize with default settings
service = ModelFinetuningService(region="us-east-1")

# Initialize with custom configuration
config = {
    "model_finetuning": {
        "base_models": {
            "nova_lite": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",
            "nova_micro": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0:128k"
        },
        "hyperparameters": {
            "default": {
                "epochCount": "2",
                "learningRate": "0.00001",
                "batchSize": "1"
            }
        }
    }
}
service = ModelFinetuningService(region="us-east-1", config=config)
```

### Constructor Parameters

- `region` (str, optional): AWS region for Bedrock service. Defaults to value from config or `AWS_REGION` environment variable.
- `config` (Dict[str, Any], optional): Configuration dictionary containing model and hyperparameter settings.

## Fine-tuning Job Management

### create_finetuning_job()

Creates a fine-tuning job for a model.

```python
from idp_common.model_finetuning import FinetuningJobConfig

# Create job configuration
job_config = FinetuningJobConfig(
    base_model="arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",
    training_data_uri="s3://bucket/training.jsonl",
    validation_data_uri="s3://bucket/validation.jsonl",  # Optional
    output_uri="s3://bucket/output/",
    role_arn="arn:aws:iam::123456789012:role/BedrockFinetuningRole",
    job_name="my-finetuning-job",
    model_name="my-finetuned-model",
    hyperparameters={
        "epochCount": "2",
        "learningRate": "0.00001",
        "batchSize": "1"
    },
    validation_split=0.2,  # Used if validation_data_uri not provided
    model_type="nova"
)

# Create the job
result = service.create_finetuning_job(job_config)
print(f"Job ARN: {result.job_arn}")
```

**Parameters:**
- `config` (Union[FinetuningJobConfig, Dict[str, Any]]): Job configuration object or dictionary

**Returns:**
- `FinetuningJobResult`: Object containing job details including ARN, name, and status

### get_job_status()

Retrieves the current status of a fine-tuning job.

```python
status = service.get_job_status(job_arn, model_type="nova")
print(f"Status: {status.status.value}")
print(f"Model ID: {status.model_id}")
```

**Parameters:**
- `job_identifier` (str): Job ARN or job name
- `model_type` (str): Type of model being fine-tuned (default: "nova")

**Returns:**
- `FinetuningJobResult`: Job result with current status

### wait_for_job_completion()

Waits for a fine-tuning job to complete with polling.

```python
final_status = service.wait_for_job_completion(
    job_arn,
    model_type="nova",
    polling_interval=60,  # Check every 60 seconds
    max_wait_time=3600   # Maximum 1 hour
)
print(f"Final status: {final_status.status.value}")
```

**Parameters:**
- `job_identifier` (str): Job ARN or job name
- `model_type` (str): Type of model being fine-tuned (default: "nova")
- `polling_interval` (int): Time in seconds between status checks (default: 60)
- `max_wait_time` (Optional[int]): Maximum time to wait in seconds

**Returns:**
- `FinetuningJobResult`: Final job status

## Provisioned Throughput Management

### create_provisioned_throughput()

Creates provisioned throughput for a fine-tuned model.

```python
from idp_common.model_finetuning import ProvisionedThroughputConfig

# Create provisioned throughput configuration
throughput_config = ProvisionedThroughputConfig(
    model_id="arn:aws:bedrock:us-east-1:123456789012:custom-model/...",
    provisioned_model_name="my-provisioned-model",
    model_units=1,
    model_type="nova"
)

# Create provisioned throughput
result = service.create_provisioned_throughput(throughput_config)
print(f"Provisioned Model ARN: {result.provisioned_model_arn}")
```

**Parameters:**
- `config` (Union[ProvisionedThroughputConfig, Dict[str, Any]]): Provisioned throughput configuration

**Returns:**
- `ProvisionedThroughputResult`: Result with provisioned model details

### get_provisioned_throughput_status()

Gets the status of provisioned throughput.

```python
status = service.get_provisioned_throughput_status(provisioned_model_id, model_type="nova")
print(f"Status: {status.status}")
```

**Parameters:**
- `provisioned_model_id` (str): Provisioned model ID
- `model_type` (str): Type of model (default: "nova")

**Returns:**
- `ProvisionedThroughputResult`: Current provisioning status

### wait_for_provisioning_completion()

Waits for provisioned throughput to be ready.

```python
final_status = service.wait_for_provisioning_completion(
    provisioned_model_id,
    model_type="nova",
    polling_interval=60,
    max_wait_time=1800
)
```

**Parameters:**
- `provisioned_model_id` (str): Provisioned model ID
- `model_type` (str): Type of model (default: "nova")
- `polling_interval` (int): Time in seconds between status checks (default: 60)
- `max_wait_time` (Optional[int]): Maximum time to wait in seconds

**Returns:**
- `ProvisionedThroughputResult`: Final provisioning status

### delete_provisioned_throughput()

Deletes provisioned throughput to avoid ongoing costs.

```python
response = service.delete_provisioned_throughput(provisioned_model_id, model_type="nova")
```

**Parameters:**
- `provisioned_model_id` (str): Provisioned model ID
- `model_type` (str): Type of model (default: "nova")

**Returns:**
- `Dict[str, Any]`: Response from delete operation

## Configuration Options

### Model Configuration

```python
config = {
    "model_finetuning": {
        "base_models": {
            "nova_lite": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",
            "nova_micro": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0:128k"
        },
        "hyperparameters": {
            "default": {
                "epochCount": "2",
                "learningRate": "0.00001", 
                "batchSize": "1"
            },
            "custom": {
                "epochCount": "3",
                "learningRate": "0.0001",
                "batchSize": "1"
            }
        }
    }
}
```

### Hyperparameter Validation

The service automatically validates hyperparameters for Nova models:

- **epochCount**: Must be between 1 and 5
- **learningRate**: Must be between 1e-6 and 1e-4
- **batchSize**: Must be >= 1

## Error Handling

The service provides comprehensive error handling:

### Validation Errors

```python
try:
    result = service.create_finetuning_job(job_config)
except ValueError as e:
    print(f"Configuration error: {e}")
```

Common validation errors:
- Missing required parameters (`base_model`, `role_arn`, `training_data_uri`)
- Invalid hyperparameter ranges
- Malformed S3 URIs

### AWS Service Errors

```python
from botocore.exceptions import ClientError

try:
    result = service.create_finetuning_job(job_config)
except ClientError as e:
    error_code = e.response['Error']['Code']
    if error_code == 'ValidationException':
        print("Invalid request parameters")
    elif error_code == 'ResourceNotFoundException':
        print("Specified resource not found")
    elif error_code == 'ThrottlingException':
        print("Request was throttled")
```

## Supported Model Types

Currently supported model types:

- **`nova`**: Amazon Nova models (Nova Lite, Nova Micro)

The service is designed to be extensible for additional model types:

```python
# Extension point for new model types
def _create_custom_job_params(self, config, data_uris):
    """Create job parameters for custom model type."""
    # Implementation for new model type
    pass
```

## Data Processing Features

### Automatic Data Splitting

When `validation_data_uri` is not provided, the service automatically:

1. Downloads training data from S3
2. Shuffles the data with a fixed seed (42) for reproducibility
3. Splits data based on `validation_split` ratio
4. Uploads the split validation data back to S3
5. Updates training data with the remaining samples

### S3 URI Parsing

The service includes utility methods for S3 operations:

```python
# Internal method for S3 URI parsing
bucket, key = service._parse_s3_uri("s3://my-bucket/path/to/file.jsonl")
```

## Example Usage Patterns

### Basic Fine-tuning Workflow

```python
from idp_common.model_finetuning import (
    ModelFinetuningService, 
    FinetuningJobConfig,
    ProvisionedThroughputConfig
)

# Initialize service
service = ModelFinetuningService(region="us-east-1")

# Create fine-tuning job
job_config = FinetuningJobConfig(
    base_model="arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0:300k",
    training_data_uri="s3://my-bucket/train.jsonl",
    output_uri="s3://my-bucket/output/",
    role_arn="arn:aws:iam::123456789012:role/BedrockRole",
    job_name="document-classifier",
    model_type="nova"
)

# Create and monitor job
job_result = service.create_finetuning_job(job_config)
final_result = service.wait_for_job_completion(job_result.job_arn)

# Create provisioned throughput
if final_result.model_id:
    throughput_config = ProvisionedThroughputConfig(
        model_id=final_result.model_id,
        provisioned_model_name="my-classifier-provisioned",
        model_units=1,
        model_type="nova"
    )
    
    throughput_result = service.create_provisioned_throughput(throughput_config)
    service.wait_for_provisioning_completion(throughput_result.provisioned_model_arn)
```

### Configuration-Driven Workflow

```python
# Load configuration from external source
import yaml

with open('finetuning_config.yaml', 'r') as f:
    config = yaml.safe_load(f)

service = ModelFinetuningService(config=config)

# Use default hyperparameters from config
job_config = FinetuningJobConfig(
    base_model=config['model_finetuning']['base_models']['nova_lite'],
    training_data_uri="s3://my-bucket/train.jsonl",
    # hyperparameters will use defaults from config
    model_type="nova"
)
```

## CLI Usage Examples

The following scripts provide command-line interfaces for end-to-end Nova
fine-tuning workflows. They live in `scripts/model_finetuning/` and every
command below is written to run from the repository root:

### Dataset Preparation

**Basic dataset preparation:**
```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-finetuning-bucket \
    --samples-per-label 100
```

**With custom dataset and prompts:**
```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-bucket \
    --directory rvl-cdip-sampled \
    --samples-per-label 100 \
    --dataset chainyo/rvl-cdip \
    --system-prompt-file custom_system.txt \
    --task-prompt-file custom_task.txt
```

### Fine-tuning Job Creation

**Create job with automatic IAM role creation:**
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name my-finetuning-job \
    --create-role
```

**Create job with custom hyperparameters:**
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --validation-data-uri s3://my-bucket/data/validation.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name custom-job \
    --create-role \
    --epoch-count 3 \
    --learning-rate 0.0001 \
    --batch-size 1
```

**Monitor job status:**
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --status-only \
    --job-arn arn:aws:bedrock:us-east-1:123456789012:model-customization-job/job-id
```

### Provisioned Throughput Management

**Create provisioned throughput from job details:**
```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --job-details-file finetuning_job_20241201_120000.json \
    --provisioned-model-name my-provisioned-model \
    --model-units 1
```

**Create from model ID:**
```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --model-id arn:aws:bedrock:us-east-1:123456789012:custom-model/... \
    --provisioned-model-name my-provisioned-model \
    --model-units 2
```

**List all provisioned models:**
```bash
python scripts/model_finetuning/create_provisioned_throughput.py --list-models
```

**Delete provisioned throughput:**
```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --delete \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
```

### Model Inference and Evaluation

**Single image inference with base model:**
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png
```

**Batch inference with fine-tuned model:**
```bash
python scripts/model_finetuning/inference_example.py \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \
    --image-directory /path/to/images/ \
    --output-file results.json
```

**Inference with custom parameters:**
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png \
    --temperature 0.1 \
    --top-k 10 \
    --max-tokens 500 \
    --verbose
```

**Model comparison with ground truth evaluation:**
```bash
python scripts/model_finetuning/inference_example.py \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \
    --image-directory /path/to/images/ \
    --compare-with-base \
    --ground-truth-file labels.json \
    --output-file comparison.json
```

### Complete Workflow Example

```bash
# 1. Prepare dataset
python scripts/model_finetuning/prepare_nova_finetuning_data.py --bucket-name my-bucket --samples-per-label 100

# 2. Create fine-tuning job  
python scripts/model_finetuning/create_finetuning_job.py --training-data-uri s3://my-bucket/train.jsonl --job-name my-job --create-role

# 3. Create provisioned throughput
python scripts/model_finetuning/create_provisioned_throughput.py --job-details-file job.json --provisioned-model-name my-model --model-units 1

# 4. Run inference
python scripts/model_finetuning/inference_example.py --provisioned-model-arn <arn> --image-directory /path/to/images --output-file results.json

# 5. Clean up
python scripts/model_finetuning/create_provisioned_throughput.py --delete --provisioned-model-arn <arn>
```

## Requirements

- AWS credentials with appropriate permissions for Amazon Bedrock and S3
- IAM role with permissions for Amazon Bedrock fine-tuning
- Training data in JSONL format according to Bedrock conversation schema
- S3 buckets for input and output data

## Related Resources

For end-to-end workflows, dataset preparation, and comprehensive examples, see:

- **[Nova Fine-tuning Documentation](../../../../docs/nova-finetuning.md)**: Complete guide with CLI scripts and workflows
- **Python Scripts**:
  - `scripts/model_finetuning/prepare_nova_finetuning_data.py`: Dataset preparation
  - `scripts/model_finetuning/create_finetuning_job.py`: Job creation and monitoring
  - `scripts/model_finetuning/create_provisioned_throughput.py`: Provisioned throughput management
  - `scripts/model_finetuning/inference_example.py`: Model inference and evaluation
- **Notebooks**:
  - [Dataset Preparation Notebook](../../../../notebooks/misc/finetuning_dataset_prep.ipynb)
  - [Fine-tuning Service Demo Notebook](../../../../notebooks/misc/finetuning_model_service_demo.ipynb)
  - [Model Evaluation Notebook](../../../../notebooks/misc/finetuning_model_document_classification_evaluation.ipynb)
