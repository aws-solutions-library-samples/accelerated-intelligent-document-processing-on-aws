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
from idp_common.api_adapter import ResourceAccessDenied
from idp_common.config_scope import (
    ScopeLookupError,
    caller_email_from_claims,
    caller_sub_from_claims,
    resolve_allowed_config_versions,
    scope_allows,
)
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

# Per-container cache of callers' configuration scopes (see config_scope).
_user_scope_cache: dict = {}
_USER_SCOPE_CACHE_TTL = 60  # seconds


def _caller_config_scope(event):
    """The caller's ``allowedConfigVersions``, or None when unrestricted.

    Admins are unrestricted and never looked up, as in every other resolver that
    applies this rule. For everyone else the shared lookup fails CLOSED: a scope
    that cannot be evaluated is refused, never read as "unrestricted".
    """
    if _caller_in_groups(event, ("Admin",)):
        return None
    claims = (event.get("identity") or {}).get("claims") or {}
    try:
        return resolve_allowed_config_versions(
            caller_email_from_claims(claims),
            caller_sub=caller_sub_from_claims(claims),
            users_table_name=os.environ.get("USERS_TABLE_NAME", ""),
            dynamodb=dynamodb,
            cache=_user_scope_cache,
            cache_ttl=_USER_SCOPE_CACHE_TTL,
        )
    except ScopeLookupError as e:
        logger.error(f"Denying discovery request: configuration scope could not be resolved: {e}")
        raise PermissionError(
            "Unauthorized: your configuration scope could not be verified"
        ) from e


def _assert_profile_in_scope(event, version, missing_means_active=False):
    """Refuse a configuration profile outside the caller's allowedConfigVersions.

    Every discovery operation reads the named profile's settings and most of them
    write the discovered schema back into it, so the rule is the one
    ``updateConfiguration`` and ``setActiveVersion`` apply: a scoped caller may name
    only a profile their scope admits, and an empty or missing name is refused for
    a scoped caller because it cannot be shown to be in scope.

    ``missing_means_active`` is for the one read-only operation that accepts no
    version and then reads the active profile: the check is then made on the
    profile that is actually read.
    """
    allowed = _caller_config_scope(event)
    if allowed is None:
        return
    if not version and missing_means_active:
        version = _active_version()
    if not scope_allows(allowed, version):
        logger.warning("Refusing discovery request: configuration profile is outside the caller's scope")
        raise ResourceAccessDenied(
            f"Unauthorized: configuration profile '{version}' is not in your allowed scope"
        )


def _assert_source_in_scope(event, bucket, prefix, is_zip_upload):
    """Refuse a scoped caller a multi-document source outside their scope.

    ``startMultiDocDiscovery`` reads every document under ``s3Bucket``/``s3Prefix``
    and saves a schema, example values included, into a profile the caller can
    read, so the documents it reads are held to the caller's scope as well as the
    profile it saves into. For a caller with a configuration scope:

    * the discovery bucket (including an uploaded zip) is allowed: it is the
      shared Admin/Author discovery workspace — every user's discovery uploads and
      the documents extracted from multi-document zips — and nothing in it records
      a profile or an uploader, so there is nothing to check it against (see the
      Known Limitations in docs/rbac.md);
    * an Input-bucket prefix is allowed only when a configuration prefix mapping
      covers the whole prefix (the prefix lies inside a ``prefix``-kind mapping),
      the mapping's profile is in scope, the mapping lets no document's own
      metadata pick another profile (``metadataPrecedence`` other than
      ``metadata``), and no more specific mapping under the prefix names a profile
      outside scope;
    * anything else — the Test Set bucket, an unmapped Input prefix, any other
      bucket — is refused, because nothing maps it to a profile or test set the
      caller holds. A mapping table that cannot be read refuses too.

    Admins and unscoped callers are not affected.
    """
    allowed = _caller_config_scope(event)
    if allowed is None:
        return
    discovery_bucket = os.environ.get("DISCOVERY_BUCKET", "")
    if is_zip_upload or not bucket or bucket == discovery_bucket:
        return
    refusal = ResourceAccessDenied(
        "Unauthorized: that document source is outside your allowed configuration scope"
    )
    input_bucket = os.environ.get("SCOPED_SOURCE_INPUT_BUCKET", "")
    if not input_bucket or bucket != input_bucket or not prefix:
        logger.warning("Refusing multi-document discovery: source bucket is not mapped to the caller's scope")
        raise refusal

    from idp_common.config.prefix_mappings import (
        MATCH_PREFIX,
        PRECEDENCE_METADATA,
        PrefixMappingStore,
        canonical_key,
        find_match,
    )

    try:
        table = dynamodb.Table(os.environ["CONFIGURATION_TABLE_NAME"])
        mappings = PrefixMappingStore(table).list()
    except Exception as e:  # noqa: BLE001 — an unreadable mapping cannot admit
        logger.error(f"Refusing multi-document discovery: prefix mappings unreadable: {e}")
        raise refusal from e

    wanted = canonical_key(prefix)
    governing = find_match(prefix, mappings)
    if (
        governing is None
        or governing["matchKind"] != MATCH_PREFIX
        or governing["metadataPrecedence"] == PRECEDENCE_METADATA
        or not scope_allows(allowed, governing["configProfile"])
    ):
        logger.warning("Refusing multi-document discovery: the source prefix is not mapped into the caller's scope")
        raise refusal
    for entry in mappings:
        if not entry.get("enabled") or not entry.get("configProfile"):
            continue
        if not canonical_key(entry["prefix"]).startswith(wanted):
            continue
        if entry.get("metadataPrecedence") == PRECEDENCE_METADATA or not scope_allows(
            allowed, entry["configProfile"]
        ):
            logger.warning("Refusing multi-document discovery: a mapping under the source prefix is outside the caller's scope")
            raise refusal


def _assert_replace_allowed(version):
    """Refuse ``saveMode: replace`` against the profile documents are processed under.

    Replace clears the profile's discovered schema before discovery runs, so on the
    active profile every document arriving during the run would be classified
    against an empty class list. Augment is unaffected, and replace on any other
    profile is unaffected.
    """
    if version == _active_version():
        raise ValueError(
            f"'Replace' cannot be used on the active configuration profile '{version}'. "
            "Use 'Augment', or run discovery into another profile and activate it afterwards."
        )


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

    # The profile's discovery settings (model, prompt) are read for this call. With
    # no version the active profile is read, which is a profile name too, so the
    # check runs on what is actually read.
    _assert_profile_in_scope(event, version, missing_means_active=True)

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


def _active_version():
    from idp_common.config.configuration_manager import ConfigurationManager

    return ConfigurationManager().resolve_active_version(raise_errors=True)


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

        # A discovery job writes its result into this profile, so it has to be
        # named: falling back to whichever profile is active would write into a
        # profile the caller never chose. The presign-only call (skipJobCreation)
        # creates no job and writes nothing.
        if not skip_job_creation and not version:
            raise ValueError(
                "version is required: name the configuration profile to save the discovered schema to"
            )
        if version or not skip_job_creation:
            _assert_profile_in_scope(event, version)

        # The 'default' config version is read-only — discovery must target a
        # user-created version so the built-in defaults are never overwritten.
        if version == 'default':
            raise ValueError(
                "The 'default' configuration version is read-only. Create a new version to save the discovered schema."
            )

        if save_mode == 'replace' and not skip_job_creation:
            _assert_replace_allowed(version)

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

    _assert_profile_in_scope(event, config_version)

    # The 'default' config version is read-only — discovery must target a
    # user-created version so the built-in defaults are never overwritten.
    if config_version == 'default':
        raise ValueError(
            "The 'default' configuration version is read-only. Create a new version to save the discovered schema."
        )

    if not s3_bucket and not zip_file_name:
        raise ValueError("Either s3Bucket/s3Prefix or zipFileName is required")

    _assert_source_in_scope(event, s3_bucket, s3_prefix, bool(zip_file_name))

    # Replace mode: clear the version's classes before the Step Functions
    # pipeline runs, so the Save step (which merges each discovered class via
    # ClassesDiscovery._merge_and_save_class) rebuilds the list from scratch.
    if save_mode == 'replace':
        _assert_replace_allowed(config_version)
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
        # The zip's key is derived from its name, never taken from the caller: the
        # Prepare step deletes the zip once it is extracted, so a caller-chosen key
        # would name another upload for deletion. A caller passing back the
        # objectKey uploadMultiDocDiscoveryZip returned passes the same key.
        expected = multi_doc_zip_key(zip_file_name)
        if prefix and prefix != expected:
            raise ValueError(
                "s3Prefix must be omitted for a zip upload, or be the objectKey "
                "uploadMultiDocDiscoveryZip returned for this zipFileName"
            )
        prefix = expected
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

    _assert_profile_in_scope(event, config_version)

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


