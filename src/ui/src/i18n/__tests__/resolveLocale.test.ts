// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The locale resolver is an allow-list (decision D19 of #1770): a stored choice only when
 * it exactly matches an enabled locale, else the browser's languages by exact then
 * language-subtag match, else the fallback. Every case is parametrised on the enabled list,
 * because `fr-CA` is not enabled yet and the rules must already hold for when it is.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_LOCALE, ENABLED_LOCALES, LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { readResolvedLocale, resolveLocale, type ResolveLocaleInput } from '../resolveLocale';

const EN_ONLY: readonly Locale[] = ['en'];
const EN_FR: readonly Locale[] = ['en', 'fr-CA'];
const WITH_PSEUDO: readonly Locale[] = ['en', 'fr-CA', 'en-XA'];

const resolve = (input: Partial<ResolveLocaleInput>): Locale =>
  resolveLocale({ stored: null, browserLanguages: [], enabled: EN_FR, fallback: DEFAULT_LOCALE, ...input });

describe('resolveLocale: stored choice', () => {
  it('honours a stored value that exactly equals an enabled locale, over the browser', () => {
    expect(resolve({ stored: 'fr-CA', browserLanguages: ['en-US'] })).toBe('fr-CA');
    expect(resolve({ stored: 'en', browserLanguages: ['fr-FR'] })).toBe('en');
  });

  it('trims surrounding whitespace before comparing', () => {
    expect(resolve({ stored: ' fr-CA ' })).toBe('fr-CA');
  });

  it('ignores a stored value outside the allow-list and falls back', () => {
    expect(resolve({ stored: 'de' })).toBe('en');
    expect(resolve({ stored: 'fr-CA', enabled: EN_ONLY })).toBe('en');
  });

  it('ignores an unknown stored value without blocking the browser step', () => {
    expect(resolve({ stored: 'de', browserLanguages: ['fr-FR'] })).toBe('fr-CA');
  });

  it.each([
    ['a trailing quote', ' fr-CA"'],
    ['upper case', 'FR-CA'],
    ['lower case', 'fr-ca'],
    ['a prototype key', '__proto__'],
    ['another prototype key', 'constructor'],
    ['a 10 KB string', 'x'.repeat(10 * 1024)],
    ['an embedded NUL', 'fr-CA\u0000'],
  ])('rejects a tampered stored value with %s', (_label, stored) => {
    expect(resolve({ stored })).toBe('en');
  });

  it('requires an exact match for stored values: fr-FR is not fr-CA', () => {
    expect(resolve({ stored: 'fr-FR' })).toBe('en');
    expect(resolve({ stored: 'fr-FR', browserLanguages: ['en-US'] })).toBe('en');
  });

  it('treats the empty string as no choice', () => {
    expect(resolve({ stored: '' })).toBe('en');
    expect(resolve({ stored: '', browserLanguages: ['fr-FR'] })).toBe('fr-CA');
  });
});

describe('resolveLocale: browser languages', () => {
  it.each<[readonly Locale[], Locale]>([
    [EN_ONLY, 'en'],
    [EN_FR, 'fr-CA'],
  ])('resolves a French browser against enabled %j to %s', (enabled, expected) => {
    expect(resolve({ browserLanguages: ['fr-FR', 'en-US'], enabled })).toBe(expected);
  });

  it('matches by language subtag: en-GB and bare fr', () => {
    expect(resolve({ browserLanguages: ['en-GB'] })).toBe('en');
    expect(resolve({ browserLanguages: ['fr'] })).toBe('fr-CA');
  });

  it('matches exactly without regard to case', () => {
    expect(resolve({ browserLanguages: ['FR-ca'] })).toBe('fr-CA');
    expect(resolve({ browserLanguages: ['EN'] })).toBe('en');
  });

  it('walks the languages in order and the first hit wins', () => {
    expect(resolve({ browserLanguages: ['fr-FR', 'en'] })).toBe('fr-CA');
    expect(resolve({ browserLanguages: ['de-DE', 'en-AU', 'fr-CA'] })).toBe('en');
  });

  it('falls back when no language matches', () => {
    expect(resolve({ browserLanguages: ['de-DE'] })).toBe('en');
    expect(resolve({ browserLanguages: ['de-DE', 'ja'] })).toBe('en');
  });

  it('returns the given fallback, not a hard-coded one', () => {
    expect(resolve({ browserLanguages: ['de-DE'], fallback: 'fr-CA' })).toBe('fr-CA');
  });

  it('never picks the pseudo-locale by subtag match', () => {
    expect(resolve({ browserLanguages: ['en-GB'], enabled: WITH_PSEUDO })).toBe('en');
  });
});

describe('resolveLocale: pseudo-locale and empty input', () => {
  it('resolves en-XA only when it is enabled', () => {
    expect(resolve({ stored: 'en-XA' })).toBe('en');
    expect(resolve({ stored: 'en-XA', enabled: WITH_PSEUDO })).toBe('en-XA');
    expect(resolve({ browserLanguages: ['en-XA'] })).toBe('en');
    expect(resolve({ browserLanguages: ['en-XA'], enabled: WITH_PSEUDO })).toBe('en-XA');
  });

  it('falls back on empty input', () => {
    expect(resolve({})).toBe('en');
    expect(resolve({ enabled: [] })).toBe('en');
  });
});

describe('readResolvedLocale (storage and navigator)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    window.history.replaceState(null, '', '/');
  });

  const setBrowserLanguages = (languages: readonly string[]): void => {
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
  };

  it('reads the stored choice under the idp-locale key', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    expect(readResolvedLocale(EN_FR)).toBe('fr-CA');
  });

  it('reads navigator.languages when nothing is stored', () => {
    setBrowserLanguages(['fr-FR', 'en-US']);
    expect(readResolvedLocale(EN_FR)).toBe('fr-CA');
    expect(readResolvedLocale(EN_ONLY)).toBe('en');
  });

  it('falls back to navigator.language when navigator.languages is empty', () => {
    setBrowserLanguages([]);
    vi.spyOn(navigator, 'language', 'get').mockReturnValue('fr-CA');
    expect(readResolvedLocale(EN_FR)).toBe('fr-CA');
  });

  it('defaults to the production allow-list, where only en is enabled', () => {
    expect(ENABLED_LOCALES).toEqual(['en']);
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['fr-CA']);
    expect(readResolvedLocale()).toBe('en');
  });

  it('never reads the URL', () => {
    window.history.replaceState(null, '', '/?lang=fr-CA&locale=fr-CA#locale=fr-CA');
    expect(window.location.search).toBe('?lang=fr-CA&locale=fr-CA');
    setBrowserLanguages(['en-US']);
    expect(readResolvedLocale(EN_FR)).toBe('en');
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
    setBrowserLanguages(['fr-FR']);
    expect(readResolvedLocale(EN_FR)).toBe('en');
  });

  it('treats a throwing localStorage as no stored choice', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    setBrowserLanguages(['fr-FR']);
    expect(readResolvedLocale(EN_FR)).toBe('fr-CA');
    expect(readResolvedLocale(EN_ONLY)).toBe('en');
  });
});
