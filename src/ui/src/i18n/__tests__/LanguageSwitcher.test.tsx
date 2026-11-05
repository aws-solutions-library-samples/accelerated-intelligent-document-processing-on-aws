// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The language switcher utility: nothing while one locale is enabled (so the English shell
 * is unchanged), a dropdown of the enabled locales by their own names otherwise, choosing one
 * switches, and its own two strings come from the catalog like any other message.
 */
import React from 'react';
import { join } from 'node:path';
import { TopNavigation } from '@cloudscape-design/components';
import type { TopNavigationProps } from '@cloudscape-design/components';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { compileCatalog, readCatalog, sourceHash } from '../../../scripts/i18n/catalog.mjs';
import { messages, useLanguageSwitcherUtility } from '../LanguageSwitcher';
import type { CatalogMessages } from '../loadCatalog';
import { LOCALE_DISPLAY_NAMES, LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { LocaleProvider } from '../LocaleProvider';

const EN_ONLY: readonly Locale[] = ['en'];
const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

/** The committed English catalog, so the fixture translation is checked against the real ids. */
const EN_JSON = readCatalog(join(__dirname, '..', 'en.json'));
const FR_JSON = {
  'i18n.switcher.label': { message: 'Langue', sourceHash: sourceHash(messages.label.defaultMessage) },
  'i18n.switcher.ariaLabel': { message: 'Langue, actuellement {language}', sourceHash: sourceHash(messages.ariaLabel.defaultMessage) },
};

const { frLoader } = vi.hoisted(() => ({ frLoader: vi.fn<() => Promise<{ default: Record<string, unknown> }>>() }));

vi.mock('virtual:i18n-catalogs', () => ({
  availableLocales: ['en', 'fr-CA'],
  loaders: { 'fr-CA': frLoader },
}));

const compiled = (): CatalogMessages => {
  const { messages: table, problems } = compileCatalog(EN_JSON, FR_JSON, 'fr-CA');
  expect(problems).toEqual([]);
  return table;
};

type MenuUtility = TopNavigationProps.MenuDropdownUtility;
type ItemClickEvent = Parameters<NonNullable<MenuUtility['onItemClick']>>[0];

const menu = (utility: TopNavigationProps.Utility | null): MenuUtility => {
  expect(utility).not.toBeNull();
  expect(utility?.type).toBe('menu-dropdown');
  return utility as MenuUtility;
};

const choose = (utility: MenuUtility, id: string): void => {
  act(() => {
    utility.onItemClick?.(new CustomEvent('click', { detail: { id } }) as unknown as ItemClickEvent);
  });
};

/** A `renderHook` wrapper: the provider with the given allow-list. */
const wrapper = (enabledLocales: readonly Locale[]) => {
  const Wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
    <LocaleProvider enabledLocales={enabledLocales}>{children}</LocaleProvider>
  );
  return Wrapper;
};

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
};

describe('useLanguageSwitcherUtility', () => {
  beforeEach(() => {
    frLoader.mockReset();
    frLoader.mockImplementation(async () => ({ default: compiled() }));
    setBrowserLanguages(['en-US']);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('is null while only one locale is enabled, so the top navigation adds nothing', () => {
    const { result } = renderHook(() => useLanguageSwitcherUtility(), { wrapper: wrapper(EN_ONLY) });
    expect(result.current).toBeNull();
  });

  it('is a dropdown of the enabled locales by their own names, showing the current one', () => {
    const { result } = renderHook(() => useLanguageSwitcherUtility(), { wrapper: wrapper(EN_FR) });
    const utility = menu(result.current);
    expect(utility.text).toBe('English');
    expect(utility.iconName).toBe('globe');
    expect(utility.title).toBe('Language');
    expect(utility.ariaLabel).toBe('Language, currently English');
    expect(utility.items).toEqual([
      { id: 'en', text: LOCALE_DISPLAY_NAMES.en },
      { id: 'fr-CA', text: LOCALE_DISPLAY_NAMES['fr-CA'] },
    ]);
  });

  it('choosing an item calls setLocale: the choice persists and the dropdown follows it', async () => {
    const { result } = renderHook(() => useLanguageSwitcherUtility(), { wrapper: wrapper(EN_FR) });
    choose(menu(result.current), 'fr-CA');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('fr-CA');
    expect(document.documentElement.lang).toBe('fr-CA');
    expect(menu(result.current).text).toBe('Français (Canada)');
    // Its own strings come from the catalog once it has loaded.
    await waitFor(() => expect(menu(result.current).title).toBe('Langue'));
    expect(menu(result.current).ariaLabel).toBe('Langue, actuellement Français (Canada)');

    choose(menu(result.current), 'en');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('en');
    expect(menu(result.current).text).toBe('English');
    expect(menu(result.current).title).toBe('Language');
  });

  it('ignores an item outside the enabled list', () => {
    const { result } = renderHook(() => useLanguageSwitcherUtility(), { wrapper: wrapper(EN_FR) });
    choose(menu(result.current), 'de');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBeNull();
    expect(menu(result.current).text).toBe('English');
  });

  it('renders in the top navigation with its accessible label, in English and then in French', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    const Harness = (): React.JSX.Element => {
      const utility = useLanguageSwitcherUtility();
      return <TopNavigation identity={{ href: '#', title: 'Console' }} utilities={utility ? [utility] : []} />;
    };
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Harness />
      </LocaleProvider>,
    );
    // Inline English until the French catalog chunk arrives.
    expect(screen.getAllByLabelText('Language, currently Français (Canada)').length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getAllByLabelText('Langue, actuellement Français (Canada)').length).toBeGreaterThan(0));
    expect(screen.queryAllByLabelText('Language, currently Français (Canada)')).toEqual([]);
  });

  it('declares its messages with stable dotted ids, literal English and a description, as extracted into en.json', () => {
    for (const descriptor of Object.values(messages)) {
      expect(descriptor.id).toMatch(/^i18n\.switcher\.[a-zA-Z]+$/);
      expect(typeof descriptor.defaultMessage).toBe('string');
      expect(EN_JSON[descriptor.id]).toEqual({ defaultMessage: descriptor.defaultMessage, description: descriptor.description });
    }
  });
});
