// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Vitest coverage for the 7-state FeaturePage state machine.
 *
 * Hooks are mocked at the module boundary so we don't need AppSync/Cognito.
 * Each test exercises one row of the state table in FeaturePage.tsx.
 */

import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import FeaturePage from './FeaturePage';
import type { CatalogFeature, FeatureEntitlement, InstalledFeature } from '../../types/feature-platform';

vi.mock('../../hooks/use-installed-features');
vi.mock('../../hooks/use-catalog-features');
vi.mock('../../hooks/use-feature-entitlement');
vi.mock('../../hooks/use-feature-launch-url');
vi.mock('../../hooks/use-subscribe-feature');
vi.mock('../../hooks/use-unsubscribe-feature');
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));
// FeatureLoader would try to inject a <script>; stub it.
vi.mock('./FeatureLoader', () => ({
  default: ({ featureId }: { featureId: string }) => <div data-testid="feature-loader">Feature bundle loaded: {featureId}</div>,
}));

import useInstalledFeatures from '../../hooks/use-installed-features';
import useCatalogFeatures from '../../hooks/use-catalog-features';
import useFeatureEntitlement from '../../hooks/use-feature-entitlement';
import useFeatureLaunchUrl from '../../hooks/use-feature-launch-url';
import useSubscribeFeature from '../../hooks/use-subscribe-feature';
import useUnsubscribeFeature from '../../hooks/use-unsubscribe-feature';

const mockedUseInstalled = vi.mocked(useInstalledFeatures);
const mockedUseCatalog = vi.mocked(useCatalogFeatures);
const mockedUseEntitlement = vi.mocked(useFeatureEntitlement);
const mockedUseLaunchUrl = vi.mocked(useFeatureLaunchUrl);
const mockedUseSubscribe = vi.mocked(useSubscribeFeature);
const mockedUseUnsubscribe = vi.mocked(useUnsubscribeFeature);

const installed = (overrides: Partial<InstalledFeature> = {}): InstalledFeature => ({
  featureId: 'docs-by-status',
  displayName: 'DemoFeature - Docs By Status',
  installedVersion: '1.0.0',
  latestVersion: '1.0.0',
  updateAvailable: false,
  stackName: 'idp-feature-docs-by-status',
  stackRegion: 'us-east-1',
  stackId: null,
  uiBundlePath: 'features/docs-by-status/v1.0.0/',
  featureApiEndpoint: 'https://feat.example.com',
  iconUrl: null,
  installedAt: '2026-01-01T00:00:00Z',
  installedBy: null,
  ...overrides,
});

const ent = (overrides: Partial<FeatureEntitlement> = {}): FeatureEntitlement => ({
  featureId: 'docs-by-status',
  state: 'ACTIVE',
  expiresAt: null,
  customerIdentifier: 'CUST',
  productCode: 'prod123',
  source: 'simulated',
  ...overrides,
});

/**
 * `withMarketplaceUrlsProp` defaults to true for historical reasons, but NOTE:
 * no route passes that prop in production. Tests that care about the marketplace
 * link must set it false, or they assert against an override real users never
 * have — which is precisely how the missing catalog fallback shipped.
 */
function renderPage(groups: string[], { withMarketplaceUrlsProp = true } = {}) {
  return render(
    <MemoryRouter initialEntries={['/features/docs-by-status']}>
      <Routes>
        <Route
          path="/features/:featureId"
          element={
            <FeaturePage
              groups={groups}
              mainStackName="idp-main"
              marketplaceUrls={withMarketplaceUrlsProp ? { 'docs-by-status': 'https://aws.amazon.com/marketplace/...' } : undefined}
            />
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

/** Default: no catalog entry, matching the pre-mock behavior of these tests. */
function mockCatalog(entry?: Partial<CatalogFeature>) {
  mockedUseCatalog.mockReturnValue({
    features: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    byId: () =>
      entry
        ? ({
            featureId: 'docs-by-status',
            displayName: 'DemoFeature - Docs By Status',
            latestVersion: '1.0.0',
            iconUrl: null,
            description: null,
            docsUrl: null,
            showInNav: true,
            source: 'marketplace',
            productCode: 'prod123',
            marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/x',
            availableInRegion: true,
            availableRegions: [],
            ...entry,
          } as CatalogFeature)
        : undefined,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockCatalog();
  mockedUseLaunchUrl.mockReturnValue({
    fetch: vi.fn(),
    loading: false,
    error: null,
  });
  mockedUseSubscribe.mockReturnValue({
    subscribe: vi.fn(),
    loading: false,
    error: null,
  });
  mockedUseUnsubscribe.mockReturnValue({
    unsubscribe: vi.fn(),
    loading: false,
    error: null,
  });
});

describe('FeaturePage region availability', () => {
  it('shows "Not available in this Region" instead of a dead-end Subscribe CTA', () => {
    // The whole point: a Subscribe button here would take the admin's money (or
    // at least their time) for something they cannot install in this Region.
    mockCatalog({ availableInRegion: false, availableRegions: ['us-west-2', 'eu-central-1'] });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Not available in this Region/i)).toBeInTheDocument();
    expect(screen.getByText(/us-west-2, eu-central-1/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Subscribe$/i })).not.toBeInTheDocument();
  });

  it('does not flag an ALREADY-INSTALLED feature as region-unavailable', () => {
    // It demonstrably works here, whatever the catalog now claims.
    mockCatalog({ availableInRegion: false, availableRegions: ['us-west-2'] });
    mockedUseInstalled.mockReturnValue({
      features: [installed()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => installed(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/Not available in this Region/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
  });

  it('treats an absent availableInRegion (older host) as available', () => {
    mockCatalog({ availableInRegion: null, availableRegions: null });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/Not available in this Region/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Subscription required/i)).toBeInTheDocument();
  });
});

describe('FeaturePage 7-state renderer', () => {
  it('shows "Subscription required" when entitlement is NONE', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByText(/Subscription required/i)).toBeInTheDocument();
    expect(screen.queryByTestId('feature-loader')).not.toBeInTheDocument();
  });

  it('shows Launch Stack button when ACTIVE + not installed + admin', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    expect(screen.getByRole('button', { name: /Launch stack/i })).toBeInTheDocument();
    // This screen asserts the subscription is ACTIVE, so it owes the customer the
    // same route to managing it as the installed page — not having launched the
    // stack yet doesn't make the subscription any less billable.
    expect(screen.getByRole('link', { name: /Manage subscription/i })).toHaveAttribute(
      'href',
      'https://console.aws.amazon.com/marketplace/home#/subscriptions',
    );
  });

  it('does not offer "Manage subscription" for an OSS extension', () => {
    // No Marketplace contract exists, so there is nothing to manage — a link into
    // the subscriptions console would be a dead end.
    mockCatalog({ source: 'oss' });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'oss' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    expect(screen.getByRole('button', { name: /Launch stack/i })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Manage subscription/i })).not.toBeInTheDocument();
  });

  it('shows "Awaiting installation" when ACTIVE + not installed + non-admin', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByText(/Awaiting installation/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Launch stack/i })).not.toBeInTheDocument();
    // Managing the subscription is an AWS-account action guarded by the caller's
    // own IAM permissions, not by the IDP Admin group — a viewer may be the
    // account's billing owner, and they are the one being charged.
    expect(screen.getByRole('link', { name: /Manage subscription/i })).toBeInTheDocument();
  });

  it('renders feature UI + up-to-date banner when ACTIVE + installed at latest', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0', updateAvailable: false });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
    expect(screen.getByText(/up to date/i)).toBeInTheDocument();
  });

  it('renders feature UI + update banner + Update button when admin and update available', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.1.0', updateAvailable: true });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
    expect(screen.getByText(/Update available.*v1\.1\.0/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Update$/ })).toBeInTheDocument();
  });

  it('renders update banner WITHOUT button for non-admin when update available', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.1.0', updateAvailable: true });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByText(/Update available/i)).toBeInTheDocument();
    expect(screen.getByText(/ask your admin/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Update$/ })).not.toBeInTheDocument();
  });

  it('renders feature UI (dimmed) + Renew button when EXPIRED + installed', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'EXPIRED' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByText(/Subscription expired/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Renew/i })).toBeInTheDocument();
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
  });

  it('shows spinner while loading', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: true,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: null,
      loading: true,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.getByText(/Checking subscription/i)).toBeInTheDocument();
  });

  // --- Task 6: Subscribe / Cancel Subscription button wiring ---------------

  it('shows Subscribe button in NONE state for admin', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    expect(screen.getByRole('button', { name: /^Subscribe$/ })).toBeInTheDocument();
  });

  it('hides Subscribe button in NONE state for non-admin', () => {
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.queryByRole('button', { name: /^Subscribe$/ })).not.toBeInTheDocument();
  });

  it('clicks Subscribe → opens marketplaceUrl in new tab + refreshes on window focus', async () => {
    // The new behaviour mirrors real AWS Marketplace: subscribe returns a URL
    // to redirect to (new tab), and the UI refreshes entitlement state when
    // the admin returns to the original tab (window focus).
    const subscribe = vi
      .fn()
      .mockResolvedValue(ent({ state: 'NONE', marketplaceUrl: 'http://sim.example.com/marketplace/pp/prod123?x=1' }));
    const refreshInstalled = vi.fn().mockResolvedValue(undefined);
    const refreshEntitlement = vi.fn().mockResolvedValue(undefined);
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);

    mockedUseSubscribe.mockReturnValue({ subscribe, loading: false, error: null });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: refreshInstalled,
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE' }),
      loading: false,
      error: null,
      refresh: refreshEntitlement,
    });

    renderPage(['Admin']);
    fireEvent.click(screen.getByRole('button', { name: /^Subscribe$/ }));

    await waitFor(() =>
      expect(subscribe).toHaveBeenCalledWith('docs-by-status', expect.objectContaining({ returnUrl: expect.any(String) })),
    );
    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith('http://sim.example.com/marketplace/pp/prod123?x=1', '_blank', 'noopener,noreferrer'),
    );

    // Simulate the admin finishing the Marketplace flow and returning to the tab.
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(refreshEntitlement).toHaveBeenCalled());
    await waitFor(() => expect(refreshInstalled).toHaveBeenCalled());

    openSpy.mockRestore();
  });

  it('shows Cancel Subscription button in ACTIVE+installed state for admin', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'simulated' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    // `simulated` is an UNVERIFIED source, so the single banner is the honest
    // one — and the Cancel action travels with it, because it is the only
    // subscription banner rendered in this state.
    expect(screen.getByRole('button', { name: /Cancel Subscription/i })).toBeInTheDocument();
    expect(screen.getByText(/Access allowed without a verified subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/source: simulated/i)).toBeInTheDocument();
    // The claim it used to sit next to must be gone, not merely reworded.
    expect(screen.queryByText(/Subscription active/i)).not.toBeInTheDocument();
  });

  it('hides Cancel Subscription button in ACTIVE+installed state for non-admin', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'simulated' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.queryByRole('button', { name: /Cancel Subscription/i })).not.toBeInTheDocument();
    // The subscription banner itself still renders — without the admin action.
    expect(screen.getByText(/Access allowed without a verified subscription/i)).toBeInTheDocument();
  });

  it('shows the green "Subscription active" banner ONLY for a verified subscription', () => {
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'marketplace-live' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    // One line, and it names the product rather than the internal licenseMode
    // identifier: "Subscription active (AWS Marketplace)", not a second row
    // reading "Source: marketplace-live".
    expect(screen.getByText(/Subscription active \(AWS Marketplace\)/i)).toBeInTheDocument();
    expect(screen.queryByText(/^Source:$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/marketplace-live/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Access allowed without a verified subscription/i)).not.toBeInTheDocument();
    // Cancel is only offered for simulator-backed subscriptions — a real AWS
    // Marketplace subscription can only be cancelled in the buyer's console,
    // which is what "Manage subscription" links to.
    expect(screen.queryByRole('button', { name: /Cancel Subscription/i })).not.toBeInTheDocument();
    const manage = screen.getByRole('link', { name: /Manage subscription/i });
    expect(manage).toHaveAttribute('href', 'https://console.aws.amazon.com/marketplace/home#/subscriptions');
    expect(manage).toHaveAttribute('target', '_blank');
  });

  it('offers "Manage subscription" to a non-admin too', () => {
    // Viewing and cancelling your own Marketplace subscription happens in the AWS
    // console under the caller's own IAM permissions, so the host has no business
    // gating the link on the IDP Admin group — an IDP viewer may well be the
    // account's billing owner.
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'marketplace-live' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);

    expect(screen.getByRole('link', { name: /Manage subscription/i })).toBeInTheDocument();
  });

  it('never renders both subscription banners at once', () => {
    // The reported defect: a yellow "not verified" warning stacked directly on
    // top of a green "Subscription active · Source: advisory". Two host banners
    // contradicting each other is worse than either alone, and it made the
    // extension's own honest red panel read as a third opinion.
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    for (const source of ['advisory', 'simulated', 'auto', 'marketplace-live'] as const) {
      mockedUseInstalled.mockReturnValue({
        features: [inst],
        loading: false,
        error: null,
        refresh: vi.fn(),
        byId: (id) => (id === inst.featureId ? inst : undefined),
      });
      mockedUseEntitlement.mockReturnValue({
        entitlement: ent({ state: 'ACTIVE', source }),
        loading: false,
        error: null,
        refresh: vi.fn(),
      });
      const { unmount } = renderPage(['Admin']);

      const active = screen.queryAllByText(/Subscription active/i).length;
      const unverified = screen.queryAllByText(/Access allowed without a verified subscription/i).length;
      expect(active + unverified, `source=${source} rendered ${active} active + ${unverified} unverified banners`).toBe(1);
      unmount();
    }
  });

  it('the version banner says nothing about the subscription', () => {
    // It used to append the source — "v1.0.0 — up to date (advisory)" — piling a
    // third, subscription-flavoured banner onto a stack that already disagreed
    // with itself. Version and subscription are separate facts.
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'advisory' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/up to date/i)).toBeInTheDocument();
    expect(screen.queryByText(/up to date \(advisory\)/i)).not.toBeInTheDocument();
  });

  it('shows NO subscription banner or Cancel button for an OSS extension', () => {
    // An OSS extension has no subscription: check_feature_entitlement
    // short-circuits source="oss" straight to ACTIVE. Rendering "Subscription
    // active / Source: oss" with an admin-only Cancel Subscription button offered
    // an action that cannot work — unsubscribeFeature targets a Marketplace or
    // simulator entitlement, and there is neither here.
    mockCatalog({ source: 'oss' });
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'oss' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/Subscription active/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Cancel Subscription/i })).not.toBeInTheDocument();
    // The feature itself must still render — this is a banner fix, not a gate.
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
  });

  it('clicks Cancel Subscription → calls unsubscribeFeature + refreshes caches', async () => {
    const unsubscribe = vi.fn().mockResolvedValue(ent({ state: 'EXPIRED' }));
    const refreshInstalled = vi.fn().mockResolvedValue(undefined);
    const refreshEntitlement = vi.fn().mockResolvedValue(undefined);
    const inst = installed({ installedVersion: '1.0.0', latestVersion: '1.0.0' });

    mockedUseUnsubscribe.mockReturnValue({ unsubscribe, loading: false, error: null });
    mockedUseInstalled.mockReturnValue({
      features: [inst],
      loading: false,
      error: null,
      refresh: refreshInstalled,
      byId: (id) => (id === inst.featureId ? inst : undefined),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'simulated' }),
      loading: false,
      error: null,
      refresh: refreshEntitlement,
    });

    renderPage(['Admin']);
    fireEvent.click(screen.getByRole('button', { name: /Cancel Subscription/i }));

    await waitFor(() => expect(unsubscribe).toHaveBeenCalledWith('docs-by-status'));
    await waitFor(() => expect(refreshEntitlement).toHaveBeenCalled());
    await waitFor(() => expect(refreshInstalled).toHaveBeenCalled());
  });
});

describe('FeaturePage unverified-subscription warning', () => {
  const paidInstalled = () => installed({ featureId: 'docs-by-status' });

  it('warns when a PAID extension is served from auto mode', () => {
    // auto = subscription checks switched off. The page is otherwise
    // indistinguishable from a real subscription, which is exactly why this
    // must not be silent.
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paidInstalled()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paidInstalled(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'auto' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Access allowed without a verified subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/FeaturePlatformSubscriptionMode=auto/)).toBeInTheDocument();
    // Still renders the feature — the host gate is advisory, not a block.
    expect(screen.getByTestId('feature-loader')).toBeInTheDocument();
  });

  it('warns when the live check was unreachable (advisory) and names the cause', () => {
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paidInstalled()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paidInstalled(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'advisory' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Access allowed without a verified subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/SearchAgreements/)).toBeInTheDocument();
  });

  it('does NOT warn for an OSS extension in auto mode', () => {
    // OSS has no subscription to verify; warning would be pure noise on the
    // default deployment.
    mockCatalog({ source: 'oss' });
    mockedUseInstalled.mockReturnValue({
      features: [paidInstalled()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paidInstalled(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'auto' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/Access allowed without a verified subscription/i)).not.toBeInTheDocument();
  });

  it('does NOT warn when the subscription was actually verified', () => {
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paidInstalled()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paidInstalled(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'ACTIVE', source: 'marketplace-live' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/Access allowed without a verified subscription/i)).not.toBeInTheDocument();
  });
});

describe('FeaturePage per-extension licenseMode', () => {
  const paid = () => installed({ featureId: 'docs-by-status' });

  it('does NOT offer the in-UI Subscribe button for a marketplace-live extension', () => {
    // `subscribeFeature` drives the simulator's admin API, so on a
    // simulator-configured stack that button would mint a subscription the
    // extension ignores — it only honours real AWS Marketplace. The listing link
    // is the only real path, and the only place a real subscription can be made.
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE', source: 'marketplace-live', licenseMode: 'marketplace-live' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Subscription required/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Subscribe$/i })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View on AWS Marketplace/i })).toBeInTheDocument();
    expect(screen.getByText(/can only be created there/i)).toBeInTheDocument();
  });

  it('DOES offer the in-UI Subscribe button for a simulated extension', () => {
    // The dev loop must keep working — this is the case the simulator exists for.
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE', source: 'simulated', licenseMode: 'simulated' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByRole('button', { name: /^Subscribe$/i })).toBeInTheDocument();
  });

  it('shows the mismatch note on the single unverified banner, not as a new banner', () => {
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paid()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paid(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({
        state: 'ACTIVE',
        source: 'advisory',
        licenseMode: 'marketplace-live',
        declaredLicenseMode: 'marketplace-live',
        catalogLicenseMode: 'simulated',
        licenseModeMismatch: true,
      }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Access allowed without a verified subscription/i)).toBeInTheDocument();
    expect(screen.getByText(/simulator development/i)).toBeInTheDocument();
    // Still exactly one subscription banner — the note rides along, it doesn't add one.
    const active = screen.queryAllByText(/Subscription active/i).length;
    const unverified = screen.queryAllByText(/Access allowed without a verified subscription/i).length;
    expect(active + unverified).toBe(1);
  });

  it('shows the mismatch note on a VERIFIED subscription too', () => {
    // A stale catalog entry is worth saying even when the check succeeded —
    // otherwise it only ever appears in the resolver's logs.
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paid()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paid(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({
        state: 'ACTIVE',
        source: 'marketplace-live',
        licenseMode: 'marketplace-live',
        declaredLicenseMode: 'marketplace-live',
        catalogLicenseMode: 'simulated',
        licenseModeMismatch: true,
      }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.getByText(/Subscription active/i)).toBeInTheDocument();
    expect(screen.getByText(/simulator development/i)).toBeInTheDocument();
  });

  it('says nothing about licenseMode when the two agree', () => {
    mockCatalog({ source: 'marketplace' });
    mockedUseInstalled.mockReturnValue({
      features: [paid()],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => paid(),
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({
        state: 'ACTIVE',
        source: 'marketplace-live',
        licenseMode: 'marketplace-live',
        declaredLicenseMode: 'marketplace-live',
        catalogLicenseMode: 'marketplace-live',
        licenseModeMismatch: false,
      }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);

    expect(screen.queryByText(/licenseMode/i)).not.toBeInTheDocument();
  });
});

describe('FeaturePage marketplace listing link', () => {
  /**
   * Regression: an admin who genuinely needed to subscribe was shown
   * "Subscription required" with NO buttons at all.
   *
   * Two causes compounded. `marketplaceUrls` is a prop no route passes, so it was
   * always undefined in production, and only two of the five call sites had a
   * `?? catalogEntry.marketplaceListingUrl` fallback — NONE was not one of them.
   * The in-UI Subscribe button had been masking it, and suppressing that button
   * for `marketplace-live` extensions (correctly, since it drives the simulator)
   * removed the mask. Every test passed throughout, because the shared
   * `renderPage` helper supplies the prop that production never does.
   */
  function noneStateWithCatalog(entry: Partial<CatalogFeature>) {
    mockCatalog({ source: 'marketplace', ...entry });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE', source: 'marketplace-live', licenseMode: 'marketplace-live' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
  }

  it('renders the listing button from the CATALOG when no prop is supplied', () => {
    noneStateWithCatalog({ marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/prodview-real' });
    renderPage(['Admin'], { withMarketplaceUrlsProp: false });

    const link = screen.getByRole('link', { name: /View on AWS Marketplace/i });
    expect(link).toHaveAttribute('href', 'https://aws.amazon.com/marketplace/pp/prodview-real');
  });

  it('gives a non-admin the listing link too', () => {
    // Subscribing is a Marketplace-side action in the buyer's own account; the
    // host has no reason to hide the listing from a viewer.
    noneStateWithCatalog({ marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/prodview-real' });
    renderPage(['Viewer'], { withMarketplaceUrlsProp: false });

    expect(screen.getByRole('link', { name: /View on AWS Marketplace/i })).toBeInTheDocument();
  });

  it('the prop still overrides the catalog when it IS supplied', () => {
    noneStateWithCatalog({ marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/from-catalog' });
    renderPage(['Admin']);

    expect(screen.getByRole('link', { name: /View on AWS Marketplace/i })).toHaveAttribute(
      'href',
      'https://aws.amazon.com/marketplace/...',
    );
  });

  it('says what to fix when there is no listing URL anywhere', () => {
    // A marketplace-live extension has no in-UI Subscribe button, so with no
    // listing URL the screen has no way forward. It must not be a silent dead end.
    noneStateWithCatalog({ marketplaceListingUrl: null });
    renderPage(['Admin'], { withMarketplaceUrlsProp: false });

    expect(screen.queryByRole('link', { name: /View on AWS Marketplace/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Subscribe$/i })).not.toBeInTheDocument();
    expect(screen.getByText(/extensions-marketplace\.yaml/)).toBeInTheDocument();
  });

  it('still offers the in-UI Subscribe button for a simulated extension', () => {
    // The dev loop is unaffected by any of this.
    mockCatalog({ source: 'marketplace', marketplaceListingUrl: 'https://aws.amazon.com/marketplace/pp/x' });
    mockedUseInstalled.mockReturnValue({
      features: [],
      loading: false,
      error: null,
      refresh: vi.fn(),
      byId: () => undefined,
    });
    mockedUseEntitlement.mockReturnValue({
      entitlement: ent({ state: 'NONE', source: 'simulated', licenseMode: 'simulated' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin'], { withMarketplaceUrlsProp: false });

    expect(screen.getByRole('button', { name: /^Subscribe$/i })).toBeInTheDocument();
  });
});
