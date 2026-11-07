// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `renderWithIntl`: `@testing-library/react`'s `render` under the app's `LocaleProvider`.
 *
 * Why it exists. The conversion PRs of #1770 (3–6) turn each screen's English into react-intl
 * messages and must show the screen rendering French from a fixture catalog. That takes the
 * provider stack the app mounts (`IntlProvider` with the locale's catalog, Cloudscape's
 * `I18nProvider` with its `fr` bundle, Amplify UI's language), a chosen locale, a fixture
 * catalog and a way to switch at run time. This helper is that, once, instead of one more
 * hand-rolled wrapper per test file.
 *
 * When not to use it. English tests keep plain `render`: English is inline (`defaultMessage`),
 * and `LocaleProvider` under `en` adds nothing to the markup (asserted in this helper's tests:
 * identical apart from per-render generated ids, which they normalise first). A test moves to
 * `renderWithIntl` when it renders another locale or its component reads `useLocale()`.
 *
 * Fixture catalogs. Catalogs are a build artefact (`virtual:i18n-catalogs`), so a test that
 * passes `messages` mocks that module once, at the top of the file:
 *
 *   vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));
 *
 * The helper routes `messages` through that mock, so `loadCatalog` and `LocaleProvider` run
 * unchanged; without the line, `messages` throws rather than quietly rendering English. The
 * catalog arrives asynchronously, as the chunk does in the browser, so the first assertion on
 * translated text awaits (`findBy*`, `waitFor`). Each call starts from an empty fixture
 * registry, so a fixture never outlives the call that registered it.
 *
 * Locale. The provider resolves its locale as in the browser (storage, then
 * `navigator.languages`), never from a prop; the helper seeds `localStorage` with `locale`
 * for the first render and restores the previous value right after, so the seed does not
 * leak into the next test. The returned `setLocale` is the app's own: it writes storage and
 * `<html lang>`, and Amplify's `I18n` language is a global, so a file that switches locales
 * resets those in `afterEach` as `src/i18n/__tests__` do.
 *
 * Console noise. `LocaleProvider`'s `onError` already swallows MISSING_TRANSLATION, so an id
 * the fixture lacks falls back to its English `defaultMessage` silently. This helper installs
 * no console spies; a test that asserts silence spies inside itself.
 */
import React from 'react';
import {
  act,
  render,
  renderHook,
  type RenderHookOptions,
  type RenderHookResult,
  type RenderOptions,
  type RenderResult,
} from '@testing-library/react';
import { loaders } from 'virtual:i18n-catalogs';
import { DEFAULT_LOCALE, LOCALE_STORAGE_KEY, type Locale } from '../i18n/locales';
import { LocaleProvider, useLocale, type LocaleContextValue } from '../i18n/LocaleProvider';
import { clearFixtureCatalogs, loaders as fixtureLoaders, setFixtureCatalog, type FixtureMessages } from './i18nCatalogsMock';

export type { FixtureMessages } from './i18nCatalogsMock';

export interface IntlRenderOptions {
  /** The locale to render in; must be in `enabledLocales`. Default `'en'`. */
  locale?: Locale;
  /** The allow-list handed to `LocaleProvider`. Default `['en']`, as the app ships today. */
  enabledLocales?: readonly Locale[];
  /** The fixture catalog for `locale` (never `en`, which is inline). Needs the `vi.mock` line above. */
  messages?: FixtureMessages;
  /** Rendered inside `LocaleProvider`, around the UI: an `AppContext.Provider`, a `MemoryRouter`, ... */
  wrapper?: React.JSXElementConstructor<{ children: React.ReactNode }>;
}

export type RenderWithIntlOptions = IntlRenderOptions & Omit<RenderOptions, 'wrapper' | 'queries'>;

export type RenderHookWithIntlOptions<Props> = IntlRenderOptions & Omit<RenderHookOptions<Props>, 'wrapper' | 'queries'>;

export interface IntlAccessors {
  /** The provider's current locale. */
  readonly locale: Locale;
  /** The app's `setLocale` from `useLocale()`, run inside `act`, so the tree has re-rendered on return. */
  setLocale: (next: string) => void;
}

const DEFAULT_ENABLED_LOCALES: readonly Locale[] = [DEFAULT_LOCALE];

const MOCK_HINT = "vi.mock('virtual:i18n-catalogs', () => import('<path to src/>test-utils/i18nCatalogsMock'))";

interface Captured {
  current: LocaleContextValue | null;
}

/** Renders nothing; keeps the provider's context value where the accessors can read it. */
const Capture = ({ into }: { into: Captured }): null => {
  into.current = useLocale();
  return null;
};

const captured = (into: Captured): LocaleContextValue => {
  if (into.current === null) throw new Error('renderWithIntl: the tree is not mounted');
  return into.current;
};

/**
 * Store the locale the way the switcher does, for the provider's first render, and return
 * the undo. The provider reads storage once, in its state initialiser, so the undo can run
 * as soon as `render` returns.
 */
const seedStoredLocale = (locale: Locale): (() => void) => {
  const previous = window.localStorage.getItem(LOCALE_STORAGE_KEY);
  window.localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  return () => {
    if (previous === null) window.localStorage.removeItem(LOCALE_STORAGE_KEY);
    else window.localStorage.setItem(LOCALE_STORAGE_KEY, previous);
  };
};

interface Prepared {
  wrapper: React.JSXElementConstructor<{ children: React.ReactNode }>;
  into: Captured;
  restoreStorage: () => void;
}

/** Check the options, register the fixture, seed storage and build the wrapper. */
const prepare = ({
  locale = DEFAULT_LOCALE,
  enabledLocales = DEFAULT_ENABLED_LOCALES,
  messages,
  wrapper: Inner,
}: IntlRenderOptions): Prepared => {
  if (!enabledLocales.includes(locale)) {
    throw new Error(
      `renderWithIntl: locale ${JSON.stringify(locale)} is not in enabledLocales ${JSON.stringify(enabledLocales)}; ` +
        `pass enabledLocales: ['en', ${JSON.stringify(locale)}]`,
    );
  }
  if (messages !== undefined) {
    if (locale === DEFAULT_LOCALE) {
      throw new Error('renderWithIntl: `messages` is a fixture catalog for a locale other than en; English is inline and has no catalog');
    }
    if (loaders !== fixtureLoaders) {
      throw new Error(`renderWithIntl: \`messages\` needs the catalog module mocked at the top of this test file: ${MOCK_HINT}`);
    }
  }
  if (loaders === fixtureLoaders) {
    clearFixtureCatalogs();
    if (messages !== undefined) setFixtureCatalog(locale, messages);
  }

  const into: Captured = { current: null };
  const Wrapper = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
    <LocaleProvider enabledLocales={enabledLocales}>
      <Capture into={into} />
      {Inner ? <Inner>{children}</Inner> : children}
    </LocaleProvider>
  );
  return { wrapper: Wrapper, into, restoreStorage: seedStoredLocale(locale) };
};

/** The render result with the accessors; `locale` stays a getter, so it follows `setLocale`. */
const withAccessors = <T extends object>(result: T, into: Captured): T & IntlAccessors => ({
  ...result,
  get locale() {
    return captured(into).locale;
  },
  setLocale: (next: string) => {
    act(() => {
      captured(into).setLocale(next);
    });
  },
});

/**
 * `render(ui)` inside the app's `LocaleProvider`, in `locale`, returning the render result
 * plus the `locale` and `setLocale` accessors.
 */
export const renderWithIntl = (ui: React.ReactNode, options: RenderWithIntlOptions = {}): RenderResult & IntlAccessors => {
  const { locale, enabledLocales, messages, wrapper, ...renderOptions } = options;
  const { wrapper: Wrapper, into, restoreStorage } = prepare({ locale, enabledLocales, messages, wrapper });
  try {
    const result = render(ui, { ...renderOptions, wrapper: Wrapper });
    return withAccessors(result, into);
  } finally {
    restoreStorage();
  }
};

/**
 * `renderHook(callback)` inside the same provider, for a hook that reads `useIntl()`,
 * `useLocale()` or `useFormatters()`.
 */
export const renderHookWithIntl = <Result, Props>(
  callback: (initialProps: Props) => Result,
  options: RenderHookWithIntlOptions<Props> = {},
): RenderHookResult<Result, Props> & IntlAccessors => {
  const { locale, enabledLocales, messages, wrapper, ...hookOptions } = options;
  const { wrapper: Wrapper, into, restoreStorage } = prepare({ locale, enabledLocales, messages, wrapper });
  try {
    const result = renderHook(callback, { ...hookOptions, wrapper: Wrapper });
    return withAccessors(result, into);
  } finally {
    restoreStorage();
  }
};
