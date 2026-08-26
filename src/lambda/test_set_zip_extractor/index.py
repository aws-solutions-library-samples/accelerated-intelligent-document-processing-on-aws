# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import logging
import os
import zipfile
import tempfile

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# Document formats supported as test set inputs. Used as a fallback when a
# baseline directory name does not match any known input filename, so that
# genuinely orphaned baselines still surface as "extra baselines" rather than
# being silently dropped.
SUPPORTED_EXTENSIONS = ('.pdf', '.png', '.jpg', '.jpeg', '.tiff', '.tif')


def _match_baseline_name(path_parts, input_names):
    """Find the baseline directory name for a baseline file's path segments.

    The baseline directory is named after the input filename (e.g.
    ``baseline/category/document1.png/sections/1/result.json``). Match it
    extension-agnostically against the set of known input filenames; fall back
    to a supported-extension check only when no segment matches an input name.
    """
    # Prefer an exact match against a known input filename (extension-agnostic).
    for part in path_parts:
        if part in input_names:
            return part

    # Fallback: a segment that looks like a supported document filename. This
    # lets orphaned baselines still be reported as extras instead of vanishing.
    for part in path_parts:
        if part.lower().endswith(SUPPORTED_EXTENSIONS):
            return part

    return None

def handler(event, context):
    """Process S3 events for uploaded ZIP files"""
    logger.info(f"Zip extractor invoked with {len(event['Records'])} S3 events")
    
    for record in event['Records']:
        try:
            # Parse S3 event
            bucket = record['s3']['bucket']['name']
            key = record['s3']['object']['key']
            
            # Extract test set ID from key (key format: test_set_id/test_set_id.zip)
            if '/' in key and key.endswith('.zip'):
                test_set_id = key.split('/')[0]  # Get the folder name
                zip_key = key  # Full path to ZIP file
            else:
                # Fallback for old format
                test_set_id = key
                zip_key = key
            
            logger.info(f"Processing zip extraction for test set: {test_set_id}, key: {key}")
            
            # Extract the uploaded ZIP file
            _extract_uploaded_zip(bucket, test_set_id, zip_key)

            # Recount total files in test set (accurate for both new and append)
            file_count = _count_test_set_files(bucket, test_set_id)

            # Update test set status to COMPLETED with file count
            _update_test_set_status(test_set_id, 'COMPLETED', None, file_count)
            
            logger.info(f"Successfully processed zip extraction for test set {test_set_id}")
            
        except Exception as e:
            logger.error(f"Error processing S3 event: {str(e)}")
            # Update test set status to FAILED
            _update_test_set_status(test_set_id, 'FAILED', str(e))

    
    return {'statusCode': 200}

def _extract_uploaded_zip(bucket, test_set_id, zip_key):
    """Extract uploaded ZIP file and organize into input/ and baseline/ folders"""
    
    # Download ZIP file to temporary location
    with tempfile.NamedTemporaryFile() as temp_file:
        s3.download_fileobj(bucket, zip_key, temp_file)
        temp_file.seek(0)
        
        # Extract ZIP contents
        with zipfile.ZipFile(temp_file, 'r') as zip_ref:
            # Validate zip structure and extract files
            input_files = []
            baseline_files = []
            input_names = set()
            baseline_names = set()
            
            # First pass: partition input vs baseline files and collect input
            # filenames. ZIP entry order is not guaranteed, so baseline names
            # are resolved in a second pass once all input names are known.
            for file_info in zip_ref.infolist():
                if not file_info.is_dir():
                    file_path = file_info.filename

                    # Check if file is in input/ or baseline/ folder
                    if '/input/' in file_path:
                        input_files.append(file_info)
                        # Extract filename for matching
                        filename = file_path.split('/')[-1]
                        input_names.add(filename)
                    elif '/baseline/' in file_path:
                        baseline_files.append(file_info)
                    else:
                        logger.warning(f"Skipping file not in input/ or baseline/ folder: {file_path}")

            # Second pass: resolve baseline directory names. The baseline dir is
            # named after the input filename and may use any supported document
            # extension (.pdf, .png, .jpg, .jpeg, .tiff, .tif), not just .pdf.
            for file_info in baseline_files:
                # Extract folder name after /baseline/ for matching
                parts = file_info.filename.split('/baseline/', 1)
                if len(parts) == 2 and '/' in parts[1]:
                    # Handle nested structure: baseline/category/filename.png/sections/...
                    path_parts = parts[1].split('/')
                    if len(path_parts) >= 2:
                        baseline_name = _match_baseline_name(path_parts, input_names)
                        if baseline_name:
                            baseline_names.add(baseline_name)

            if not input_files:
                raise ValueError(f"No files found in input/ folder within zip file")
            
            if not baseline_files:
                raise ValueError(f"No files found in baseline/ folder within zip file")
            
            # Validate file count and names match
            # Check that each input file has a corresponding baseline file
            missing_baselines = input_names - baseline_names
            if missing_baselines:
                raise ValueError(f"Missing baseline files for: {', '.join(missing_baselines)}")
            
            extra_baselines = baseline_names - input_names
            if extra_baselines:
                raise ValueError(f"Extra baseline files without corresponding input: {', '.join(extra_baselines)}")
            
            logger.info(f"Validation passed: {len(input_names)} input documents match {len(baseline_names)} baseline documents")
            
            # Extract input files
            for file_info in input_files:
                file_content = zip_ref.read(file_info.filename)
                
                # Extract relative path after /input/
                parts = file_info.filename.split('/input/', 1)
                if len(parts) == 2:
                    relative_path = parts[1]
                    dest_key = f"{test_set_id}/input/{relative_path}"
                    
                    s3.put_object(
                        Bucket=bucket,
                        Key=dest_key,
                        Body=file_content
                    )
                    
                    logger.info(f"Extracted input file: {file_info.filename} -> {dest_key}")
            
            # Extract baseline files
            for file_info in baseline_files:
                file_content = zip_ref.read(file_info.filename)
                
                # Extract relative path after /baseline/
                parts = file_info.filename.split('/baseline/', 1)
                if len(parts) == 2:
                    relative_path = parts[1]
                    dest_key = f"{test_set_id}/baseline/{relative_path}"
                    
                    s3.put_object(
                        Bucket=bucket,
                        Key=dest_key,
                        Body=file_content
                    )
                    
                    logger.info(f"Extracted baseline file: {file_info.filename} -> {dest_key}")
    
    # Delete original ZIP file
    s3.delete_object(Bucket=bucket, Key=zip_key)
    logger.info(f"Deleted original ZIP file: {zip_key}")


def _count_test_set_files(bucket, test_set_id):
    """Count total input files in a test set by listing S3 objects"""
    prefix = f"{test_set_id}/input/"
    count = 0

    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            if not obj['Key'].endswith('/'):
                count += 1

    logger.info(f"Counted {count} total input files in test set {test_set_id}")
    return count

def _update_test_set_status(test_set_id, status, error=None, file_count=None):
    """Update test set status and optionally file count in tracking table"""
    table = dynamodb.Table(os.environ['TRACKING_TABLE'])  # type: ignore
    
    try:
        update_expression = 'SET #status = :status'
        expression_values = {':status': status}
        expression_names = {'#status': 'status'}
        
        if error:
            update_expression += ', #error = :error'
            expression_values[':error'] = error
            expression_names['#error'] = 'error'
        
        if file_count is not None:
            update_expression += ', fileCount = :count'
            expression_values[':count'] = file_count
        
        table.update_item(
            Key={'PK': f'testset#{test_set_id}', 'SK': 'metadata'},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values
        )
        
        logger.info(f"Updated test set {test_set_id} status to {status}" + 
                   (f" with {file_count} files" if file_count else ""))
        
    except Exception as e:
        logger.error(f"Failed to update test set status for {test_set_id}: {e}")  # nosec B608 - log message f-string, not a SQL query
