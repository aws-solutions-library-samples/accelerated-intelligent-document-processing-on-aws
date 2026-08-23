// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import type { FeatureEntitlement, FeatureEntitlementState } from '../../types/feature-platform';

type Source = FeatureEntitlement['source'];

/**
 * The only source that represents a **real** subscription check.
 *
 * `marketplace-live` calls the buyer-side AWS Marketplace Agreement API against
 * real AWS. It is reported ONLY when no `AWS_ENDPOINT_URL_MARKETPLACE_*`
 * override is in effect: `check_feature_entitlement` derives the source from the
 * endpoint rather than from the `SubscriptionMode` parameter, so a
 * simulator-backed check reports `simulated` however the stack is configured.
 * (It previously copied the parameter, which let a stack pointed at a simulator
 * report simulator answers as a verified live Marketplace check — the exact
 * bypass this module exists to surface.)
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

/**
 * Human-readable name for the authority that answered the subscription check.
 *
 * The raw values are internal identifiers (`marketplace-live`, `simulated`, …).
 * They belong in the unverified banner, where naming the exact mode is the whole
 * point, but a customer reading a confirmed subscription should see the product
 * they bought it from — "AWS Marketplace", not `marketplace-live`.
 *
 * Unknown values fall through verbatim rather than being hidden: a source we
 * don't recognise is worth seeing raw.
 */
const SOURCE_LABELS: Partial<Record<NonNullable<Source>, string>> = {
  'marketplace-live': 'AWS Marketplace',
  simulated: 'Marketplace simulator',
  advisory: 'unconfirmed',
  auto: 'checks disabled',
  oss: 'open source',
  none: 'not checked',
};

export function sourceDisplayLabel(source: Source | undefined): string {
  if (!source) return 'unknown';
  return SOURCE_LABELS[source] ?? source;
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
    return "The AWS Marketplace subscription check could not be completed, so access was allowed rather than blocking a subscription you may hold. Either the host is missing the aws-marketplace:SearchAgreements permission, or it is calling the Agreement API in a Region that doesn't host it (MarketplaceAgreementRegion must be us-east-1). The resolver's CloudWatch logs name which.";
  }
  if (source === 'simulated') {
    return 'This deployment is pointed at a marketplace simulator or a custom entitlement endpoint (FeaturePlatformSimulatorEndpoint), not real AWS Marketplace, so the subscription shown here is simulated. Expected in development; not expected in production.';
  }
  return 'The subscription state for this extension was not verified.';
}

/**
 * Extra sentence for the case where the host and the extension were configured to
 * check different authorities.
 *
 * The host resolves to the EXTENSION's declaration, so its check is already
 * aligned; what is left is an operator problem — the catalog entry is stale or
 * wrong. Returned as a note appended to whichever single banner is already being
 * rendered rather than as a banner of its own: the contradictory-banner stack is
 * exactly what the previous change removed.
 *
 * Deliberately not a block. Two independent gates that can disagree is the
 * original problem in mirror image, and the extension's own gate is the answer.
 */
export function licenseModeMismatchNote(entitlement: FeatureEntitlement | null | undefined): string | null {
  if (!entitlement?.licenseModeMismatch) return null;
  const declared = entitlement.declaredLicenseMode;
  const inCatalog = entitlement.catalogLicenseMode;
  if (declared === 'marketplace-live' && inCatalog === 'simulated') {
    // The reported case, and the one that actually misleads: the page would
    // otherwise show a simulator-backed subscription the extension will ignore.
    return 'This extension requires a real AWS Marketplace subscription, but this stack’s catalog lists it for simulator development. The host is checking real AWS Marketplace to match the extension; update the catalog entry (licenseMode) to agree.';
  }
  return `This extension enforces its subscription against ${declared ?? 'an unknown authority'}, but this stack’s catalog lists it as ${inCatalog ?? 'unknown'}. The host is following the extension; update the catalog entry (licenseMode) to agree.`;
}
