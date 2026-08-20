# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Query.checkFeatureEntitlement resolver.

Resolves the caller's entitlement state for a given feature. There are two
production-capable paths plus the dev simulator, selected by
`SIMULATOR_SOURCE_TAG`:

    auto             → every feature ACTIVE, no API call (no simulator, no
                       Marketplace endpoint configured)
    simulator |      → `marketplace-entitlement:GetEntitlements`, with boto3
    marketplace        pointed at `AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE`
                       (the local marketplace-simulator, or an admin-supplied
                       endpoint). UNCHANGED — this is the dev/CI path.
    marketplace-live → the buyer-side AWS Marketplace **Agreement** API
                       (`SearchAgreements`). Also simulatable: boto3 honors
                       `AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT` (derived from the
                       service model, same convention as the entitlement
                       override), so a marketplace-simulator can back this path
                       too. The mode is chosen independently of whether an
                       endpoint is set — see docs/feature-platform.md
                       "Deployment modes".

Why `marketplace-live` doesn't just call GetEntitlements
-------------------------------------------------------
`GetEntitlements` cannot work as a buyer-side gate, for two independent reasons
that were confirmed empirically before this was written:

1. **It's a seller-side API.** AWS's guidance for SaaS integrations is that
   these calls "must be signed by credentials from your AWS Marketplace Seller
   account", and the documented IAM policy groups `GetEntitlements` with
   `ResolveCustomer` / `BatchMeterUsage` as seller-side actions.
2. **Entitlements only exist for SaaS *Contract* products.** In the contract
   model AWS communicates entitlements through the Entitlement Service; a
   usage-based SaaS *Subscription* meters instead and has no entitlement records
   at all. For such a listing GetEntitlements returns an empty list forever.

Critically, it does not FAIL in either case — called from a buyer account with
someone else's product code it returns HTTP 200 with `{"Entitlements": []}`. A
fail-closed gate built on that denies every legitimate customer while logging
nothing, and looks perfectly healthy against the simulator. So the live path
uses `SearchAgreements` (documented for buyers: "Acceptor can perform search
across all agreements that they participated in as acceptor"), filtered to this
product via `ResourceIdentifier` — which needs only plain IAM, no License
Manager service role.

The live path deliberately distinguishes three outcomes, because two of them
look identical if you only check for emptiness:

    ACTIVE   an ACTIVE PurchaseAgreement exists for this product → entitled.
    NONE     the call SUCCEEDED and returned nothing. Authoritative: unlike
             GetEntitlements, SearchAgreements is scoped to the caller's own
             account, so empty really does mean "no agreement here".
    UNKNOWN  the call ERRORED (IAM not granted, API unavailable in this
             partition/region). We CANNOT distinguish this from "not
             subscribed", so we degrade to advisory-ACTIVE and log loudly.
             Failing closed on a host misconfiguration would brick a paying
             customer's extension.

Known false-negative: if an AWS Organization holds the subscription in the
management account while this stack runs in a member account, SearchAgreements
from the member account reports nothing. That is why NONE is surfaced to the UI
as "couldn't confirm your subscription" with the Subscribe CTA rather than a hard
block, and why the authoritative commercial gate is the extension's own runtime
entitlement check — not this resolver.

Each feature's Marketplace product identity is read from its `InstalledFeatures`
row (baked from the feature manifest and written at install), falling back to the
**catalog** entry — which is what makes the NOT-YET-INSTALLED path work at all,
since that row doesn't exist before install. The caller's CustomerIdentifier is
resolved from:
  1. `X-Amzn-Marketplace-Customer-Identifier` header via event.request.headers
     (when the main stack is deployed inside a subscribed account), or
  2. The env var `DEFAULT_CUSTOMER_IDENTIFIER` (dev/simulator convenience).

Returns `{state: ACTIVE, source: 'auto'}` immediately when SIMULATOR_SOURCE_TAG=auto
(stack deployed without simulator or Marketplace endpoint — all features are
treated as subscribed and the UI goes straight to the Install prompt).
Returns `{state: ACTIVE, source: 'oss'}` immediately for features whose catalog
entry is source="oss" — open-source features have no Marketplace contract and
install directly even when a simulator/Marketplace endpoint is configured. This
mirrors get_feature_launch_url, which skips the entitlement check for OSS.
Returns `{state: NONE}` if no product code is registered for the feature.
Returns `{state: NONE}` if the caller has no active entitlement.
Returns `{state: ACTIVE, expiresAt}` if at least one entitlement is active.
Returns `{state: EXPIRED, expiresAt}` if an entitlement exists but has expired.

Environment:
    INSTALLED_FEATURES_TABLE   DynamoDB table holding installed-feature rows
                               (productCode per featureId, baked from the manifest).
    DEFAULT_CUSTOMER_IDENTIFIER  (optional) fallback customer identifier
    DEFAULT_BUYER_ACCOUNT_ID   buyer AWS account used as the GetEntitlements
                               filter when no CustomerIdentifier is available
                               (the deterministic key shared with subscribeFeature).
    SIMULATOR_SOURCE_TAG       "auto" | "simulator" | "marketplace" | "marketplace-live"
    MARKETPLACE_AGREEMENT_REGION  Region for the Agreement API (default us-east-1)
    CONFIGURATION_BUCKET       (optional) bucket holding catalog.json; used to
                               detect OSS features and to resolve productCode /
                               productId before install. Blank disables both.
    CATALOG_KEY                Catalog key (default config_library/catalog.json)
    LOG_LEVEL                  Logging level (default INFO)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DEFAULT_CUSTOMER_IDENTIFIER = os.environ.get("DEFAULT_CUSTOMER_IDENTIFIER", "")
_DEFAULT_BUYER_ACCOUNT_ID = os.environ.get("DEFAULT_BUYER_ACCOUNT_ID", "111122223333")
_SOURCE_TAG = os.environ.get("SIMULATOR_SOURCE_TAG", "marketplace")
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
# The AWS Marketplace Agreement API is not available in every region; us-east-1
# is where AWS Marketplace itself lives and is the documented default.
_AGREEMENT_REGION = os.environ.get("MARKETPLACE_AGREEMENT_REGION", "us-east-1")
# The live, buyer-side path. Kept as a distinct tag rather than an ad-hoc branch
# so `simulator` / `marketplace` (dev + CI) behave EXACTLY as before.
_LIVE_TAG = "marketplace-live"

_dynamodb = boto3.resource("dynamodb")


def _installed_product_code(feature_id: str) -> Optional[str]:
    """Read productCode from the feature's InstalledFeatures row (baked from the
    manifest at install time). Returns None when absent."""
    if not _INSTALLED_FEATURES_TABLE:
        return None
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
        return None
    return row.get("productCode")


# Lazily constructed so unit tests can patch endpoint_url via env vars.
_entitlement_client = None

# Catalog lives in the stack's own ConfigurationBucket (Lambda's default region).
_config_s3_client = None


def _config_s3():
    global _config_s3_client
    if _config_s3_client is None:
        _config_s3_client = boto3.client("s3")
    return _config_s3_client


def _read_catalog_entry(feature_id: str) -> Optional[Dict[str, Any]]:
    """Return the catalog.json entry for `feature_id`, or None if absent.

    Single GetObject against ConfigurationBucket — never lists. Mirrors
    `_read_catalog_entry` in get_feature_launch_url so the two resolvers agree on
    which features are open-source (install-direct, no entitlement) and on each
    feature's Marketplace identity.
    """
    if not _CONFIGURATION_BUCKET:
        return None
    try:
        resp = _config_s3().get_object(Bucket=_CONFIGURATION_BUCKET, Key=_CATALOG_KEY)
        catalog = json.loads(resp["Body"].read().decode("utf-8"))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        logger.warning("Failed to read catalog: %s", exc)
        return None
    except (BotoCoreError, ValueError) as exc:
        logger.warning("Bad catalog JSON: %s", exc)
        return None
    for entry in catalog.get("features") or []:
        if isinstance(entry, dict) and entry.get("featureId") == feature_id:
            return entry
    return None


# Short explicit timeouts (override botocore's 60s default) so a stalled cold-
# start TLS/HTTP exchange is retried and fails fast inside the 30s Lambda
# budget, rather than hanging the whole invocation until Lambda kills it.
# 3 attempts × (5s connect + 5s read) worst-case = ~30s with jittered retries.
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


def _resolve_customer_identifier(event: Dict[str, Any]) -> Optional[str]:
    # AppSync Lambda resolver event has `request.headers` (lowercase) when
    # the caller passed custom HTTP headers through the AppSync API.
    headers = (event.get("request", {}) or {}).get("headers", {}) or {}
    for key in (
        "x-amzn-marketplace-customer-identifier",
        "X-Amzn-Marketplace-Customer-Identifier",
    ):
        if headers.get(key):
            return headers[key]
    return _DEFAULT_CUSTOMER_IDENTIFIER or None


def _get_entitlements(
    product_code: str,
    *,
    customer_identifier: Optional[str] = None,
    customer_aws_account_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Call GetEntitlements filtered by customer identifier OR buyer AWS account.

    The two filters are mutually exclusive (per the real API). When the caller
    has a concrete CustomerIdentifier (Marketplace header / configured default)
    we filter by it; otherwise we filter by the buyer AWS account, which is the
    deterministic key both subscribe and check share in simulator mode (the
    simulator mints a random CustomerIdentifier per subscribe, so the account is
    the only id known on both sides ahead of time).
    """
    client = _client()
    if customer_identifier:
        filt = {"CUSTOMER_IDENTIFIER": [customer_identifier]}
    elif customer_aws_account_id:
        filt = {"CUSTOMER_AWS_ACCOUNT_ID": [customer_aws_account_id]}
    else:
        return []
    try:
        resp = client.get_entitlements(ProductCode=product_code, Filter=filt)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("GetEntitlements failed for product %s: %s", product_code, exc)
        return []
    return resp.get("Entitlements", []) or []


# Lazily constructed AWS Marketplace Agreement API client (buyer-side).
_agreement_client_obj = None


def _agreement_client():
    global _agreement_client_obj
    if _agreement_client_obj is None:
        _agreement_client_obj = boto3.client(
            "marketplace-agreement",
            region_name=_AGREEMENT_REGION,
            config=_CLIENT_CONFIG,
        )
    return _agreement_client_obj


def _search_active_agreements(product_id: str) -> Tuple[str, Optional[datetime]]:
    """Buyer-side subscription check via the AWS Marketplace Agreement API.

    Returns (outcome, latest_end_time) where outcome is:

        "ACTIVE"   at least one ACTIVE PurchaseAgreement for this product
        "NONE"     the call succeeded and matched nothing — authoritative for
                   THIS account (see the module docstring's caveat about an
                   Organization holding the subscription elsewhere)
        "UNKNOWN"  the call failed; indistinguishable from NONE, so the caller
                   must degrade to advisory rather than deny

    The filter combination (PartyType + AgreementType + ResourceIdentifier +
    Status) is the documented buyer form and was verified against a live account.
    `ResourceIdentifier` matches the SaaS product ENTITY id (`prod-…`), which is
    why the catalog carries `productId` alongside `productCode`.
    """
    if not product_id:
        logger.warning(
            "No productId available; cannot run a buyer-side agreement check. "
            "Add `productId` to the feature's catalog entry."
        )
        return "UNKNOWN", None
    try:
        resp = _agreement_client().search_agreements(
            catalog="AWSMarketplace",
            filters=[
                {"name": "PartyType", "values": ["Acceptor"]},
                {"name": "AgreementType", "values": ["PurchaseAgreement"]},
                {"name": "ResourceIdentifier", "values": [product_id]},
                {"name": "Status", "values": ["ACTIVE"]},
            ],
        )
    except (ClientError, BotoCoreError) as exc:
        # AccessDenied (IAM not granted / not propagated), an unsupported
        # partition, throttling — all indistinguishable from "not subscribed".
        logger.warning(
            "SearchAgreements failed for product %s: %s. Treating entitlement as "
            "UNKNOWN (advisory allow) rather than denying a possibly-paying "
            "customer.",
            product_id,
            exc,
        )
        return "UNKNOWN", None

    summaries = resp.get("agreementViewSummaries") or []
    if not summaries:
        return "NONE", None

    end_times: List[datetime] = []
    for summary in summaries:
        end = summary.get("endTime")
        if isinstance(end, datetime):
            end_times.append(end if end.tzinfo else end.replace(tzinfo=timezone.utc))
        elif isinstance(end, (int, float)):
            end_times.append(datetime.fromtimestamp(end, tz=timezone.utc))
    # An open-ended agreement has no endTime — ACTIVE with no expiry.
    return "ACTIVE", max(end_times) if end_times else None


def _parse_expiration(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        # Accept both 2026-05-05T10:00:00Z and 2026-05-05T10:00:00+00:00
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable expiration %r", raw)
            return None
    return None


def _evaluate(entitlements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the most-permissive entitlement and derive state+expiresAt.

    ACTIVE wins over EXPIRED; the latest expiration is reported.
    """
    if not entitlements:
        return {"state": "NONE", "expiresAt": None}

    now = datetime.now(timezone.utc)
    active_expirations: List[datetime] = []
    expired_expirations: List[datetime] = []
    any_no_expiry = False

    for ent in entitlements:
        exp = _parse_expiration(ent.get("ExpirationDate"))
        if exp is None:
            any_no_expiry = True
            continue
        if exp > now:
            active_expirations.append(exp)
        else:
            expired_expirations.append(exp)

    if any_no_expiry or active_expirations:
        latest_active = max(active_expirations) if active_expirations else None
        return {
            "state": "ACTIVE",
            "expiresAt": latest_active.isoformat().replace("+00:00", "Z")
            if latest_active
            else None,
        }
    latest_expired = max(expired_expirations) if expired_expirations else None
    return {
        "state": "EXPIRED",
        "expiresAt": latest_expired.isoformat().replace("+00:00", "Z")
        if latest_expired
        else None,
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    logger.info("checkFeatureEntitlement event: %s", event)

    args = event.get("arguments", {}) or {}
    feature_id = args.get("featureId")
    if not feature_id or not isinstance(feature_id, str):
        raise ValueError("featureId is required")

    # Auto-subscribe mode: stack was deployed without a marketplace simulator
    # or external Marketplace endpoint. Every catalog feature is treated as
    # subscribed so the UI goes straight to the Install prompt; no Marketplace
    # call is needed (and the boto3 client is never instantiated).
    if _SOURCE_TAG == "auto":
        return {
            "featureId": feature_id,
            "state": "ACTIVE",
            "expiresAt": None,
            "customerIdentifier": None,
            "productCode": None,
            "source": "auto",
        }

    # Read the catalog entry ONCE: it tells us whether this is an OSS feature and
    # carries the Marketplace identity we need before the feature is installed.
    catalog_entry = _read_catalog_entry(feature_id) or {}

    # OSS features have no AWS Marketplace contract — they install directly
    # regardless of whether a simulator/Marketplace endpoint is configured.
    # Short-circuit to ACTIVE so the UI shows the Install prompt instead of
    # "Subscription required". This mirrors get_feature_launch_url, which skips
    # the entitlement check for source=="oss" catalog entries. Only consult the
    # entitlement endpoint for marketplace features below.
    if catalog_entry.get("source") == "oss":
        return {
            "featureId": feature_id,
            "state": "ACTIVE",
            "expiresAt": None,
            "customerIdentifier": None,
            "productCode": None,
            "source": "oss",
        }

    # Resolve product code from the feature's InstalledFeatures row (baked from
    # the manifest at install), FALLING BACK TO THE CATALOG. The fallback is what
    # makes the not-yet-installed path work: that DDB row only exists after
    # install, so before install the row lookup necessarily comes back empty and
    # this resolver used to report NONE/source="none" even for a genuinely
    # subscribed customer — the UI then showed "no entitlement" with no way
    # forward. The catalog has had productCode all along.
    product_code = _installed_product_code(feature_id) or (
        catalog_entry.get("productCode") or None
    )
    product_id = catalog_entry.get("productId") or ""

    # --- Live, buyer-side path -------------------------------------------
    # Checked BEFORE the productCode bail-out below, because this path keys on
    # productId (the SaaS product ENTITY id) — productCode is only useful to the
    # seller-side GetEntitlements API, which cannot answer this question at all.
    if _SOURCE_TAG == _LIVE_TAG:
        outcome, end_time = _search_active_agreements(product_id)
        if outcome == "ACTIVE":
            return {
                "featureId": feature_id,
                "state": "ACTIVE",
                "expiresAt": end_time.isoformat().replace("+00:00", "Z")
                if end_time
                else None,
                "customerIdentifier": None,
                "productCode": product_code,
                "source": _LIVE_TAG,
            }
        if outcome == "NONE":
            # Authoritative for this account: SearchAgreements is scoped to the
            # caller, so an empty successful result really is "not subscribed
            # here". The UI shows Subscribe.
            return {
                "featureId": feature_id,
                "state": "NONE",
                "expiresAt": None,
                "customerIdentifier": None,
                "productCode": product_code,
                "source": _LIVE_TAG,
            }
        # UNKNOWN — the call failed, so we genuinely cannot tell "not
        # subscribed" from "host misconfigured". Degrade to advisory ACTIVE
        # rather than block: the extension performs its own runtime entitlement
        # check, so a wrongly-permissive host gate costs nothing, whereas a
        # wrongly-restrictive one bricks a paying customer's extension.
        logger.warning(
            "Entitlement for %r is UNKNOWN (Agreement API unavailable); "
            "returning advisory ACTIVE. The extension's own runtime check "
            "remains the authoritative gate.",
            feature_id,
        )
        return {
            "featureId": feature_id,
            "state": "ACTIVE",
            "expiresAt": None,
            "customerIdentifier": None,
            "productCode": product_code,
            "source": "advisory",
        }

    if not product_code:
        if _SOURCE_TAG == "simulator":
            product_code = f"prod-{feature_id}-sim"
            logger.info(
                "No productCode on the install row for %r; using synthesized %r "
                "for simulator mode.",
                feature_id,
                product_code,
            )
        else:
            logger.info(
                "No productCode on the install row for feature %s; returning NONE.",
                feature_id,
            )
            return {
                "featureId": feature_id,
                "state": "NONE",
                "expiresAt": None,
                "customerIdentifier": None,
                "productCode": None,
                "source": "none",
            }

    # Resolve who to look up. A concrete CustomerIdentifier (Marketplace header
    # or configured default) wins. Otherwise — the common simulator case — fall
    # back to the buyer AWS account, the deterministic key shared with
    # subscribe_feature: the simulator mints a RANDOM CustomerIdentifier per
    # subscribe, so the account is the only id both sides know ahead of time.
    # GetEntitlements(CUSTOMER_AWS_ACCOUNT_ID) resolves it to whatever the
    # subscription recorded.
    #
    # The account fallback is keyed on DEFAULT_BUYER_ACCOUNT_ID being set, NOT on
    # SOURCE_TAG == "simulator": the main stack only ever emits SOURCE_TAG "auto"
    # (no endpoint, short-circuited above) or "marketplace" (any endpoint set —
    # whether the standalone simulator or a real Marketplace API). There is no
    # "simulator" path from the main stack, so gating on it left the fallback
    # dead and every post-subscribe check returned NONE. CUSTOMER_AWS_ACCOUNT_ID
    # is a real Marketplace filter, so this is correct in both modes: in
    # simulator mode the buyer account is the deterministic shared key; in real-
    # Marketplace mode it only resolves entitlements actually subscribed under
    # that account (and a header/default CustomerIdentifier still wins first).
    customer_identifier = _resolve_customer_identifier(event)
    account_filter = None
    if not customer_identifier:
        if _DEFAULT_BUYER_ACCOUNT_ID:
            account_filter = _DEFAULT_BUYER_ACCOUNT_ID
            logger.info(
                "No CustomerIdentifier provided; filtering by buyer AWS account %r.",
                account_filter,
            )
        else:
            logger.info(
                "No CustomerIdentifier available for feature %s; returning NONE.",
                feature_id,
            )
            return {
                "featureId": feature_id,
                "state": "NONE",
                "expiresAt": None,
                "customerIdentifier": None,
                "productCode": product_code,
                "source": _SOURCE_TAG,
            }

    entitlements = _get_entitlements(
        product_code,
        customer_identifier=customer_identifier,
        customer_aws_account_id=account_filter,
    )
    evaluated = _evaluate(entitlements)

    # Echo back the resolved customer identifier from the matched entitlement
    # when we looked up by account (so the UI can display it).
    resolved_cid = customer_identifier
    if resolved_cid is None and entitlements:
        resolved_cid = entitlements[0].get("CustomerIdentifier")

    return {
        "featureId": feature_id,
        "state": evaluated["state"],
        "expiresAt": evaluated["expiresAt"],
        "customerIdentifier": resolved_cid,
        "productCode": product_code,
        "source": _SOURCE_TAG,
    }
