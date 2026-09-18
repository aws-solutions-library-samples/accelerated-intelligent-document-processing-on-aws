"""
Lambda function to deploy the RealKIE-FCC-Verified dataset from HuggingFace
to the TestSetBucket during stack deployment.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict

import boto3
import cfnresponse
from botocore.config import Config

# Set HuggingFace cache to /tmp (Lambda's writable directory)
os.environ["HF_HOME"] = "/tmp/huggingface"  # nosec B108 - isolated Lambda environment
os.environ["HUGGINGFACE_HUB_CACHE"] = "/tmp/huggingface/hub"  # nosec B108

# Lightweight HuggingFace access
import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download
from log_sanitizer import sanitize_event_for_logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# AWS clients.
# When S3_ENDPOINT_URL is set (private VPC mode), force virtual-host
# addressing so the SigV4 host header matches the VPC interface endpoint
# bucket-vhost DNS. boto3's auto default usually picks virtual for
# DNS-compliant bucket names but is brittle (e.g. dotted bucket names fall
# back to path), so we set it explicitly to match the presigner Lambdas.
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"
s3_client = boto3.client(
    "s3",
    endpoint_url=_s3_endpoint_url,
    config=Config(signature_version="s3v4", s3={"addressing_style": _s3_addressing}),
)
dynamodb = boto3.resource("dynamodb")

# Environment variables
TESTSET_BUCKET = os.environ.get("TESTSET_BUCKET")
TRACKING_TABLE = os.environ.get("TRACKING_TABLE")

# Constants
DATASET_NAME = "RealKIE-FCC-Verified"
DATASET_PREFIX = "realkie-fcc-verified/"
TEST_SET_ID = "realkie-fcc-verified"


def handler(event, context):
    """
    Main Lambda handler for deploying the FCC dataset.
    """
    logger.info(f"Event: {json.dumps(sanitize_event_for_logging(event))}")

    try:
        request_type = event["RequestType"]

        if request_type == "Delete":
            # On stack deletion, we leave the data in place
            logger.info("Delete request - keeping dataset in place")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            return

        # Extract properties
        properties = event["ResourceProperties"]
        dataset_version = properties.get("DatasetVersion", "1.0")
        dataset_description = properties.get("DatasetDescription", "")

        logger.info(f"Processing dataset version: {dataset_version}")

        # Check if dataset already exists with this version
        if check_existing_version(dataset_version):
            logger.info(
                f"Dataset version {dataset_version} already deployed, updating description only"
            )
            update_description_only(dataset_description)
            cfnresponse.send(
                event,
                context,
                cfnresponse.SUCCESS,
                {
                    "Message": f"Dataset version {dataset_version} already exists, description updated"
                },
            )
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
                version=properties.get("DatasetVersion", "1.0")
                if "properties" in dir()
                else "1.0",
                error_message=str(e),
            )
        except Exception as record_err:
            logger.error(f"Failed to create error test set record: {record_err}")
        cfnresponse.send(
            event,
            context,
            cfnresponse.SUCCESS,
            {
                "Status": "DEPLOYMENT_FAILED",
                "Message": f"Test set deployment failed (non-blocking): {str(e)[:200]}",
            },
        )


def update_description_only(description: str):
    """
    Update only the description field in the existing DynamoDB record.
    """
    try:
        table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
        table.update_item(
            Key={"PK": f"testset#{TEST_SET_ID}", "SK": "metadata"},
            UpdateExpression="SET description = :desc",
            ExpressionAttributeValues={":desc": description},
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
            Key={"PK": f"testset#{TEST_SET_ID}", "SK": "metadata"}
        )

        if "Item" in response:
            existing_version = response["Item"].get("datasetVersion", "")
            logger.info(f"Found existing dataset version: {existing_version}")

            # Check if version matches, status is not FAILED, and files exist
            if existing_version == version:
                existing_status = response["Item"].get("status", "")
                if existing_status == "FAILED":
                    logger.info("Previous deployment failed, retrying deployment")
                    return False
                # Verify at least some files exist in S3
                try:
                    response = s3_client.list_objects_v2(
                        Bucket=TESTSET_BUCKET,
                        Prefix=f"{DATASET_PREFIX}input/",
                        MaxKeys=1,
                    )
                    if response.get("KeyCount", 0) > 0:
                        logger.info("Files exist in S3, skipping deployment")
                        return True
                except Exception as e:
                    logger.warning(f"Error checking S3 files: {e}")

        return False

    except Exception as e:
        logger.warning(f"Error checking existing version: {e}")
        return False


def get_page_count(data_dict: dict, idx: int) -> int:
    """
    Get the number of pages for a document by counting image files.

    The RealKIE-FCC-Verified dataset contains an 'image_files' column
    with a list of image filenames, one per page.

    Args:
        data_dict: Parquet data dictionary
        idx: Document index

    Returns:
        Number of pages (image file count)
    """
    image_files = data_dict["image_files"][idx]
    if image_files and isinstance(image_files, list):
        return len(image_files)

    logger.warning(f"Could not determine page count for document index {idx}")
    return 0


def deploy_dataset(version: str, description: str) -> Dict[str, Any]:
    """
    Deploy the dataset by downloading PDFs and ground truth from HuggingFace
    using lightweight hf_hub_download and pyarrow.
    """
    try:
        # Ensure cache directory exists in /tmp (Lambda's writable directory)
        cache_dir = "/tmp/huggingface/hub"  # nosec B108
        os.makedirs(cache_dir, exist_ok=True)
        logger.info(f"Using cache directory: {cache_dir}")

        logger.info(
            f"Downloading dataset from HuggingFace: amazon-agi/RealKIE-FCC-Verified"
        )

        # Download the parquet file with metadata using hf_hub_download
        parquet_path = hf_hub_download(  # nosec B615 - trusted HuggingFace dataset
            repo_id="amazon-agi/RealKIE-FCC-Verified",
            filename="data/test-00000-of-00001.parquet",
            repo_type="dataset",
            cache_dir=cache_dir,
        )

        logger.info(f"Downloaded parquet metadata file")

        # Read parquet file with pyarrow
        table = pq.read_table(parquet_path)
        data_dict = table.to_pydict()

        num_documents = len(data_dict["id"])
        logger.info(f"Loaded {num_documents} documents from parquet")

        # TEMPORARY: Log parquet schema for debugging
        logger.info(f"Parquet schema: {table.schema}")
        logger.info(f"Available columns: {list(data_dict.keys())}")

        # Sample first document to see structure (if exists)
        if num_documents > 0:
            sample_keys = list(data_dict.keys())
            logger.info(f"Sample document column names: {sample_keys}")
            # Log a few sample values (avoiding large data)
            for key in sample_keys[:5]:
                value = data_dict[key][0]
                if isinstance(value, (list, dict)):
                    logger.info(
                        f"  {key}: {type(value).__name__} with {len(value) if hasattr(value, '__len__') else 'N/A'} items"
                    )
                else:
                    logger.info(f"  {key}: {type(value).__name__}")

        # Process and upload each document
        file_count = 0
        skipped_count = 0
        total_pages = 0
        page_count_distribution = {}

        for idx in range(num_documents):
            try:
                document_id = data_dict["id"][idx]
                json_response = data_dict["json_response"][idx]

                if not json_response:
                    logger.warning(f"Skipping {document_id}: no json_response")
                    skipped_count += 1
                    continue

                # Get page count from images
                page_count = get_page_count(data_dict, idx)

                # Validate page count
                if page_count == 0:
                    logger.warning(
                        f"Skipping {document_id}: no pages found (page_count=0)"
                    )
                    skipped_count += 1
                    continue

                # Track statistics
                total_pages += page_count
                page_count_distribution[page_count] = (
                    page_count_distribution.get(page_count, 0) + 1
                )

                logger.info(f"Processing {document_id} ({page_count} pages)")

                # Download PDF file from HuggingFace repository using hf_hub_download
                try:
                    pdf_path = hf_hub_download(  # nosec B615
                        repo_id="amazon-agi/RealKIE-FCC-Verified",
                        filename=f"pdfs/{document_id}",
                        repo_type="dataset",
                        cache_dir=cache_dir,
                    )

                    # Read the downloaded PDF
                    with open(pdf_path, "rb") as f:
                        pdf_bytes = f.read()

                    logger.info(
                        f"Downloaded PDF for {document_id} ({len(pdf_bytes):,} bytes)"
                    )

                    # Upload PDF to input folder
                    pdf_key = f"{DATASET_PREFIX}input/{document_id}"
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=pdf_key,
                        Body=pdf_bytes,
                        ContentType="application/pdf",
                    )

                except Exception as e:
                    logger.error(
                        f"Error downloading/uploading PDF for {document_id}: {e}"
                    )
                    skipped_count += 1
                    continue

                # Generate zero-indexed page_indices array
                page_indices = list(range(page_count))

                # Create enhanced baseline with document split classification fields
                result_json = {
                    "document_class": {"type": "Invoice"},
                    "split_document": {"page_indices": page_indices},
                    "inference_result": json_response,
                }

                # Upload ground truth baseline
                result_key = (
                    f"{DATASET_PREFIX}baseline/{document_id}/sections/1/result.json"
                )
                s3_client.put_object(
                    Bucket=TESTSET_BUCKET,
                    Key=result_key,
                    Body=json.dumps(result_json, indent=2),
                    ContentType="application/json",
                )

                file_count += 1

                if file_count % 10 == 0:
                    logger.info(f"Processed {file_count}/{num_documents} documents...")

            except Exception as e:
                logger.error(f"Error processing document {idx} ({document_id}): {e}")
                skipped_count += 1
                continue

        # Log comprehensive statistics
        logger.info(
            f"Successfully deployed {file_count} documents (skipped {skipped_count})"
        )
        logger.info(f"Total pages across all documents: {total_pages}")
        logger.info(
            f"Average pages per document: {total_pages / file_count if file_count > 0 else 0:.2f}"
        )
        logger.info(
            f"Page count distribution: {dict(sorted(page_count_distribution.items()))}"
        )

        # Create test set record in DynamoDB
        create_testset_record(version, description, file_count)

        return {
            "DatasetVersion": version,
            "FileCount": file_count,
            "SkippedCount": skipped_count,
            "TotalPages": total_pages,
            "PageCountDistribution": page_count_distribution,
            "Message": f"Successfully deployed {file_count} documents with enhanced baseline files (doc split fields included)",
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
    timestamp = datetime.utcnow().isoformat() + "Z"

    item = {
        "PK": f"testset#{TEST_SET_ID}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": timestamp,
        "id": TEST_SET_ID,
        "name": DATASET_NAME,
        "filePattern": "",
        "fileCount": 0,
        "status": "FAILED",
        "createdAt": timestamp,
        "datasetVersion": version,
        "source": "huggingface:amazon-agi/RealKIE-FCC-Verified",
        "description": (
            f"⚠️ Deployment failed: {error_message[:500]}. "
            f"This test set could not be downloaded from its source. "
            f"It will be retried on the next stack update."
        ),
    }

    table.put_item(Item=item)
    logger.info(f"Created FAILED test set record in DynamoDB: {TEST_SET_ID}")


def create_testset_record(version: str, description: str, file_count: int):
    """
    Create or update the test set record in DynamoDB.
    """
    table = dynamodb.Table(TRACKING_TABLE)  # type: ignore[attr-defined]
    timestamp = datetime.utcnow().isoformat() + "Z"

    item = {
        "PK": f"testset#{TEST_SET_ID}",
        "SK": "metadata",
        "ItemType": "testset",
        "InitialEventTime": timestamp,
        "id": TEST_SET_ID,
        "name": DATASET_NAME,
        "description": description,
        "filePattern": "",
        "fileCount": file_count,
        "status": "COMPLETED",
        "createdAt": timestamp,
        "datasetVersion": version,
        "source": "huggingface:amazon-agi/RealKIE-FCC-Verified",
        "description": description or "RealKIE-FCC-Verified dataset from HuggingFace",
    }

    table.put_item(Item=item)
    logger.info(f"Created test set record in DynamoDB: {TEST_SET_ID}")
