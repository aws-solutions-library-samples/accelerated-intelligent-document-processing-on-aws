// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The runtime under the pseudo-locale (`en-XA`, decision D21 of #1770), as a flagged build
 * exposes it: `virtual:i18n-catalogs` lists `en-XA` with a loader whose chunk holds the ASTs
 * `formatjs compile --pseudo-locale en-XA` produces. Nothing in the runtime knows the
 * pseudo-locale specially: the enabled list derives it, the switcher offers it by its display
 * name, the provider loads its catalog like a translation and sets `<html lang>` from it, and an
 * English browser still resolves to `en`.
 */
import React from 'react';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { TopNavigationProps } from '@cloudscape-design/components';
import { FormattedMessage } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { messages, useLanguageSwitcherUtility } from '../LanguageSwitcher';
import type { CatalogMessages } from '../loadCatalog';
import { ENABLED_LOCALES, LOCALE_DISPLAY_NAMES, LOCALE_STORAGE_KEY } from '../locales';
import { LocaleProvider, useLocale } from '../LocaleProvider';
import { readResolvedLocale } from '../resolveLocale';

/** The compiled `en-XA` chunk: what the CLI produces for these three English messages. */
const PSEUDO_CATALOG: CatalogMessages = {
  'test.hello': [
    { type: 0, value: '[' },
    { type: 0, value: 'Ħḗḗŀŀǿǿ' },
    { type: 0, value: ']' },
  ],
  'i18n.switcher.label': [
    { type: 0, value: '[' },
    { type: 0, value: 'Ŀȧȧƞɠŭŭȧȧɠḗḗ' },
    { type: 0, value: ']' },
  ],
  'i18n.switcher.ariaLabel': [
    { type: 0, value: '[' },
    { type: 0, value: 'Ŀȧȧƞɠŭŭȧȧɠḗḗ, ƈŭŭřřḗḗƞŧŀẏ ' },
    { type: 1, value: 'language' },
    { type: 0, value: ']' },
  ],
};

const { xaLoader } = vi.hoisted(() => ({ xaLoader: vi.fn<() => Promise<{ default: Record<string, unknown> }>>() }));

vi.mock('virtual:i18n-catalogs', () => ({
  availableLocales: ['en', 'en-XA'],
  loaders: { 'en-XA': xaLoader },
}));

const Probe = (): React.JSX.Element => {
  const { locale } = useLocale();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="hello">
        <FormattedMessage id="test.hello" defaultMessage="Hello" />
      </span>
    </div>
  );
};

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
};

type MenuUtility = TopNavigationProps.MenuDropdownUtility;
type ItemClickEvent = Parameters<NonNullable<MenuUtility['onItemClick']>>[0];

const menu = (utility: TopNavigationProps.Utility | null): MenuUtility => {
  expect(utility?.type).toBe('menu-dropdown');
  return utility as MenuUtility;
};

const Wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => <LocaleProvider>{children}</LocaleProvider>;

describe('the pseudo-locale on a flagged build', () => {
  beforeEach(() => {
    xaLoader.mockReset();
    xaLoader.mockImplementation(async () => ({ default: PSEUDO_CATALOG }));
    setBrowserLanguages(['en-US']);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('is enabled by the catalog the build exposes, after en, with its display name', () => {
    expect(ENABLED_LOCALES).toEqual(['en', 'en-XA']);
    expect(LOCALE_DISPLAY_NAMES['en-XA']).toBe('Pseudo-locale (test)');
  });

  it('is never picked for an English browser that chose nothing: en stays the default', () => {
    expect(readResolvedLocale()).toBe('en');
    setBrowserLanguages(['en-GB', 'en']);
    expect(readResolvedLocale()).toBe('en');
    expect(xaLoader).not.toHaveBeenCalled();
  });

  it('renders the pseudo-localised text, not the English, and sets <html lang> to en-XA when chosen', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'en-XA');
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('en-XA');
    expect(document.documentElement.lang).toBe('en-XA');
    expect(xaLoader).toHaveBeenCalledTimes(1);
    // Inline English until the chunk arrives, as for any locale.
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');

    await waitFor(() => expect(screen.getByTestId('hello')).toHaveTextContent('[Ħḗḗŀŀǿǿ]'));
    expect(screen.getByTestId('hello')).not.toHaveTextContent('Hello');
  });

  it('is offered by the switcher as "Pseudo-locale (test)", and choosing it switches the switcher’s own strings', async () => {
    const { result } = renderHook(() => useLanguageSwitcherUtility(), { wrapper: Wrapper });
    const utility = menu(result.current);
    expect(utility.text).toBe('English');
    expect(utility.items).toEqual([
      { id: 'en', text: 'English' },
      { id: 'en-XA', text: 'Pseudo-locale (test)' },
    ]);

    act(() => {
      utility.onItemClick?.(new CustomEvent('click', { detail: { id: 'en-XA' } }) as unknown as ItemClickEvent);
    });
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('en-XA');
    expect(document.documentElement.lang).toBe('en-XA');
    expect(menu(result.current).text).toBe('Pseudo-locale (test)');
    await waitFor(() => expect(menu(result.current).title).toBe('[Ŀȧȧƞɠŭŭȧȧɠḗḗ]'));
    expect(menu(result.current).ariaLabel).toBe('[Ŀȧȧƞɠŭŭȧȧɠḗḗ, ƈŭŭřřḗḗƞŧŀẏ Pseudo-locale (test)]');
    expect(menu(result.current).title).not.toBe(messages.label.defaultMessage);
  });
});
