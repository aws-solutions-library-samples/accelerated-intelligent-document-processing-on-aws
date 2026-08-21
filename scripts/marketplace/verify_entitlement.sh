#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Probe how AWS Marketplace answers "is this account subscribed?" for a paid
# extension, from the BUYER's account.
#
# Why this script exists
# ---------------------
# The obvious API — marketplace-entitlement GetEntitlements — cannot be used as a
# buyer-side gate, and it fails in the most misleading way possible: it returns
# HTTP 200 with an EMPTY list rather than an error. Two independent reasons:
#
#   1. It is seller-side. AWS's SaaS guidance is that these calls "must be signed
#      by credentials from your AWS Marketplace Seller account".
#   2. Entitlement records only exist for SaaS *Contract* products. A usage-based
#      SaaS Subscription meters instead and has no entitlements at all.
#
# So the host uses the buyer-side AWS Marketplace *Agreement* API instead
# (SearchAgreements). This script runs both, side by side, so the difference is
# visible rather than argued about. Everything here is READ-ONLY.
#
# Usage:
#   scripts/marketplace/verify_entitlement.sh                 # Auto Optimizer defaults
#   scripts/marketplace/verify_entitlement.sh <productCode> <productId> [listingId]
#
# Env (deliberately NOT AWS_PROFILE / AWS_REGION — see below):
#   MP_PROFILE   AWS CLI profile to use.  Default: default
#   MP_REGION    Region to call.          Default: us-east-1
#
# These use dedicated names on purpose. An ambient AWS_REGION=us-west-2 sends
# these calls to endpoints that don't exist ("Could not connect to
# agreement-marketplace.us-west-2.amazonaws.com"), and an ambient AWS_PROFILE
# silently probes the wrong account — both of which look like "the API doesn't
# work" rather than "you asked the wrong endpoint". AWS_* credential env vars
# still take precedence over a profile in the CLI itself, so the identity is
# echoed below: check it before trusting any result.

set -uo pipefail

PRODUCT_CODE="${1:-q0k0s3zuuga46hle6fecx547}"
PRODUCT_ID="${2:-prod-a5ee62vs2xa72}"
LISTING_ID="${3:-prodview-44jb64lvdxr3y}"
# NB `${MP_PROFILE-default}` uses `-`, not `:-`, on purpose: `:-` substitutes for
# an EMPTY value as well as an unset one, which made `MP_PROFILE=` (documented
# below as "use ambient credentials") silently fall back to the default profile
# and probe the wrong account.
AWS_PROFILE_SELECTED="${MP_PROFILE-default}"
REGION="${MP_REGION:-us-east-1}"
if [[ -n "$AWS_PROFILE_SELECTED" ]]; then
  # Ambient static credentials would override AWS_PROFILE. Clear them so the
  # profile is authoritative.
  export AWS_PROFILE="$AWS_PROFILE_SELECTED"
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
else
  # MP_PROFILE= (explicitly empty) -> use whatever credentials are in the
  # environment, e.g. a subscribed buyer account's temporary session.
  unset AWS_PROFILE
fi
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

hr() { printf '\n%s\n' "----------------------------------------------------------------"; }

hr
echo "Account under test"
aws sts get-caller-identity --output json || exit 1

hr
echo "1. Listing visibility (marketplace-discovery GetListing)"
echo "   Proves the product id is right and the listing is visible to this"
echo "   account — so an empty subscription result is a true negative, not a typo."
aws marketplace-discovery get-listing \
    --listing-id "$LISTING_ID" --region "$REGION" \
    --query '{productId: associatedEntities[0].product.productId,
              productName: associatedEntities[0].product.productName,
              offerId: associatedEntities[0].offer.offerId,
              pricingModels: pricingModels[].pricingModelType,
              fulfillment: fulfillmentOptionSummaries[].fulfillmentOptionType}' \
    --output json

hr
echo "2. Offer terms (marketplace-discovery GetOfferTerms)"
echo "   A usageBasedPricingTerm with no contract term means there are NO"
echo "   entitlement records to fetch — the root cause behind step 3."
aws marketplace-discovery get-offer-terms \
    --offer-id "$(aws marketplace-discovery get-listing --listing-id "$LISTING_ID" \
        --region "$REGION" --query 'associatedEntities[0].offer.offerId' --output text)" \
    --region "$REGION" \
    --query 'offerTerms[].keys(@)[]' --output json

hr
echo "3. SELLER-side check: marketplace-entitlement GetEntitlements"
echo "   Expect: 200 + empty list, even when subscribed. NOT usable as a gate."
aws marketplace-entitlement get-entitlements \
    --product-code "$PRODUCT_CODE" --region "$REGION" --output json

hr
echo "4. BUYER-side check: marketplace-agreement SearchAgreements  <-- the gate"
echo "   Expect: an ACTIVE agreement once this account is subscribed."
aws marketplace-agreement search-agreements \
    --catalog AWSMarketplace --region "$REGION" \
    --filters "[{\"name\":\"PartyType\",\"values\":[\"Acceptor\"]},
                {\"name\":\"AgreementType\",\"values\":[\"PurchaseAgreement\"]},
                {\"name\":\"ResourceIdentifier\",\"values\":[\"$PRODUCT_ID\"]},
                {\"name\":\"Status\",\"values\":[\"ACTIVE\"]}]" \
    --query 'agreementViewSummaries[].{agreementId: agreementId, status: status,
                                       startTime: startTime, endTime: endTime}' \
    --output json

hr
echo "5. Positive control: ALL active agreements in this account"
echo "   If step 4 is empty but this is not, the API works and the account simply"
echo "   isn't subscribed to THIS product — which is exactly what the host"
echo "   reports as state=NONE."
aws marketplace-agreement search-agreements \
    --catalog AWSMarketplace --region "$REGION" \
    --filters '[{"name":"PartyType","values":["Acceptor"]},
                {"name":"AgreementType","values":["PurchaseAgreement"]}]' \
    --query 'agreementViewSummaries[].{status: status,
              resource: proposalSummary.resources[0].type}' \
    --output json

hr
cat <<'NOTES'
How to read this:

  step 4 non-empty                 -> host reports ACTIVE  (source=marketplace-live)
  step 4 empty, call succeeded     -> host reports NONE    (authoritative for THIS
                                      account; the UI shows Subscribe)
  step 4 errors (AccessDenied,...) -> host reports ACTIVE  (source=advisory) and logs
                                      loudly. Indistinguishable from "not subscribed",
                                      so it must not deny a possibly-paying customer.

Caveat: if an AWS Organization holds the subscription in the management account
while the IDP stack runs in a member account, step 4 is empty even though the org
IS subscribed. That is why NONE routes to Subscribe rather than hard-blocking, and
why the authoritative commercial gate is the extension's own runtime entitlement
check — not the host.
NOTES
