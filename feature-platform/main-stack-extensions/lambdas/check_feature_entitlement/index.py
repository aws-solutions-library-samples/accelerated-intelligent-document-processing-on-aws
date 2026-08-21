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

WHICH API we call and WHERE the call goes are two independent axes, and only one
of them decides what we may CLAIM
-----------------------------------------------------------------------------
`SIMULATOR_SOURCE_TAG` picks the API. `AWS_ENDPOINT_URL_MARKETPLACE_*` picks the
server. An operator can set them inconsistently — `marketplace-live` with the
endpoint aimed at a simulator is in fact the supported way to develop the live
path — so the mode alone cannot be trusted to describe what happened.

The reported `source` is therefore DERIVED, not copied from the parameter: if an
endpoint override is in effect the answer came from something that is not AWS, so
it is reported as `simulated` however the mode is set. `marketplace-live` — the
only source `isVerifiedEntitlement()` treats as real, and the only one extension
authors are told to trust — is reachable only when boto3 is talking to real AWS
Marketplace. See `_reported_source`.

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
   at all. For such a listing GetEntitlements returns an empty list forever —
   VERIFIED, including from the seller account with the correct product code, so
   this is not a permissions artefact.

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
    AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT           (optional) botocore endpoint
    AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE  overrides. Non-empty means
    AWS_ENDPOINT_URL                                  "not real AWS", which
                               downgrades the reported source to `simulated`.
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
# Default to the LIVE path. The template always sets this, so the default only
# applies to a misconfigured deployment — and there the safe landing place is a
# real check, not an unverifiable one. (The three entitlement Lambdas previously
# disagreed here: this one defaulted to "marketplace", subscribe/unsubscribe to
# "simulator", from the same env var.)
_SOURCE_TAG = os.environ.get("SIMULATOR_SOURCE_TAG", "marketplace-live")
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
# The AWS Marketplace Agreement API is not available in every region; us-east-1
# is where AWS Marketplace itself lives and is the documented default.
_AGREEMENT_REGION = os.environ.get("MARKETPLACE_AGREEMENT_REGION", "us-east-1")
# The live, buyer-side path. Kept as a distinct tag rather than an ad-hoc branch
# so `simulator` / `marketplace` (dev + CI) behave EXACTLY as before.
_LIVE_TAG = "marketplace-live"


def _agreement_api_regions() -> frozenset:
    """Regions where the Agreement API exists, across every known partition.

    Read from the bundled botocore endpoint data rather than hardcoded, so it
    tracks the SDK. In the `aws` partition that is `{us-east-1}` only — the API
    does not exist in us-west-2, and `MARKETPLACE_AGREEMENT_REGION` is an
    operator-settable parameter, so pointing it at the stack's own Region is an
    easy mistake that turns every check into a permanent `advisory` with a
    misleading "missing permission" message. We warn instead of refusing: the
    union across partitions keeps a GovCloud/ISO deployment from being told its
    own correct Region is wrong.
    """
    try:
        session = boto3.Session()
        return frozenset(
            region
            for partition in session.get_available_partitions()
            for region in session.get_available_regions(
                "marketplace-agreement", partition_name=partition
            )
        )
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not break import
        logger.debug("Could not enumerate Agreement API regions: %s", exc)
        return frozenset()


_AGREEMENT_API_REGIONS = _agreement_api_regions()

# Endpoint overrides that point boto3 at something other than real AWS.
#
# botocore derives the two service-specific names from the service models
# ("Marketplace Agreement" / "Marketplace Entitlement Service"); `AWS_ENDPOINT_URL`
# is the global override that applies to every service. The CloudFormation
# template ALWAYS sets the two service-specific vars — to the empty string when
# no simulator is configured — so presence is meaningless here and only a
# non-empty value counts.
_ENDPOINT_OVERRIDE_VARS = (
    "AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT",
    "AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE",
    "AWS_ENDPOINT_URL",
)


def _endpoint_override() -> str:
    """Return the first non-empty Marketplace endpoint override, else ""."""
    for var in _ENDPOINT_OVERRIDE_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            return value
    return ""


_ENDPOINT_OVERRIDE = _endpoint_override()


def _reported_source(source_tag: str, endpoint_override: str) -> str:
    """Map the deployment MODE to the source we may honestly REPORT.

    The DEPLOYMENT MODE and the SOURCE REPORTED TO EXTENSIONS are deliberately
    separate concerns, and this is where they part company.

    `SIMULATOR_SOURCE_TAG` has four modes because they behave differently *here*
    (notably `simulator` synthesises a productCode below). But `simulator` and
    `marketplace` are indistinguishable to a CONSUMER: both call the seller-side
    GetEntitlements API, which returns 200-with-an-empty-list from a buyer
    account and therefore proves nothing against real AWS. Both were already
    reported as unverified and already shared one explanation string in the UI.

    So both collapse to one reported source, `simulated`. Reporting the mode
    verbatim also made `marketplace` — the WEAKEST source — read more
    authoritative than `marketplace-live`, which is exactly backwards.

    `marketplace-live` collapses to `simulated` too WHENEVER AN ENDPOINT
    OVERRIDE IS IN EFFECT. That combination is legitimate and supported — it is
    how the buyer-side path is developed against the marketplace-simulator — but
    the answer still came from a server the operator chose, so claiming the one
    source documented as "a real check happened" would be a lie that extension
    authors are explicitly told to rely on (`entitlementVerified`,
    `isVerifiedEntitlement`). Deriving this from the endpoint rather than from
    the mode parameter is what makes the claim unforgeable by configuration:
    there is no combination of parameters that reports `marketplace-live` while
    boto3 is aimed somewhere else.
    """
    if source_tag in ("simulator", "marketplace"):
        return "simulated"
    if source_tag == _LIVE_TAG and endpoint_override:
        return "simulated"
    return source_tag


_REPORTED_SOURCE = _reported_source(_SOURCE_TAG, _ENDPOINT_OVERRIDE)

if _SOURCE_TAG == _LIVE_TAG and _ENDPOINT_OVERRIDE:
    logger.warning(
        "SIMULATOR_SOURCE_TAG=%s but a Marketplace endpoint override is in "
        "effect (%s), so SearchAgreements will NOT reach real AWS Marketplace. "
        "Reporting entitlementSource=%r instead of %r; extensions will see "
        "entitlementVerified=false. This is expected in development and is NOT "
        "expected in production.",
        _SOURCE_TAG,
        _ENDPOINT_OVERRIDE,
        _REPORTED_SOURCE,
        _LIVE_TAG,
    )

if (
    _SOURCE_TAG == _LIVE_TAG
    and not _ENDPOINT_OVERRIDE
    and _AGREEMENT_API_REGIONS
    and _AGREEMENT_REGION not in _AGREEMENT_API_REGIONS
):
    logger.warning(
        "MARKETPLACE_AGREEMENT_REGION=%r is not a Region where the AWS "
        "Marketplace Agreement API exists (known: %s). Every SearchAgreements "
        "call will fail to connect and every entitlement will degrade to "
        "advisory with a misleading 'missing permission' hint. Set it to "
        "us-east-1 (the default).",
        _AGREEMENT_REGION,
        ", ".join(sorted(_AGREEMENT_API_REGIONS)),
    )

_METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "GENAIDP")

_dynamodb = boto3.resource("dynamodb")


def _emit_unverified_grant_metric(feature_id: str, source: str) -> None:
    """Record that a PAID extension was granted access without verification.

    Emitted when the host answers ACTIVE for a marketplace feature from `auto`
    (checks disabled) or `advisory` (check unreachable, allowed rather than
    locking out a possibly-paying customer). Both states are invisible in the
    product otherwise — the page looks exactly like a real subscription — so this
    is the operator-side signal that they are happening at all, and how often.

    Uses **CloudWatch Embedded Metric Format** (a structured log line) rather
    than `idp_common.metrics.put_metric` / `PutMetricData`, deliberately:
    `checkFeatureEntitlement` runs on every page load, so a synchronous
    CloudWatch API call would add latency to an interactive path and require
    `cloudwatch:PutMetricData` on this role. EMF costs one log write and no IAM.

    Never raises: a metric must not be able to break the resolver.
    """
    try:
        logger.info(
            json.dumps(
                {
                    "_aws": {
                        # Required by the EMF spec — a record without it is
                        # ingested as a plain log line and silently produces no
                        # metric, which is the worst outcome for a signal whose
                        # whole job is to be noticed.
                        "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                        "CloudWatchMetrics": [
                            {
                                "Namespace": _METRIC_NAMESPACE,
                                "Dimensions": [["FeatureId", "EntitlementSource"]],
                                "Metrics": [
                                    {
                                        "Name": "UnverifiedEntitlementGrant",
                                        "Unit": "Count",
                                    }
                                ],
                            }
                        ],
                    },
                    "FeatureId": feature_id,
                    "EntitlementSource": source,
                    "UnverifiedEntitlementGrant": 1,
                    "message": (
                        f"Granted access to paid feature {feature_id!r} without a "
                        f"verified subscription (source={source})"
                    ),
                }
            )
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the query
        logger.warning("Could not emit UnverifiedEntitlementGrant metric: %s", exc)


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


def _agreement_filters(
    product_identifier: str, *, include_party_type: bool = True
) -> List[Dict[str, Any]]:
    """Build the SearchAgreements filter list.

    `PartyType=Acceptor` is what makes this the BUYER-side query, and on real AWS
    the four-filter set is the only combination accepted — verified: dropping
    PartyType returns `ValidationException: Provided combination of filters is not
    supported`, and passing two `ResourceIdentifier` values returns `Provided
    filter values is invalid`. So it is not negotiable against AWS, and
    `include_party_type=False` exists solely for the simulator (see
    `_search_active_agreements`).
    """
    filters: List[Dict[str, Any]] = []
    if include_party_type:
        filters.append({"name": "PartyType", "values": ["Acceptor"]})
    filters.extend(
        [
            {"name": "AgreementType", "values": ["PurchaseAgreement"]},
            {"name": "ResourceIdentifier", "values": [product_identifier]},
            {"name": "Status", "values": ["ACTIVE"]},
        ]
    )
    return filters


def _diagnose_agreement_failure(exc: Exception) -> str:
    """Classify a SearchAgreements failure into an actionable one-liner.

    "Unreachable" and "denied" have completely different fixes and used to be
    collapsed into one message that always blamed IAM — which sent an operator
    who had merely set the wrong Region hunting for a permission they already
    had.
    """
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "") or ""
        if code.startswith("AccessDenied") or code in (
            "UnauthorizedException",
            "UnrecognizedClientException",
        ):
            return "ACCESS DENIED — grant this role aws-marketplace:SearchAgreements"
        if code == "ValidationException":
            return (
                "REQUEST REJECTED — the endpoint did not accept the buyer-side "
                "filter set; if this is a simulator it does not implement the "
                "real API surface"
            )
        if "Throttl" in code or code == "TooManyRequestsException":
            return "THROTTLED — transient, retry"
        return f"API ERROR ({code})"
    return (
        f"UNREACHABLE — could not reach the Agreement API in region "
        f"{_AGREEMENT_REGION!r}"
        + (
            f" (the API exists only in: {', '.join(sorted(_AGREEMENT_API_REGIONS))})"
            if _AGREEMENT_API_REGIONS
            and _AGREEMENT_REGION not in _AGREEMENT_API_REGIONS
            else ""
        )
    )


def _summarize_agreements(resp: Dict[str, Any]) -> Tuple[str, Optional[datetime]]:
    """Map a SearchAgreements response to (outcome, latest_end_time)."""
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


def _search_active_agreements(
    product_id: str, product_code: Optional[str] = None
) -> Tuple[str, Optional[datetime]]:
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

    Simulator compatibility
    -----------------------
    The marketplace-simulator implements a SUBSET of the API, and against it the
    canonical query fails outright: `ValidationException: unknown filter name:
    PartyType` (observed in production logs on a simulator-backed stack, which is
    why every check there degraded to `advisory`). It also records agreements
    against the product CODE rather than the product ENTITY id, because its buyer
    console is keyed on productCode (`/marketplace/pp/<productCode>` — see
    subscribe_feature), so even a PartyType-less query finds nothing under
    `productId`.

    Both accommodations are therefore made, and BOTH are gated on an endpoint
    override actually being in effect:
      1. retry without `PartyType` when the endpoint rejects it, and
      2. retry under `productCode` when `productId` matches nothing.
    Real AWS never takes either path — it accepts PartyType, and the reduced
    filter set is rejected there anyway — so the production query is unchanged
    and cannot be silently weakened. And because an endpoint override also forces
    the reported source to `simulated`, nothing found this way can ever be
    reported as a verified subscription.
    """
    if not product_id:
        logger.warning(
            "No productId available; cannot run a buyer-side agreement check. "
            "Add `productId` to the feature's catalog entry."
        )
        return "UNKNOWN", None

    identifiers = [product_id]
    if _ENDPOINT_OVERRIDE and product_code and product_code != product_id:
        identifiers.append(product_code)

    include_party_type = True
    for identifier in identifiers:
        for attempt in range(2):
            try:
                resp = _agreement_client().search_agreements(
                    catalog="AWSMarketplace",
                    filters=_agreement_filters(
                        identifier, include_party_type=include_party_type
                    ),
                )
            except ClientError as exc:
                is_unknown_filter = (
                    exc.response.get("Error", {}).get("Code") == "ValidationException"
                )
                # One retry, simulator-only: drop PartyType and try again. Then
                # remember it for the remaining identifiers.
                if (
                    attempt == 0
                    and include_party_type
                    and is_unknown_filter
                    and _ENDPOINT_OVERRIDE
                ):
                    logger.warning(
                        "The Marketplace endpoint override (%s) rejected the "
                        "buyer-side PartyType filter (%s). Retrying without it — "
                        "this is a simulator that implements a subset of the "
                        "real API; the result is reported as an UNVERIFIED "
                        "source.",
                        _ENDPOINT_OVERRIDE,
                        exc,
                    )
                    include_party_type = False
                    continue
                logger.warning(
                    "SearchAgreements failed for product %s: %s [%s]. Treating "
                    "entitlement as UNKNOWN (advisory allow) rather than denying "
                    "a possibly-paying customer.",
                    identifier,
                    exc,
                    _diagnose_agreement_failure(exc),
                )
                return "UNKNOWN", None
            except BotoCoreError as exc:
                # Endpoint unreachable, connect/read timeout, no credentials —
                # all indistinguishable from "not subscribed".
                logger.warning(
                    "SearchAgreements failed for product %s: %s [%s]. Treating "
                    "entitlement as UNKNOWN (advisory allow) rather than denying "
                    "a possibly-paying customer.",
                    identifier,
                    exc,
                    _diagnose_agreement_failure(exc),
                )
                return "UNKNOWN", None

            outcome, end_time = _summarize_agreements(resp)
            if outcome == "ACTIVE":
                if identifier != product_id:
                    logger.info(
                        "Matched an agreement under productCode %r rather than "
                        "productId %r — expected against the simulator, whose "
                        "buyer console is keyed on productCode.",
                        identifier,
                        product_id,
                    )
                return outcome, end_time
            break  # NONE for this identifier — fall through to the next, if any

    return "NONE", None


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

    # Read the catalog entry ONCE: it tells us whether this is an OSS feature and
    # carries the Marketplace identity we need before the feature is installed.
    #
    # NB: this read now happens in `auto` mode too, which it previously skipped.
    # The cost is one extra S3 GetObject per call on auto-mode stacks; the reason
    # is that `auto` cannot otherwise tell a PAID extension from an OSS one, and
    # a metric that misses the primary bypass path is not worth emitting. Every
    # other branch already performs this same read, so it is consistent with the
    # resolver's existing cost, not a new class of work.
    catalog_entry = _read_catalog_entry(feature_id) or {}
    is_marketplace_feature = (catalog_entry.get("source") or "oss") == "marketplace"

    # OSS features have no AWS Marketplace contract — they install directly
    # regardless of whether a simulator/Marketplace endpoint is configured.
    # Short-circuit to ACTIVE so the UI shows the Install prompt instead of
    # "Subscription required". This mirrors get_feature_launch_url, which skips
    # the entitlement check for source=="oss" catalog entries. Only consult the
    # entitlement endpoint for marketplace features below.
    #
    # Checked BEFORE the `auto` branch, deliberately. Being open-source is a
    # property of the EXTENSION; the deployment mode cannot change it. With the
    # order reversed, an OSS extension reported `auto` on an auto-mode stack and
    # `oss` everywhere else, so `oss` was not a dependable signal for "this is not
    # a paid extension" — the one thing it exists to say.
    #
    # Must be an EXPLICIT source=="oss" test, not `not is_marketplace_feature`:
    # an absent or unreadable catalog entry also yields a falsy
    # is_marketplace_feature, and treating that as OSS would grant access to a
    # paid extension whose catalog entry merely failed to load. Unknown falls
    # through to the entitlement check below, which is the safe direction.
    if catalog_entry.get("source") == "oss":
        return {
            "featureId": feature_id,
            "state": "ACTIVE",
            "expiresAt": None,
            "customerIdentifier": None,
            "productCode": None,
            "source": "oss",
        }

    # Auto-subscribe mode: subscription checks are switched off for this stack.
    # Every catalog feature is treated as subscribed so the UI goes straight to
    # the Install prompt; no Marketplace call is made (the boto3 client is never
    # instantiated). Confirmed-OSS features returned above, but an UNKNOWN catalog
    # entry still reaches here — hence the guard: only emit the bypass metric when
    # we know this is a paid extension.
    if _SOURCE_TAG == "auto":
        if is_marketplace_feature:
            _emit_unverified_grant_metric(feature_id, "auto")
        return {
            "featureId": feature_id,
            "state": "ACTIVE",
            "expiresAt": None,
            "customerIdentifier": None,
            "productCode": None,
            "source": "auto",
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
        outcome, end_time = _search_active_agreements(product_id, product_code)
        if outcome == "ACTIVE":
            # `_REPORTED_SOURCE`, never `_LIVE_TAG` literal: on an
            # endpoint-overridden stack this answer came from a simulator, and
            # reporting the one source documented as verified would let a fake
            # Marketplace mint `entitlementVerified: true`.
            if _REPORTED_SOURCE != _LIVE_TAG and is_marketplace_feature:
                _emit_unverified_grant_metric(feature_id, _REPORTED_SOURCE)
            return {
                "featureId": feature_id,
                "state": "ACTIVE",
                "expiresAt": end_time.isoformat().replace("+00:00", "Z")
                if end_time
                else None,
                "customerIdentifier": None,
                "productCode": product_code,
                "source": _REPORTED_SOURCE,
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
                "source": _REPORTED_SOURCE,
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
        _emit_unverified_grant_metric(feature_id, "advisory")
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
                "source": _REPORTED_SOURCE,
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

    # A simulator / endpoint-override ACTIVE is not a real subscription check:
    # boto3 was pointed at whatever AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE
    # names. Record it for a paid feature so a production host aimed at a
    # simulator is visible rather than rendering as a clean "subscription active".
    if evaluated["state"] == "ACTIVE" and is_marketplace_feature:
        _emit_unverified_grant_metric(feature_id, _REPORTED_SOURCE)

    return {
        "featureId": feature_id,
        "state": evaluated["state"],
        "expiresAt": evaluated["expiresAt"],
        "customerIdentifier": resolved_cid,
        "productCode": product_code,
        "source": _REPORTED_SOURCE,
    }
