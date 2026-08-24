# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AppSync Query.checkFeatureEntitlement resolver.

Resolves the caller's entitlement state for a given feature.

The authority is PER EXTENSION, not per stack
---------------------------------------------
Each extension declares a `licenseMode` naming the authority that must answer for
it, and one stack resolves different extensions against different authorities in
the same invocation:

    none             → check nothing. Reports `oss` for an open-source extension
                       and `auto` for a paid one (checks switched off).
    simulated        → seller-side `marketplace-entitlement:GetEntitlements`
                       against the stack's marketplace-simulator. The dev/CI
                       path, and the one the simulator's subscribe/unsubscribe
                       admin API is wired against end to end.
    marketplace-live → buyer-side AWS Marketplace **Agreement** API
                       (`SearchAgreements`) against REAL AWS. The production path.

This is what lets one stack host a listed, published extension whose subscription
must be confirmed against real AWS Marketplace *alongside* in-development
extensions checked against a simulator. Choosing the authority once for the whole
stack made that impossible: pointing the stack at a simulator pointed it at the
simulator for everything, including a live listed product — so the host showed a
simulator-backed "Subscription active" for an extension that only honours real
Marketplace, and the extension correctly disagreed.

Where the mode comes from, in order (see `_resolve_authority`):
  1. `licenseMode` on the feature's `InstalledFeatures` row — what the extension
     itself enforces, propagated through `registerFeature` at install.
  2. `licenseMode` on the catalog entry — what the host is configured to check.
  3. The legacy stack-wide `SubscriptionMode` — migration only, for a catalog
     published before `licenseMode` existed.
  4. `marketplace-live` for a marketplace catalog entry, `none` otherwise.

`SubscriptionMode` survives only as a KILL SWITCH (`auto` = check nothing on this
stack). It no longer selects the authority and no longer names the reported
source. What stays stack-scoped is where the simulator LIVES
(`FeaturePlatformSimulatorEndpoint`); whether an extension uses it does not.

WHICH API we call and WHERE the call goes are two axes, and only one of them
decides what we may CLAIM
-------------------------------------------------------------------------
The reported `source` is DERIVED from the endpoint THIS CALL used, never from a
parameter and never from process-wide state. `marketplace-live` — the only source
`isVerifiedEntitlement()` treats as real, and the only one extension authors are
told to trust — is reachable only with `endpoint=None`, i.e. real AWS
Marketplace. `_authority_for_mode` pins the live authority to real AWS by
construction, so **a simulator-backed answer can never report
`marketplace-live`**, and one extension's authority cannot leak into another's
answer. See `_reported_source`.

Note the endpoint is passed explicitly to every client. The legacy
`AWS_ENDPOINT_URL_MARKETPLACE_*` variables are POPPED from the environment at
import (`_consume_endpoint_overrides`): environment variables are per-function, so
while boto3 read the endpoint from the environment, per-extension authority was
not merely unimplemented but unimplementable.

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

Returns `{state: ACTIVE, source: 'auto'}` immediately when checks are switched off
for the feature — either the stack kill switch (SIMULATOR_SOURCE_TAG=auto) or an
explicit `licenseMode: none` — so the UI goes straight to the Install prompt.
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
    SIMULATOR_SOURCE_TAG       Legacy stack-wide setting. Only "auto" still has a
                               live meaning (kill switch: check nothing). The other
                               values are read solely as step 3 of the migration
                               chain for a catalog with no `licenseMode`.
    MARKETPLACE_SIMULATOR_ENDPOINT  Where the marketplace-simulator lives. Used as
                               the explicit endpoint_url for the `simulated`
                               authority. Blank = no simulator on this stack.
    MARKETPLACE_AGREEMENT_REGION  Region for the Agreement API (default us-east-1)
    AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT           Legacy botocore overrides.
    AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE  POPPED at import and used
    AWS_ENDPOINT_URL                                  only as a fallback for the
                               simulator location; never the per-extension
                               mechanism.
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
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_DEFAULT_CUSTOMER_IDENTIFIER = os.environ.get("DEFAULT_CUSTOMER_IDENTIFIER", "")
_DEFAULT_BUYER_ACCOUNT_ID = os.environ.get("DEFAULT_BUYER_ACCOUNT_ID", "111122223333")
_CONFIGURATION_BUCKET = os.environ.get("CONFIGURATION_BUCKET", "")
_CATALOG_KEY = os.environ.get("CATALOG_KEY", "config_library/catalog.json")
_INSTALLED_FEATURES_TABLE = os.environ.get("INSTALLED_FEATURES_TABLE", "")
# The AWS Marketplace Agreement API is not available in every region; us-east-1
# is where AWS Marketplace itself lives and is the documented default.
_AGREEMENT_REGION = os.environ.get("MARKETPLACE_AGREEMENT_REGION", "us-east-1")


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

# ---------------------------------------------------------------------------
# Legacy endpoint overrides — CONSUMED (popped), not read.
#
# botocore derives the two service-specific names from the service models
# ("Marketplace Agreement" / "Marketplace Entitlement Service"); `AWS_ENDPOINT_URL`
# is the global override that applies to every service.
#
# These are POPPED from os.environ at import, before any boto3 client exists, and
# that is the whole point. Environment variables are PER-FUNCTION, so as long as
# boto3 picks the endpoint up from the environment, one `checkFeatureEntitlement`
# Lambda can only ever be aimed at one authority — the simulator for every
# extension, or real AWS for every extension. The env-var mechanism was not a
# stack-wide *policy*; it was what FORCED stack-wide behaviour, and no amount of
# per-extension configuration works until it is gone. Every client below is now
# built with an explicit `endpoint_url=` chosen per request from the feature's own
# license mode.
#
# The popped value survives as `_LEGACY_ENDPOINT_OVERRIDE`, used for exactly two
# things: as a fallback for where the simulator lives (step 3 of the migration
# chain), and to reproduce the previous release's behaviour for a stack whose
# catalog predates `licenseMode`. It is never the per-extension mechanism.
#
# The template ALWAYS sets the two service-specific vars — to the empty string
# when no simulator is configured — so presence is meaningless and only a
# non-empty value counts.
_ENDPOINT_OVERRIDE_VARS = (
    "AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT",
    "AWS_ENDPOINT_URL_MARKETPLACE_ENTITLEMENT_SERVICE",
    "AWS_ENDPOINT_URL",
)


def _consume_endpoint_overrides() -> str:
    """Pop every legacy override from the environment; return the first non-empty.

    Popping the *global* `AWS_ENDPOINT_URL` too is deliberate: it would otherwise
    also redirect this function's S3 (catalog) and DynamoDB (install rows)
    clients, which must always talk to real AWS. Nothing in the shipped template
    sets it; a stack that did was silently misdirecting those reads.
    """
    first = ""
    for var in _ENDPOINT_OVERRIDE_VARS:
        value = (os.environ.pop(var, "") or "").strip()
        if value and not first:
            first = value
    return first


_LEGACY_ENDPOINT_OVERRIDE = _consume_endpoint_overrides()

# Where the simulator lives. STACK-scoped on purpose: the stack says where the
# simulator is, each extension says whether it uses it. Location is a property of
# the deployment; authority is a property of the extension.
_SIMULATOR_ENDPOINT = (
    os.environ.get("MARKETPLACE_SIMULATOR_ENDPOINT", "").strip()
    or _LEGACY_ENDPOINT_OVERRIDE
)

# ---------------------------------------------------------------------------
# License modes — the per-extension declaration of WHICH AUTHORITY must answer.
#
# Deliberately the same three words as the `entitlementSource` vocabulary so the
# host and the extension speak one language. Note `none` here means "check
# nothing", which is NOT what the `none` *source* means ("no productCode
# registered, access denied") — a mode of `none` reports source `oss` for an
# open-source extension and `auto` for a paid one, because `auto` is precisely
# "subscription checks are switched off". The union is unchanged.
_MODE_NONE = "none"
_MODE_SIMULATED = "simulated"
_MODE_LIVE = "marketplace-live"
_LICENSE_MODES = (_MODE_NONE, _MODE_SIMULATED, _MODE_LIVE)

# Which API answers for each authority. `marketplace-live` is buyer-side
# SearchAgreements against real AWS; `simulated` is the seller-side
# GetEntitlements dev loop, which is the one the simulator's subscribe /
# unsubscribe admin API is wired end-to-end against.
_API_AGREEMENT = "agreement"
_API_ENTITLEMENT = "entitlement"

# The stack-wide setting, reduced to a KILL SWITCH. It used to do three jobs —
# pick the authority, name the reported source, and switch checks off — and only
# the last is genuinely stack-scoped. Kept under its old env var name so an
# existing stack keeps working; see `_legacy_stack_mode` for the migration path.
_SUBSCRIPTION_MODE = os.environ.get("SIMULATOR_SOURCE_TAG", "").strip()
_CHECKS_DISABLED = _SUBSCRIPTION_MODE == "auto"

# Step 3 of the resolution chain: what this stack's legacy setting implies when
# NEITHER the install row NOR the catalog declares a mode. Only reachable from a
# catalog.json published before `licenseMode` existed.
_LEGACY_STACK_MODES = {
    "auto": _MODE_NONE,
    "simulator": _MODE_SIMULATED,
    "marketplace": _MODE_SIMULATED,
    "marketplace-live": _MODE_LIVE,
}


class _Authority(NamedTuple):
    """The authority that will answer for ONE feature, on ONE request.

    Everything the resolver needs to make a call and then describe it honestly:

        mode        the license mode this resolved to (`_LICENSE_MODES`)
        api         which API answers — `_API_AGREEMENT` (buyer-side
                    SearchAgreements) or `_API_ENTITLEMENT` (seller-side
                    GetEntitlements), or "" when nothing is called
        endpoint    the explicit `endpoint_url` for the client. **None means real
                    AWS.** This is the field the reported source is anchored to,
                    which is why it is carried per request rather than read from
                    process-wide state.
        provenance  where the mode came from — for the log line that has to
                    explain a four-step resolution chain to an operator
    """

    mode: str
    api: str
    endpoint: Optional[str]
    provenance: str


def _valid_mode(raw: Any) -> Optional[str]:
    """Normalize a declared license mode, or None if it isn't one of the three."""
    if not isinstance(raw, str):
        return None
    mode = raw.strip()
    return mode if mode in _LICENSE_MODES else None


def _authority_for_mode(mode: str, provenance: str) -> _Authority:
    """Bind a license mode to a concrete API + endpoint.

    `marketplace-live` ALWAYS gets `endpoint=None`. That is the load-bearing line
    in this module: the live authority is real AWS by construction, so no
    parameter, env var or catalog entry can produce a `marketplace-live` answer
    from somewhere else.
    """
    if mode == _MODE_NONE:
        return _Authority(_MODE_NONE, "", None, provenance)
    if mode == _MODE_LIVE:
        return _Authority(_MODE_LIVE, _API_AGREEMENT, None, provenance)
    # `simulated` → the seller-side GetEntitlements dev loop, which is what the
    # simulator's subscribe/unsubscribe admin API is wired against end to end.
    return _Authority(
        _MODE_SIMULATED, _API_ENTITLEMENT, _SIMULATOR_ENDPOINT or None, provenance
    )


def _legacy_stack_authority() -> Optional[_Authority]:
    """Step 3: what this stack's pre-`licenseMode` setting implies.

    Only reached when neither the install row nor the catalog entry declares a
    mode, i.e. an existing stack running a catalog.json published before this
    change. It exists so such a stack keeps behaving exactly as it did.

    The `marketplace-live` + endpoint-override case is reproduced faithfully,
    including the API: the previous release called the buyer-side Agreement API
    against the simulator (with the PartyType / productCode accommodations) and
    reported `simulated`. Mapping it to the seller-side API instead would change
    which API a live stack calls during an upgrade, which is not a thing a
    migration path should do silently.
    """
    mode = _LEGACY_STACK_MODES.get(_SUBSCRIPTION_MODE)
    if mode is None:
        return None
    provenance = f"legacy stack SubscriptionMode={_SUBSCRIPTION_MODE!r}"
    if mode == _MODE_LIVE and _LEGACY_ENDPOINT_OVERRIDE:
        return _Authority(
            _MODE_SIMULATED,
            _API_AGREEMENT,
            _LEGACY_ENDPOINT_OVERRIDE,
            provenance + " with a legacy endpoint override",
        )
    return _authority_for_mode(mode, provenance)


def _resolve_authority(
    feature_id: str,
    installed_row: Dict[str, Any],
    catalog_entry: Dict[str, Any],
) -> Tuple[_Authority, Optional[str], Optional[str]]:
    """Pick the authority for ONE feature. Returns (authority, declared, catalog).

    `declared` is the mode the INSTALLED EXTENSION says it enforces (propagated
    through registerFeature at install); `catalog` is the mode the HOST says it
    should check. Both are returned so the caller can report a mismatch.

    Resolution order:
      1. `licenseMode` on the InstalledFeatures row — what the extension actually
         enforces. It wins because aligning the host with the extension is the
         only way the two gates can agree; a host checking a simulator for an
         extension that honours real Marketplace is the reported bug.
      2. `licenseMode` in the catalog entry — what the host is configured to
         check. Written by publish.py for every marketplace entry.
      3. The legacy stack-wide setting — migration only.
      4. Hard default: `marketplace-live` for a marketplace catalog entry,
         `none` for anything else.

    Step 4's marketplace default is deliberately the OPPOSITE of the
    extension-side default (`none`, i.e. serve-and-declare). The failure
    directions differ, so the safe direction differs:
      - on the EXTENSION side an unrecognised value must not lock a paying
        customer out, so it degrades to serving;
      - on the HOST side an unrecognised value must not OVER-CLAIM verification
        for something listed in the *marketplace* catalog, so it degrades to the
        strictest authority.
    Do not "fix" the inconsistency — it is the point.
    """
    # The kill switch outranks every declaration: "check nothing on this stack"
    # is genuinely stack-scoped, and a dev escape hatch that per-extension
    # configuration could override would not be one.
    if _CHECKS_DISABLED:
        return (
            _Authority(
                _MODE_NONE, "", None, "stack kill switch (SubscriptionMode=auto)"
            ),
            _valid_mode(installed_row.get("licenseMode")),
            _valid_mode(catalog_entry.get("licenseMode")),
        )

    declared = _valid_mode(installed_row.get("licenseMode"))
    from_catalog = _valid_mode(catalog_entry.get("licenseMode"))

    if installed_row.get("licenseMode") and declared is None:
        logger.warning(
            "Feature %r declared an unrecognised licenseMode %r at install; "
            "ignoring it and falling back to the catalog/stack default. Valid "
            "values are %s.",
            feature_id,
            installed_row.get("licenseMode"),
            ", ".join(_LICENSE_MODES),
        )
    if catalog_entry.get("licenseMode") and from_catalog is None:
        logger.warning(
            "Catalog entry for %r has an unrecognised licenseMode %r; ignoring.",
            feature_id,
            catalog_entry.get("licenseMode"),
        )

    if declared is not None:
        return (
            _authority_for_mode(declared, "extension install row"),
            declared,
            from_catalog,
        )
    if from_catalog is not None:
        return (
            _authority_for_mode(from_catalog, "host catalog entry"),
            declared,
            from_catalog,
        )

    legacy = _legacy_stack_authority()
    if legacy is not None:
        return legacy, declared, from_catalog

    is_marketplace = (catalog_entry.get("source") or "oss") == "marketplace"
    fallback = _MODE_LIVE if is_marketplace else _MODE_NONE
    return (
        _authority_for_mode(
            fallback,
            "default for a %s catalog entry"
            % (catalog_entry.get("source") or "unknown"),
        ),
        declared,
        from_catalog,
    )


def _reported_source(authority: _Authority) -> str:
    """The source we may honestly report for an answer from `authority`.

    Anchored to `authority.endpoint` — the endpoint THIS call used — rather than
    to any process-wide state, because a single invocation now resolves different
    features against different authorities. The invariant established previously
    is preserved and merely re-anchored: **a simulator-backed answer can never
    report `marketplace-live`.**

    The `endpoint` test on the live branch is belt-and-braces —
    `_authority_for_mode` already pins the live authority to real AWS — but it is
    cheap, it is the one claim extension authors are told to trust
    (`entitlementVerified` / `isVerifiedEntitlement`), and it keeps the invariant
    stated where it is read rather than only where it is constructed.
    """
    if authority.mode == _MODE_NONE:
        # Checks are off for this extension. OSS returns earlier with `oss`, so
        # anything reaching here is paid-or-unknown, which is exactly `auto`.
        return "auto"
    if authority.mode == _MODE_LIVE:
        return _MODE_LIVE if not authority.endpoint else _MODE_SIMULATED
    return _MODE_SIMULATED


if (
    not _CHECKS_DISABLED
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


def _installed_row(feature_id: str) -> Dict[str, Any]:
    """Read the feature's InstalledFeatures row. Returns {} when absent.

    Carries `productCode` (baked from the manifest at install) and `licenseMode`
    (what the extension itself enforces, propagated through registerFeature).
    Both are absent before install, and `licenseMode` is also absent for an
    extension installed before it existed — hence the resolution chain.
    """
    if not _INSTALLED_FEATURES_TABLE:
        return {}
    try:
        return (
            _dynamodb.Table(_INSTALLED_FEATURES_TABLE)
            .get_item(Key={"featureId": feature_id})
            .get("Item")
            or {}
        )
    except Exception as exc:  # noqa: BLE001 — treat lookup failure as "absent"
        logger.warning(
            "Could not read InstalledFeatures row for %s: %s", feature_id, exc
        )
        return {}


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


# One cached client PER AUTHORITY, not per call and not one for the process.
#
# Keyed by (service, endpoint) so a mixed-mode stack builds at most two
# marketplace clients per service — one aimed at real AWS, one at the simulator —
# and reuses them across features and invocations. Building a boto3 client costs
# ~50-100ms of model loading, which on an interactive path called per page load
# is worth caching; keying on the endpoint is what keeps the cache from leaking
# one extension's authority into another's answer.
_clients: Dict[Tuple[str, Optional[str], Optional[str]], Any] = {}


def _marketplace_client(
    service: str, endpoint: Optional[str], region: Optional[str] = None
):
    """Build (or reuse) a marketplace client aimed at exactly one endpoint.

    `endpoint=None` means real AWS. Because the legacy `AWS_ENDPOINT_URL_*` vars
    were popped from the environment at import, "no explicit endpoint" now
    genuinely resolves to AWS rather than silently inheriting a simulator.
    """
    key = (service, endpoint, region)
    client = _clients.get(key)
    if client is None:
        kwargs: Dict[str, Any] = {"config": _CLIENT_CONFIG}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        client = boto3.client(service, **kwargs)
        _clients[key] = client
    return client


def _entitlement_client(authority: _Authority):
    """Seller-side GetEntitlements client for this authority."""
    return _marketplace_client("marketplace-entitlement", authority.endpoint)


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
    authority: _Authority,
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
    client = _entitlement_client(authority)
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


def _agreement_client(authority: _Authority):
    """Buyer-side AWS Marketplace Agreement API client for this authority.

    The region is pinned even when an endpoint is supplied: botocore still needs a
    region to sign with, and `_AGREEMENT_REGION` is the only one the real API is
    published in.
    """
    return _marketplace_client(
        "marketplace-agreement", authority.endpoint, _AGREEMENT_REGION
    )


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
    product_id: str,
    authority: _Authority,
    product_code: Optional[str] = None,
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

    Both accommodations are therefore made, and BOTH are gated on THIS CALL'S
    authority being simulator-backed (`authority.endpoint` set) rather than on any
    process-wide state:
      1. retry without `PartyType` when the endpoint rejects it, and
      2. retry under `productCode` when `productId` matches nothing.
    The live authority is pinned to `endpoint=None` by `_authority_for_mode`, so
    it never takes either path — and real AWS would reject the reduced filter set
    anyway. The production query is therefore unchanged and cannot be weakened by
    a sibling extension on the same stack running in simulator mode. Because a
    simulator-backed authority also reports `simulated`, nothing found this way
    can ever be reported as a verified subscription.
    """
    if not product_id:
        logger.warning(
            "No productId available; cannot run a buyer-side agreement check. "
            "Add `productId` to the feature's catalog entry."
        )
        return "UNKNOWN", None

    simulator_backed = bool(authority.endpoint)
    identifiers = [product_id]
    if simulator_backed and product_code and product_code != product_id:
        identifiers.append(product_code)

    include_party_type = True
    for identifier in identifiers:
        for attempt in range(2):
            try:
                resp = _agreement_client(authority).search_agreements(
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
                    and simulator_backed
                ):
                    logger.warning(
                        "The simulator endpoint (%s) rejected the buyer-side "
                        "PartyType filter (%s). Retrying without it — this is a "
                        "simulator that implements a subset of the real API; the "
                        "result is reported as an UNVERIFIED source.",
                        authority.endpoint,
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


def _answer(
    feature_id: str,
    state: str,
    *,
    source: str,
    license_mode: str,
    catalog_license_mode: Optional[str] = None,
    declared_license_mode: Optional[str] = None,
    license_mode_mismatch: bool = False,
    expires_at: Optional[str] = None,
    product_code: Optional[str] = None,
    customer_identifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the resolver's response.

    One constructor for every return path, because the response grew three
    diagnostic fields and eight hand-built dicts is eight places to forget one.

        licenseMode           the authority the host actually used for THIS
                              feature — the answer to "who did you ask?"
        declaredLicenseMode   what the installed extension says it enforces
        catalogLicenseMode    what the host catalog says to check
        licenseModeMismatch   the two disagree; the UI explains rather than blocks
    """
    return {
        "featureId": feature_id,
        "state": state,
        "expiresAt": expires_at,
        "customerIdentifier": customer_identifier,
        "productCode": product_code,
        "source": source,
        "licenseMode": license_mode,
        "declaredLicenseMode": declared_license_mode,
        "catalogLicenseMode": catalog_license_mode,
        "licenseModeMismatch": license_mode_mismatch,
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
        return _answer(
            feature_id,
            "ACTIVE",
            source="oss",
            license_mode=_MODE_NONE,
            catalog_license_mode=_valid_mode(catalog_entry.get("licenseMode")),
        )

    # Everything from here is per-EXTENSION. `installed_row` carries what the
    # extension itself enforces (`licenseMode`, propagated at install) plus its
    # productCode; the catalog carries what the host is configured to check.
    installed_row = _installed_row(feature_id)
    authority, declared_mode, catalog_mode = _resolve_authority(
        feature_id, installed_row, catalog_entry
    )
    # The dangerous disagreement is "catalog says simulated, extension enforces
    # marketplace-live": the host would show a simulator-backed subscription the
    # extension is going to ignore. Surfaced, not enforced — the host's job is to
    # stop CLAIMING an authority the extension doesn't use, not to add a second
    # gate that can disagree with the first.
    mismatch = (
        declared_mode is not None
        and catalog_mode is not None
        and declared_mode != catalog_mode
    )
    if mismatch:
        logger.warning(
            "licenseMode MISMATCH for %r: the installed extension enforces %r but "
            "the host catalog declares %r. Using the extension's value (%s) so the "
            "host stops claiming an authority the extension ignores. Fix the "
            "catalog entry in config_library/extensions-marketplace.yaml.",
            feature_id,
            declared_mode,
            catalog_mode,
            authority.mode,
        )
    logger.info(
        "Entitlement authority for %r: mode=%s api=%s endpoint=%s (from %s)",
        feature_id,
        authority.mode,
        authority.api or "-",
        authority.endpoint or "real AWS Marketplace",
        authority.provenance,
    )

    def _answer_for(
        state, *, expires_at=None, source, product_code=None, customer_identifier=None
    ):
        return _answer(
            feature_id,
            state,
            source=source,
            license_mode=authority.mode,
            catalog_license_mode=catalog_mode,
            declared_license_mode=declared_mode,
            license_mode_mismatch=mismatch,
            expires_at=expires_at,
            product_code=product_code,
            customer_identifier=customer_identifier,
        )

    # Checks switched off for this extension: either the stack kill switch, or an
    # explicit `licenseMode: none`. No Marketplace call is made. Confirmed-OSS
    # features returned above, but an UNKNOWN catalog entry still reaches here —
    # hence the guard: only emit the bypass metric when we know this is paid.
    if authority.mode == _MODE_NONE:
        source = _reported_source(authority)
        if is_marketplace_feature:
            _emit_unverified_grant_metric(feature_id, source)
        return _answer_for("ACTIVE", source=source)

    # Resolve product code from the feature's InstalledFeatures row (baked from
    # the manifest at install), FALLING BACK TO THE CATALOG. The fallback is what
    # makes the not-yet-installed path work: that DDB row only exists after
    # install, so before install the row lookup necessarily comes back empty and
    # this resolver used to report NONE/source="none" even for a genuinely
    # subscribed customer — the UI then showed "no entitlement" with no way
    # forward. The catalog has had productCode all along.
    product_code = installed_row.get("productCode") or (
        catalog_entry.get("productCode") or None
    )
    product_id = catalog_entry.get("productId") or ""

    # A simulator authority with nowhere to point is a misconfiguration, not a
    # verdict: `licenseMode: simulated` but no FeaturePlatformSimulatorEndpoint on
    # the stack. Degrade to advisory (allow + declare) rather than inventing an
    # answer, and say which of the two settings to fix.
    if authority.mode == _MODE_SIMULATED and not authority.endpoint:
        logger.warning(
            "Feature %r resolves to licenseMode=simulated (from %s) but this stack "
            "has no simulator endpoint configured. Set "
            "FeaturePlatformSimulatorEndpoint, or change the feature's licenseMode. "
            "Returning advisory ACTIVE.",
            feature_id,
            authority.provenance,
        )
        _emit_unverified_grant_metric(feature_id, "advisory")
        return _answer_for("ACTIVE", source="advisory", product_code=product_code)

    # --- Buyer-side Agreement API path -----------------------------------
    # Checked BEFORE the productCode bail-out below, because this path keys on
    # productId (the SaaS product ENTITY id) — productCode is only useful to the
    # seller-side GetEntitlements API, which cannot answer this question at all.
    if authority.api == _API_AGREEMENT:
        outcome, end_time = _search_active_agreements(
            product_id, authority, product_code
        )
        source = _reported_source(authority)
        if outcome == "ACTIVE":
            # `_reported_source(authority)`, never the `marketplace-live` literal: if this
            # call went to a simulator, reporting the one source documented as
            # verified would let a fake Marketplace mint `entitlementVerified`.
            if source != _MODE_LIVE and is_marketplace_feature:
                _emit_unverified_grant_metric(feature_id, source)
            return _answer_for(
                "ACTIVE",
                source=source,
                product_code=product_code,
                expires_at=end_time.isoformat().replace("+00:00", "Z")
                if end_time
                else None,
            )
        if outcome == "NONE":
            # Authoritative for this account: SearchAgreements is scoped to the
            # caller, so an empty successful result really is "not subscribed
            # here". The UI shows Subscribe.
            return _answer_for("NONE", source=source, product_code=product_code)
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
        return _answer_for("ACTIVE", source="advisory", product_code=product_code)

    if not product_code:
        # Simulator authority with no productCode anywhere: synthesise one, which
        # is what the simulator's own subscribe flow keys on. Keyed on the
        # AUTHORITY now, not on a stack-wide tag — this is the seller-side path, so
        # reaching here at all means the authority is simulator-backed.
        product_code = f"prod-{feature_id}-sim"
        logger.info(
            "No productCode on the install row or catalog for %r; using "
            "synthesized %r for the simulator authority.",
            feature_id,
            product_code,
        )

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
            return _answer_for(
                "NONE",
                source=_reported_source(authority),
                product_code=product_code,
            )

    entitlements = _get_entitlements(
        product_code,
        authority,
        customer_identifier=customer_identifier,
        customer_aws_account_id=account_filter,
    )
    evaluated = _evaluate(entitlements)

    # Echo back the resolved customer identifier from the matched entitlement
    # when we looked up by account (so the UI can display it).
    resolved_cid = customer_identifier
    if resolved_cid is None and entitlements:
        resolved_cid = entitlements[0].get("CustomerIdentifier")

    # A seller-side ACTIVE is never a real subscription check — GetEntitlements
    # returns 200-with-an-empty-list from a buyer account, and here it was aimed at
    # the simulator besides. Record it for a paid feature so a production host
    # pointed at a simulator is visible rather than rendering as a clean
    # "subscription active".
    source = _reported_source(authority)
    if evaluated["state"] == "ACTIVE" and is_marketplace_feature:
        _emit_unverified_grant_metric(feature_id, source)

    return _answer_for(
        evaluated["state"],
        source=source,
        expires_at=evaluated["expiresAt"],
        product_code=product_code,
        customer_identifier=resolved_cid,
    )
