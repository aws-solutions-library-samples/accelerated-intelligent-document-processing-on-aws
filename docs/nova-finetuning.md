---
title: "Fine-Tuning and Deploying Amazon Nova Models"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Fine-Tuning and Deploying Amazon Nova Models

This guide provides comprehensive step-by-step instructions for fine-tuning Amazon Nova models using Amazon Bedrock, creating provisioned throughput, and running inference for document classification tasks.

## Table of Contents

- [Prerequisites](#prerequisites)
- [1. Prepare Nova Fine-tuning Dataset](#1-prepare-nova-fine-tuning-dataset)
- [2. Create Fine-tuning Jobs](#2-create-fine-tuning-jobs)
- [3. Create Provisioned Throughput](#3-create-provisioned-throughput)
- [4. Run Inference](#4-run-inference)
- [5. Evaluation and Performance Analysis](#5-evaluation-and-performance-analysis)
- [6. Cost Management](#6-cost-management)
- [7. Best Practices](#7-best-practices)
- [8. Troubleshooting](#8-troubleshooting)
- [9. References](#9-references)

## Prerequisites

> **Region Availability**: Nova fine-tuning is only available in the `us-east-1` AWS region. Ensure your IDP Accelerator stack is deployed to `us-east-1` to use this feature. The Custom Models UI is automatically hidden in deployments to other regions.

### AWS Setup
Set up AWS CLI and credentials:
```bash
aws configure
```

### Required Permissions
Your AWS account needs permissions for:
- Amazon Bedrock (fine-tuning and inference)
- Amazon S3 (data storage and access)
- AWS IAM (role creation and management)

### Required Python Packages
Install the required packages:
```bash
pip install boto3 pillow python-dotenv datasets tqdm
```

`create_finetuning_job.py` and `create_provisioned_throughput.py` also import
`idp_common.model_finetuning`, so install the library from the local checkout as
well (never by bare name — see
[dependency-confusion.md](dependency-confusion.md)):
```bash
pip install -e "lib/idp_common_pkg[all]"
```

### Where the scripts live
Every command on this page is written to run from the repository root; the four
scripts are in `scripts/model_finetuning/`.

### Supported Model Types
Currently supports Amazon Nova models:
- Nova Lite (`amazon.nova-lite-v1:0`)
- Nova Pro (`amazon.nova-pro-v1:0`)

## 1. Prepare Nova Fine-tuning Dataset

### 1.1. Dataset Requirements

Your dataset should be prepared in the Bedrock fine-tuning format. Each training example should be a JSON object with the following structure:

```json
{
  "schemaVersion": "bedrock-conversation-2024",
  "system": [{
    "text": "You are a document classification expert who can analyze and identify document types from images..."
  }],
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "Task prompt with document type definitions..."
        },
        {
          "image": {
            "format": "png",
            "source": {
              "s3Location": {
                "uri": "s3://bucket-name/path/to/image.png",
                "bucketOwner": "123456789012"
              }
            }
          }
        }
      ]
    },
    {
      "role": "assistant",
      "content": [{
        "text": "{\"type\": \"invoice\"}"
      }]
    }
  ]
}
```

### 1.2. Prepare Dataset Using RVL-CDIP

Use the provided script to prepare a dataset from the RVL-CDIP document classification dataset:

```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-finetuning-bucket \
    --directory rvl-cdip-sampled \
    --samples-per-label 100 \
    --dataset chainyo/rvl-cdip \
    --split train
```

#### Parameters:
- `--bucket-name`: S3 bucket name for storing prepared data (required)
- `--directory`: S3 directory prefix (default: nova-finetuning-data)
- `--samples-per-label`: Number of samples per document class (default: 100)
- `--dataset`: Hugging Face dataset name (default: chainyo/rvl-cdip)
- `--validation-split`: Validation split ratio (default: 0.1)

#### Examples:

**Basic dataset preparation:**
```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-bucket \
    --samples-per-label 50
```

**Using a custom dataset:**
It should have the similar structure of [RVL-CDIP](https://huggingface.co/datasets/chainyo/rvl-cdip)
```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-bucket \
    --local-dataset /path/to/local/dataset \
    --samples-per-label 75
```

**With custom prompts:**
```bash
python scripts/model_finetuning/prepare_nova_finetuning_data.py \
    --bucket-name my-bucket \
    --samples-per-label 100 \
    --system-prompt-file custom_system.txt \
    --task-prompt-file custom_task.txt
```

### 1.3. Dataset Structure

After preparation, your S3 bucket will contain:

```
s3://my-finetuning-bucket/
├── rvl-cdip-sampled/
    ├── images/
    │   ├── advertissement_1_uuid.png
    │   ├── budget_1_uuid.png
    │   └── ...
    ├── train.jsonl          # Training data
    ├── validation.jsonl     # Validation data
    └── updated_dataset.json # Dataset metadata
```

### 1.4. Supported Document Classes

The default configuration supports 16 document classes from RVL-CDIP:

| Class | Description |
|-------|-------------|
| advertissement | Marketing or promotional material |
| budget | Financial documents with numerical data |
| email | Electronic correspondence |
| file_folder | Document organization structures |
| form | Structured documents with fields |
| handwritten | Documents with handwritten content |
| invoice | Billing documents |
| letter | Formal correspondence |
| memo | Internal business communications |
| news_article | Journalistic content |
| presentation | Slide-based documents |
| questionnaire | Survey forms |
| resume | Employment documents |
| scientific_publication | Academic papers |
| scientific_report | Technical research documents |
| specification | Technical requirement documents |

## 2. Create Fine-tuning Jobs

### 2.1. IAM Role Setup

Before creating fine-tuning jobs, set up the required IAM role:

```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name my-finetuning-job \
    --create-role
```

This automatically creates an IAM role with necessary permissions for Bedrock fine-tuning and S3 access.

### 2.2. Create Fine-tuning Job with Separate Validation Data

```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --validation-data-uri s3://my-bucket/data/validation.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name my-finetuning-job \
    --model-name my-finetuned-model \
    --role-arn arn:aws:iam::123456789012:role/BedrockFinetuningRole
```

### 2.3. Create Fine-tuning Job with Automatic Data Splitting

```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name my-auto-split-job \
    --validation-split 0.2 \
    --create-role
```

### 2.4. Custom Hyperparameters

```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name custom-job \
    --create-role \
    --epoch-count 3 \
    --learning-rate 0.0001 \
    --batch-size 1
```

#### Hyperparameter Guidelines:
- **Epoch Count**: 1-5 (default: 2)
- **Learning Rate**: 1e-6 to 1e-4 (default: 0.00001)
- **Batch Size**: Typically 1 for Nova models

### 2.5. Monitor Job Progress

Check job status:
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --status-only \
    --job-arn arn:aws:bedrock:us-east-1:123456789012:model-customization-job/job-id
```

Wait for completion with monitoring:
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --training-data-uri s3://my-bucket/data/train.jsonl \
    --output-uri s3://my-bucket/output/ \
    --job-name monitored-job \
    --create-role \
    --polling-interval 60 \
    --max-wait-time 3600
```

### 2.6. Job Results Location

Fine-tuning results are stored at:
```
s3://<output-bucket>/<job-name>/
├── training_artifacts/
│   └── step_wise_training_metrics.csv
├── validation_artifacts/
│   └── post_fine_tuning_validation/
└── model_artifacts/
```

Job details are saved locally as JSON:
```json
{
  "job_arn": "arn:aws:bedrock:us-east-1:123456789012:model-customization-job/...",
  "job_name": "my-finetuning-job",
  "status": "Completed",
  "model_id": "arn:aws:bedrock:us-east-1:123456789012:custom-model/...",
  "creation_time": "2024-01-01T12:00:00Z",
  "end_time": "2024-01-01T13:30:00Z"
}
```

## 3. Create Provisioned Throughput

### 3.1. Create Provisioned Throughput from Job Details

```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --job-details-file finetuning_job_20241201_120000.json \
    --provisioned-model-name my-provisioned-model \
    --model-units 1
```

### 3.2. Create Provisioned Throughput from Model ID

```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --model-id arn:aws:bedrock:us-east-1:123456789012:custom-model/... \
    --provisioned-model-name my-provisioned-model \
    --model-units 2
```

### 3.3. Create Provisioned Throughput from Job ARN

```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --job-arn arn:aws:bedrock:us-east-1:123456789012:model-customization-job/... \
    --provisioned-model-name my-provisioned-model \
    --model-units 1
```

### 3.4. Monitor Provisioning Status

Check provisioning status:
```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --status-only \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
```

### 3.5. List All Provisioned Models

```bash
python scripts/model_finetuning/create_provisioned_throughput.py --list-models
```

### 3.6. Model Units Guidelines

| Use Case | Recommended Units | Notes |
|----------|------------------|-------|
| Development/Testing | 1 | Sufficient for low-volume testing |
| Production (Low) | 1-2 | Up to 100 requests/minute |
| Production (Medium) | 3-5 | Up to 500 requests/minute |
| Production (High) | 5+ | 1000+ requests/minute |

## 4. Run Inference

### 4.1. Single Image Inference

**With base model:**
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png
```

**With fine-tuned provisioned model:**
```bash
python scripts/model_finetuning/inference_example.py \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \
    --image-path document.png
```

### 4.2. Batch Inference

Process multiple images:
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-directory /path/to/images/ \
    --output-file results.json
```

### 4.3. Inference with Ground Truth

Evaluate accuracy with known labels:
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-directory /path/to/images/ \
    --ground-truth-file labels.json \
    --output-file results_with_accuracy.json
```

Ground truth file format (`labels.json`):
```json
{
  "/path/to/image1.png": "invoice",
  "/path/to/image2.png": "letter",
  "/path/to/image3.png": "form"
}
```

### 4.4. Model Comparison

Compare base model with fine-tuned model:
```bash
python scripts/model_finetuning/inference_example.py \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/... \
    --image-directory /path/to/images/ \
    --compare-with-base \
    --ground-truth-file labels.json \
    --output-file comparison.json
```

### 4.5. Custom Prompts

Use custom system and task prompts:
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png \
    --system-prompt-file custom_system.txt \
    --task-prompt-file custom_task.txt
```

### 4.6. Inference Parameters

Fine-tune inference behavior:
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png \
    --temperature 0.1 \
    --top-k 10 \
    --max-tokens 500 \
    --verbose
```

## 5. Evaluation and Performance Analysis

### 5.1. Automated Evaluation

The inference script automatically calculates performance metrics when ground truth is provided:

```
Model Results Summary:
=
Total Images: 100
Successful Inferences: 98
Success Rate: 98.00%
Correct Predictions: 85
Accuracy: 85.00%
Average Inference Time: 2.34s
Total Tokens Used: 12,500
Average Tokens per Image: 125.0
```

### 5.2. Detailed Results Analysis

Results are saved in JSON format with detailed metrics:

```json
{
  "model_id": "arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...",
  "model_name": "Fine-tuned Model",
  "results": [
    {
      "image_path": "/path/to/image.png",
      "status": "success",
      "prediction": "invoice",
      "ground_truth": "invoice",
      "correct": true,
      "confidence": 1.0,
      "inference_time_seconds": 2.1,
      "input_tokens": 850,
      "output_tokens": 15,
      "total_tokens": 865
    }
  ],
  "metrics": {
    "total_images": 100,
    "successful_inferences": 98,
    "success_rate": 0.98,
    "correct_predictions": 85,
    "accuracy": 0.85,
    "average_inference_time_seconds": 2.34,
    "total_tokens_used": 12500,
    "average_tokens_per_image": 125.0
  }
}
```

### 5.3. Model Comparison Analysis

When comparing models, results include side-by-side metrics:

```json
{
  "comparison_type": "model_comparison",
  "models": {
    "Fine-tuned Model": {
      "model_id": "arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...",
      "metrics": {
        "accuracy": 0.87,
        "average_inference_time_seconds": 2.1
      }
    },
    "Base Model": {
      "model_id": "us.amazon.nova-lite-v1:0",
      "metrics": {
        "accuracy": 0.72,
        "average_inference_time_seconds": 1.8
      }
    }
  }
}
```

## 6. Cost Management

### 6.1. Understanding Costs

Nova fine-tuning costs include:

1. **Fine-tuning Job Costs**: Based on training time and data size
2. **Provisioned Throughput Costs**: Hourly charges for reserved capacity
3. **Inference Costs**: Per-token charges for on-demand inference

### 6.2. Cost Optimization

**Data Preparation:**
- Start with smaller datasets (50-100 samples per class)
- Use efficient image formats (PNG recommended)
- Optimize hyperparameters to reduce training time

**Provisioned Throughput:**
- Start with 1 model unit for testing
- Scale based on actual usage patterns
- Delete provisioned throughput when not needed

**Inference:**
- Use efficient prompting to minimize token usage
- Batch process multiple images when possible
- Consider using base models for simple tasks

### 6.3. Delete Provisioned Throughput

To avoid ongoing costs, delete provisioned throughput when not needed:

```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --delete \
    --provisioned-model-arn arn:aws:bedrock:us-east-1:123456789012:provisioned-model/...
```

**⚠️ IMPORTANT**: Provisioned throughput incurs costs even when not in use. Always delete when no longer needed.

## 7. Best Practices

### 7.1. Dataset Preparation

- **Quality over Quantity**: 50-100 high-quality examples per class often outperform 1000+ poor examples
- **Balanced Classes**: Ensure roughly equal representation across document types
- **Image Quality**: Use clear, high-resolution images (300+ DPI for scanned documents)
- **Representative Examples**: Include diverse layouts and formats within each class

### 7.2. Fine-tuning Configuration

- **Start Simple**: Begin with default hyperparameters
- **Incremental Tuning**: Adjust one parameter at a time
- **Monitor Overfitting**: Use validation data to prevent overfitting
- **Early Stopping**: Stop training if validation metrics plateau

### 7.3. Production Deployment

- **Gradual Rollout**: Test with small traffic percentage before full deployment
- **Monitoring**: Set up CloudWatch alarms for inference errors and latency
- **Fallback Strategy**: Have base model as fallback for fine-tuned model failures
- **Cost Monitoring**: Track token usage and provisioned throughput costs

### 7.4. Performance Optimization

- **Image Preprocessing**: Resize images to optimal dimensions (typically 1024x1024 max)
- **Prompt Engineering**: Use concise, specific prompts
- **Batch Processing**: Process multiple documents together when possible
- **Caching**: Cache results for repeated classifications

## 8. Troubleshooting

### 8.1. Common Issues

**Fine-tuning Job Fails:**
- Check IAM role permissions for Bedrock and S3
- Verify training data format (JSONL with correct schema)
- Ensure S3 bucket accessibility from Bedrock service
- Check hyperparameter ranges (epochs: 1-5, learning rate: 1e-6 to 1e-4)

**Provisioned Throughput Creation Fails:**
- Ensure fine-tuning job completed successfully
- Verify model ID is correct (use job details file)
- Check account limits for provisioned throughput

**Inference Errors:**
- Verify model ID/ARN is correct and accessible
- Check image format and size (max 20MB)
- Ensure proper AWS credentials and region configuration
- Monitor CloudWatch logs for detailed error messages

**Low Accuracy:**
- Review training data quality and labeling consistency
- Increase dataset size or improve class balance
- Adjust hyperparameters (try lower learning rate)
- Verify prompt templates match training format

### 8.2. Debugging Tools

**Enable verbose logging:**
```bash
python scripts/model_finetuning/inference_example.py \
    --model-id us.amazon.nova-lite-v1:0 \
    --image-path document.png \
    --verbose
```

**Check job logs:**
```bash
python scripts/model_finetuning/create_finetuning_job.py \
    --status-only \
    --job-arn <job-arn>
```

**Monitor provisioning:**
```bash
python scripts/model_finetuning/create_provisioned_throughput.py \
    --status-only \
    --provisioned-model-arn <model-arn>
```

### 8.3. Performance Issues

**Slow Inference:**
- Check provisioned throughput status (should be "InService")
- Optimize image sizes and formats
- Consider using multiple model units for higher throughput

**High Costs:**
- Monitor token usage per inference
- Optimize prompts to reduce token count
- Delete unused provisioned throughput
- Use batch processing for multiple documents

## 9. References

### 9.1. Documentation Links

- [Amazon Bedrock Fine-tuning Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/custom-models.html)
- [Amazon Nova Documentation](https://docs.aws.amazon.com/nova/latest/userguide/customize-fine-tune.html)
- [AWS IAM Best Practices](https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html)

### 9.2. Related Resources

- **IDP Common Library**: `genaiic-idp-accelerator/lib/idp_common_pkg/`
- **Notebooks**: 
  - [Dataset Preparation](../notebooks/misc/finetuning_dataset_prep.ipynb)
  - [Fine-tuning Service Demo](../notebooks/misc/finetuning_model_service_demo.ipynb) 
  - [Model Evaluation](../notebooks/misc/finetuning_model_document_classification_evaluation.ipynb)
- **Python Scripts**:
  - `prepare_nova_finetuning_data.py`
  - `create_finetuning_job.py`
  - `create_provisioned_throughput.py`
  - `inference_example.py`

### 9.3. Dataset References

- **RVL-CDIP Dataset**: [Harley et al., 2015](https://www.cs.cmu.edu/~aharley/rvl-cdip/)
- **Hugging Face RVL-CDIP**: [chainyo/rvl-cdip](https://huggingface.co/datasets/chainyo/rvl-cdip)

### 9.4. Example Commands Summary

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

---

*This documentation provides comprehensive guidance for fine-tuning Amazon Nova models for document classification. For additional support, refer to the AWS documentation and support resources.*
