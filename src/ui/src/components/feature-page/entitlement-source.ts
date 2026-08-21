// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { FeatureEntitlement, FeatureEntitlementState } from '../../types/feature-platform';

type Source = FeatureEntitlement['source'];

/**
 * The only source that represents a **real** subscription check.
 *
 * `marketplace-live` calls the buyer-side AWS Marketplace Agreement API. By
 * default that is real AWS; it can be pointed at a simulator via
 * `AWS_ENDPOINT_URL_MARKETPLACE_AGREEMENT`, which the host cannot detect — so
 * this is "as verified as the host can know", not a guarantee.
 *
 * Deliberately EXCLUDED, and each exclusion is load-bearing:
 *  - `simulated`  — the seller-side GetEntitlements path, whether aimed at the
 *                   bundled simulator or an admin-supplied endpoint. That API
 *                   returns 200-with-an-empty-list from a buyer account, so it
 *                   cannot verify anything against real AWS. Treating it as
 *                   verified let a production host be pointed at a fake
 *                   Marketplace and report a *verified* subscription with no
 *                   warning and no metric — a silent bypass of exactly the kind
 *                   this module exists to surface.
 *  - `auto`       — entitlement checks are switched off for the whole stack.
 *  - `advisory`   — the live check was unreachable, so the host allowed rather
 *                   than locking out a possibly-paying customer. An
 *                   allow-on-error is not evidence of a subscription.
 *  - `none`       — no product code registered for the feature.
 *
 * `oss` is excluded too: open-source extensions have no Marketplace contract, so
 * "verified subscription" is not a meaningful claim about them. Callers that care
 * about OSS should branch on `source === 'oss'` explicitly rather than reading
 * this as "unlicensed".
 */
const CHECKED_SOURCES: ReadonlySet<Source> = new Set<Source>(['marketplace-live']);

export function isVerifiedEntitlement(state: FeatureEntitlementState | undefined, source: Source | undefined): boolean {
  return state === 'ACTIVE' && !!source && CHECKED_SOURCES.has(source);
}

/** Sources that grant access without a real subscription check behind it. */
const UNVERIFIED_SOURCES: ReadonlySet<Source> = new Set<Source>([
  'auto', // checks switched off stack-wide
  'advisory', // live check unreachable, allowed rather than locked out
  'simulated', // seller-side API against a simulator or custom endpoint
]);

/**
 * True when the host is granting access it never really verified.
 *
 * This is the state worth surfacing: it is indistinguishable from a real
 * subscription to anyone reading the page, which is exactly why it must not be
 * silent. It covers simulator-backed modes too — a production host pointed at a
 * simulator is a bypass, and it used to render as a clean "subscription active".
 */
export function isUnverifiedGrant(state: FeatureEntitlementState | undefined, source: Source | undefined): boolean {
  return state === 'ACTIVE' && !!source && UNVERIFIED_SOURCES.has(source);
}

/** Human-readable explanation of why a grant is unverified. */
export function unverifiedReason(source: Source | undefined): string {
  if (source === 'auto') {
    return 'Subscription checks are turned off for this deployment (FeaturePlatformSubscriptionMode=auto), so every extension is treated as subscribed.';
  }
  if (source === 'advisory') {
    return "The AWS Marketplace subscription check could not be completed, so access was allowed rather than blocking a subscription you may hold. This usually means the host is missing the aws-marketplace:SearchAgreements permission, or the Agreement API isn't available in this Region.";
  }
  if (source === 'simulated') {
    return 'This deployment is pointed at a marketplace simulator or a custom entitlement endpoint (FeaturePlatformSimulatorEndpoint), not real AWS Marketplace, so the subscription shown here is simulated. Expected in development; not expected in production.';
  }
  return 'The subscription state for this extension was not verified.';
}
