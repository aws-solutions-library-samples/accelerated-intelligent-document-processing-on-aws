"""
Lambda function to deploy the OmniAI OCR Benchmark dataset from HuggingFace
to the TestSetBucket during stack deployment.

This deployer uses a pre-selected list of images from the top format/schema
combinations (those with >5 samples per format).

Dataset structure (getomni-ai/ocr-benchmark):
- test/metadata.jsonl: JSONL file with fields: id, metadata, json_schema, true_json_output, true_markdown_output
- test/images/{id}.{ext}: Individual image files (png, jpg, jpeg)
"""

import json
import os
import logging
import boto3
import re
from datetime import datetime
from typing import Dict, Any, List
from io import BytesIO
import cfnresponse

# Set HuggingFace cache to /tmp (Lambda's writable directory)
os.environ['HF_HOME'] = '/tmp/huggingface'  # nosec B108 - isolated Lambda environment
os.environ['HUGGINGFACE_HUB_CACHE'] = '/tmp/huggingface/hub'  # nosec B108

# Lightweight HuggingFace access
from huggingface_hub import hf_hub_download, list_repo_files
from log_sanitizer import sanitize_event_for_logging
from PIL import Image

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
DATASET_NAME = 'OmniAI-OCR-Benchmark'
DATASET_PREFIX = 'ocr-benchmark/'
TEST_SET_ID = 'ocr-benchmark'
HF_REPO_ID = 'getomni-ai/ocr-benchmark'

# Hardcoded image IDs for each format (293 images total)
# These are the top format/schema pairs with >5 samples each
HARDCODED_IMAGES: Dict[str, List[int]] = {
    "BANK_CHECK": [17, 102, 115, 143, 152, 159, 165, 201, 252, 275, 280, 285, 289, 292, 322, 355, 369, 377, 404, 408, 420, 442, 449, 503, 508, 544, 553, 558, 577, 606, 618, 623, 626, 648, 691, 706, 707, 751, 763, 805, 838, 862, 870, 872, 914, 924, 941, 959, 966, 979, 994, 999],
    "COMMERCIAL_LEASE_AGREEMENT": [65, 86, 109, 120, 129, 146, 166, 179, 183, 212, 224, 227, 236, 249, 279, 281, 290, 301, 304, 311, 321, 330, 338, 360, 364, 401, 429, 440, 496, 518, 536, 555, 557, 596, 602, 639, 680, 701, 754, 756, 775, 776, 786, 793, 816, 841, 876, 916, 942, 947, 978, 996],
    "CREDIT_CARD_STATEMENT": [117, 206, 340, 349, 352, 396, 410, 529, 797, 810, 894],
    "DELIVERY_NOTE": [147, 170, 284, 314, 316, 473, 568, 719],
    "EQUIPMENT_INSPECTION": [81, 95, 242, 296, 359, 458, 511, 645, 659, 854, 902],
    "GLOSSARY": [75, 78, 106, 125, 156, 185, 188, 226, 243, 246, 312, 325, 379, 393, 397, 427, 460, 480, 543, 546, 603, 692, 722, 740, 758, 766, 774, 806, 828, 920, 960],
    "PETITION_FORM": [72, 77, 90, 114, 123, 149, 154, 182, 200, 286, 294, 339, 348, 368, 382, 384, 389, 400, 406, 431, 472, 476, 488, 499, 520, 525, 526, 528, 532, 571, 575, 654, 665, 670, 682, 718, 732, 739, 771, 791, 849, 861, 875, 893, 919, 925, 933, 964, 977, 987, 998],
    "REAL_ESTATE": [32, 104, 110, 122, 126, 133, 135, 178, 196, 210, 215, 237, 250, 295, 306, 309, 318, 341, 350, 351, 357, 363, 376, 380, 413, 416, 448, 454, 481, 492, 530, 533, 559, 578, 589, 594, 599, 611, 644, 650, 662, 667, 668, 669, 689, 697, 702, 705, 755, 799, 803, 837, 846, 882, 895, 928, 930, 956, 972],
    "SHIFT_SCHEDULE": [23, 105, 218, 241, 262, 346, 438, 441, 501, 523, 597, 690, 769, 819, 865, 879, 913, 970],
}


def handler(event, context):
    """
    Main Lambda handler for deploying the OCR Benchmark dataset.
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
                    response = s3_client.list_objects_v2(
                        Bucket=TESTSET_BUCKET,
                        Prefix=f'{DATASET_PREFIX}input/',
                        MaxKeys=1
                    )
                    if response.get('KeyCount', 0) > 0:
                        logger.info("Files exist in S3, skipping deployment")
                        return True
                except Exception as e:
                    logger.warning(f"Error checking S3 files: {e}")
        
        return False
        
    except Exception as e:
        logger.warning(f"Error checking existing version: {e}")
        return False


def build_image_id_mapping(cache_dir: str) -> Dict[int, str]:
    """
    Build a mapping of image IDs to their filenames by listing repository files.
    
    Returns:
        Dict mapping id (int) -> filename (e.g., "0.png", "10.jpg")
    """
    logger.info("Building image ID to filename mapping...")
    
    # List all files in the repo
    files = list_repo_files(HF_REPO_ID, repo_type="dataset")
    
    # Filter for image files in test/images/ and extract id -> filename mapping
    image_pattern = re.compile(r'^test/images/(\d+)\.(png|jpg|jpeg)$', re.IGNORECASE)
    id_to_filename = {}
    
    for filepath in files:
        match = image_pattern.match(filepath)
        if match:
            image_id = int(match.group(1))
            filename = f"{match.group(1)}.{match.group(2)}"
            id_to_filename[image_id] = filename
    
    logger.info(f"Found {len(id_to_filename)} image files")
    return id_to_filename


def load_metadata_for_ids(metadata_path: str, target_ids: set) -> Dict[int, Dict[str, Any]]:
    """
    Load metadata from JSONL file and return only records matching target IDs.
    
    Args:
        metadata_path: Path to the downloaded metadata.jsonl file
        target_ids: Set of image IDs we want to load
        
    Returns:
        Dict mapping image ID -> record with true_json_output
    """
    logger.info(f"Loading metadata for {len(target_ids)} target images...")
    
    id_to_record = {}
    
    with open(metadata_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                image_id = record.get('id')
                if image_id in target_ids:
                    id_to_record[image_id] = {
                        'true_json_output': record.get('true_json_output'),
                        'metadata': record.get('metadata'),
                    }
            except json.JSONDecodeError as e:
                logger.warning(f"Error parsing metadata line: {e}")
                continue
    
    logger.info(f"Loaded metadata for {len(id_to_record)} images")
    return id_to_record


def download_and_convert_image(image_id: int, filename: str, cache_dir: str) -> bytes:
    """
    Download an image from HuggingFace and convert to PNG bytes.
    
    Args:
        image_id: The ID of the image
        filename: The filename (e.g., "0.png", "10.jpg")
        cache_dir: HuggingFace cache directory
        
    Returns:
        PNG image bytes
    """
    # Download the image file
    image_path = hf_hub_download(  # nosec B615 - trusted HuggingFace dataset
        repo_id=HF_REPO_ID,
        filename=f"test/images/{filename}",
        repo_type="dataset",
        cache_dir=cache_dir
    )
    
    # Open and convert to PNG
    with Image.open(image_path) as img:
        # Convert to RGB if necessary (e.g., for RGBA or palette images)
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGB')
        
        buffer = BytesIO()
        img.save(buffer, format='PNG')
        return buffer.getvalue()


def deploy_dataset(version: str, description: str) -> Dict[str, Any]:
    """
    Deploy the dataset by downloading from HuggingFace and uploading
    the pre-selected images and baselines to S3.
    """
    try:
        # Ensure cache directory exists in /tmp (Lambda's writable directory)
        cache_dir = '/tmp/huggingface/hub'  # nosec B108
        os.makedirs(cache_dir, exist_ok=True)
        logger.info(f"Using cache directory: {cache_dir}")
        
        logger.info(f"Downloading metadata from HuggingFace: {HF_REPO_ID}")
        
        # Download the metadata.jsonl file
        metadata_path = hf_hub_download(  # nosec B615
            repo_id=HF_REPO_ID,
            filename="test/metadata.jsonl",
            repo_type="dataset",
            cache_dir=cache_dir
        )
        
        logger.info(f"Downloaded metadata to: {metadata_path}")
        
        # Build set of all target image IDs
        all_target_ids = set()
        for image_ids in HARDCODED_IMAGES.values():
            all_target_ids.update(image_ids)
        
        total_expected = len(all_target_ids)
        logger.info(f"Total target images: {total_expected}")
        
        # Load metadata only for target IDs (to get true_json_output)
        id_to_metadata = load_metadata_for_ids(metadata_path, all_target_ids)
        
        # Build image ID to filename mapping
        id_to_filename = build_image_id_mapping(cache_dir)
        
        # Process and upload each image
        file_count = 0
        skipped_count = 0
        format_distribution = {}
        
        for doc_format, image_ids in HARDCODED_IMAGES.items():
            format_distribution[doc_format] = 0
            
            for image_id in image_ids:
                try:
                    # Check if we have metadata for this ID
                    if image_id not in id_to_metadata:
                        logger.warning(f"Skipping {doc_format}/{image_id}: no metadata found")
                        skipped_count += 1
                        continue
                    
                    # Check if we have a filename for this ID
                    if image_id not in id_to_filename:
                        logger.warning(f"Skipping {doc_format}/{image_id}: no image file found")
                        skipped_count += 1
                        continue
                    
                    metadata = id_to_metadata[image_id]
                    true_json_output = metadata.get('true_json_output')
                    filename = id_to_filename[image_id]
                    
                    if not true_json_output:
                        logger.warning(f"Skipping {doc_format}/{image_id}: no true_json_output")
                        skipped_count += 1
                        continue
                    
                    # Create output filename using format and id
                    output_filename = f"{doc_format}_{image_id}.png"
                    
                    logger.debug(f"Processing {output_filename}")
                    
                    # Download and convert image to PNG
                    try:
                        image_bytes = download_and_convert_image(image_id, filename, cache_dir)
                    except Exception as e:
                        logger.error(f"Error downloading image {image_id}: {e}")
                        skipped_count += 1
                        continue
                    
                    # Upload image to input folder
                    input_key = f'{DATASET_PREFIX}input/{output_filename}'
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=input_key,
                        Body=image_bytes,
                        ContentType='image/png'
                    )
                    
                    # Parse true_json_output if it's a string
                    if isinstance(true_json_output, str):
                        try:
                            inference_result = json.loads(true_json_output)
                        except json.JSONDecodeError:
                            logger.warning(f"Could not parse true_json_output for {image_id}, using as-is")
                            inference_result = {"raw_output": true_json_output}
                    else:
                        inference_result = true_json_output
                    
                    # Create baseline with document classification fields
                    # Single page images always have page_indices [0]
                    result_json = {
                        "document_class": {
                            "type": doc_format
                        },
                        "split_document": {
                            "page_indices": [0]
                        },
                        "inference_result": inference_result
                    }
                    
                    # Upload baseline to baseline folder
                    baseline_key = f'{DATASET_PREFIX}baseline/{output_filename}/sections/1/result.json'
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=baseline_key,
                        Body=json.dumps(result_json, indent=2),
                        ContentType='application/json'
                    )
                    
                    file_count += 1
                    format_distribution[doc_format] += 1
                    
                    if file_count % 25 == 0:
                        logger.info(f"Processed {file_count}/{total_expected} images...")
                        
                except Exception as e:
                    logger.error(f"Error processing {doc_format}/{image_id}: {e}")
                    skipped_count += 1
                    continue
        
        # Log comprehensive statistics
        logger.info(f"Successfully deployed {file_count} images (skipped {skipped_count})")
        logger.info(f"Format distribution: {dict(sorted(format_distribution.items()))}")
        
        # Create test set record in DynamoDB
        create_testset_record(version, description, file_count, format_distribution)
        
        return {
            'DatasetVersion': version,
            'FileCount': file_count,
            'SkippedCount': skipped_count,
            'FormatDistribution': format_distribution,
            'Message': f'Successfully deployed {file_count} images from OCR benchmark'
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
                          format_distribution: Dict[str, int]):
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
        'formatDistribution': format_distribution,
        'description': description or 'OCR benchmark with 9 document formats (293 pre-selected images)'
    }
    
    table.put_item(Item=item)
    logger.info(f"Created test set record in DynamoDB: {TEST_SET_ID}")
