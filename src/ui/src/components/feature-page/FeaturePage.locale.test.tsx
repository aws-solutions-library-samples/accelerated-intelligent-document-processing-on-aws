// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The locale FeaturePage passes an installed extension, through the real FeatureLoader
 * (FeaturePage.test.tsx stubs the loader for the whole file). jsdom never fetches the
 * bundle's `<script>`, so each test plays it: the bundle's top-level `register` call, then
 * the script's `load` event. FeatureLoader's registry outlives a test, so every render uses
 * its own featureId.
 */

import React, { useEffect } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import FeaturePage from './FeaturePage';
import type { CatalogFeature, FeatureContext, FeatureEntitlement, InstalledFeature } from '../../types/feature-platform';
import { LocaleProvider } from '../../i18n/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '../../i18n/locales';
import { renderWithIntl } from '../../test-utils/renderWithIntl';
import useInstalledFeatures from '../../hooks/use-installed-features';
import useCatalogFeatures from '../../hooks/use-catalog-features';
import useFeatureEntitlement from '../../hooks/use-feature-entitlement';
import useFeatureLaunchUrl from '../../hooks/use-feature-launch-url';
import useSubscribeFeature from '../../hooks/use-subscribe-feature';
import useUnsubscribeFeature from '../../hooks/use-unsubscribe-feature';

vi.mock('../../hooks/use-installed-features');
vi.mock('../../hooks/use-catalog-features');
vi.mock('../../hooks/use-feature-entitlement');
vi.mock('../../hooks/use-feature-launch-url');
vi.mock('../../hooks/use-subscribe-feature');
vi.mock('../../hooks/use-unsubscribe-feature');
vi.mock('aws-amplify/auth', () => ({ fetchAuthSession: vi.fn() }));
// A locale other than en loads its catalog; the fixture module serves an empty one.
vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));

const VERSION = '1.0.0';

/**
 * The page's data for one installed extension: open source, so the page shows no
 * subscription banner, and ACTIVE, so it renders the extension.
 */
const mockInstalledExtension = (featureId: string): void => {
  const installed: InstalledFeature = {
    featureId,
    displayName: featureId,
    installedVersion: VERSION,
    latestVersion: VERSION,
    updateAvailable: false,
    stackName: `idp-feature-${featureId}`,
    stackRegion: 'us-east-1',
    stackId: null,
    uiBundlePath: `features/${featureId}/v${VERSION}/`,
    featureApiEndpoint: null,
    iconUrl: null,
    installedAt: new Date(0).toISOString(),
    installedBy: null,
  };
  const catalogEntry: CatalogFeature = {
    featureId,
    displayName: featureId,
    latestVersion: VERSION,
    iconUrl: null,
    description: null,
    docsUrl: null,
    showInNav: true,
    source: 'oss',
    productCode: null,
    marketplaceListingUrl: null,
    availableInRegion: true,
    availableRegions: [],
  };
  const entitlement: FeatureEntitlement = {
    featureId,
    state: 'ACTIVE',
    expiresAt: null,
    customerIdentifier: null,
    productCode: null,
    source: 'oss',
  };
  vi.mocked(useInstalledFeatures).mockReturnValue({
    features: [installed],
    loading: false,
    error: null,
    refresh: vi.fn(),
    byId: (id) => (id === featureId ? installed : undefined),
  });
  vi.mocked(useCatalogFeatures).mockReturnValue({
    features: [catalogEntry],
    loading: false,
    error: null,
    refresh: vi.fn(),
    byId: (id) => (id === featureId ? catalogEntry : undefined),
  });
  vi.mocked(useFeatureEntitlement).mockReturnValue({ entitlement, loading: false, error: null, refresh: vi.fn() });
  vi.mocked(useFeatureLaunchUrl).mockReturnValue({ fetch: vi.fn(), loading: false, error: null });
  vi.mocked(useSubscribeFeature).mockReturnValue({ subscribe: vi.fn(), loading: false, error: null });
  vi.mocked(useUnsubscribeFeature).mockReturnValue({ unsubscribe: vi.fn(), loading: false, error: null });
};

const page = (featureId: string): React.JSX.Element => (
  <FeaturePage featureIdOverride={featureId} groups={['Viewer']} mainStackName="idp-main" />
);

/**
 * Play the extension's bundle on the `<script>` FeatureLoader appended, as the browser does:
 * the bundle's top-level code registers its component, then the element fires `load`.
 */
const playBundle = (featureId: string, Component: React.ComponentType<FeatureContext>): void => {
  const script = document.head.querySelector<HTMLScriptElement>(`script[data-idp-feature-id="${featureId}"]`);
  if (!script) throw new Error(`FeatureLoader appended no bundle script for ${featureId}`);
  window.IdpFeatures?.register(featureId, { Component, version: VERSION, displayName: featureId });
  fireEvent.load(script);
};

/** An extension that shows the locale it receives. */
const ShowsLocale = ({ locale }: FeatureContext): React.JSX.Element => <p>locale: {locale}</p>;

describe('FeaturePage passes the UI locale to the extension', () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('passes the current locale and follows a switch without remounting', async () => {
    const featureId = 'locale-switch';
    mockInstalledExtension(featureId);
    const mounted = vi.fn();
    const CountsMounts = ({ locale }: FeatureContext): React.JSX.Element => {
      useEffect(() => {
        mounted();
      }, []);
      return <p>locale: {locale}</p>;
    };
    const view = renderWithIntl(page(featureId), { enabledLocales: ['en', 'fr-CA', 'en-XA'], wrapper: MemoryRouter });
    playBundle(featureId, CountsMounts);
    expect(await screen.findByText('locale: en')).toBeInTheDocument();

    view.setLocale('fr-CA');
    expect(await screen.findByText('locale: fr-CA')).toBeInTheDocument();
    view.setLocale('en-XA');
    expect(await screen.findByText('locale: en-XA')).toBeInTheDocument();

    // Re-rendered with each value, never remounted, and the bundle's script added once.
    expect(mounted).toHaveBeenCalledTimes(1);
    expect(document.head.querySelectorAll(`script[data-idp-feature-id="${featureId}"]`)).toHaveLength(1);
  });

  it('mounts an extension built before the field existed', async () => {
    const featureId = 'built-before-locale';
    mockInstalledExtension(featureId);
    // Compiled against the context without `locale`: the host passes one more prop, which it never reads.
    const BuiltBefore: React.FC<Omit<FeatureContext, 'locale'>> = ({ featureId: id, installedVersion }) => (
      <p>
        {id} v{installedVersion}
      </p>
    );
    renderWithIntl(page(featureId), { wrapper: MemoryRouter });
    playBundle(featureId, BuiltBefore);
    expect(await screen.findByText(`${featureId} v${VERSION}`)).toBeInTheDocument();
  });

  it('passes only an enabled locale, never the stored text', async () => {
    // The provider is rendered directly: renderWithIntl would replace the stored value with its own locale.
    for (const [index, stored] of ['de-DE', '"><img src=x onerror=alert(1)>'].entries()) {
      const featureId = `stored-locale-${index}`;
      mockInstalledExtension(featureId);
      window.localStorage.setItem(LOCALE_STORAGE_KEY, stored);
      const view = render(<LocaleProvider enabledLocales={['en', 'fr-CA']}>{page(featureId)}</LocaleProvider>, { wrapper: MemoryRouter });
      playBundle(featureId, ShowsLocale);
      expect(await screen.findByText('locale: en')).toBeInTheDocument();
      view.unmount();
    }
  });

  it('passes en without a LocaleProvider', async () => {
    const featureId = 'no-locale-provider';
    mockInstalledExtension(featureId);
    render(page(featureId), { wrapper: MemoryRouter });
    playBundle(featureId, ShowsLocale);
    expect(await screen.findByText('locale: en')).toBeInTheDocument();
  });
});
