# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import html
import json
import logging
import mimetypes
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Set up logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger('idp_common.bedrock.client').setLevel(os.environ.get("BEDROCK_LOG_LEVEL", "INFO"))
# Get LOG_LEVEL from environment variable with INFO as default

# Force Signature Version 4 for presigned URLs. The IDP buckets are encrypted
# with SSE-KMS, and S3 rejects SigV2-signed requests for KMS-encrypted objects
# ("Requests specifying Server Side Encryption with AWS KMS managed keys require
# AWS Signature Version 4"). Also pin the regional endpoint so the signed host
# matches the bucket's region.
s3_client = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION"),
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)


# Bucket allow-list for get_file_contents.
# --------------------------------------------
# The AppSync schema for getFileContents accepts an arbitrary `s3Uri`
# argument from any authenticated Cognito user. In the default
# single-tenant deployment, all authenticated users are trusted to read
# any document processed by this stack — that is by design. However,
# the Lambda's execution role has S3Read permission on several IDP
# buckets (input, output, baseline, configuration, reporting), and if
# a user passed in a completely unrelated S3 URI (for example an
# object from another stack or a third-party bucket the execution
# role happens to be able to read via a cross-account policy), the
# resolver would happily proxy its contents.
#
# To prevent use of this resolver as a generic S3-read gadget, we
# restrict the accepted buckets to those explicitly passed in via
# environment variables (set by `nested/api-resolvers/template.yaml` from
# the main stack's bucket refs). If the env vars are unset
# (unusual — older deployments that haven't been redeployed), we
# fail open with a warning to preserve functionality, and operators
# are expected to redeploy to pick up the new template.
_ALLOWED_BUCKETS_ENV = {
    "INPUT_BUCKET",
    "OUTPUT_BUCKET",
    "CONFIGURATION_BUCKET",
    "EVALUATION_BASELINE_BUCKET",
    "REPORTING_BUCKET",
    "TEST_SET_BUCKET",
    "DISCOVERY_BUCKET",
    "WORKING_BUCKET",
}
ALLOWED_BUCKETS = {
    os.environ[name]
    for name in _ALLOWED_BUCKETS_ENV
    if os.environ.get(name)
}


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

def _validate_bucket(bucket: str) -> None:
    """Reject the request if `bucket` is not in the allow-list.

    No-op (with a warning log) when ALLOWED_BUCKETS is empty, which
    happens if none of the bucket env vars are set (legacy deployment
    path). Production template always sets at least INPUT_BUCKET and
    OUTPUT_BUCKET.
    """
    if not ALLOWED_BUCKETS:
        logger.warning(
            "get_file_contents_resolver: no bucket allow-list configured "
            "(all of %s are unset). Skipping bucket validation. Redeploy "
            "the stack to pick up the tightened template.",
            sorted(_ALLOWED_BUCKETS_ENV),
        )
        return
    if bucket not in ALLOWED_BUCKETS:
        # Avoid echoing the bucket name back to the client (minor
        # information-disclosure hardening).
        logger.warning(
            "get_file_contents_resolver: rejecting request for bucket "
            "%r (not in allow-list %s).",
            bucket,
            sorted(ALLOWED_BUCKETS),
        )
        raise Exception("Unauthorized: requested bucket is not accessible from this deployment.")


# Presigned GET URLs expire after this many seconds. Short-lived: the UI
# fetches immediately after receiving the URL.
_PRESIGNED_URL_EXPIRY_SECONDS = 300


def _parse_and_validate_uri(event):
    """Parse `s3Uri`/`versionId` from the GraphQL args, validate, and return
    ``(bucket, key, version_id)``. Shared by both handler branches."""
    s3_uri = event['arguments']['s3Uri']
    # Optional S3 object VersionId — used to fetch the exact bytes of a
    # prior document version (see document version history). None fetches
    # the current object.
    version_id = event['arguments'].get('versionId')
    logger.info(f"Processing S3 URI: {s3_uri}"
                + (f" (versionId={version_id})" if version_id else ""))

    # Parse S3 URI to get bucket and key. The URI must be of the
    # form `s3://<bucket>/<key>`. We intentionally do NOT accept
    # virtual-hosted-style HTTPS URIs here because they require a
    # completely different parsing path.
    #
    # Parse with a plain string split rather than urllib.parse.urlparse:
    # object keys may contain '#' (e.g. "Borrowing_Notice_#2.pdf/pages/1/
    # result.json"), which urlparse treats as a URL fragment delimiter and
    # silently truncates, producing a wrong key and a NoSuchKey error.
    if not s3_uri.startswith("s3://"):
        raise Exception("Invalid S3 URI: expected s3://<bucket>/<key>")
    # Strip scheme, then split once into bucket and key.
    parts = s3_uri[len("s3://"):].split("/", 1)
    if len(parts) < 2 or not parts[0]:
        raise Exception("Invalid S3 URI: expected s3://<bucket>/<key>")
    bucket, key = parts
    if not key:
        raise Exception("Invalid S3 URI: key is required")

    # Enforce that the requested bucket belongs to this IDP stack's
    # known bucket set — prevents use of this resolver as a generic
    # S3-read gadget.
    _validate_bucket(bucket)

    if version_id == "null":
        version_id = None
    return bucket, key, version_id


# Content types a browser renders natively; anything else is left to download.
_INLINE_RENDERABLE_TYPES = ("application/pdf",)
_INLINE_RENDERABLE_PREFIXES = ("image/", "text/")

# Types a browser renders by EXECUTING them. Served inline from the bucket origin,
# an uploaded evil.html or evil.svg would run script there instead of downloading —
# so these are forced to download even though their prefixes are renderable. The
# bucket is a separate origin from the app and an authenticated uploader is
# required, which is why this is defence in depth rather than a live hole.
_NEVER_INLINE_TYPES = frozenset(
    {
        "text/html",
        "text/xml",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/javascript",
    }
)


def _is_executable_type(content_type):
    """True when a browser would run script while rendering this type."""
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() in _NEVER_INLINE_TYPES


def _is_inline_renderable(content_type):
    """True when a browser can display this type in-page without executing it."""
    if not content_type:
        return False
    if _is_executable_type(content_type):
        return False
    lowered = content_type.split(";")[0].strip().lower()
    return lowered in _INLINE_RENDERABLE_TYPES or lowered.startswith(
        _INLINE_RENDERABLE_PREFIXES
    )


def _handle_presigned_url(event):
    """Return a short-lived presigned GET URL for the requested object.

    Used for files too large to return through :func:`_handle_file_contents`
    (Lambda's synchronous response is capped at 6 MB). The browser fetches the
    URL directly from S3, so the file bytes never traverse this Lambda.
    """
    bucket, key, version_id = _parse_and_validate_uri(event)
    logger.info(f"Generating presigned URL for bucket: {bucket}, key: {key}")

    # HEAD the object first so we (a) surface NoSuchKey as a clean error before
    # minting a URL, and (b) return size/contentType the UI uses to decide how
    # to handle the content.
    head_kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        head_kwargs["VersionId"] = version_id
    head = s3_client.head_object(**head_kwargs)

    content_type = head.get('ContentType', '')
    if not content_type or content_type in ('binary/octet-stream', 'application/octet-stream'):
        content_type = mimetypes.guess_type(key)[0] or 'text/plain'

    presign_params = {"Bucket": bucket, "Key": key}
    if version_id:
        presign_params["VersionId"] = version_id

    # Override the response headers instead of trusting stored metadata: objects
    # uploaded without a ContentType are binary/octet-stream, which a browser
    # downloads rather than renders. Overriding here also repairs objects already
    # stored with the wrong type.
    presign_params["ResponseContentType"] = content_type
    if _is_inline_renderable(content_type):
        presign_params["ResponseContentDisposition"] = "inline"
    elif _is_executable_type(content_type):
        # Must be explicit: with no disposition the browser decides from the
        # Content-Type alone, so text/html would still render — merely declining to
        # say "inline" is not the same as saying "attachment".
        presign_params["ResponseContentDisposition"] = "attachment"

    presigned_url = s3_client.generate_presigned_url(
        "get_object",
        Params=presign_params,
        ExpiresIn=_PRESIGNED_URL_EXPIRY_SECONDS,
    )

    logger.info(f"Generated presigned URL (size={head['ContentLength']}, "
                f"contentType={content_type})")
    return {
        'presignedUrl': presigned_url,
        'contentType': content_type,
        'size': head['ContentLength'],
    }


def _handle_file_contents(event):
    """Read a file's contents from S3 and return them inline (subject to
    Lambda's 6 MB synchronous response cap)."""
    bucket, key, version_id = _parse_and_validate_uri(event)

    logger.info(f"Fetching from bucket: {bucket}, key: {key}")

    # Get object from S3 (optionally a specific version)
    get_kwargs = {"Bucket": bucket, "Key": key}
    if version_id:
        get_kwargs["VersionId"] = version_id
    response = s3_client.get_object(**get_kwargs)

    # Get content type from S3 response or infer from file extension
    content_type = response.get('ContentType', '')
    if not content_type or content_type == 'binary/octet-stream' or content_type == 'application/octet-stream':
        content_type = mimetypes.guess_type(key)[0] or 'text/plain'

    logger.info(f"File content type: {content_type}")
    logger.info(f"File size: {response['ContentLength']}")

    # Read file content with error handling for different encodings
    try:
        # First try UTF-8
        file_content = response['Body'].read().decode('utf-8')
    except UnicodeDecodeError:
        # If UTF-8 fails, try with error handling
        try:
            response['Body'].seek(0)  # Reset the file pointer
            file_content = response['Body'].read().decode('utf-8', errors='replace')
            logger.warning("File content contained invalid UTF-8 characters that were replaced")
        except Exception as decode_error:
            # Last resort - if it's a binary file format with text extension
            logger.error(f"Failed to decode content with error handling: {str(decode_error)}")
            return {
                'content': "This file contains binary content that cannot be displayed as text.",
                'contentType': content_type,
                'size': response['ContentLength'],
                'isBinary': True
            }

    # For HTML content, escape the HTML to prevent XSS
    if content_type.startswith('text/html') or content_type.startswith('application/xhtml+xml'):
        file_content = html.escape(file_content)

    # Return both content and metadata
    return {
        'content': file_content,
        'contentType': content_type,
        'size': response['ContentLength'],
        'isBinary': False
    }


def handler(event, context):
    """Resolver for both ``getFileContents`` and ``getFilePresignedUrl``.

    Routes on the GraphQL ``fieldName``:
      - ``getFilePresignedUrl`` -> return a presigned GET URL (no size limit;
        browser fetches bytes directly from S3).
      - ``getFileContents`` (default) -> return the file bytes inline (capped
        at Lambda's 6 MB synchronous response limit).

    Parameters:
        event (dict): Lambda event data containing GraphQL arguments
        context (object): Lambda context

    Returns:
        dict: Field-shaped response (see the two handlers above).

    Raises:
        Exception: Various exceptions related to S3 operations or invalid input
    """
    try:
        logger.info(f"Received event: {json.dumps(_sanitize_for_log(event))}")

        field_name = (event.get('info') or {}).get('fieldName', 'getFileContents')
        if field_name == 'getFilePresignedUrl':
            return _handle_presigned_url(event)
        return _handle_file_contents(event)

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']
        logger.error(f"S3 ClientError: {error_code} - {error_message}")

        # NoSuchKey (get_object) and 404 (head_object) both mean "missing object".
        if error_code in ('NoSuchKey', '404'):
            raise Exception("File not found")
        elif error_code in ('NoSuchBucket', 'NoSuchVersion'):
            raise Exception(f"Error accessing S3: {error_message}")
        else:
            raise Exception(f"Error accessing S3: {error_message}")

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise Exception(f"Error fetching file: {str(e)}")