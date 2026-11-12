// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `loadCatalog` against a mocked `virtual:i18n-catalogs`: `en` never calls a loader, a locale
 * with a loader resolves to that loader's compiled catalog, and a locale without one resolves
 * to the empty (English-fallback) catalog.
 */
import { describe, expect, it, vi } from 'vitest';
import { EMPTY_CATALOG, loadCatalog } from '../loadCatalog';
import { ENABLED_LOCALES, type Locale } from '../locales';

const FR_CATALOG = { 'test.hello': [{ type: 0, value: 'Bonjour' }] };

const { frLoader, xaLoader } = vi.hoisted(() => ({
  frLoader: vi.fn(async () => ({ default: { 'test.hello': [{ type: 0, value: 'Bonjour' }] } })),
  xaLoader: vi.fn(async () => {
    throw new Error('chunk unreachable');
  }),
}));

vi.mock('virtual:i18n-catalogs', () => ({
  availableLocales: ['en', 'en-XA', 'fr-CA'],
  loaders: { 'fr-CA': frLoader, 'en-XA': xaLoader },
}));

describe('loadCatalog', () => {
  it('resolves en to the frozen empty catalog without calling any loader', async () => {
    const catalog = await loadCatalog('en');
    expect(catalog).toBe(EMPTY_CATALOG);
    expect(catalog).toEqual({});
    expect(Object.isFrozen(catalog)).toBe(true);
    expect(frLoader).not.toHaveBeenCalled();
    expect(xaLoader).not.toHaveBeenCalled();
  });

  it('loads a locale with a loader through that loader, once per call', async () => {
    expect(await loadCatalog('fr-CA')).toEqual(FR_CATALOG);
    expect(frLoader).toHaveBeenCalledTimes(1);
    expect(await loadCatalog('fr-CA')).toEqual(FR_CATALOG);
    expect(frLoader).toHaveBeenCalledTimes(2);
  });

  it('resolves a locale without a loader to the empty catalog', async () => {
    expect(await loadCatalog('de' as Locale)).toBe(EMPTY_CATALOG);
  });

  it('does not mistake an inherited property for a loader', async () => {
    expect(await loadCatalog('constructor' as Locale)).toBe(EMPTY_CATALOG);
    expect(await loadCatalog('toString' as Locale)).toBe(EMPTY_CATALOG);
  });

  it('rejects when the loader rejects, leaving the decision to the caller', async () => {
    await expect(loadCatalog('en-XA')).rejects.toThrow('chunk unreachable');
    expect(xaLoader).toHaveBeenCalledTimes(1);
  });

  it('derives the enabled locales from the catalogs the build exposes, in KNOWN_LOCALES order', () => {
    expect(ENABLED_LOCALES).toEqual(['en', 'fr-CA', 'en-XA']);
  });
});
