// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { isUnverifiedGrant, isVerifiedEntitlement, unverifiedReason } from './entitlement-source';

describe('isVerifiedEntitlement', () => {
  it('is true only for ACTIVE from a real Marketplace check', () => {
    expect(isVerifiedEntitlement('ACTIVE', 'marketplace-live')).toBe(true);
    expect(isVerifiedEntitlement('ACTIVE', 'marketplace')).toBe(true);
    expect(isVerifiedEntitlement('ACTIVE', 'simulator')).toBe(true);
  });

  it('is FALSE for auto and advisory — the whole point of the flag', () => {
    // `auto` means checks are switched off; `advisory` means the check was
    // unreachable and we allowed rather than locking out a paying customer.
    // Neither is evidence of a subscription, and collapsing them into "active"
    // is what makes `uiAccessAllowed` unusable as a licence signal.
    expect(isVerifiedEntitlement('ACTIVE', 'auto')).toBe(false);
    expect(isVerifiedEntitlement('ACTIVE', 'advisory')).toBe(false);
  });

  it('is false for oss (no Marketplace contract to verify) and none', () => {
    expect(isVerifiedEntitlement('ACTIVE', 'oss')).toBe(false);
    expect(isVerifiedEntitlement('ACTIVE', 'none')).toBe(false);
  });

  it('is false whenever the state is not ACTIVE', () => {
    expect(isVerifiedEntitlement('NONE', 'marketplace-live')).toBe(false);
    expect(isVerifiedEntitlement('EXPIRED', 'marketplace-live')).toBe(false);
  });

  it('is false for missing inputs rather than throwing', () => {
    expect(isVerifiedEntitlement(undefined, undefined)).toBe(false);
    expect(isVerifiedEntitlement('ACTIVE', undefined)).toBe(false);
  });
});

describe('isUnverifiedGrant', () => {
  it('is true exactly when access is granted without verification', () => {
    expect(isUnverifiedGrant('ACTIVE', 'auto')).toBe(true);
    expect(isUnverifiedGrant('ACTIVE', 'advisory')).toBe(true);
  });

  it('is false for a verified grant', () => {
    expect(isUnverifiedGrant('ACTIVE', 'marketplace-live')).toBe(false);
  });

  it('is false when nothing was granted', () => {
    // Not granting access is not an "unverified grant" — no warning is due.
    expect(isUnverifiedGrant('NONE', 'auto')).toBe(false);
    expect(isUnverifiedGrant('EXPIRED', 'advisory')).toBe(false);
  });

  it('never overlaps with isVerifiedEntitlement', () => {
    const sources = ['marketplace', 'marketplace-live', 'simulator', 'auto', 'advisory', 'oss', 'none'] as const;
    for (const source of sources) {
      expect(isVerifiedEntitlement('ACTIVE', source) && isUnverifiedGrant('ACTIVE', source)).toBe(false);
    }
  });
});

describe('unverifiedReason', () => {
  it('names the parameter for auto so an admin can find it', () => {
    expect(unverifiedReason('auto')).toContain('FeaturePlatformSubscriptionMode=auto');
  });

  it('names the likely cause for advisory', () => {
    // The common real cause is a missing IAM grant, so say so — otherwise the
    // admin has no path from "not verified" to "fixed".
    expect(unverifiedReason('advisory')).toContain('SearchAgreements');
  });

  it('falls back to a generic explanation', () => {
    expect(unverifiedReason('none')).toBeTruthy();
    expect(unverifiedReason(undefined)).toBeTruthy();
  });
});
