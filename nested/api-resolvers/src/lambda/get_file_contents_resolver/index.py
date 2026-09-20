# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import html
import json
import logging
import mimetypes
import os

import boto3
import s3_targets
from botocore.config import Config
from botocore.exceptions import ClientError
from log_sanitizer import sanitize_event_for_logging

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
# The schema for getFileContents accepts an arbitrary `s3Uri` from any
# authenticated caller holding a group, and this function's execution role can read
# several IDP buckets. The allow-list is what stops the operation being a generic
# S3-read gadget for whatever that role reaches.
#
# The rule itself lives in `s3_targets`, shared byte-for-byte with the write paths in
# upload_resolver and discovery_upload_resolver — one allow-list, not three that
# drift. `test_s3_targets_vendored.py` fails if a copy diverges. This path checks the
# BUCKET only: the write-once key rule in that module is for writes, and reading a
# revision body or a run manifest is legitimate.
_ALLOWED_BUCKETS_ENV = s3_targets.BUCKET_ENV_NAMES
ALLOWED_BUCKETS = s3_targets.resolve_allowed_buckets()


def _validate_bucket(bucket: str) -> None:
    """Reject the request if `bucket` is not one this deployment owns.

    Raises `PermissionError` -> HTTP 403 `Unauthorized`. See `s3_targets` for why the
    exception type and the message prefix both matter, and why an empty allow-list
    fails closed.
    """
    s3_targets.assert_bucket_allowed(bucket, ALLOWED_BUCKETS, logger=logger)


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
    #
    # `ValueError`, not a bare `Exception`: the dispatcher maps `ValueError` to
    # **400 BadRequest** and everything it does not recognise to 500
    # `InternalError`. A malformed argument is the caller's, and reporting it as a
    # server fault both misleads whoever is debugging it and pollutes the 5xx rate.
    if not s3_uri.startswith("s3://"):
        raise ValueError("Invalid S3 URI: expected s3://<bucket>/<key>")
    # Strip scheme, then split once into bucket and key.
    parts = s3_uri[len("s3://"):].split("/", 1)
    if len(parts) < 2 or not parts[0]:
        raise ValueError("Invalid S3 URI: expected s3://<bucket>/<key>")
    bucket, key = parts
    if not key:
        raise ValueError("Invalid S3 URI: key is required")

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
        PermissionError: the requested bucket is outside this deployment's
            allow-list. The dispatcher reports this as **403 Unauthorized**.
        ValueError: the `s3Uri` is malformed, or names an object that is not there.
            The dispatcher reports these as **400 BadRequest**.
        Exception: a genuine server-side fault (the resolver's own role, the bucket
            policy or the KMS key refused it, or S3 failed). **500 InternalError**.
    """
    try:
        logger.info(f"Received event: {json.dumps(sanitize_event_for_logging(event))}")

        field_name = (event.get('info') or {}).get('fieldName', 'getFileContents')
        if field_name == 'getFilePresignedUrl':
            return _handle_presigned_url(event)
        return _handle_file_contents(event)

    except ClientError as e:
        error_code = e.response['Error']['Code']
        error_message = e.response['Error']['Message']

        # NoSuchKey (get_object) and 404 (head_object) both mean "missing object",
        # which is the caller naming a key that is not there — a 400, not a 500.
        if error_code in ('NoSuchKey', '404', 'NoSuchVersion'):
            logger.info("Requested object is absent: %s", error_code)
            raise ValueError("File not found") from e

        # Everything else is the DEPLOYMENT failing to read an object the caller was
        # allowed to ask for: the caller's own credentials never touch S3 here, so an
        # AccessDenied is this function's role, the bucket policy or the KMS key —
        # a server fault, and correctly a 500.
        #
        # ⚠️ S3 answers a missing key with 403 AccessDenied rather than 404 when the
        # reader lacks s3:ListBucket on the bucket, so an absent object can land here
        # and be reported as a fault. That is not hypothetical in this deployment:
        # REPORTING_BUCKET is in the allow-list above but the function's Policies
        # block grants it no S3ReadPolicy, and s3:GetObjectVersion is granted only on
        # the output bucket — so every read there fails this way whether or not the
        # object exists. The log line below is what distinguishes it; the message to
        # the caller deliberately cannot, because guessing would be worse.
        #
        # The raw S3 message is NOT returned. It distinguished NoSuchBucket from
        # AccessDenied from Forbidden, which made this a working existence oracle
        # over the allow-listed buckets, and it read as an authorization problem to
        # the UI (S3's text for a denial is literally "Access Denied").
        logger.error(
            "S3 refused or failed a read this deployment is expected to be able to "
            "do: %s - %s. Check this function's S3 read policy, the bucket policy "
            "and the KMS key grant. NOTE: S3 also reports a MISSING key as "
            "AccessDenied/403 when s3:ListBucket is absent, so this may be an "
            "absent object rather than a permissions fault.",
            error_code,
            error_message,
        )
        raise Exception(
            "This deployment could not read the requested file. "
            "Contact an administrator."
        ) from e

    except (PermissionError, ValueError):
        # Re-raised unchanged, deliberately. The catch-all below used to wrap every
        # exception as `Exception(f"Error fetching file: {e}")`, which destroyed the
        # class name AND moved the "Unauthorized" prefix off the front of the
        # message — the two things the dispatcher uses to choose a status. So the
        # bucket allow-list refusal, every malformed-URI rejection and every
        # missing-object report all arrived as 500 `InternalError`. The refusals are
        # already logged at their raise site; re-logging them here as "Unexpected
        # error" is what made a deliberate denial indistinguishable from a crash in
        # CloudWatch and in any alarm watching ERROR.
        raise

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        raise Exception(f"Error fetching file: {str(e)}") from e