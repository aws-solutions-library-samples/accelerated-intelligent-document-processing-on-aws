# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Seller-side activation endpoint for paid Feature Platform extensions.

Runs in the **seller's** AWS account. A buyer's installed extension calls it to
exchange "I am AWS account X, deploying product Y" for a short-lived, signed
activation token. The extension then requires that token to do anything of value.

Why this has to live seller-side
--------------------------------
A paid extension deploys into the **buyer's** account, and the buyer owns the
Lambda, its environment variables, its IAM role, and its code. So an entitlement
check that runs in the buyer's account can always be edited out. Enforcement is
only possible where the seller controls the code AND the answer — which is here.

It is also the only place the Marketplace APIs can answer the question:
``SearchAgreements`` with ``PartyType=Proposer`` (and ``GetEntitlements`` /
``ResolveCustomer``) are seller-side operations. From a buyer account the same
calls return an empty result rather than an error, which is what makes a
buyer-side gate silently deny every real customer.

How the caller's identity is established
----------------------------------------
The API Gateway REST API in front of this Lambda uses ``AWS_IAM`` authorization
with a resource policy that admits any AWS principal. API Gateway verifies the
SigV4 signature *before* invoking us and reports the verified caller in
``requestContext.identity``. We read the account id from there and **never** from
the request body — a body field would be trivially spoofable, which would let
anyone claim to be a subscribed account.

What this does NOT do
---------------------
It does not make the extension tamper-proof. A determined customer can patch the
extension to skip the token check. This design raises the effort from "flip a
CloudFormation parameter" to "reverse-engineer and modify the product", and gives
the seller a reliable activation signal for commercial follow-up. Enforcement
becomes real only to the extent the token gates something the customer genuinely
needs from the seller (a prompt/strategy config, a hosted planner, model routing).
See the README's threat model.

Environment:
    PRODUCT_REGISTRY_JSON  JSON map of productId -> {productCode, allowFreeTier}
                           for the products this endpoint serves.
    TOKEN_TTL_SECONDS      Lifetime of a minted token (default 3600).
    SIGNING_KEY_ARN        KMS asymmetric key used to sign tokens.
    AGREEMENT_REGION       Region for the Agreement API (default us-east-1).
    ALLOWED_ACCOUNTS       Optional comma-separated allow-list of buyer accounts
                           that bypass the subscription check (internal/testing).
    LOG_LEVEL              Logging level (default INFO).
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "3600"))
_SIGNING_KEY_ARN = os.environ.get("SIGNING_KEY_ARN", "")
_AGREEMENT_REGION = os.environ.get("AGREEMENT_REGION", "us-east-1")
_ALLOWED_ACCOUNTS = {
    a.strip() for a in os.environ.get("ALLOWED_ACCOUNTS", "").split(",") if a.strip()
}

_CLIENT_CONFIG = Config(
    connect_timeout=3, read_timeout=5, retries={"max_attempts": 3, "mode": "standard"}
)

_agreement_client = None
_kms_client = None


def _product_registry() -> Dict[str, Dict[str, Any]]:
    """productId -> {productCode, allowFreeTier} for the products we serve."""
    raw = os.environ.get("PRODUCT_REGISTRY_JSON", "{}")
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError as exc:
        logger.error("PRODUCT_REGISTRY_JSON is not valid JSON: %s", exc)
        return {}


def _agreement():
    global _agreement_client
    if _agreement_client is None:
        _agreement_client = boto3.client(
            "marketplace-agreement",
            region_name=_AGREEMENT_REGION,
            config=_CLIENT_CONFIG,
        )
    return _agreement_client


def _kms():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms", config=_CLIENT_CONFIG)
    return _kms_client


def _caller_account_id(event: Dict[str, Any]) -> Optional[str]:
    """The AWS account API Gateway *verified* via SigV4. Never from the body.

    With `AWS_IAM` authorization, API Gateway validates the signature before
    invoking this function and populates `requestContext.identity`. Trusting a
    body field instead would let any caller claim to be a subscribed account,
    which would defeat the entire purpose of this endpoint.
    """
    identity = (event.get("requestContext") or {}).get("identity") or {}
    account_id = identity.get("accountId")
    if account_id:
        return str(account_id)
    # Fall back to parsing the verified caller ARN, which API Gateway also sets.
    caller_arn = identity.get("userArn") or identity.get("caller") or ""
    parts = str(caller_arn).split(":")
    if len(parts) > 4 and parts[4].isdigit():
        return parts[4]
    return None


def _has_active_agreement(product_id: str, buyer_account_id: str) -> Tuple[bool, str]:
    """Seller-side check: does `buyer_account_id` hold an ACTIVE agreement?

    Returns (entitled, detail). `detail` is for logging/response diagnostics.

    Filter combination — **verified against a live seller account** with both a
    positive control (a genuinely subscribed buyer returns its ACTIVE agreement)
    and a negative one (an unsubscribed buyer returns an empty list, not an
    error):

        PartyType=Proposer + AgreementType=PurchaseAgreement
        + AcceptorAccountId=<buyer> + ResourceIdentifier=<productId>
        + Status=ACTIVE

    The filter name is ``ResourceIdentifier``. The AWS docs' prose for the
    Proposer-side combination list says ``ResourceId``, which the service
    **rejects** with ``ValidationException: Provided filter name is invalid``.
    ``FilterName`` is a free-form string with no client-side validation, so this
    is only discoverable by calling it — hence the note.

    An API error is reported as NOT entitled — the opposite of the host's
    advisory-allow. That asymmetry is deliberate: this endpoint runs in the
    seller's account where an error means *our* infrastructure is broken, and
    handing out tokens on our own failure would make the gate meaningless. The
    buyer-side grace period on the last-known-good token (see README) is what
    protects a paying customer from our outage.
    """
    filters: List[Dict[str, Any]] = [
        {"name": "PartyType", "values": ["Proposer"]},
        {"name": "AgreementType", "values": ["PurchaseAgreement"]},
        {"name": "AcceptorAccountId", "values": [buyer_account_id]},
        {"name": "ResourceIdentifier", "values": [product_id]},
        {"name": "Status", "values": ["ACTIVE"]},
    ]
    try:
        resp = _agreement().search_agreements(catalog="AWSMarketplace", filters=filters)
    except (ClientError, BotoCoreError) as exc:
        logger.warning(
            "SearchAgreements failed for buyer=%s product=%s: %s",
            buyer_account_id,
            product_id,
            exc,
        )
        return False, f"agreement lookup failed: {exc}"

    summaries = resp.get("agreementViewSummaries") or []
    logger.info(
        "SearchAgreements ok: buyer=%s product=%s matched=%d",
        buyer_account_id,
        product_id,
        len(summaries),
    )
    if summaries:
        # A live open-ended agreement has no endTime; treat presence + ACTIVE as
        # entitled rather than requiring an expiry.
        return True, f"active agreement {summaries[0].get('agreementId', '')}"
    return False, "no active agreement for this account"


def _mint_token(
    *, product_id: str, buyer_account_id: str, free_tier: bool
) -> Dict[str, Any]:
    """Sign a short-lived activation token bound to the buyer's account.

    Bound to the account so it cannot be shared between customers, and
    short-lived so a lapsed subscription actually stops working rather than
    running on a token issued months ago.

    Signed with a KMS **asymmetric** key so the extension (and anything else that
    needs to) can verify with the public key without being able to mint tokens.
    """
    issued = datetime.now(timezone.utc)
    claims = {
        "productId": product_id,
        "buyerAccountId": buyer_account_id,
        "freeTier": free_tier,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=_TOKEN_TTL_SECONDS)).timestamp()),
    }
    payload = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _kms().sign(
        KeyId=_SIGNING_KEY_ARN,
        Message=payload,
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PSS_SHA_256",
    )["Signature"]
    return {
        "token": base64.b64encode(payload).decode("ascii"),
        "signature": base64.b64encode(signature).decode("ascii"),
        "signingAlgorithm": "RSASSA_PSS_SHA_256",
        "expiresAt": datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _response(status: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    # Deliberately does not log the whole event: it is an authenticated request
    # and the body may grow to carry deployment metadata.
    buyer_account_id = _caller_account_id(event)
    if not buyer_account_id:
        # Should be impossible with AWS_IAM authorization; if it happens the API
        # is misconfigured (e.g. authorization set to NONE) and we must not mint.
        logger.error(
            "No verified caller account in requestContext.identity — refusing to "
            "mint a token. Check that the API method uses AWS_IAM authorization."
        )
        return _response(401, {"error": "unauthenticated"})

    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _response(400, {"error": "body must be JSON"})

    product_id = (body.get("productId") or "").strip()
    if not product_id:
        return _response(400, {"error": "productId is required"})

    registry = _product_registry()
    product = registry.get(product_id)
    if not product:
        # Don't reveal which products exist.
        logger.warning("Activation requested for unknown productId %r", product_id)
        return _response(404, {"error": "unknown product"})

    if buyer_account_id in _ALLOWED_ACCOUNTS:
        logger.info(
            "Buyer %s is on ALLOWED_ACCOUNTS — issuing token without a "
            "subscription check (internal/testing path).",
            buyer_account_id,
        )
        entitled, detail, free_tier = True, "allow-listed account", True
    else:
        entitled, detail = _has_active_agreement(product_id, buyer_account_id)
        free_tier = bool(product.get("allowFreeTier")) and not entitled
        # A free-tier product may serve unsubscribed accounts in a reduced mode;
        # a paid-only product must not.
        if not entitled and free_tier:
            logger.info(
                "Buyer %s has no agreement for %s; issuing FREE-TIER token.",
                buyer_account_id,
                product_id,
            )
            entitled = True

    if not entitled:
        logger.info(
            "Refusing activation for buyer=%s product=%s: %s",
            buyer_account_id,
            product_id,
            detail,
        )
        return _response(
            403,
            {
                "error": "not_entitled",
                "detail": "No active AWS Marketplace subscription for this account.",
            },
        )

    if not _SIGNING_KEY_ARN:
        logger.error("SIGNING_KEY_ARN is not configured; cannot mint a token.")
        return _response(500, {"error": "activation service misconfigured"})

    try:
        token = _mint_token(
            product_id=product_id,
            buyer_account_id=buyer_account_id,
            free_tier=free_tier,
        )
    except (ClientError, BotoCoreError) as exc:
        logger.error("KMS sign failed: %s", exc)
        return _response(500, {"error": "could not issue token"})

    logger.info(
        "Issued activation token: buyer=%s product=%s freeTier=%s ttl=%ss (%s)",
        buyer_account_id,
        product_id,
        free_tier,
        _TOKEN_TTL_SECONDS,
        detail,
    )
    return _response(200, {**token, "freeTier": free_tier})
