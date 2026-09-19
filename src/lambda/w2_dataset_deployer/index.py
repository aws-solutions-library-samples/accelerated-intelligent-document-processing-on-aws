"""
Lambda function to deploy the Fake W-2 Tax Form dataset from HuggingFace
to the TestSetBucket during stack deployment.

Source: https://huggingface.co/datasets/singhsays/fake-w2-us-tax-form-dataset
Original: https://www.kaggle.com/datasets/mcvishnu1/fake-w2-us-tax-form-dataset (CC0: Public Domain)

This deployer:
1. Downloads parquet files for all 3 splits (train, test, validation) from HuggingFace
2. Extracts JPG images and ground truth from parquet data
3. Converts gt_parse ground truth to accelerator inference_result format
4. Uploads images and baselines to S3
"""

import json
import os
import logging
import boto3
from datetime import datetime
from typing import Dict, Any
import cfnresponse

# Set HuggingFace cache to /tmp (Lambda's writable directory)
os.environ['HF_HOME'] = '/tmp/huggingface'  # nosec B108 - isolated Lambda environment
os.environ['HUGGINGFACE_HUB_CACHE'] = '/tmp/huggingface/hub'  # nosec B108

# Lightweight HuggingFace access
from huggingface_hub import hf_hub_download
from log_sanitizer import sanitize_event_for_logging
import pyarrow.parquet as pq

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

# AWS clients
s3_client = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Environment variables
TESTSET_BUCKET = os.environ.get('TESTSET_BUCKET')
TRACKING_TABLE = os.environ.get('TRACKING_TABLE')

# Constants
DATASET_NAME = 'Fake-W2-Tax-Forms'
DATASET_PREFIX = 'fake-w2/'
TEST_SET_ID = 'fake-w2'
HF_REPO_ID = 'singhsays/fake-w2-us-tax-form-dataset'

# Parquet files for each split
PARQUET_FILES = {
    'train': 'data/train-00000-of-00001-26677581e561d5be.parquet',
    'test': 'data/test-00000-of-00001-d2b8d24cfd674b24.parquet',
    'validation': 'data/validation-00000-of-00001-dec92c2111026d5a.parquet',
}


def handler(event, context):
    """
    Main Lambda handler for deploying the Fake W-2 dataset.
    """
    logger.info(f"Event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        request_type = event['RequestType']

        if request_type == 'Delete':
            # On stack deletion, we leave the data in place
            logger.info("Delete request - keeping dataset in place")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

        # Extract properties
        properties = event['ResourceProperties']
        dataset_version = properties.get('DatasetVersion', '1.0')
        dataset_description = properties.get('DatasetDescription', '')

        logger.info(f"Processing dataset version: {dataset_version}")

        # Check if dataset already exists with this version
        if check_existing_version(dataset_version):
            logger.info(f"Dataset version {dataset_version} already deployed, updating description only")
            update_description_only(dataset_description)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': f'Dataset version {dataset_version} already exists, description updated'
            })
            return

        # Download and deploy the dataset
        result = deploy_dataset(dataset_version, dataset_description)

        logger.info(f"Dataset deployment completed: {result}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, result)

    except Exception as e:
        logger.error(f"Error deploying dataset: {str(e)}", exc_info=True)
        # Graceful degradation: don't fail the stack due to test set download issues.
        # Instead, create a FAILED test set record visible in the Test Studio UI.
        try:
            create_failed_testset_record(
                version=properties.get('DatasetVersion', '1.0') if 'properties' in dir() else '1.0',
                error_message=str(e)
            )
        except Exception as record_err:
            logger.error(f"Failed to create error test set record: {record_err}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'Status': 'DEPLOYMENT_FAILED',
            'Message': f'Test set deployment failed (non-blocking): {str(e)[:200]}'
        })


def update_description_only(description: str):
    """
    Update only the description field in the existing DynamoDB record.
    """
    try:
        table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
        table.update_item(
            Key={
                'PK': f'testset#{TEST_SET_ID}',
                'SK': 'metadata'
            },
            UpdateExpression='SET description = :desc',
            ExpressionAttributeValues={
                ':desc': description
            }
        )
        logger.info(f"Updated description for test set {TEST_SET_ID}")
    except Exception as e:
        logger.error(f"Failed to update description: {e}")
        raise


def check_existing_version(version: str) -> bool:
    """
    Check if the dataset with the specified version already exists.
    """
    try:
        table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
        response = table.get_item(
            Key={
                'PK': f'testset#{TEST_SET_ID}',
                'SK': 'metadata'
            }
        )

        if 'Item' in response:
            existing_version = response['Item'].get('datasetVersion', '')
            logger.info(f"Found existing dataset version: {existing_version}")

            # Check if version matches, status is not FAILED, and files exist
            if existing_version == version:
                existing_status = response['Item'].get('status', '')
                if existing_status == 'FAILED':
                    logger.info("Previous deployment failed, retrying deployment")
                    return False
                # Verify at least some files exist in S3
                try:
                    s3_response = s3_client.list_objects_v2(
                        Bucket=TESTSET_BUCKET,
                        Prefix=f'{DATASET_PREFIX}input/',
                        MaxKeys=1
                    )
                    if s3_response.get('KeyCount', 0) > 0:
                        logger.info("Files exist in S3, skipping deployment")
                        return True
                except Exception as e:
                    logger.warning(f"Error checking S3 files: {e}")

        return False

    except Exception as e:
        logger.warning(f"Error checking existing version: {e}")
        return False


def convert_gt_to_inference_result(gt_parse: dict) -> dict:
    """
    Convert the gt_parse ground truth format to a flat inference_result dict.
    The gt_parse fields are already flat key-value pairs matching W-2 box numbers,
    so we pass them through with string conversion for consistency.
    """
    result = {}
    for key, value in gt_parse.items():
        # Convert "None" strings to empty string, and numbers to strings
        if value == "None" or value is None:
            result[key] = ""
        elif isinstance(value, (int, float)):
            result[key] = str(value)
        else:
            result[key] = str(value)
    return result


def deploy_dataset(version: str, description: str) -> Dict[str, Any]:
    """
    Deploy the dataset by downloading parquet files from HuggingFace,
    extracting images and ground truth, and uploading to S3.
    """
    try:
        # Ensure cache directory exists in /tmp (Lambda's writable directory)
        cache_dir = '/tmp/huggingface/hub'  # nosec B108
        os.makedirs(cache_dir, exist_ok=True)
        logger.info(f"Using cache directory: {cache_dir}")

        file_count = 0
        skipped_count = 0
        split_counts = {}

        # Process each split
        for split_name, parquet_filename in PARQUET_FILES.items():
            logger.info(f"Processing split: {split_name} ({parquet_filename})")

            # Download the parquet file
            parquet_path = hf_hub_download(  # nosec B615 - trusted HuggingFace dataset
                repo_id=HF_REPO_ID,
                filename=parquet_filename,
                repo_type="dataset",
                cache_dir=cache_dir
            )

            logger.info(f"Downloaded parquet file for {split_name}")

            # Read parquet file
            table = pq.read_table(parquet_path)
            num_rows = table.num_rows
            logger.info(f"Split {split_name}: {num_rows} documents")

            split_file_count = 0

            for idx in range(num_rows):
                try:
                    # Extract image bytes
                    image_data = table.column('image')[idx].as_py()
                    if image_data is None:
                        logger.warning(f"Skipping {split_name}/{idx}: no image data")
                        skipped_count += 1
                        continue

                    # Handle image data - may be dict with 'bytes' key or raw bytes
                    if isinstance(image_data, dict):
                        image_bytes = image_data.get('bytes', None)
                    elif isinstance(image_data, bytes):
                        image_bytes = image_data
                    else:
                        logger.warning(f"Skipping {split_name}/{idx}: unexpected image type {type(image_data)}")
                        skipped_count += 1
                        continue

                    if not image_bytes:
                        logger.warning(f"Skipping {split_name}/{idx}: empty image bytes")
                        skipped_count += 1
                        continue

                    # Extract ground truth
                    gt_str = table.column('ground_truth')[idx].as_py()
                    if not gt_str:
                        logger.warning(f"Skipping {split_name}/{idx}: no ground truth")
                        skipped_count += 1
                        continue

                    gt_data = json.loads(gt_str)
                    gt_parse = gt_data.get('gt_parse', {})

                    if not gt_parse:
                        logger.warning(f"Skipping {split_name}/{idx}: empty gt_parse")
                        skipped_count += 1
                        continue

                    # Create document filename
                    doc_filename = f"w2_{split_name}_{idx:04d}.jpg"

                    # Upload image to input folder
                    input_key = f'{DATASET_PREFIX}input/{doc_filename}'
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=input_key,
                        Body=image_bytes,
                        ContentType='image/jpeg'
                    )

                    # Convert ground truth to inference_result format
                    inference_result = convert_gt_to_inference_result(gt_parse)

                    # Create baseline with document classification fields
                    result_json = {
                        "document_class": {
                            "type": "W2"
                        },
                        "split_document": {
                            "page_indices": [0]
                        },
                        "inference_result": inference_result
                    }

                    # Upload ground truth baseline
                    baseline_key = f'{DATASET_PREFIX}baseline/{doc_filename}/sections/1/result.json'
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=baseline_key,
                        Body=json.dumps(result_json, indent=2),
                        ContentType='application/json'
                    )

                    file_count += 1
                    split_file_count += 1

                    if file_count % 100 == 0:
                        logger.info(f"Processed {file_count} documents so far...")

                except Exception as e:
                    logger.error(f"Error processing {split_name}/{idx}: {e}")
                    skipped_count += 1
                    continue

            split_counts[split_name] = split_file_count
            logger.info(f"Split {split_name}: deployed {split_file_count} documents")

            # Clean up parquet file from /tmp to free space for next split
            try:
                os.remove(parquet_path)
            except Exception:
                pass

        # Log statistics
        logger.info(f"Successfully deployed {file_count} documents (skipped {skipped_count})")
        logger.info(f"Split counts: {split_counts}")

        # Create test set record in DynamoDB
        create_testset_record(version, description, file_count, split_counts)

        return {
            'DatasetVersion': version,
            'FileCount': file_count,
            'SkippedCount': skipped_count,
            'SplitCounts': split_counts,
            'Message': f'Successfully deployed {file_count} W-2 tax form documents'
        }

    except Exception as e:
        logger.error(f"Error deploying dataset: {e}", exc_info=True)
        raise


def create_failed_testset_record(version: str, error_message: str):
    """
    Create a FAILED test set record in DynamoDB so the error is visible in Test Studio UI.
    On the next stack update, check_existing_version will detect the FAILED status and retry.
    """
    table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
    timestamp = datetime.utcnow().isoformat() + 'Z'

    item = {
        'PK': f'testset#{TEST_SET_ID}',
        'SK': 'metadata',
        'ItemType': 'testset',
        'InitialEventTime': timestamp,
        'id': TEST_SET_ID,
        'name': DATASET_NAME,
        'filePattern': '',
        'fileCount': 0,
        'status': 'FAILED',
        'createdAt': timestamp,
        'datasetVersion': version,
        'source': f'huggingface:{HF_REPO_ID}',
        'description': (
            f'⚠️ Deployment failed: {error_message[:500]}. '
            f'This test set could not be downloaded from its source. '
            f'It will be retried on the next stack update.'
        ),
    }

    table.put_item(Item=item)
    logger.info(f"Created FAILED test set record in DynamoDB: {TEST_SET_ID}")


def create_testset_record(version: str, description: str, file_count: int,
                          split_counts: Dict[str, int]):
    """
    Create or update the test set record in DynamoDB.
    """
    table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
    timestamp = datetime.utcnow().isoformat() + 'Z'

    item = {
        'PK': f'testset#{TEST_SET_ID}',
        'SK': 'metadata',
        'ItemType': 'testset',
        'InitialEventTime': timestamp,
        'id': TEST_SET_ID,
        'name': DATASET_NAME,
        'filePattern': '',
        'fileCount': file_count,
        'status': 'COMPLETED',
        'createdAt': timestamp,
        'datasetVersion': version,
        'source': f'huggingface:{HF_REPO_ID}',
        'splitCounts': split_counts,
        'description': description or (
            'Fake W-2 Tax Form dataset - 2,000 synthetic US W-2 tax form images '
            'with 45-field structured ground truth for extraction evaluation. '
            'CC0: Public Domain license.'
        )
    }

    table.put_item(Item=item)
    logger.info(f"Created test set record in DynamoDB: {TEST_SET_ID}")
