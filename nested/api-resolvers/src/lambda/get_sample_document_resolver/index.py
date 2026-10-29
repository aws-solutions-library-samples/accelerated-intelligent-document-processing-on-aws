# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Presign a GET URL for a bundled sample document under the ConfigurationBucket
`samples/` prefix (see samples-manifest.json). Only `samples/` keys are allowed."""

import logging
import mimetypes
import os
import posixpath

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


class ReportedServerError(RuntimeError):
    """A 500 whose message is written for the caller, so the dispatcher passes it on.

    Declared locally because this resolver does not import ``idp_common``; the
    dispatcher matches the class by name. The canonical class, and the rule for
    when to raise it, is ``idp_common.api_adapter.ReportedServerError``.
    """


CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET")
_SAMPLES_PREFIX = "samples/"
_URL_TTL_SECONDS = 900

_s3_endpoint_url = os.environ.get("S3_ENDPOINT_URL") or None
s3_client = boto3.client(
    "s3",
    endpoint_url=_s3_endpoint_url,
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual" if _s3_endpoint_url else "auto"},
    ),
)


def _validate_key(key: str) -> None:
    if not key:
        raise ValueError("s3Key is required")
    if key.startswith("/") or ".." in key or not key.startswith(_SAMPLES_PREFIX):
        raise ValueError("s3Key must be a bundled sample under 'samples/'")


def _caller_in_groups(event, allowed) -> bool:
    """Defense-in-depth RBAC check against the caller's Cognito groups, matching
    the sibling listSampleDocuments resolver in upload_resolver/index.py."""
    groups = (event.get("identity") or {}).get("claims", {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return bool(set(allowed).intersection(groups))


def handler(event, context=None):
    args = event.get("arguments", {}) or {}
    key = args.get("s3Key", "")
    logger.info("getSampleDocumentUrl request for key=%s", key)

    # Parity with listSampleDocuments: viewing a sample is an Admin/Author/Viewer
    # operation. Raises PermissionError -> the dispatcher maps it to a 403.
    if not _caller_in_groups(event, ("Admin", "Author", "Viewer")):
        raise PermissionError(
            "Unauthorized: getSampleDocumentUrl requires Admin, Author, or Viewer group"
        )

    _validate_key(key)
    if not CONFIGURATION_BUCKET:
        raise ValueError("CONFIGURATION_BUCKET is not configured")

    # Force the response content-type/disposition on the presigned URL so the
    # browser opens documents inline (objects were uploaded as octet-stream,
    # which otherwise downloads). Zips are served as attachments to download.
    filename = posixpath.basename(key)
    if key.lower().endswith(".zip"):
        content_type = "application/zip"
        disposition = f'attachment; filename="{filename}"'
    else:
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        disposition = f'inline; filename="{filename}"'

    try:
        url = s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": CONFIGURATION_BUCKET,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=_URL_TTL_SECONDS,
        )
    except ClientError as e:
        logger.error("Failed to presign sample %s: %s", key, e)
        raise ReportedServerError("Could not generate a link for the requested sample")

    return {"url": url, "s3Key": key}
