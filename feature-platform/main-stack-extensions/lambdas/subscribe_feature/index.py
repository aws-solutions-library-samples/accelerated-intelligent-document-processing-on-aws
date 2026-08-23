# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Mutation.subscribeFeature resolver. Admin-only.

Returns a **URL** the UI must redirect the admin to — the AWS Marketplace
product listing / terms-acceptance page. The UI opens this URL in a new
tab, the admin accepts the 3 required terms checkboxes, the simulator
(or real AWS Marketplace) records the subscription, and the UI refreshes
the entitlement state via the existing `checkFeatureEntitlement` query.

**This mirrors how the real AWS Marketplace flow works:** a subscription
is not a silent, one-click RPC — the buyer is redirected to a Marketplace-
hosted page where they accept pricing + seller EULA + AWS Customer
Agreement before the subscription becomes ACTIVE.

Returned shape (`FeatureEntitlement`):

    {
      featureId:          <input>,
      state:              "NONE",          # still NONE until admin completes the flow
      expiresAt:          null,
      customerIdentifier: <default>,       # echoed for client-side logging
      productCode:        <resolved>,
      source:             "simulator"|"marketplace",
      marketplaceUrl:     "<url to redirect to>",   # <-- NEW
    }

The simulator's HTML buyer console lives at
``${SIMULATOR_ADMIN_ENDPOINT}/marketplace/pp/<productCode>`` — see
``subscription-features/marketplace-simulator/mp_simulator/handlers/marketplace_ui.py``.

The product code and marketplace listing URL come from the feature's
``InstalledFeatures`` row — baked from ``feature.yaml``'s ``marketplace`` block
at publish time and written at install — **falling back to the catalog entry**.

That fallback is not optional. Subscribe is by definition something you do
*before* installing, but the ``InstalledFeatures`` row only exists *after*
install, so reading it alone meant the row lookup always came back empty on the
one path this resolver exists to serve: in real-Marketplace mode (no simulator
endpoint) it raised ``SubscribeError`` and the admin got an error instead of the
listing. ``catalog.json`` has carried ``productCode`` and
``marketplaceListingUrl`` all along.

Env vars:
    SIMULATOR_ADMIN_ENDPOINT     Base URL of the simulator (e.g. https://sim.example.com).
                                 When blank and SOURCE_TAG is "simulator", raises.
                                 Also used in marketplace mode if set, otherwise the
                                 feature's marketplaceListingUrl (install row, else
                                 catalog) is required.
    INSTALLED_FEATURES_TABLE     DynamoDB table holding installed-feature rows
                                 (productCode / marketplaceListingUrl per featureId).
    CONFIGURATION_BUCKET         (optional) bucket holding catalog.json — the
                                 pre-install fallback for productCode /
                                 marketplaceListingUrl. Blank disables it.
    CATALOG_KEY                  Catalog key (default config_library/catalog.json).
    FEATURE_OFFER_ID_MAP         JSON map {featureId: offerId}. Optional; simulator
                                 auto-creates a default public offer if missing.
    DEFAULT_CUSTOMER_IDENTIFIER  Fallback CustomerIdentifier.
    DEFAULT_BUYER_ACCOUNT_ID     12-digit simulator buyer account. Default "111122223333".
    ADMIN_GROUP                  Cognito group name required (default "Admin").
    SIMULATOR_SOURCE_TAG         "simulator" | "marketplace" (default "simulator").
    LOG_LEVEL                    Logging level (default INFO).
"""

import json
import logging
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_SIMULATOR_ADMIN_ENDPOINT = os.environ.get("SIMULATOR_ADMIN_ENDPOINT", "").rstrip("/")
_FEATURE_OFFER_ID_MAP_RAW = os.environ.get("FEATURE_OFFER_ID_MAP", "{}")
_DEFAULT_CUSTOMER_IDENTIFIER = os.environ.get("DEFAULT_CUSTOMER_IDENTIFIER", "")
_DEFAULT_BUYER_ACCOUNT_ID = os.environ.get("DEFAULT_BUYER_ACCOUNT_ID", "111122223333")
_ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
# Same default as check_feature_entitlement — see the note there.
_SOURCE_TAG = os.environ.get("SIMULATOR_SOURCE_TAG", "marketplace-live")
# `simulator` and `marketplace` are indistinguishable to a consumer (both are the
# seller-side GetEntitlements API, meaningful only against a simulator), so they
# collapse to one reported source. Must match check_feature_entitlement exactly.
_REPORTED_SOURCE = {
    "simulator": "simulated",
    "marketplace": "simulated",
}.get(_SOURCE_TAG, _SOURCE_TAG)

_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")

_dynamodb = boto3.resource("dynamodb")
_config_s3_client = None


def _config_s3():
    global _config_s3_client
    if _config_s3_client is None:
        _config_s3_client = boto3.client("s3")
    return _config_s3_client


def _catalog_marketplace_identity(
    feature_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Read (productCode, marketplaceListingUrl) from catalog.json.

    The pre-install source of truth: unlike the InstalledFeatures row, the
    catalog exists before anything is installed — which is precisely when
    Subscribe is used. Single GetObject, never lists. Returns (None, None) on any
    failure so the caller degrades rather than erroring.
    """
    if not _CONFIGURATION_BUCKET:
        return None, None
    try:
        resp = _config_s3().get_object(Bucket=_CONFIGURATION_BUCKET, Key=_CATALOG_KEY)
        catalog = json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("NoSuchKey", "404", "NotFound"):
            logger.warning("Failed to read catalog: %s", exc)
        return None, None
    except (BotoCoreError, ValueError) as exc:
        logger.warning("Bad catalog JSON: %s", exc)
        return None, None
    for entry in catalog.get("features") or []:
        if isinstance(entry, dict) and entry.get("featureId") == feature_id:
            return (
                entry.get("productCode") or None,
                entry.get("marketplaceListingUrl") or None,
            )
    return None, None


def _load_json_map(raw: str, name: str) -> Dict[str, str]:
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"{name} must be a JSON object")
        return parsed
    except ValueError as exc:
        logger.warning("%s is not valid JSON: %s. Using {}.", name, exc)
        return {}


_FEATURE_OFFER_ID_MAP = _load_json_map(
    _FEATURE_OFFER_ID_MAP_RAW, "FEATURE_OFFER_ID_MAP"
)


def _installed_marketplace_identity(
    feature_id: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Read (productCode, marketplaceListingUrl) from the feature's
    InstalledFeatures row — baked from the feature manifest at install time, so
    the host needs no per-feature configuration. Returns (None, None) when the
    feature isn't installed or carries no marketplace identity."""
    if not _INSTALLED_FEATURES_TABLE:
        return None, None
    try:
        row = (
            _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
            .get_item(Key={"featureId": feature_id})
            .get("Item")
            or {}
        )
    except Exception as exc:  # noqa: BLE001 — treat lookup failure as "absent"
        logger.warning(
            "Could not read InstalledFeatures row for %s: %s", feature_id, exc
        )
        return None, None
    return row.get("productCode"), row.get("marketplaceListingUrl")


class AuthorizationError(Exception):
    """Raised when a non-admin caller requests subscribeFeature."""


class SubscribeError(Exception):
    """Raised when the Lambda cannot build a valid marketplace URL."""


def _assert_admin(event: Dict[str, Any]) -> None:
    groups = event.get("identity", {}).get("claims", {}).get("cognito:groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    if _ADMIN_GROUP not in groups:
        raise AuthorizationError(
            f"subscribeFeature requires membership in group {_ADMIN_GROUP!r}"
        )


def _resolve_customer_identifier(event: Dict[str, Any]) -> Optional[str]:
    headers = (event.get("request", {}) or {}).get("headers", {}) or {}
    for key in (
        "x-amzn-marketplace-customer-identifier",
        "X-Amzn-Marketplace-Customer-Identifier",
    ):
        if headers.get(key):
            return headers[key]
    return _DEFAULT_CUSTOMER_IDENTIFIER or None


def _resolve_return_url(event: Dict[str, Any], feature_id: str) -> str:
    """Pull the caller-supplied `returnUrl` query arg (AppSync mutation arg).

    The UI sends the current FeaturePage URL so the simulator can redirect
    the admin back to the app after they complete the flow. If the UI
    didn't supply one we fall back to a relative /features/{featureId}
    path so at least the query string (`?subscribe=success`) is preserved.
    """
    args = event.get("arguments", {}) or {}
    return_url = args.get("returnUrl")
    if isinstance(return_url, str) and return_url.strip():
        return return_url.strip()
    return f"/features/{feature_id}"


def _build_simulator_url(
    *,
    product_code: str,
    offer_id: Optional[str],
    feature_id: str,
    buyer_account_id: str,
    return_url: str,
) -> str:
    """Build `${SIMULATOR_ADMIN_ENDPOINT}/marketplace/pp/<productCode>?...`."""
    if not _SIMULATOR_ADMIN_ENDPOINT:
        raise SubscribeError(
            "SIMULATOR_ADMIN_ENDPOINT is not configured; subscribeFeature "
            "cannot build a Marketplace Simulation URL."
        )
    params = {
        "featureId": feature_id,
        "buyerAccountId": buyer_account_id,
        "returnUrl": return_url,
    }
    if offer_id:
        params["offerId"] = offer_id
    return (
        f"{_SIMULATOR_ADMIN_ENDPOINT}/marketplace/pp/{product_code}?{urlencode(params)}"
    )


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("subscribeFeature event: %s", event)
    _assert_admin(event)

    args = event.get("arguments", {}) or {}
    feature_id = args.get("featureId")
    if not feature_id or not isinstance(feature_id, str):
        raise ValueError("featureId is required")

    # Resolve product code from the feature's InstalledFeatures row (baked from
    # the manifest at install time), then from the CATALOG. The catalog fallback
    # is the one that matters here: Subscribe runs before install, so the install
    # row does not exist yet on this path.
    product_code, installed_listing_url = _installed_marketplace_identity(feature_id)
    if not (product_code and installed_listing_url):
        catalog_code, catalog_listing_url = _catalog_marketplace_identity(feature_id)
        product_code = product_code or catalog_code
        installed_listing_url = installed_listing_url or catalog_listing_url
    if not product_code:
        if _SOURCE_TAG == "simulator":
            product_code = f"prod-{feature_id}-sim"
            logger.info(
                "No productCode on the install row for %r; synthesizing %r for "
                "simulator mode.",
                feature_id,
                product_code,
            )
        else:
            raise SubscribeError(
                f"No productCode for feature {feature_id!r} in either its install "
                f"row or the catalog. Set `productCode` on the feature's entry in "
                f"config_library/extensions-marketplace.yaml and re-publish (or "
                f"publish the feature with marketplace.productCode set in "
                f"feature.yaml so it travels with the install)."
            )

    customer_identifier = _resolve_customer_identifier(event)
    if not customer_identifier and _SOURCE_TAG == "simulator":
        customer_identifier = "cust-idp-default"
        logger.info(
            "No CustomerIdentifier provided; using default %r for simulator mode.",
            customer_identifier,
        )

    return_url = _resolve_return_url(event, feature_id)

    # Build the URL the UI should redirect to.
    #
    # A configured simulator endpoint is AUTHORITATIVE: when one is set, the host
    # is in simulator/dev mode for ALL features and we redirect to the simulator's
    # product page — matching check_feature_entitlement, which points boto3 at the
    # same endpoint for every feature (no per-feature simulator-vs-real split).
    # The feature's real AWS Marketplace listing URL (from its install row) is
    # used only in true production, where there is NO simulator endpoint.
    if _SIMULATOR_ADMIN_ENDPOINT:
        marketplace_url = _build_simulator_url(
            product_code=product_code,
            offer_id=_FEATURE_OFFER_ID_MAP.get(feature_id),
            feature_id=feature_id,
            buyer_account_id=_DEFAULT_BUYER_ACCOUNT_ID,
            return_url=return_url,
        )
    elif installed_listing_url:
        marketplace_url = installed_listing_url
    else:
        raise SubscribeError(
            f"No simulator endpoint and no marketplace listing URL for feature "
            f"{feature_id!r}. Set `marketplaceListingUrl` on the feature's entry "
            f"in config_library/extensions-marketplace.yaml (or publish it with "
            f"marketplace.listingUrl set in feature.yaml), or configure "
            f"FeaturePlatformSimulatorEndpoint for simulator mode."
        )

    logger.info(
        "Returning marketplaceUrl for feature=%s product=%s: %s",
        feature_id,
        product_code,
        marketplace_url,
    )

    # Entitlement state remains NONE until the admin accepts the terms and the
    # simulator (or real Marketplace) records the subscription. The UI polls /
    # refreshes checkFeatureEntitlement after the new-tab flow completes.
    return {
        "featureId": feature_id,
        "state": "NONE",
        "expiresAt": None,
        "customerIdentifier": customer_identifier,
        "productCode": product_code,
        "source": _REPORTED_SOURCE,
        "marketplaceUrl": marketplace_url,
    }
