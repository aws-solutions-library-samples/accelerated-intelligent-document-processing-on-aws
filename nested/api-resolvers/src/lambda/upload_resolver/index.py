# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# src/lambda/upload_resolver/index.py

import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# Two S3 clients: one for the presigned URLs handed to the browser, one for this
# resolver's own S3 API calls. See the long note in test_set_resolver/index.py —
# same reasoning, same failure mode. S3_ENDPOINT_URL is the S3 interface VPC
# endpoint host; presigning with it is correct (offline signing, the URL is for
# the browser), but calling S3 with it from this NON-VPC-attached function hangs
# on private addresses it cannot route to, which the 29s REST API Gateway
# integration ceiling turns into an unexplained 504.
#
# Data-plane calls here are listSampleDocuments / uploadSampleDocument
# (get_object on the manifest, list_objects_v2, copy_object).
_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
_s3_addressing = "virtual" if _s3_endpoint_url else "path"

# Browser-facing presigned POST/GET URLs only.
s3_presign_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": _s3_addressing},
)
s3_presign_client = boto3.client(
    "s3", endpoint_url=_s3_endpoint_url, config=s3_presign_config
)

# Bounds for the data-plane client, applied ONLY where there is private-network
# S3 configuration to get wrong (see the fuller note in test_set_resolver). A
# public deployment has no endpoint or route to misconfigure and keeps botocore's
# stock timeouts, so this change cannot introduce a new way for it to fail.
# 2 total attempts, worst case 2 x (3s connect + 8s read) = 22s, inside the 29s
# API Gateway integration budget.
_S3_DATAPLANE_BOUNDS = (
    {
        "connect_timeout": 3,
        "read_timeout": 8,
        "retries": {"mode": "standard", "max_attempts": 1},
    }
    if _s3_endpoint_url
    else {}
)

# Every actual S3 API call this resolver makes.
s3_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    **_S3_DATAPLANE_BOUNDS,
)
s3_client = boto3.client("s3", config=s3_config)

# --- inline log sanitizer ---------------------------------------------------
# Minimal inline redactor. Kept here rather than importing from idp_common to
# avoid adding a Lambda Layer dependency to this resolver. If this file grows
# to need idp_common anyway, promote to
# `from idp_common.utils.log_sanitizer import sanitize_event_for_logging`.
_LOG_SENSITIVE_KEYS = (
    "password", "secret", "token", "authorization", "apikey", "api_key",
    "cookie", "credential", "claims", "identity",
)


def _sanitize_for_log(obj):
    """Deep-copy `obj` redacting values whose keys match the denylist."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in _LOG_SENSITIVE_KEYS):
                out[k] = "***REDACTED***" if v is not None else None
            else:
                out[k] = _sanitize_for_log(v)
        return out
    if isinstance(obj, list):
        return [_sanitize_for_log(v) for v in obj]
    return obj


def _caller_in_groups(event, allowed):
    """Defense-in-depth RBAC check against the caller's Cognito groups.

    The schema restricts this field via @aws_cognito_user_pools(cognito_groups),
    but we also enforce the group server-side so the operation is never reachable
    by an unauthorized caller even if the schema directive is missing or
    misconfigured (e.g. the prior @aws_auth directive, which AppSync silently
    ignores on a multi-auth API).
    """
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))

def handler(event, context=None):
    """Dispatch upload-related resolver operations by GraphQL field name.

    Serves ``uploadDocument`` (presigned POST for local uploads),
    ``listSampleDocuments`` (read the bundled samples manifest), and
    ``uploadSampleDocument`` (server-side copy of a bundled sample into the
    InputBucket). ``uploadDocument`` remains the default when no field name is
    present so existing callers are unaffected.
    """
    logger.info(f"Received event: {json.dumps(_sanitize_for_log(event))}")

    field_name = (event.get("info") or {}).get("fieldName") or "uploadDocument"

    if field_name == "listSampleDocuments":
        return _handle_list_sample_documents(event)
    if field_name == "uploadSampleDocument":
        return _handle_upload_sample_document(event)
    return _handle_upload_document(event)


def _handle_upload_document(event):
    """Generate a presigned POST URL for a local-file S3 upload."""
    try:
        # Defense-in-depth: uploadDocument is an Admin+Author operation.
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: uploadDocument requires Admin or Author group"
            )

        # Extract variables from the event
        arguments = event.get('arguments', {})
        file_name = arguments.get('fileName')
        content_type = arguments.get('contentType', 'application/octet-stream')
        prefix = arguments.get('prefix', '')
        version = arguments.get('version')  # Optional version parameter
        revision = arguments.get('revision')  # Optional revision of that version
        
        if not file_name:
            raise ValueError("fileName is required")
        
        # Get bucket from arguments or fallback to INPUT_BUCKET if needed by patterns
        bucket_name = arguments.get('bucket')
        
        if not bucket_name and os.environ.get('INPUT_BUCKET'):
            # Support legacy pattern usage that relies on INPUT_BUCKET
            bucket_name = os.environ.get('INPUT_BUCKET')
            logger.info(f"Using INPUT_BUCKET fallback: {bucket_name}")
        elif not bucket_name:
            raise ValueError("bucket parameter is required when INPUT_BUCKET is not configured")
        
        # Sanitize file name to avoid URL encoding issues
        sanitized_file_name = file_name.replace(' ', '_')
        
        # Build the object key - only use prefix if provided
        if prefix:
            object_key = f"{prefix}/{sanitized_file_name}"
        else:
            object_key = sanitized_file_name
        
        # Generate a presigned POST URL for uploading
        logger.info(f"Generating presigned POST data for: {object_key} with content type: {content_type}")
        
        # Prepare fields and conditions
        fields = {'Content-Type': content_type}
        conditions = [
            ['content-length-range', 1, 104857600],  # 1 Byte to 100 MB
            {'Content-Type': content_type}
        ]
        
        # Add version as metadata
        if version:
            fields['x-amz-meta-config-version'] = version
            conditions.append({'x-amz-meta-config-version': version})
            # A revision only means something in the context of a profile, so it
            # is only stamped when one was chosen. The queue processor pins the
            # profile's current revision when this is absent.
            if revision is not None:
                fields['x-amz-meta-config-revision'] = str(revision)
                conditions.append({'x-amz-meta-config-revision': str(revision)})
        
        # Presign client: this URL goes to the browser, so it must carry the
        # VPC-endpoint host in private deployments.
        presigned_post = s3_presign_client.generate_presigned_post(
            Bucket=bucket_name,
            Key=object_key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=900  # 15 minutes
        )
        
        logger.info(f"Generated presigned POST data: {json.dumps(presigned_post)}")
        
        # Return the presigned POST data and object key.
        # usePostMethod is a STRING ("true") per the schema (PresignedUploadUrl.
        # usePostMethod: String!) and the UI parses it via
        # usePostMethod.toLowerCase() === 'true'. AppSync used to coerce a bool
        # to a string; the REST dispatcher passes JSON through verbatim, so a
        # bool here reaches the UI as `true` and breaks .toLowerCase(). Keep it
        # a string.
        return {
            'presignedUrl': json.dumps(presigned_post),
            'objectKey': object_key,
            'usePostMethod': 'true'
        }
    
    except Exception as e:
        logger.error(f"Error generating presigned URL: {str(e)}")
        raise


# Samples manifest key within the ConfigurationBucket (mirrors the publish-time
# _SAMPLES_MANIFEST_FILE / SAMPLES_MANIFEST_KEY default).
_SAMPLES_MANIFEST_KEY = os.environ.get(
    "SAMPLES_MANIFEST_KEY", "config_library/samples-manifest.json"
)


def _load_samples_manifest():
    """Read and parse config_library/samples-manifest.json from the ConfigurationBucket."""
    config_bucket = os.environ.get("CONFIGURATION_BUCKET")
    if not config_bucket:
        raise ValueError("CONFIGURATION_BUCKET is not configured")
    obj = s3_client.get_object(Bucket=config_bucket, Key=_SAMPLES_MANIFEST_KEY)
    manifest = json.loads(obj["Body"].read())
    return config_bucket, manifest.get("samples", [])


def _handle_list_sample_documents(event):
    """Return the bundled sample documents from the manifest (Admin/Author/Viewer)."""
    try:
        if not _caller_in_groups(event, ("Admin", "Author", "Viewer")):
            raise PermissionError(
                "Unauthorized: listSampleDocuments requires Admin, Author, or Viewer group"
            )
        _, samples = _load_samples_manifest()
        return {"success": True, "samples": samples, "error": None}
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error listing sample documents: {str(e)}")
        return {"success": False, "samples": None, "error": str(e)}


def _handle_upload_sample_document(event):
    """Copy a bundled sample from the ConfigurationBucket into the InputBucket.

    For ``kind=document`` copies the single object; for ``kind=batch`` copies
    every document under the sample's prefix. Stamps the config version as
    object metadata (config-version) so downstream processing selects the right
    configuration, mirroring the presigned-POST x-amz-meta-config-version path.
    The InputBucket "Object Created" EventBridge rule then drives processing.
    """
    try:
        if not _caller_in_groups(event, ("Admin", "Author")):
            raise PermissionError(
                "Unauthorized: uploadSampleDocument requires Admin or Author group"
            )

        arguments = event.get("arguments", {})
        sample_id = arguments.get("sampleId")
        prefix = (arguments.get("prefix") or "").strip("/")
        version = arguments.get("version")
        revision = arguments.get("revision")
        if not sample_id:
            raise ValueError("sampleId is required")

        input_bucket = os.environ.get("INPUT_BUCKET")
        if not input_bucket:
            raise ValueError("INPUT_BUCKET is not configured")

        config_bucket, samples = _load_samples_manifest()
        sample = next((s for s in samples if s.get("id") == sample_id), None)
        if sample is None:
            raise ValueError(f"Unknown sampleId: {sample_id}")

        s3_key = sample.get("s3Key", "")
        kind = sample.get("kind", "document")

        # Resolve the source object keys within the ConfigurationBucket.
        if kind == "batch":
            source_prefix = s3_key.rstrip("/") + "/"
            paginator = s3_client.get_paginator("list_objects_v2")
            source_keys = [
                obj["Key"]
                for page in paginator.paginate(
                    Bucket=config_bucket, Prefix=source_prefix
                )
                for obj in page.get("Contents", [])
                if not obj["Key"].endswith("/")
            ]
        else:
            source_keys = [s3_key]

        if not source_keys:
            raise ValueError(f"No source files found for sample: {sample_id}")

        extra_args = {}
        if version:
            extra_args["Metadata"] = {"config-version": version}
            if revision is not None:
                extra_args["Metadata"]["config-revision"] = str(revision)
            extra_args["MetadataDirective"] = "REPLACE"

        object_keys = []
        for source_key in source_keys:
            base_name = os.path.basename(source_key)
            target_key = f"{prefix}/{base_name}" if prefix else base_name
            s3_client.copy_object(
                CopySource={"Bucket": config_bucket, "Key": source_key},
                Bucket=input_bucket,
                Key=target_key,
                **extra_args,
            )
            object_keys.append(target_key)

        logger.info(
            f"Copied {len(object_keys)} sample file(s) for '{sample_id}' to "
            f"{input_bucket}"
        )
        return {"success": True, "objectKeys": object_keys, "error": None}
    except PermissionError:
        raise
    except Exception as e:
        logger.error(f"Error uploading sample document: {str(e)}")
        return {"success": False, "objectKeys": None, "error": str(e)}
