// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import {
  isUnverifiedGrant,
  isVerifiedEntitlement,
  licenseModeMismatchNote,
  sourceDisplayLabel,
  unverifiedReason,
} from './entitlement-source';
import type { FeatureEntitlement } from '../../types/feature-platform';

describe('isVerifiedEntitlement', () => {
  it('is true only for ACTIVE from the real buyer-side check', () => {
    expect(isVerifiedEntitlement('ACTIVE', 'marketplace-live')).toBe(true);
  });

  it('is FALSE for simulator-backed modes — the bypass this must not hide', () => {
    // `simulator` and `marketplace` (endpoint-override) both mean boto3 was
    // pointed at a fake Marketplace. Counting them as verified let a production
    // host aimed at a simulator render a clean "subscription active" with no
    // warning and no metric.
    expect(isVerifiedEntitlement('ACTIVE', 'simulated')).toBe(false);
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

  it('is true for simulator-backed grants', () => {
    expect(isUnverifiedGrant('ACTIVE', 'simulated')).toBe(true);
  });

  it('is false when nothing was granted', () => {
    // Not granting access is not an "unverified grant" — no warning is due.
    expect(isUnverifiedGrant('NONE', 'auto')).toBe(false);
    expect(isUnverifiedGrant('EXPIRED', 'advisory')).toBe(false);
  });

  it('never overlaps with isVerifiedEntitlement', () => {
    const sources = ['marketplace-live', 'simulated', 'auto', 'advisory', 'oss', 'none'] as const;
    for (const source of sources) {
      expect(isVerifiedEntitlement('ACTIVE', source) && isUnverifiedGrant('ACTIVE', source)).toBe(false);
    }
  });
});

describe('sourceDisplayLabel', () => {
  it('names the product a customer bought from, not the licenseMode identifier', () => {
    expect(sourceDisplayLabel('marketplace-live')).toBe('AWS Marketplace');
  });

  it('still distinguishes a simulator, so the label cannot launder a fake check', () => {
    expect(sourceDisplayLabel('simulated')).toBe('Marketplace simulator');
    expect(sourceDisplayLabel('simulated')).not.toMatch(/^AWS Marketplace$/);
  });

  it('passes an unrecognised source through verbatim rather than hiding it', () => {
    // A source we don't know about is worth seeing raw — inventing a friendly
    // name for it would be the one case where the label misleads.
    expect(sourceDisplayLabel('brand-new-mode' as never)).toBe('brand-new-mode');
  });

  it('does not throw on a missing source', () => {
    expect(sourceDisplayLabel(undefined)).toBe('unknown');
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

  it('names the simulator for simulator-backed modes', () => {
    for (const source of ['simulated'] as const) {
      expect(unverifiedReason(source)).toContain('simulator');
    }
  });

  it('falls back to a generic explanation', () => {
    expect(unverifiedReason('none')).toBeTruthy();
    expect(unverifiedReason(undefined)).toBeTruthy();
  });
});

describe('licenseModeMismatchNote', () => {
  const ent = (over: Partial<FeatureEntitlement> = {}): FeatureEntitlement => ({
    featureId: 'idp-auto-optimizer',
    state: 'ACTIVE',
    expiresAt: null,
    customerIdentifier: null,
    productCode: 'p',
    source: 'marketplace-live',
    ...over,
  });

  it('is null when there is no mismatch', () => {
    expect(licenseModeMismatchNote(ent())).toBeNull();
    expect(licenseModeMismatchNote(ent({ licenseModeMismatch: false }))).toBeNull();
    expect(licenseModeMismatchNote(null)).toBeNull();
    expect(licenseModeMismatchNote(undefined)).toBeNull();
  });

  it('names the reported case specifically — catalog simulated, extension live', () => {
    // The dangerous direction: the page would otherwise show a simulator-backed
    // subscription that the extension is going to ignore.
    const note = licenseModeMismatchNote(
      ent({ licenseModeMismatch: true, declaredLicenseMode: 'marketplace-live', catalogLicenseMode: 'simulated' }),
    );
    expect(note).toMatch(/real AWS Marketplace subscription/i);
    expect(note).toMatch(/simulator development/i);
    expect(note).toMatch(/licenseMode/);
  });

  it('falls back to a generic note for any other disagreement', () => {
    const note = licenseModeMismatchNote(
      ent({ licenseModeMismatch: true, declaredLicenseMode: 'simulated', catalogLicenseMode: 'marketplace-live' }),
    );
    expect(note).toMatch(/simulated/);
    expect(note).toMatch(/marketplace-live/);
  });

  it('says the host follows the EXTENSION, not the catalog', () => {
    // Aligning the host with the extension is the only way the two gates agree;
    // the note must not imply the host is about to check the catalog's authority.
    const note = licenseModeMismatchNote(
      ent({ licenseModeMismatch: true, declaredLicenseMode: 'marketplace-live', catalogLicenseMode: 'simulated' }),
    );
    expect(note).toMatch(/checking real AWS Marketplace to match the extension/i);
  });
});
