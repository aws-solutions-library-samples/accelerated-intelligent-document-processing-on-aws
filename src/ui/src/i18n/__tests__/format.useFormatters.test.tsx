// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `useFormatters` inside the provider: the helpers bound to the current locale, one stable
 * object per locale, re-bound when the locale changes. `fr-CA` is enabled through a mocked
 * `virtual:i18n-catalogs`, as the other provider tests do.
 */
import React from 'react';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatDateTime, formatUsDollars, useFormatters } from '../format';
import { EMPTY_CATALOG } from '../loadCatalog';
import { LocaleProvider, useLocale } from '../LocaleProvider';

vi.mock('virtual:i18n-catalogs', () => ({
  availableLocales: ['en', 'fr-CA'],
  loaders: { 'fr-CA': async () => ({ default: EMPTY_CATALOG }) },
}));

const DATE = new Date(Date.UTC(2026, 9, 30, 17, 40, 5));
const UTC = { timeZone: 'UTC' } as const;

const Wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => <LocaleProvider>{children}</LocaleProvider>;

const useProbe = () => ({ formatters: useFormatters(), locale: useLocale() });

describe('useFormatters', () => {
  beforeEach(() => {
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['en-US']);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('binds every helper to the current locale and re-binds after setLocale', () => {
    const { result } = renderHook(useProbe, { wrapper: Wrapper });
    expect(result.current.locale.locale).toBe('en');
    const en = result.current.formatters;
    expect(en.formatDateTime(DATE, UTC)).toBe(formatDateTime(DATE, 'en', UTC));
    expect(en.formatDateTime(DATE, UTC)).toBe(DATE.toLocaleString(undefined, UTC));
    expect(en.formatUsDollars(1234.5, { fractionDigits: 2 })).toBe(formatUsDollars(1234.5, 'en', { fractionDigits: 2 }));

    act(() => result.current.locale.setLocale('fr-CA'));
    expect(result.current.locale.locale).toBe('fr-CA');
    const fr = result.current.formatters;
    expect(fr.formatDateTime(DATE, UTC)).toBe('2026-10-30 17 h 40 min 05 s');
    expect(fr.formatDate(DATE, { dateStyle: 'long', ...UTC })).toBe('30 octobre 2026');
    expect(fr.formatTime(DATE, { timeStyle: 'short', ...UTC })).toBe('17 h 40');
    expect(fr.formatNumber(1234.56)).toMatch(/^1\s234,56$/);
    expect(fr.formatFixed(1234.5, 2, { useGrouping: false })).toBe('1234,50');
    expect(fr.formatPercent(0.125, { fractionDigits: 1 })).toMatch(/^12,5\s%$/);
    expect(fr.formatUsDollars(1234.5, { fractionDigits: 2 })).toMatch(/^1\s234,50\sUS\$$/);
    // Still bound to `en`: the first object is not mutated, it is replaced.
    expect(en.formatDateTime(DATE, UTC)).toBe(DATE.toLocaleString(undefined, UTC));
  });

  it('returns the same object across re-renders with the same locale, a new one per locale', () => {
    const { result, rerender } = renderHook(useProbe, { wrapper: Wrapper });
    const first = result.current.formatters;
    rerender();
    expect(result.current.formatters).toBe(first);

    act(() => result.current.locale.setLocale('fr-CA'));
    const second = result.current.formatters;
    expect(second).not.toBe(first);
    rerender();
    expect(result.current.formatters).toBe(second);

    act(() => result.current.locale.setLocale('en'));
    expect(result.current.formatters).not.toBe(second);
  });
});
