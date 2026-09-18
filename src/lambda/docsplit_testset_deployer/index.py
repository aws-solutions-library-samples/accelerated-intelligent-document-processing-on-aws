"""
Lambda function to deploy the RVL-CDIP-NMP Packet TestSet from HuggingFace
to the TestSetBucket during stack deployment.

This deployer:
1. Downloads the data.tar.gz from HuggingFace
2. Extracts source PDFs to /tmp
3. Creates packet PDFs by merging pages from source PDFs based on bundled manifest
4. Generates ground truth baseline files for classification and splitting evaluation
5. Uploads packets and baselines to S3

Dataset: jordyvl/rvl_cdip_n_mp
This TestSet contains 500 packet PDFs created from 13 document types.
"""

import json
import os
import logging
import tarfile
import tempfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, Any, List

import boto3
import requests
import cfnresponse
from log_sanitizer import sanitize_event_for_logging
from pypdf import PdfReader, PdfWriter

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
DATASET_NAME = 'DocSplit-Poly-Seq'
DATASET_PREFIX = 'docsplit/'
TEST_SET_ID = 'docsplit'
HF_DATA_URL = 'https://huggingface.co/datasets/jordyvl/rvl_cdip_n_mp/resolve/main/data.tar.gz'

# Path to bundled manifest
MANIFEST_PATH = Path(__file__).parent / 'packets.json'


def normalize_doc_type(doc_type: str) -> str:
    """Normalize document type for JSON Schema compatibility (replace spaces with underscores)."""
    return doc_type.replace(" ", "_")


def handler(event, context):
    """
    Main Lambda handler for deploying the RVL-CDIP-NMP dataset.
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
            logger.info(f"Dataset version {dataset_version} already deployed, skipping")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'Message': f'Dataset version {dataset_version} already exists'
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


def load_manifest() -> Dict[str, Any]:
    """
    Load the bundled packet manifest.
    """
    logger.info(f"Loading manifest from: {MANIFEST_PATH}")
    
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    logger.info(f"Loaded manifest with {manifest['packet_count']} packets")
    return manifest


def is_safe_tar_member(member: tarfile.TarInfo, extract_dir: Path) -> bool:
    """
    Validate that a tar member is safe to extract.
    
    Checks for:
    - Absolute paths
    - Path traversal attempts (using ..)
    - Symbolic links pointing outside extraction directory
    
    Returns:
        True if the member is safe to extract, False otherwise
    """
    # Reject absolute paths
    if os.path.isabs(member.name):
        logger.warning(f"Rejecting absolute path in tar: {member.name}")
        return False
    
    # Normalize the path and check for traversal
    # This handles cases like "foo/../../../etc/passwd"
    target_path = os.path.normpath(os.path.join(extract_dir, member.name))
    
    # Ensure the target path is within the extraction directory
    try:
        # resolve() would follow symlinks, but we use normpath + comparison
        # to check the path stays within extract_dir
        extract_dir_str = str(extract_dir.resolve())
        if not target_path.startswith(extract_dir_str + os.sep) and target_path != extract_dir_str:
            logger.warning(f"Rejecting path traversal attempt in tar: {member.name}")
            return False
    except (ValueError, RuntimeError) as e:
        logger.warning(f"Rejecting member due to path resolution error: {member.name}, {e}")
        return False
    
    # Check symbolic links
    if member.issym() or member.islnk():
        # For symlinks, check if the link target would escape the extraction directory
        if member.issym():
            link_target = member.linkname
            if os.path.isabs(link_target):
                logger.warning(f"Rejecting symlink with absolute target: {member.name} -> {link_target}")
                return False
            
            # Resolve the symlink target relative to its location
            link_dir = os.path.dirname(target_path)
            resolved_target = os.path.normpath(os.path.join(link_dir, link_target))
            
            if not resolved_target.startswith(extract_dir_str + os.sep) and resolved_target != extract_dir_str:
                logger.warning(f"Rejecting symlink escaping extraction dir: {member.name} -> {link_target}")
                return False
    
    return True


def safe_extract_tar(tar: tarfile.TarFile, extract_dir: Path) -> None:
    """
    Safely extract tar members after validating each one.
    
    Args:
        tar: Open tarfile object
        extract_dir: Directory to extract to
    """
    safe_members = []
    for member in tar.getmembers():
        if is_safe_tar_member(member, extract_dir):
            safe_members.append(member)
        else:
            logger.warning(f"Skipping unsafe tar member: {member.name}")
    
    logger.info(f"Extracting {len(safe_members)} safe members from tar archive")
    tar.extractall(path=extract_dir, members=safe_members)  # nosec B202 - members validated by is_safe_tar_member


def download_and_extract_dataset(extract_dir: Path) -> Path:
    """
    Download data.tar.gz from HuggingFace and extract to temporary directory.
    
    Returns:
        Path to the extracted assets directory containing document type folders
    """
    logger.info(f"Downloading dataset from: {HF_DATA_URL}")
    
    # Download with streaming
    response = requests.get(HF_DATA_URL, stream=True, timeout=600)
    response.raise_for_status()
    
    # Stream to tar extraction
    tar_bytes = BytesIO(response.content)
    logger.info(f"Downloaded {len(response.content) / (1024*1024):.1f} MB")
    
    logger.info(f"Extracting to: {extract_dir}")
    with tarfile.open(fileobj=tar_bytes, mode='r:gz') as tar:
        safe_extract_tar(tar, extract_dir)
    
    # Find the assets directory - handle two common structures:
    # 1. Tar contains wrapper directory with doc type folders inside
    # 2. Tar contains doc type folders directly at top level
    
    # Get all top-level items
    top_level_items = list(extract_dir.iterdir())
    directories = [item for item in top_level_items if item.is_dir()]
    
    logger.info(f"Found {len(directories)} directories in extracted archive:")
    for d in directories:
        logger.info(f"  - {d.name}")
    
    # Expected document type folders from the manifest
    expected_doc_types = {
        'invoice', 'email', 'form', 'letter', 'memo', 'resume', 
        'budget', 'news article', 'scientific publication', 
        'specification', 'questionnaire', 'handwritten', 'language'
    }
    
    # Case 1: Single directory - check if it's the wrapper containing doc types
    if len(directories) == 1:
        wrapper_dir = directories[0]
        logger.info(f"Checking if '{wrapper_dir.name}' contains document type folders...")
        
        subdirs = {d.name.lower() for d in wrapper_dir.iterdir() if d.is_dir()}
        matching_types = expected_doc_types.intersection(subdirs)
        
        if len(matching_types) >= 3:  # Require at least 3 matching doc types
            logger.info(f"Found {len(matching_types)} document type folders in '{wrapper_dir.name}'")
            logger.info(f"Using assets directory: {wrapper_dir}")
            return wrapper_dir
        else:
            logger.warning(f"Directory '{wrapper_dir.name}' doesn't contain expected doc types")
            logger.warning(f"Found subdirs: {sorted(subdirs)}")
    
    # Case 2: Multiple directories - check if doc types are at top level
    top_level_names = {d.name.lower() for d in directories}
    matching_types = expected_doc_types.intersection(top_level_names)
    
    if len(matching_types) >= 3:  # Require at least 3 matching doc types
        logger.info(f"Found {len(matching_types)} document type folders at top level")
        logger.info(f"Using extract directory as assets directory: {extract_dir}")
        return extract_dir
    
    # Case 3: Unexpected structure - provide diagnostic info
    error_msg = (
        f"Unexpected archive structure. Found {len(directories)} directories but "
        f"could not locate document type folders.\n"
        f"Top-level directories: {[d.name for d in directories]}\n"
        f"Expected document types: {sorted(expected_doc_types)}"
    )
    logger.error(error_msg)
    raise RuntimeError(error_msg)


def create_packet_pdf(
    packet: Dict[str, Any],
    assets_dir: Path
) -> bytes:
    """
    Create a combined packet PDF by merging pages from source PDFs.
    
    Returns:
        PDF bytes
    """
    writer = PdfWriter()
    
    for subdoc in packet["subdocuments"]:
        source_pdf_path = assets_dir / subdoc["source_pdf"]
        
        if not source_pdf_path.exists():
            raise FileNotFoundError(f"Source PDF not found: {source_pdf_path}")
        
        reader = PdfReader(source_pdf_path)
        
        for page_num in subdoc["source_pages"]:
            # Convert 1-indexed to 0-indexed
            page_idx = page_num - 1
            
            if page_idx < 0 or page_idx >= len(reader.pages):
                raise ValueError(
                    f"Invalid page {page_num} for {source_pdf_path} "
                    f"(has {len(reader.pages)} pages)"
                )
            
            writer.add_page(reader.pages[page_idx])
    
    # Write to bytes
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def create_baseline_results(packet: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Create ground truth baseline results for a packet.
    
    Returns:
        List of (section_num, result_json) tuples
    """
    results = []
    
    for section_idx, subdoc in enumerate(packet["subdocuments"], start=1):
        # Convert page_ordinals (1-indexed) to page_indices (0-indexed)
        page_indices = [p - 1 for p in subdoc["page_ordinals"]]
        
        result = {
            "document_class": {
                "type": normalize_doc_type(subdoc["doc_type_id"])
            },
            "split_document": {
                "page_indices": page_indices
            },
            "inference_result": {}
        }
        
        results.append((section_idx, result))
    
    return results


def deploy_dataset(version: str, description: str) -> Dict[str, Any]:
    """
    Deploy the dataset by downloading from HuggingFace and creating packets.
    """
    # Create temporary directory for extraction
    with tempfile.TemporaryDirectory(prefix='rvl-cdip-nmp-') as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Download and extract dataset
        assets_dir = download_and_extract_dataset(tmp_path)
        
        # Load manifest
        manifest = load_manifest()
        packets = manifest['packets']
        
        # Statistics
        stats = {
            "total": len(packets),
            "success": 0,
            "failed": 0,
            "total_pages": 0,
            "total_sections": 0
        }
        doc_type_distribution: Dict[str, int] = {}
        
        # Process each packet
        for idx, packet in enumerate(packets, start=1):
            packet_id = packet["packet_id"]
            
            if idx % 50 == 0:
                logger.info(f"Processing packet {idx}/{len(packets)}...")
            
            try:
                # Create packet PDF
                pdf_bytes = create_packet_pdf(packet, assets_dir)
                
                # Upload PDF to input folder
                pdf_key = f'{DATASET_PREFIX}input/{packet_id}.pdf'
                s3_client.put_object(
                    Bucket=TESTSET_BUCKET,
                    Key=pdf_key,
                    Body=pdf_bytes,
                    ContentType='application/pdf'
                )
                
                # Create and upload baseline files
                baseline_results = create_baseline_results(packet)
                
                for section_num, result_json in baseline_results:
                    baseline_key = (
                        f'{DATASET_PREFIX}baseline/{packet_id}.pdf/'
                        f'sections/{section_num}/result.json'
                    )
                    s3_client.put_object(
                        Bucket=TESTSET_BUCKET,
                        Key=baseline_key,
                        Body=json.dumps(result_json, indent=2),
                        ContentType='application/json'
                    )
                    
                    # Track document types
                    doc_type = result_json["document_class"]["type"]
                    doc_type_distribution[doc_type] = (
                        doc_type_distribution.get(doc_type, 0) + 1
                    )
                
                stats["success"] += 1
                stats["total_pages"] += packet["total_pages"]
                stats["total_sections"] += len(packet["subdocuments"])
                
            except Exception as e:
                logger.error(f"Error processing {packet_id}: {e}")
                stats["failed"] += 1
                continue
        
        # Log statistics
        logger.info("=" * 60)
        logger.info("Deployment Statistics:")
        logger.info(f"  Total packets: {stats['total']}")
        logger.info(f"  Successful: {stats['success']}")
        logger.info(f"  Failed: {stats['failed']}")
        logger.info(f"  Total pages: {stats['total_pages']}")
        logger.info(f"  Total sections: {stats['total_sections']}")
        if stats['success'] > 0:
            logger.info(f"  Avg pages/packet: {stats['total_pages']/stats['success']:.1f}")
            logger.info(f"  Avg sections/packet: {stats['total_sections']/stats['success']:.1f}")
        logger.info(f"  Document types: {dict(sorted(doc_type_distribution.items()))}")
        logger.info("=" * 60)
        
        # Create test set record in DynamoDB
        create_testset_record(
            version, 
            description, 
            stats['success'],
            doc_type_distribution
        )
        
        return {
            'DatasetVersion': version,
            'FileCount': stats['success'],
            'FailedCount': stats['failed'],
            'TotalPages': stats['total_pages'],
            'TotalSections': stats['total_sections'],
            'DocTypeDistribution': doc_type_distribution,
            'Message': f'Successfully deployed {stats["success"]} packets'
        }


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
        'source': 'huggingface:jordyvl/rvl_cdip_n_mp',
        'description': (
            f'⚠️ Deployment failed: {error_message[:500]}. '
            f'This test set could not be downloaded from its source. '
            f'It will be retried on the next stack update.'
        ),
    }

    table.put_item(Item=item)
    logger.info(f"Created FAILED test set record in DynamoDB: {TEST_SET_ID}")


def create_testset_record(
    version: str,
    description: str,
    file_count: int,
    doc_type_distribution: Dict[str, int]
):
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
        'source': f'huggingface:jordyvl/rvl_cdip_n_mp',
        'docTypeDistribution': doc_type_distribution,
        'description': description or (
            'RVL-CDIP-NMP Packet TestSet - 500 multi-page packets with 13 document '
            'types for classification and document splitting evaluation.'
        )
    }
    
    table.put_item(Item=item)
    logger.info(f"Created test set record in DynamoDB: {TEST_SET_ID}")