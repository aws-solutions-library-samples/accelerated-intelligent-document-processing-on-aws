// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Vitest coverage for the Extensions info (Tools) panel.
 *
 * The panel is built from listCatalogFeatures + listInstalledFeatures only — it
 * has no entitlement verdict — so these tests are mostly about it not claiming to
 * know things it can't. That is the bug they were written for: it told a customer
 * who had already paid to "Subscribe to install".
 */

import React from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import FeaturesToolsPanel from './FeaturesToolsPanel';
import type { CatalogFeature, InstalledFeature } from '../../types/feature-platform';

vi.mock('../../hooks/use-catalog-features');
vi.mock('../../hooks/use-installed-features');

import useCatalogFeatures from '../../hooks/use-catalog-features';
import useInstalledFeatures from '../../hooks/use-installed-features';

const mockedUseCatalog = vi.mocked(useCatalogFeatures);
const mockedUseInstalled = vi.mocked(useInstalledFeatures);

const FEATURE_ID = 'idp-auto-optimizer';

function mockCatalog(entry?: Partial<CatalogFeature>) {
  mockedUseCatalog.mockReturnValue({
    features: [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    byId: () =>
      entry
        ? ({
            featureId: FEATURE_ID,
            displayName: 'Auto Optimizer (Beta)',
            latestVersion: '0.3.0',
            iconUrl: null,
            description: 'Autonomous configuration optimization.',
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

function mockInstalled(entry?: Partial<InstalledFeature>) {
  const row = entry
    ? ({
        featureId: FEATURE_ID,
        displayName: 'Auto Optimizer (Beta)',
        installedVersion: '0.3.0',
        latestVersion: '0.3.0',
        updateAvailable: false,
        stackName: 'idp-feature-auto-optimizer',
        stackRegion: 'us-east-1',
        stackId: null,
        uiBundlePath: 'features/idp-auto-optimizer/v0.3.0/',
        featureApiEndpoint: 'https://feat.example.com',
        iconUrl: null,
        installedAt: '2026-01-01T00:00:00Z',
        installedBy: null,
        ...entry,
      } as InstalledFeature)
    : undefined;
  mockedUseInstalled.mockReturnValue({
    features: row ? [row] : [],
    loading: false,
    error: null,
    refresh: vi.fn(),
    byId: () => row,
  });
}

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={[`/features/${FEATURE_ID}`]}>
      <Routes>
        <Route path="/features/:featureId" element={<FeaturesToolsPanel />} />
      </Routes>
    </MemoryRouter>,
  );
}

/**
 * Scope queries to the "Selected extension" block.
 *
 * The overview below it legitimately uses the same words — "AWS Marketplace",
 * "Open source", "Ready" all appear in the "Two kinds" and "Lifecycle" lists — so
 * an unscoped getByText matches two nodes and says nothing about the one under
 * test.
 */
function selectedBlock(): HTMLElement {
  const heading = screen.getByRole('heading', { name: 'Selected extension' });
  return heading.parentElement as HTMLElement;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockCatalog({});
  mockInstalled();
});

describe('FeaturesToolsPanel selected extension', () => {
  it('does NOT tell a not-yet-installed marketplace extension to subscribe', () => {
    // The reported defect. This panel cannot see entitlement, so "Subscribe to
    // install" was a guess — and it was wrong for the one customer it mattered
    // to: someone already subscribed who only needed to launch the stack.
    renderPanel();

    expect(within(selectedBlock()).getByText('Not installed')).toBeInTheDocument();
    expect(screen.queryByText(/Subscribe to install/i)).not.toBeInTheDocument();
  });

  it('states the subscription REQUIREMENT without claiming the customer lacks one', () => {
    // A property of the extension, straight from the catalog. Same split the nav
    // hover text makes.
    renderPanel();

    expect(screen.getByText(/Requires an AWS Marketplace subscription\./)).toBeInTheDocument();
  });

  it('drops the subscription note once installed — by then it is noise', () => {
    mockInstalled({});
    renderPanel();

    const block = within(selectedBlock());
    expect(block.getByText('Ready')).toBeInTheDocument();
    expect(screen.queryByText(/Requires an AWS Marketplace subscription/)).not.toBeInTheDocument();
    expect(block.getByText(/Installed v0\.3\.0/)).toBeInTheDocument();
  });

  it('says "Update available" when installed behind the published latest', () => {
    mockInstalled({ installedVersion: '0.2.0', latestVersion: '0.3.0', updateAvailable: true });
    renderPanel();

    expect(within(selectedBlock()).getByText('Update available')).toBeInTheDocument();
  });

  it('reports Region unavailability, which outranks anything about installing', () => {
    // Installing cannot succeed here at all, so leading with "Not installed"
    // would point the admin at an action that dead-ends.
    mockCatalog({ availableInRegion: false });
    renderPanel();

    const block = within(selectedBlock());
    expect(block.getByText('Not available in this Region')).toBeInTheDocument();
    expect(block.queryByText('Not installed')).not.toBeInTheDocument();
  });

  it('labels the source AWS Marketplace, with no "(future)" qualifier', () => {
    renderPanel();

    expect(within(selectedBlock()).getByText('AWS Marketplace')).toBeInTheDocument();
    // Nothing anywhere in the panel — the badge, the status line, and both
    // overview lists all carried a "(future)" that is no longer true.
    expect(screen.queryByText(/future/i)).not.toBeInTheDocument();
  });

  it('labels an OSS extension Open source and asks nothing of a subscription', () => {
    mockCatalog({ source: 'oss' });
    renderPanel();

    const block = within(selectedBlock());
    expect(block.getByText('Open source')).toBeInTheDocument();
    expect(block.getByText('Not installed')).toBeInTheDocument();
    expect(screen.queryByText(/Requires an AWS Marketplace subscription/)).not.toBeInTheDocument();
  });
});

describe('FeaturesToolsPanel overview', () => {
  it('no longer describes the extension framework as an unpopulated preview', () => {
    renderPanel();

    expect(screen.queryByText(/still being built out/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/no Marketplace extensions are available yet/i)).not.toBeInTheDocument();
  });

  it('renders the overview even with no feature selected', () => {
    // /features (catalog browser) mounts the same panel with no :featureId.
    render(
      <MemoryRouter initialEntries={['/features']}>
        <Routes>
          <Route path="/features" element={<FeaturesToolsPanel />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(screen.getByText(/Extensions are installable add-ons/i)).toBeInTheDocument();
    expect(screen.queryByText(/Selected extension/i)).not.toBeInTheDocument();
  });
});
