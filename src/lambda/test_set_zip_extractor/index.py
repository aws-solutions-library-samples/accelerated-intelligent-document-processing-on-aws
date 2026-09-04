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


# The two folders a test-set zip is organised into.
ZIP_ROLES = ('input', 'baseline')


def classify_zip_entry(file_path):
    """Which role a zip entry belongs to, and its path relative to that folder.

    Returns ``(role, relative_path)`` where role is ``'input'``, ``'baseline'`` or
    ``None`` for an entry in neither.

    Matches a whole path **segment**. This was ``'/input/' in file_path`` — a substring
    test with a leading slash — so a zip built exactly as the wizard's own diagram
    shows::

        my-test-set.zip
          input/document1.pdf
          baseline/document1.pdf/sections/1/result.json

    produced entries like ``input/document1.pdf``, which contain no ``/input/`` and were
    skipped as "not in input/ or baseline/ folder". Every file was dropped and the set
    reported 0 documents. Only a zip with a wrapping folder (``wrapper/input/...``)
    matched, which is why the pre-deployed HuggingFace sets worked and a hand-made one
    never did. Both shapes resolve here.

    Archive noise macOS adds when compressing a folder is excluded, because
    ``__MACOSX/input/._document1.pdf`` *does* contain ``/input/``: it used to be taken
    for an input document and then failed validation as a baseline missing for a file
    nobody added.
    """
    parts = [p for p in file_path.split('/') if p]
    if not parts:
        return None, ''

    if parts[0] == '__MACOSX' or any(p.startswith('._') for p in parts):
        return None, ''

    for index, part in enumerate(parts):
        if part in ZIP_ROLES:
            relative = '/'.join(parts[index + 1:])
            # A bare `input/` directory entry has nothing after it.
            return (part, relative) if relative else (None, '')

    return None, ''


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
        # Bind test_set_id fresh per record. Without this, an exception raised
        # BEFORE the assignment inside the try (e.g. a malformed record with
        # no 's3' key) would raise NameError in the except clause; and for a
        # malformed record following a healthy one, the except clause would
        # carry the PREVIOUS record's test_set_id and flip that (unrelated)
        # set to FAILED. Cross-record contamination.
        test_set_id = None
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
            logger.exception(f"Error processing S3 event: {str(e)}")
            # Only update status if we identified which test set this record was
            # for — otherwise a pre-parse failure would either NameError here or,
            # for a bad record following a healthy one, incorrectly FAIL the
            # PREVIOUS record's set (cross-record contamination).
            if test_set_id is not None:
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

                    # Segment match, so the documented root-level layout works as
                    # well as a wrapped one. See classify_zip_entry.
                    role, relative = classify_zip_entry(file_path)
                    if role == 'input':
                        input_files.append(file_info)
                        input_names.add(relative.split('/')[-1])
                    elif role == 'baseline':
                        baseline_files.append(file_info)
                    else:
                        logger.warning(f"Skipping file not in input/ or baseline/ folder: {file_path}")

            # Second pass: resolve baseline directory names. The baseline dir is
            # named after the input filename and may use any supported document
            # extension (.pdf, .png, .jpg, .jpeg, .tiff, .tif), not just .pdf.
            for file_info in baseline_files:
                # Path relative to baseline/, for matching against input filenames.
                _role, relative = classify_zip_entry(file_info.filename)
                if '/' in relative:
                    # Handle nested structure: baseline/category/filename.png/sections/...
                    path_parts = relative.split('/')
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
            
            # Both roles, through classify_zip_entry for the relative path. These two
            # loops carried the same leading-slash split as the partition above, so
            # fixing only the partition would have let a root-level zip pass validation
            # and then write nothing: len(parts) == 2 was false and the put_object was
            # skipped in silence.
            for role, files in (('input', input_files), ('baseline', baseline_files)):
                for file_info in files:
                    _role, relative_path = classify_zip_entry(file_info.filename)
                    if not relative_path:
                        # classify_zip_entry already accepted it into this list, so an
                        # empty relative path is a bug in that function, not a bad zip.
                        logger.warning(
                            f"No path below {role}/ for {file_info.filename}; skipping"
                        )
                        continue

                    dest_key = f"{test_set_id}/{role}/{relative_path}"
                    s3.put_object(
                        Bucket=bucket,
                        Key=dest_key,
                        Body=zip_ref.read(file_info.filename)
                    )
                    logger.info(f"Extracted {role} file: {file_info.filename} -> {dest_key}")
    
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

        # REMOVE contentSignature so the resolver's warm-container memo
        # (in `_reconcile_test_set_tracking_entry`) doesn't skip the next
        # reconcile via TTL match: our write invalidated whatever signature
        # was there. Fires on both COMPLETED (with file_count) AND FAILED
        # (without) — a failed extract may have left partial S3 state, so
        # the reconcile needs to re-scan even though fileCount didn't move.
        update_expression += ' REMOVE contentSignature'

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
