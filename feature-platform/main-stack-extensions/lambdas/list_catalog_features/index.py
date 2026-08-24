# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Query.listCatalogFeatures resolver.

Returns every feature available to install — both open-source bundled features
and closed-source AWS Marketplace extensions — by reading a single catalog
manifest. Used by the UI to render the "Extensions" nav section so not-yet-
installed features can show an Install or Subscribe CTA.

Discovery is **manifest-driven**, never list-driven. The resolver reads ONE
object — `catalog.json` — with a single S3 GetObject. It performs NO
ListObjectsV2 (the artifacts and seller buckets only permit GetObject), and the
deployed stack does NOT depend on the artifacts bucket: `catalog.json` is
copied into the stack's own ConfigurationBucket at deploy time (by the main
stack's ConfigurationCopyFunction, alongside the rest of config_library/).

`catalog.json` is produced by `idp-cli publish`, which merges the bundled
open-source features it builds with the curated closed-source list in
`config_library/extensions-marketplace.yaml`. Shape:

    {
      "schemaVersion": "1.0",
      "features": [
        { "featureId": "docs-by-status", "displayName": "...", "source": "oss",
          "latestVersion": "1.0.2", "iconUrl": "" },
        # Marketplace entries are a future capability (none published yet):
        { "featureId": "my-paid-extension", "displayName": "...", "source": "marketplace",
          "latestVersion": "1.0.0", "productCode": "...",
          "marketplaceListingUrl": "https://aws.amazon.com/marketplace/pp/...",
          "iconUrl": "" }
      ]
    }

Each entry also reports whether the feature can actually be installed in THIS
stack's region (`availableInRegion` / `availableRegions`), so the UI can say
"not available in eu-west-2" instead of offering a Subscribe button that leads
to a dead end. Marketplace features are region-scoped because `sam package`
bakes an absolute, region-specific `s3://bucket/key` CodeUri into the published
template; OSS features ship alongside the host and are always available.

Called by any signed-in user (Viewer and up). Never raises when the catalog is
missing or unreachable: it returns [] so the UI keeps working.

Environment:
    CONFIGURATION_BUCKET    The stack's own ConfigurationBucket (catalog lives here)
    CATALOG_KEY             Key of the catalog manifest (default config_library/catalog.json)
    HOST_REGION             Region availability is evaluated against (defaults to AWS_REGION)
    LOG_LEVEL               Logging level (default INFO)
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_HOST_REGION = os.environ.get("HOST_REGION") or os.environ.get("AWS_REGION", "")

_s3 = boto3.client("s3")


def _available_regions(entry: Dict[str, Any]) -> List[str]:
    """Sorted regions a marketplace feature publishes artifacts to.

    Reads catalog schema 1.1's ``regions`` map, falling back to the deprecated
    flat ``sellerBucketRegion``. Kept byte-for-byte equivalent to the helper of
    the same name in `get_feature_launch_url` so the UI's availability badge and
    the resolver that actually builds the launch URL can never disagree — a
    feature shown as available must be launchable.
    """
    regions = entry.get("regions")
    if isinstance(regions, dict):
        found = sorted(
            str(r)
            for r, spec in regions.items()
            if isinstance(spec, dict)
            and (spec.get("sellerBucket") or "").strip()
            and (spec.get("templateKey") or "").strip()
        )
        if found:
            return found
    legacy_region = (entry.get("sellerBucketRegion") or "").strip()
    if legacy_region and (entry.get("sellerBucket") or "").strip():
        return [legacy_region]
    return []


def _read_catalog() -> Optional[Dict[str, Any]]:
    """Fetch + parse catalog.json from ConfigurationBucket. None on any failure."""
    if not _CONFIGURATION_BUCKET:
        logger.info("CONFIGURATION_BUCKET env var is empty; returning empty catalog")
        return None
    try:
        resp = _s3.get_object(Bucket=_CONFIGURATION_BUCKET, Key=_CATALOG_KEY)
        body = resp["Body"].read().decode("utf-8")
        return json.loads(body)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            logger.info(
                "No catalog at s3://%s/%s — empty catalog",
                _CONFIGURATION_BUCKET,
                _CATALOG_KEY,
            )
            return None
        logger.warning(
            "Failed to read s3://%s/%s: %s", _CONFIGURATION_BUCKET, _CATALOG_KEY, exc
        )
        return None
    except (BotoCoreError, ValueError) as exc:
        logger.warning(
            "Bad JSON in s3://%s/%s: %s", _CONFIGURATION_BUCKET, _CATALOG_KEY, exc
        )
        return None


def _to_catalog_feature(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a catalog manifest entry to the GraphQL `CatalogFeature` shape.

    Returns None for entries missing the minimal required fields.
    """
    feature_id = entry.get("featureId")
    if not isinstance(feature_id, str) or not feature_id:
        return None
    source = entry.get("source") or "oss"

    # Region availability. OSS features are published alongside the host, so
    # they are available wherever the host runs: report an empty region list and
    # availableInRegion=true rather than inventing a list to filter against.
    # Marketplace features are genuinely region-scoped (region-specific baked
    # CodeUri), so an unlisted region means "cannot install here".
    if source == "marketplace":
        available_regions = _available_regions(entry)
        # No HOST_REGION resolvable (shouldn't happen in Lambda) → don't claim
        # unavailability we can't substantiate; let the launch resolver decide.
        available_in_region = (
            (_HOST_REGION in available_regions) if _HOST_REGION else True
        )
    else:
        available_regions = []
        available_in_region = True

    return {
        "featureId": feature_id,
        "displayName": entry.get("displayName") or feature_id,
        "latestVersion": entry.get("latestVersion") or "",
        "iconUrl": entry.get("iconUrl") or None,
        "description": entry.get("description") or None,
        "docsUrl": entry.get("docsUrl") or None,
        # Whether the feature gets its own Extensions nav entry before it's
        # installed (default True; the bundled samples publish False).
        "showInNav": entry.get("showInNav", True) is not False,
        "source": source,
        # Marketplace-only metadata; null/empty for OSS features.
        "productCode": entry.get("productCode") or None,
        "marketplaceListingUrl": entry.get("marketplaceListingUrl") or None,
        # OSS-only: where the feature artifacts live in the artifacts bucket.
        "artifactBucket": entry.get("artifactBucket") or None,
        "artifactPrefix": entry.get("artifactPrefix") or None,
        # Can this feature be installed in the host's own region? Lets the UI
        # explain "not available in <region>" instead of offering a Subscribe
        # button that dead-ends. Empty availableRegions = not region-scoped.
        "availableInRegion": available_in_region,
        "availableRegions": available_regions,
    }


def handler(event: Dict[str, Any], context: Any) -> List[Dict[str, Any]]:
    """AppSync resolver entry point."""
    logger.info("listCatalogFeatures event: %s", event)

    catalog = _read_catalog()
    if not catalog or not isinstance(catalog, dict):
        return []

    raw_features = catalog.get("features")
    if not isinstance(raw_features, list):
        logger.warning("catalog.json has no 'features' list: %r", catalog)
        return []

    features: List[Dict[str, Any]] = []
    for entry in raw_features:
        if not isinstance(entry, dict):
            continue
        cf = _to_catalog_feature(entry)
        if cf is not None:
            features.append(cf)

    # Stable order by displayName for the UI (same as listInstalledFeatures).
    features.sort(key=lambda f: f["displayName"].lower())
    return features
