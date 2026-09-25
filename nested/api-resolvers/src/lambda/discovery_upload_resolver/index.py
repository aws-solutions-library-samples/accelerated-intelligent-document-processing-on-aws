# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# src/lambda/discovery_upload_resolver/index.py

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
import idp_common.s3_targets as s3_targets
from idp_common.utils.log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Configure S3 client with S3v4 signature.
# When S3_ENDPOINT_URL is set (private VPC mode), switch to virtual-host
# addressing so SigV4 host signing matches the VPC endpoint DNS.
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"
s3_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": _s3_addressing},
)
s3_client = boto3.client("s3", endpoint_url=_s3_endpoint_url, config=s3_config)

# Which bucket and key this resolver may target. See s3_targets: one allow-list
# shared with get_file_contents_resolver (read) and upload_resolver (write).
ALLOWED_BUCKETS = s3_targets.resolve_allowed_buckets()
sqs_client = boto3.client('sqs')
dynamodb = boto3.resource('dynamodb')

sfn_client = boto3.client('stepfunctions')


def _clear_version_schema(version, discovery_type='classes'):
    """Clear the discovered-schema list for a config version before discovery.

    Implements the "Replace" save mode: rather than augmenting the version's
    existing schema, we blank it out once here — up front, before any discovery
    jobs are enqueued — so the subsequent per-job merges (which append/dedupe by
    $id in ClassesDiscovery._merge_and_save_class) rebuild the list from scratch.

    Clearing once in the resolver (not per job) is what makes Replace correct for
    multi-section discovery, where a single submission spawns N jobs that each
    merge one class into the same version.

    - classes discovery clears ``classes``
    - rules/policy discovery clears ``policy_classes``

    A no-op if the version doesn't exist yet or has no config stored.
    """
    if not version:
        return

    key = 'policy_classes' if discovery_type == 'rules' else 'classes'
    try:
        from idp_common.config.configuration_manager import ConfigurationManager

        config_manager = ConfigurationManager()
        existing = config_manager.get_raw_configuration("Config", version=version) or {}
        current = existing.get(key) or []
        if not current:
            logger.info(
                f"Replace mode: version '{version}' has no '{key}' to clear — nothing to do"
            )
            return
        logger.info(
            f"Replace mode: clearing {len(current)} '{key}' entries from version '{version}'"
        )
        existing[key] = []
        config_manager.save_raw_configuration("Config", existing, version=version)
    except Exception as e:
        # Surface the failure — silently augmenting when the user asked to
        # replace would be worse than failing the request.
        logger.error(f"Failed to clear '{key}' for version '{version}': {e}")
        raise


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts these fields via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context):
    """
    Handles discovery-related GraphQL mutations:
    - uploadDiscoveryDocument: presigned URL + job creation
    - autoDetectSections: LLM-based section boundary detection
    - startMultiDocDiscovery: Start multi-document discovery pipeline
    - uploadMultiDocDiscoveryZip: Upload zip file for multi-doc discovery
    """
    logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

    # Route based on which GraphQL field is being resolved
    field_name = event.get('info', {}).get('fieldName', 'uploadDiscoveryDocument')

    # Defense-in-depth: all discovery mutations are Admin+Author operations.
    if not _caller_in_groups(event, ("Admin", "Author")):
        logger.warning(
            f"Forbidden: caller attempted '{field_name}' without Admin/Author group"
        )
        raise PermissionError(
            f"Unauthorized: '{field_name}' requires Admin or Author group"
        )

    if field_name == 'autoDetectSections':
        return handle_auto_detect_sections(event, context)
    elif field_name == 'startMultiDocDiscovery':
        return handle_start_multi_doc_discovery(event, context)
    elif field_name == 'uploadMultiDocDiscoveryZip':
        return handle_upload_multi_doc_discovery_zip(event, context)

    return handle_upload_discovery_document(event, context)


def handle_auto_detect_sections(event, context):
    """
    Use ClassesDiscovery to auto-detect document section boundaries.
    Reads config from the selected version, sends PDF to Bedrock via idp_common.
    """
    from idp_common.discovery.classes_discovery import ClassesDiscovery

    arguments = event.get('arguments', {})
    document_key = arguments.get('documentKey')
    bucket = arguments.get('bucket')
    version = arguments.get('version')

    if not document_key or not bucket:
        raise ValueError("documentKey and bucket are required")

    # A read rather than a write, so the bucket allow-list applies and the write-once
    # key rule does not.
    s3_targets.assert_bucket_allowed(bucket, ALLOWED_BUCKETS, logger=logger)

    logger.info(f"Auto-detecting sections for s3://{bucket}/{document_key}, version={version}")

    try:
        # Use ClassesDiscovery which reads config from DynamoDB (selected version)
        discovery = ClassesDiscovery(
            input_bucket=bucket,
            input_prefix=document_key,
            region=os.environ.get('AWS_REGION'),
            version=version,
        )

        sections = discovery.auto_detect_sections(
            input_bucket=bucket,
            input_prefix=document_key,
        )

        logger.info(f"Auto-detected {len(sections)} sections")
        return json.dumps(sections)

    except Exception as e:
        logger.error(f"Error auto-detecting sections: {str(e)}")
        raise


def handle_upload_discovery_document(event, context):
    """
    Generates a presigned POST URL for S3 uploads and manages discovery job tracking.
    """
    try:
        # Extract variables from the event
        arguments = event.get('arguments', {})
        file_name = arguments.get('fileName')
        content_type = arguments.get('contentType', 'application/octet-stream')
        prefix = arguments.get('prefix', '')
        ground_truth_file_name = arguments.get('groundTruthFileName')
        version = arguments.get('version')
        page_ranges = arguments.get('pageRanges') or []
        page_labels = arguments.get('pageLabels') or []
        skip_job_creation = arguments.get('skipJobCreation', False)
        discovery_type = arguments.get('discoveryType', 'classes')
        # saveMode: 'replace' clears the version's discovered schema up front so
        # discovery rebuilds it; 'augment' (default) keeps existing classes.
        save_mode = arguments.get('saveMode') or 'augment'

        if not file_name:
            raise ValueError("fileName is required")

        # The 'default' config version is read-only — discovery must target a
        # user-created version so the built-in defaults are never overwritten.
        if version == 'default':
            raise ValueError(
                "The 'default' configuration version is read-only. Create a new version to save the discovered schema."
            )

        # Get bucket from arguments
        bucket_name = arguments.get('bucket')

        if not bucket_name:
            raise ValueError("bucket parameter is required")

        object_key, presigned_post = create_s3_signed_post_url(bucket_name, content_type, file_name, 'document', prefix)
        # usePostMethod is a STRING ("true") per the schema; the UI parses it via
        # usePostMethod.toLowerCase() === 'true'. AppSync coerced a bool to a
        # string, but the REST dispatcher passes JSON verbatim, so a bool would
        # reach the UI as `true` and break .toLowerCase(). Keep it a string.
        response = {
            'presignedUrl': json.dumps(presigned_post),
            'objectKey': object_key,
            'usePostMethod': 'true'
        }
        gt_object_key = None
        if ground_truth_file_name:
            gt_object_key, gt_presigned_post = create_s3_signed_post_url(bucket_name, content_type, ground_truth_file_name, 'groundtruth', prefix)
            response['groundTruthObjectKey'] = gt_object_key
            response['groundTruthPresignedUrl'] = json.dumps(gt_presigned_post)

        # Create discovery jobs — one per page range, or one for the whole document
        # skipJobCreation=True is used by the auto-detect sections flow which only needs
        # a presigned URL to upload the document, without creating any discovery jobs.
        if not skip_job_creation:
            # Replace mode: clear the target version's schema ONCE, before
            # enqueuing any jobs, so a multi-section submission (N jobs) rebuilds
            # the class list from scratch rather than each job clobbering the last.
            if save_mode == 'replace':
                _clear_version_schema(version, discovery_type=discovery_type)
            if page_ranges and len(page_ranges) > 0:
                logger.info(f"Creating {len(page_ranges)} discovery jobs for page ranges: {page_ranges}")
                for i, page_range in enumerate(page_ranges):
                    job_id = str(uuid.uuid4())
                    page_label = page_labels[i] if i < len(page_labels) else None
                    create_discovery_job(job_id, object_key, gt_object_key, version, page_range=page_range, class_name_hint=page_label)
            else:
                job_id = str(uuid.uuid4())
                create_discovery_job(job_id, object_key, gt_object_key, version, discovery_type=discovery_type)
        else:
            logger.info("skipJobCreation=True — returning presigned URL only, no jobs created")

        # Return the presigned POST data and object key
        return response
    
    except Exception as e:
        logger.error(f"Error generating presigned URL: {str(e)}")
        raise


def create_s3_signed_post_url(bucket_name, content_type, file_name, file_type, prefix):
    # Sanitize file name to avoid URL encoding issues
    sanitized_file_name = file_name.replace(' ', '_')
    # Build the object key with file type prefix
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    if prefix:
        object_key = f"{prefix}/{file_type}/{timestamp}_{sanitized_file_name}"
    else:
        object_key = f"{file_type}/{timestamp}_{sanitized_file_name}"

    # Constrain the target here, at the one choke point every presign path in this
    # module goes through, rather than at each caller. `bucket` and `prefix` are
    # request arguments and this function's role holds write on the discovery bucket,
    # so the request is bounded here or not at all. Same allow-list the read path in
    # get_file_contents_resolver and the write path in upload_resolver use, plus the
    # write-once key rule. Raises PermissionError -> HTTP 403.
    s3_targets.assert_write_target_allowed(
        bucket_name, object_key, ALLOWED_BUCKETS, logger=logger
    )

    # Generate a presigned POST URL for uploading
    logger.info(f"Generating presigned POST data for: {object_key} with content type: {content_type}")
    presigned_post = s3_client.generate_presigned_post(
        Bucket=bucket_name,
        Key=object_key,
        Fields={
            'Content-Type': content_type
        },
        Conditions=[
            ['content-length-range', 1, 104857600],  # 1 Byte to 100 MB
            {'Content-Type': content_type}
        ],
        ExpiresIn=900  # 15 minutes
    )
    logger.info(f"Generated presigned POST data: {json.dumps(presigned_post)}")
    return object_key, presigned_post


def create_discovery_job(job_id, document_key, ground_truth_key, version, discovery_type='classes', page_range=None, class_name_hint=None):
    """
    Create a new discovery job entry in DynamoDB.
    
    Args:
        job_id (str): Unique job identifier
        document_key (str): S3 key for the document file
        ground_truth_key (str): S3 key for the ground truth file
        version (str): Configuration version to use
        discovery_type (str): Type of discovery - "classes" or "rules"
        page_range (str, optional): Page range string (e.g., "1-3") for multi-section discovery
    """
    try:
        table_name = os.environ.get('DISCOVERY_TRACKING_TABLE')
        if not table_name:
            logger.warning("DISCOVERY_TRACKING_TABLE not configured, skipping job creation")
            return
        
        table = dynamodb.Table(table_name)

        #retrieve job from table
        item = table.get_item( Key={'jobId': job_id}).get('Item', None)
        if item is None:
            item = {
                'jobId': job_id,
                'status': 'PENDING',
                'createdAt': datetime.now().isoformat(),
                'updatedAt': datetime.now().isoformat(),
                'ExpiresAfter': int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
            }
        else:
            item['updatedAt'] = datetime.now().isoformat()
            document_key = item.get('documentKey', document_key)

        if document_key:
            item['documentKey'] = document_key

        if ground_truth_key:
            item['groundTruthKey'] = ground_truth_key
            
        if version:
            item['version'] = version

        if page_range:
            item['pageRange'] = page_range

        # Persist discovery type so the UI can distinguish policy-discovery
        # jobs (whose results live in policy_classes) from regular class
        # discovery (whose results live in classes).
        if discovery_type:
            item['discoveryType'] = discovery_type
            if discovery_type == 'rules':
                item['jobType'] = 'rules'
        
        table.put_item(Item=item)
        logger.info(f"Created discovery job: {job_id}" + (f" (pages {page_range})" if page_range else ""))
        
        send_discovery_message(job_id, document_key, ground_truth_key, version, discovery_type=discovery_type, page_range=page_range, class_name_hint=class_name_hint)
        
    except Exception as e:
        logger.error(f"Error creating discovery job: {str(e)}")
        # Don't fail the upload if job tracking fails

def send_discovery_message(job_id, document_key, ground_truth_key, version, discovery_type='classes', page_range=None, class_name_hint=None):
    """
    Send a message to the discovery processing queue.
    
    Args:
        job_id (str): Unique job identifier
        document_key (str): S3 key for the document file
        ground_truth_key (str): S3 key for the ground truth file
        version (str): Configuration version to use
        discovery_type (str): Type of discovery - "classes" or "rules"
        page_range (str, optional): Page range string (e.g., "1-3") for multi-section discovery
    """
    try:
        queue_url = os.environ.get('DISCOVERY_QUEUE_URL')
        if not queue_url:
            logger.warning("DISCOVERY_QUEUE_URL not configured, skipping message send")
            return
        
        message = {
            'jobId': job_id,
            'documentKey': document_key,
            'groundTruthKey': ground_truth_key,
            'discoveryType': discovery_type,
            'bucket': os.environ.get('DISCOVERY_BUCKET'),
            'version': version,
            'timestamp': datetime.now().isoformat()
        }

        if page_range:
            message['pageRange'] = page_range

        if class_name_hint:
            message['classNameHint'] = class_name_hint
        
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message)
        )
        
        logger.info(f"Sent discovery message for job: {job_id}")
        
    except Exception as e:
        logger.error(f"Error sending discovery message: {str(e)}")
        # Don't fail the upload if message sending fails


def handle_start_multi_doc_discovery(event, context):
    """
    Start a multi-document discovery pipeline via Step Functions.

    Creates a tracking entry in DynamoDB and starts the state machine execution.
    Supports two modes:
    - S3 path: directly analyze documents at an S3 location
    - Zip upload: documents were previously uploaded as a zip file
    """
    arguments = event.get('arguments', {})
    s3_bucket = arguments.get('s3Bucket')
    s3_prefix = arguments.get('s3Prefix', '')
    config_version = arguments.get('configVersion')
    zip_file_name = arguments.get('zipFileName')
    zip_file_size = arguments.get('zipFileSize')
    save_mode = arguments.get('saveMode') or 'augment'

    if not config_version:
        raise ValueError("configVersion is required")

    # The 'default' config version is read-only — discovery must target a
    # user-created version so the built-in defaults are never overwritten.
    if config_version == 'default':
        raise ValueError(
            "The 'default' configuration version is read-only. Create a new version to save the discovered schema."
        )

    if not s3_bucket and not zip_file_name:
        raise ValueError("Either s3Bucket/s3Prefix or zipFileName is required")

    # Replace mode: clear the version's classes before the Step Functions
    # pipeline runs, so the Save step (which merges each discovered class via
    # ClassesDiscovery._merge_and_save_class) rebuilds the list from scratch.
    if save_mode == 'replace':
        _clear_version_schema(config_version, discovery_type='classes')

    job_id = str(uuid.uuid4())
    bucket = s3_bucket or os.environ.get('DISCOVERY_BUCKET', '')
    is_zip_upload = bool(zip_file_name)
    prefix = s3_prefix

    # For zip uploads, the prefix is the zip file key in the discovery bucket.
    # If the caller passed the objectKey returned by uploadMultiDocDiscoveryZip
    # back as s3Prefix, use it verbatim. Otherwise reconstruct the SAME
    # deterministic key the upload mutation used (multi_doc_zip_key) — the web
    # UI only passes zipFileName here, so inventing a job-id-based path would
    # point at a location where nothing was uploaded and the Prepare step
    # would 404.
    if is_zip_upload:
        if not prefix:
            prefix = multi_doc_zip_key(zip_file_name)
        bucket = os.environ.get('DISCOVERY_BUCKET', '')

    logger.info(
        f"Starting multi-doc discovery job {job_id}: "
        f"bucket={bucket}, prefix={prefix}, "
        f"configVersion={config_version}, isZip={is_zip_upload}"
    )

    # Create tracking entry in DynamoDB
    table_name = os.environ.get('DISCOVERY_TRACKING_TABLE')
    if table_name:
        table = dynamodb.Table(table_name)
        now = datetime.now(timezone.utc)
        item = {
            'jobId': job_id,
            'status': 'QUEUED',
            'jobType': 'multi-document',
            'currentStep': 'Queued',
            'version': config_version,
            'createdAt': now.isoformat(),
            'updatedAt': now.isoformat(),
            'ExpiresAfter': int((now + timedelta(days=7)).timestamp()),
        }
        if s3_bucket:
            item['documentKey'] = f"s3://{bucket}/{prefix}"
        if zip_file_name:
            item['documentKey'] = zip_file_name
        table.put_item(Item=item)

    # Start Step Functions execution
    state_machine_arn = os.environ.get('MULTI_DOC_DISCOVERY_STATE_MACHINE_ARN')
    if not state_machine_arn:
        raise ValueError("MULTI_DOC_DISCOVERY_STATE_MACHINE_ARN not configured")

    sfn_input = {
        'jobId': job_id,
        'bucket': bucket,
        'prefix': prefix,
        'configVersion': config_version,
        'isZipUpload': is_zip_upload,
    }

    execution = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=f"multi-doc-{job_id[:8]}",
        input=json.dumps(sfn_input),
    )

    logger.info(f"Started Step Functions execution: {execution['executionArn']}")

    return {
        'jobId': job_id,
        'status': 'QUEUED',
        'configVersion': config_version,
        'currentStep': 'Queued',
        'createdAt': datetime.now(timezone.utc).isoformat(),
        'updatedAt': datetime.now(timezone.utc).isoformat(),
    }


def multi_doc_zip_key(file_name):
    """
    Build the deterministic S3 key for a multi-doc discovery zip upload.

    The upload mutation (uploadMultiDocDiscoveryZip) and the start mutation
    (startMultiDocDiscovery) must agree on this key: the UI uploads the zip in
    one call, then starts the pipeline in a separate call passing only
    zipFileName. Deriving the key purely from the sanitized file name lets both
    sides reconstruct the same location without threading the object key
    through the UI.

    Args:
        file_name (str): Original zip file name from the UI.

    Returns:
        str: S3 object key under the discovery bucket.
    """
    sanitized = file_name.replace(' ', '_')
    return f"multi-doc-discovery/uploads/{sanitized}"


def handle_upload_multi_doc_discovery_zip(event, context):
    """
    Generate a presigned PUT URL for uploading a zip file for multi-doc discovery.

    The UI uploads with an HTTP PUT (fetch(url, {method: 'PUT', body: file})),
    so this returns a plain presigned PUT URL — not a presigned POST form.
    The object key is deterministic (see multi_doc_zip_key) so the subsequent
    startMultiDocDiscovery call locates the same object.

    Args:
        event (dict): Resolver event with arguments fileName, fileSize,
            configVersion.
        context: Lambda context (unused).

    Returns:
        dict: TestSetUploadResponse-shaped dict with testSetId, presignedUrl
            (a plain URL string), and objectKey.
    """
    arguments = event.get('arguments', {})
    file_name = arguments.get('fileName')
    file_size = arguments.get('fileSize', 0)
    config_version = arguments.get('configVersion')

    if not file_name:
        raise ValueError("fileName is required")
    if not file_name.lower().endswith('.zip'):
        raise ValueError("File must be a .zip file")
    if not config_version:
        raise ValueError("configVersion is required")

    bucket = os.environ.get('DISCOVERY_BUCKET', '')
    object_key = multi_doc_zip_key(file_name)

    logger.info(
        f"Generating presigned PUT URL for multi-doc zip: "
        f"bucket={bucket}, key={object_key}, size={file_size}"
    )

    # Presigned PUT URL to match the UI's fetch(..., {method: 'PUT'}) upload.
    # ContentType must match the Content-Type header the UI sends so the
    # request signature validates.
    presigned_url = s3_client.generate_presigned_url(
        ClientMethod='put_object',
        Params={
            'Bucket': bucket,
            'Key': object_key,
            'ContentType': 'application/zip',
        },
        ExpiresIn=900,  # 15 minutes
    )

    return {
        'testSetId': object_key,  # Reuses TestSetUploadResponse type
        'presignedUrl': presigned_url,
        'objectKey': object_key,
    }


