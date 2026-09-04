// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Tests for the Extensions nav section builder.
 *
 * This module had no tests, and the bug they were added for lived in the one
 * function that guessed: `statusOf` inferred "marketplace + not installed ⇒ not
 * subscribed" and showed a **Subscribe** badge, so a customer who had already
 * paid saw "Subscription active" on the feature page and "Subscribe" in the nav
 * at the same time.
 *
 * The nav cannot resolve entitlement — it is built from listInstalledFeatures +
 * listCatalogFeatures, neither of which carries a verdict — so the fix was to
 * stop claiming one. These tests pin that: the badge reports installed-ness, and
 * nothing about subscription state.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import SideNavigation from '@cloudscape-design/components/side-navigation';

import { buildFeaturesNavSection } from './feature-platform-nav-items';
import type { CatalogFeature, InstalledFeature } from '../../types/feature-platform';

const catalogFeature = (overrides: Partial<CatalogFeature> = {}): CatalogFeature =>
  ({
    featureId: 'idp-auto-optimizer',
    displayName: 'Auto Optimizer',
    description: 'Tunes extraction settings within a cost budget.',
    latestVersion: '0.5.1',
    iconUrl: null,
    docsUrl: null,
    showInNav: true,
    source: 'marketplace',
    productCode: 'pc',
    marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/x',
    availableInRegion: true,
    availableRegions: [],
    ...overrides,
  }) as CatalogFeature;

const installedFeature = (overrides: Partial<InstalledFeature> = {}): InstalledFeature =>
  ({
    featureId: 'idp-auto-optimizer',
    displayName: 'Auto Optimizer',
    installedVersion: '0.5.1',
    latestVersion: '0.5.1',
    updateAvailable: false,
    stackName: 'idp-feature-auto-optimizer',
    stackRegion: 'us-west-2',
    stackId: null,
    uiBundlePath: 'features/idp-auto-optimizer/v0.5.1/',
    featureApiEndpoint: null,
    iconUrl: null,
    installedAt: '2026-01-01T00:00:00Z',
    installedBy: null,
    ...overrides,
  }) as InstalledFeature;

/** Render the built section through Cloudscape so `info` ReactNodes are realised. */
function renderNav(installed: InstalledFeature[], catalog: CatalogFeature[] = [], canInstall = true) {
  const section = buildFeaturesNavSection(installed, catalog, canInstall);
  return render(<SideNavigation items={[section]} />);
}

describe('Extensions nav badges', () => {
  it('does NOT say "Subscribe" for a marketplace extension that is not installed', () => {
    // The reported bug. The nav has no entitlement verdict, so a paid-and-waiting
    // customer was being told to subscribe again.
    renderNav([], [catalogFeature()]);

    expect(screen.queryByText('Subscribe')).not.toBeInTheDocument();
    expect(screen.getByText('Install')).toBeInTheDocument();
  });

  it('shows the same badge for OSS and marketplace when neither is installed', () => {
    // One badge, because the nav knows one thing: it isn't installed. Which step
    // comes first is the feature page's business — it has the verdict.
    renderNav(
      [],
      [
        catalogFeature({ featureId: 'a', displayName: 'Paid Ext', source: 'marketplace' }),
        catalogFeature({ featureId: 'b', displayName: 'Free Ext', source: 'oss' }),
      ],
    );

    expect(screen.getAllByText('Install')).toHaveLength(2);
  });

  it('shows no badge once installed and up to date', () => {
    renderNav([installedFeature()], [catalogFeature()]);

    expect(screen.queryByText('Install')).not.toBeInTheDocument();
    expect(screen.queryByText('Update')).not.toBeInTheDocument();
    expect(screen.queryByText('Subscribe')).not.toBeInTheDocument();
  });

  it('shows Update when installed behind the published version', () => {
    renderNav([installedFeature({ updateAvailable: true, latestVersion: '0.6.0' })], [catalogFeature({ latestVersion: '0.6.0' })]);

    expect(screen.getByText('Update')).toBeInTheDocument();
    expect(screen.queryByText('Install')).not.toBeInTheDocument();
  });

  it('states the state, not the action, for someone who cannot install', () => {
    // Found on an annotator account: the Extensions section listed two extensions
    // badged "Install", an action only an Admin can take (subscribeFeature is
    // Admin-only, and the feature page routes everyone else to AwaitingAdminInstall).
    // The badge's stated job is to say "this needs action", so for a non-admin it says
    // which state the extension is in and leaves the verb out.
    renderNav([], [catalogFeature()], false);

    expect(screen.getByText('Not installed')).toBeInTheDocument();
    expect(screen.queryByText('Install')).not.toBeInTheDocument();
  });

  it('says the same about an update a non-admin cannot apply', () => {
    renderNav([installedFeature({ updateAvailable: true, latestVersion: '0.6.0' })], [catalogFeature({ latestVersion: '0.6.0' })], false);

    expect(screen.getByText('Update available')).toBeInTheDocument();
    expect(screen.queryByText('Update')).not.toBeInTheDocument();
  });

  it('keeps the admin wording by default, so existing callers are unchanged', () => {
    // The parameter defaults to true: a caller that does not know the role must not
    // silently downgrade every badge to passive wording.
    renderNav([], [catalogFeature()]);

    expect(screen.getByText('Install')).toBeInTheDocument();
    expect(screen.queryByText('Not installed')).not.toBeInTheDocument();
  });

  it('region-unavailability outranks Install — installing there could not succeed', () => {
    renderNav([], [catalogFeature({ availableInRegion: false, availableRegions: ['us-east-1'] })]);

    expect(screen.getByText('Not in this Region')).toBeInTheDocument();
    expect(screen.queryByText('Install')).not.toBeInTheDocument();
  });

  it('always offers Browse catalog, even with nothing installed or catalogued', () => {
    renderNav([], []);
    expect(screen.getByText('Browse catalog')).toBeInTheDocument();
  });
});
