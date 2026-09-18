# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GraphQL resolver for ``getLatestPublishedVersion``.

Reads a single pointer object — ``<prefix>/idp-main-latest.json`` — from the
public artifacts bucket (overwritten by ``idp-cli publish`` on every release)
and returns its ``version`` + ``templateUrl``. This is a GetObject of one known
key: **no ListObjectsV2**, so it works against the public release bucket (which
permits GetObject only, not listing).

Pointer shape::

    { "version": "0.5.15", "templateUrl": "https://s3...idp-main_0.5.15.yaml" }

The check is opt-in: when the ``PUBLIC_ARTIFACTS_BUCKET`` environment variable
is empty, the resolver immediately returns ``checkEnabled=False`` so the UI
hides the "Update available" indicator (the default for headless/private-network
deployments). Result is cached at module scope for ``CACHE_TTL_SECONDS``.
"""

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Module-level config (read once per cold start)
PUBLIC_ARTIFACTS_BUCKET = os.environ.get("PUBLIC_ARTIFACTS_BUCKET", "").strip()
PUBLIC_ARTIFACTS_PREFIX = (
    os.environ.get("PUBLIC_ARTIFACTS_PREFIX", "artifacts/genai-idp").strip().strip("/")
)
PUBLIC_ARTIFACTS_REGION = os.environ.get(
    "PUBLIC_ARTIFACTS_REGION", os.environ.get("AWS_REGION", "us-east-1")
).strip()
MAIN_TEMPLATE_BASENAME = os.environ.get(
    "PUBLIC_ARTIFACTS_TEMPLATE_BASENAME", "idp-main"
).strip()
CACHE_TTL_SECONDS = int(os.environ.get("VERSION_CHECK_CACHE_TTL", "600"))

# In-memory cache keyed by Lambda warm container.
_CACHE: dict[str, Any] = {"timestamp": 0.0, "value": None}


def _pointer_key() -> str:
    """Key of the latest-version pointer (version-stripped prefix)."""
    base = f"{PUBLIC_ARTIFACTS_PREFIX}/" if PUBLIC_ARTIFACTS_PREFIX else ""
    return f"{base}{MAIN_TEMPLATE_BASENAME}-latest.json"


def _read_latest_pointer() -> dict[str, Any] | None:
    """GetObject the latest-version pointer. None if absent/unreadable.

    Tries an unsigned request first (so a public bucket needs no per-account
    IAM grant), falling back to signed credentials. GetObject only — no listing.
    """
    key = _pointer_key()

    def _get(client: Any) -> dict[str, Any]:
        resp = client.get_object(Bucket=PUBLIC_ARTIFACTS_BUCKET, Key=key)
        return json.loads(resp["Body"].read().decode("utf-8"))

    try:
        unsigned = boto3.client(
            "s3",
            region_name=PUBLIC_ARTIFACTS_REGION,
            config=Config(signature_version=UNSIGNED),
        )
        return _get(unsigned)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        # NoSuchKey/403 on the unsigned path → retry signed (private bucket).
        logger.warning(
            "Unsigned GetObject of %s failed (%s); retrying signed", key, code
        )
        try:
            return _get(boto3.client("s3", region_name=PUBLIC_ARTIFACTS_REGION))
        except ClientError as exc2:
            code2 = exc2.response.get("Error", {}).get("Code", "Unknown")
            if code2 in ("NoSuchKey", "404", "NotFound", "AccessDenied", "403"):
                logger.info(
                    "No version pointer at s3://%s/%s (%s)",
                    PUBLIC_ARTIFACTS_BUCKET,
                    key,
                    code2,
                )
                return None
            raise
    except (BotoCoreError, ValueError) as exc:
        logger.warning(
            "Bad/unreadable version pointer s3://%s/%s: %s",
            PUBLIC_ARTIFACTS_BUCKET,
            key,
            exc,
        )
        return None


def _build_response(
    *,
    check_enabled: bool,
    latest_version: str | None = None,
    template_url: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """Shape a response that matches the ``LatestPublishedVersion`` GraphQL type."""
    return {
        "checkEnabled": check_enabled,
        "latestVersion": latest_version,
        "templateUrl": template_url,
        "errorMessage": error_message,
    }


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AppSync direct-Lambda resolver entry point.

    AppSync passes us ``{"arguments": {...}, "identity": {...}, ...}`` but
    we don't need any of it — the result is the same for every caller.
    """
    logger.debug(
        "getLatestPublishedVersion invoked: %s", sanitize_event_for_logging(event)
    )

    if not PUBLIC_ARTIFACTS_BUCKET:
        logger.info("Version check disabled (PUBLIC_ARTIFACTS_BUCKET not set)")
        return _build_response(check_enabled=False)

    now = time.time()
    cached = _CACHE.get("value")
    if cached is not None and (now - _CACHE["timestamp"]) < CACHE_TTL_SECONDS:
        logger.debug(
            "Returning cached version result (age=%.0fs)", now - _CACHE["timestamp"]
        )
        return cached

    try:
        pointer = _read_latest_pointer()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = exc.response.get("Error", {}).get("Message", str(exc))
        logger.error("Failed to read version pointer: %s (%s)", message, code)
        # Don't cache failures — we want the next page load to retry.
        return _build_response(check_enabled=True, error_message=f"{code}: {message}")
    except Exception as exc:  # noqa: BLE001 - surface but don't crash the UI
        logger.error("Unexpected error reading version pointer: %s", exc, exc_info=True)
        return _build_response(check_enabled=True, error_message=str(exc))

    latest_version = (pointer or {}).get("version")
    if not pointer or not isinstance(latest_version, str) or not latest_version:
        logger.warning(
            "No usable version pointer at s3://%s/%s",
            PUBLIC_ARTIFACTS_BUCKET,
            _pointer_key(),
        )
        result = _build_response(check_enabled=True)
        _CACHE.update(timestamp=now, value=result)
        return result

    # Prefer the pointer's own templateUrl; fall back to the conventional
    # versioned key if the publisher didn't include one.
    template_url = pointer.get("templateUrl") or (
        f"https://s3.{PUBLIC_ARTIFACTS_REGION}.amazonaws.com/"
        f"{PUBLIC_ARTIFACTS_BUCKET}/{PUBLIC_ARTIFACTS_PREFIX}/"
        f"{MAIN_TEMPLATE_BASENAME}_{latest_version}.yaml"
    )
    result = _build_response(
        check_enabled=True,
        latest_version=latest_version,
        template_url=template_url,
    )
    logger.info("Latest published version: %s (%s)", latest_version, template_url)
    _CACHE.update(timestamp=now, value=result)
    return result
