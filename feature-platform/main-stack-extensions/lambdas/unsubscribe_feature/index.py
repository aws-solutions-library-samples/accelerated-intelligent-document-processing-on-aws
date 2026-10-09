# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Mutation.unsubscribeFeature resolver. Admin-only.

Symmetric with subscribe_feature — marks the simulator entitlement as
EXPIRED by POSTing to the simulator's admin API. The real-Marketplace
equivalent is a 'Cancel subscription' redirect to the AWS Marketplace
Subscription Management portal; when pointed at the real Marketplace we
simply no-op here (the UI redirects the user to the portal instead).

The simulator's expire admin API requires a concrete CustomerIdentifier, but
the simulator mints a RANDOM CustomerIdentifier per subscribe (cust-<uuid>) that
the host never sees. So, exactly like check_feature_entitlement, when no concrete
CustomerIdentifier is available we resolve it via GetEntitlements filtered by the
buyer AWS account (DEFAULT_BUYER_ACCOUNT_ID — the deterministic key subscribe
records under) and expire whatever id the subscription minted.

Env vars mirror subscribe_feature's, plus DEFAULT_BUYER_ACCOUNT_ID and the
marketplace-entitlement endpoint for the account-resolution lookup. See
subscribe_feature / check_feature_entitlement for docs.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from log_sanitizer import sanitize_event_for_logging

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "WARN"))

_SIMULATOR_ADMIN_ENDPOINT = os.environ.get("SIMULATOR_ADMIN_ENDPOINT", "").rstrip("/")
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

_dynamodb = boto3.resource("dynamodb")

# Lazily constructed marketplace-entitlement client (boto3 picks up the
# simulator endpoint from AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE).
# Short timeouts so a stalled cold-start exchange fails fast inside the Lambda
# budget rather than hanging until Lambda kills it.
_entitlement_client = None
_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=5,
    retries={"max_attempts": 3, "mode": "standard"},
)


def _client():
    global _entitlement_client
    if _entitlement_client is None:
        _entitlement_client = boto3.client(
            "marketplace-entitlement", config=_CLIENT_CONFIG
        )
    return _entitlement_client


def _resolve_customer_by_account(product_code: str, account_id: str) -> Optional[str]:
    """Resolve the CustomerIdentifier the subscription was recorded under by
    filtering GetEntitlements on the buyer AWS account (the deterministic key
    shared with subscribe). Returns the first matched entitlement's
    CustomerIdentifier, or None when none is found / the call fails."""
    try:
        resp = _client().get_entitlements(
            ProductCode=product_code,
            Filter={"CUSTOMER_AWS_ACCOUNT_ID": [account_id]},
        )
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "GetEntitlements (by account) failed for product %s: %s",
            product_code,
            exc,
        )
        return None
    for ent in resp.get("Entitlements", []) or []:
        cid = ent.get("CustomerIdentifier")
        if cid:
            return cid
    return None


def _installed_row(feature_id: str) -> Tuple[Optional[Dict[str, Any]], bool]:
    """``(row, read_ok)`` for the feature's InstalledFeatures row.

    Two separate answers rather than one ``None``, because "this feature is not
    installed" and "the table could not be read" license different responses to
    the caller: the first is the caller naming something that is not there, the
    second is an infrastructure fault. Reporting the second as the first sends an
    admin looking for a feature when the problem is the table.

    ``read_ok`` is False when the table is unconfigured or the read raised; True
    when the read succeeded, whether or not a row came back.
    """
    if not _INSTALLED_FEATURES_TABLE:
        return None, False
    try:
        row = (
            _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
            .get_item(Key={"featureId": feature_id})
            .get("Item")
        )
    except Exception as exc:  # noqa: BLE001 — a failed read is not an absent row
        logger.warning(
            "Could not read InstalledFeatures row for %s: %s", feature_id, exc
        )
        return None, False
    return (row or None), True


class AuthorizationError(Exception):
    """Raised when a non-admin caller requests unsubscribeFeature."""


class UnsubscribeError(Exception):
    """Raised when the simulator's admin API returns an error."""


class ResourceNotFound(Exception):
    """The feature the caller named is not installed in this deployment.

    The API dispatcher maps this to HTTP 404 with ``errorType:
    "ResourceNotFound"``. It matches by class NAME out of the invoke response's
    ``errorType`` — the exception object does not cross the ``lambda:Invoke``
    boundary — so this local declaration gets the same mapping as the canonical
    ``idp_common.api_adapter.ResourceNotFound``, which this Lambda cannot import
    because it does not depend on ``idp_common``. ``AuthorizationError`` above is
    declared locally for exactly the same reason.

    Keep the name in step with the dispatcher's; a rename here silently returns
    this refusal to 500.
    """


def _assert_admin(event: Dict[str, Any]) -> None:
    groups = event.get("identity", {}).get("claims", {}).get("cognito:groups", []) or []
    if isinstance(groups, str):
        groups = [groups]
    if _ADMIN_GROUP not in groups:
        raise AuthorizationError(
            f"unsubscribeFeature requires membership in group {_ADMIN_GROUP!r}"
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


def _post_json(url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 — admin API is trusted-env  # nosec B310 - SIMULATOR_ADMIN_ENDPOINT is a deployment-set env var, not user input
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500] if exc.fp else ""
        raise UnsubscribeError(
            f"Simulator admin API returned {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise UnsubscribeError(f"Failed to reach simulator at {url}: {exc}") from exc
    try:
        return json.loads(raw) if raw else {}
    except ValueError as exc:
        raise UnsubscribeError(f"Simulator admin API returned non-JSON: {exc}") from exc


def _expire_entitlement(
    customer_identifier: str, product_code: str, feature_id: str
) -> Dict[str, Any]:
    """Call the simulator's admin endpoint to expire the entitlement."""
    if not _SIMULATOR_ADMIN_ENDPOINT:
        raise UnsubscribeError(
            "SIMULATOR_ADMIN_ENDPOINT is not configured; unsubscribeFeature "
            "requires a running simulator (or a real Marketplace redirect in "
            "production)."
        )
    url = f"{_SIMULATOR_ADMIN_ENDPOINT}/admin/entitlements/expire"
    body = {
        "customerIdentifier": customer_identifier,
        "productCode": product_code,
        "featureId": feature_id,
    }
    logger.info("Simulator expire entitlement: POST %s body=%s", url, body)
    return _post_json(url, body)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return (
            datetime.fromtimestamp(float(value), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("unsubscribeFeature event: %s", sanitize_event_for_logging(event))
    _assert_admin(event)

    args = event.get("arguments", {}) or {}
    feature_id = args.get("featureId")
    if not feature_id or not isinstance(feature_id, str):
        raise ValueError("featureId is required")

    # Resolve product code from the feature's InstalledFeatures row (baked from
    # the manifest at install). In simulator mode, synthesize the same code as
    # subscribe_feature / check_feature_entitlement so the simulator's
    # expire-entitlement call targets the row we created.
    row, row_read_ok = _installed_row(feature_id)
    product_code = (row or {}).get("productCode")
    if not product_code:
        if _SOURCE_TAG == "simulator":
            product_code = f"prod-{feature_id}-sim"
            logger.info(
                "No productCode on the install row for %r; synthesizing %r for "
                "simulator mode.",
                feature_id,
                product_code,
            )
        elif row_read_ok and row is None:
            # There is no install row at all, so there is nothing to unsubscribe
            # from and no manifest to republish. 404 rather than 500: the request
            # named something that is not there. An unreadable table takes the
            # branch below instead, keeping its 500 — see _installed_row.
            raise ResourceNotFound(f"Feature {feature_id!r} is not installed.")
        else:
            # The row exists but carries no productCode (or the table could not be
            # read): the deployment is misconfigured, and the remedy is this one.
            raise UnsubscribeError(
                f"No productCode for feature {feature_id!r}. Publish the feature "
                f"with marketplace.productCode set in feature.yaml and reinstall."
            )

    # Resolve who to expire. A concrete CustomerIdentifier (Marketplace header or
    # configured default) wins. Otherwise — the common simulator case — resolve
    # it via GetEntitlements filtered by the buyer AWS account, the deterministic
    # key subscribe records under: the simulator mints a RANDOM CustomerIdentifier
    # per subscribe, so the account is the only id both sides know ahead of time.
    # Keyed on DEFAULT_BUYER_ACCOUNT_ID being set (not SOURCE_TAG == "simulator"),
    # because the main stack only ever emits "auto" / "marketplace" — never
    # "simulator" — so gating on it would leave this dead. Mirrors the resolution
    # in check_feature_entitlement.
    customer_identifier = _resolve_customer_identifier(event)
    if not customer_identifier and _DEFAULT_BUYER_ACCOUNT_ID:
        customer_identifier = _resolve_customer_by_account(
            product_code, _DEFAULT_BUYER_ACCOUNT_ID
        )
        if customer_identifier:
            logger.info(
                "No CustomerIdentifier provided; resolved %r via buyer AWS account %r.",
                customer_identifier,
                _DEFAULT_BUYER_ACCOUNT_ID,
            )
    if not customer_identifier:
        raise UnsubscribeError(
            "No CustomerIdentifier available and none could be resolved from the "
            "buyer AWS account (no active subscription found). Configure "
            "FeaturePlatformDefaultCustomerIdentifier or pass "
            "X-Amzn-Marketplace-Customer-Identifier."
        )

    sim_resp = _expire_entitlement(customer_identifier, product_code, feature_id)
    expires_at = _iso(sim_resp.get("expiresAt"))
    # Fallback: stamp 'now' so the UI can sort by expiry.
    if expires_at is None:
        expires_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "featureId": feature_id,
        "state": "EXPIRED",
        "expiresAt": expires_at,
        "customerIdentifier": customer_identifier,
        "productCode": product_code,
        "source": _REPORTED_SOURCE,
    }
