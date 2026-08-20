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
  source: 'simulator',
  ...overrides,
});

function renderPage(groups: string[]) {
  return render(
    <MemoryRouter initialEntries={['/features/docs-by-status']}>
      <Routes>
        <Route
          path="/features/:featureId"
          element={
            <FeaturePage
              groups={groups}
              mainStackName="idp-main"
              marketplaceUrls={{ 'docs-by-status': 'https://aws.amazon.com/marketplace/...' }}
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
      entitlement: ent({ state: 'ACTIVE', source: 'simulator' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Admin']);
    expect(screen.getByRole('button', { name: /Cancel Subscription/i })).toBeInTheDocument();
    expect(screen.getByText(/Subscription active/i)).toBeInTheDocument();
    // "simulator" appears both in the ActiveSubscriptionBanner (Source: simulator)
    // and the UpToDateBanner (up to date (simulator)). Just confirm the banner text.
    expect(screen.getByText(/^Source:$/)).toBeInTheDocument();
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
      entitlement: ent({ state: 'ACTIVE', source: 'simulator' }),
      loading: false,
      error: null,
      refresh: vi.fn(),
    });
    renderPage(['Viewer']);
    expect(screen.queryByRole('button', { name: /Cancel Subscription/i })).not.toBeInTheDocument();
    // The active subscription banner itself still renders.
    expect(screen.getByText(/Subscription active/i)).toBeInTheDocument();
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
      entitlement: ent({ state: 'ACTIVE', source: 'simulator' }),
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
