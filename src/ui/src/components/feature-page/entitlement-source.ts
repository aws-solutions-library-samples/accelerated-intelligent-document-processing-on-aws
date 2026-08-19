// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { FeatureEntitlement, FeatureEntitlementState } from '../../types/feature-platform';

type Source = FeatureEntitlement['source'];

/**
 * Sources that represent an entitlement the host actually **checked** against a
 * Marketplace API.
 *
 * Deliberately excluded:
 *  - `auto`     — entitlement checks are switched off for the whole stack.
 *  - `advisory` — the live check was unreachable, so the host allowed rather than
 *                 locking out a possibly-paying customer. An allow-on-error is
 *                 not evidence of a subscription.
 *  - `none`     — no product code registered for the feature.
 *
 * `oss` is excluded too: open-source extensions have no Marketplace contract, so
 * "verified subscription" is not a meaningful claim about them. Callers that care
 * about OSS should branch on `source === 'oss'` explicitly rather than reading
 * this as "unlicensed".
 */
const CHECKED_SOURCES: ReadonlySet<Source> = new Set<Source>(['marketplace', 'marketplace-live', 'simulator']);

/**
 * True only when the host confirmed an ACTIVE entitlement via a real check.
 *
 * This is NOT a licence gate — it is host-computed and delivered to a browser in
 * the customer's own AWS account, so an admin can influence it. Its value is
 * narrower and honest: unlike `uiAccessAllowed`, it does not silently read
 * `true` when checks are disabled or unreachable. Use it to decide whether to
 * *warn*, never whether to *serve*.
 */
export function isVerifiedEntitlement(state: FeatureEntitlementState | undefined, source: Source | undefined): boolean {
  return state === 'ACTIVE' && !!source && CHECKED_SOURCES.has(source);
}

/**
 * True when the host is granting access it never verified — ACTIVE, but from
 * `auto` or `advisory`.
 *
 * This is the state worth surfacing in the UI: it is indistinguishable from a
 * real subscription to anyone reading the page, which is exactly why it should
 * not be silent.
 */
export function isUnverifiedGrant(state: FeatureEntitlementState | undefined, source: Source | undefined): boolean {
  return state === 'ACTIVE' && (source === 'auto' || source === 'advisory');
}

/** Human-readable explanation of why a grant is unverified. */
export function unverifiedReason(source: Source | undefined): string {
  if (source === 'auto') {
    return 'Subscription checks are turned off for this deployment (FeaturePlatformSubscriptionMode=auto), so every extension is treated as subscribed.';
  }
  if (source === 'advisory') {
    return "The AWS Marketplace subscription check could not be completed, so access was allowed rather than blocking a subscription you may hold. This usually means the host is missing the aws-marketplace:SearchAgreements permission, or the Agreement API isn't available in this Region.";
  }
  return 'The subscription state for this extension was not verified.';
}
