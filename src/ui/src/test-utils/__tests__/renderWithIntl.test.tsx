// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `renderWithIntl` renders the app's provider stack in a chosen locale, feeds it a fixture
 * catalog through the mocked `virtual:i18n-catalogs`, switches at run time, and under `en`
 * changes nothing in the markup. Explicit locales throughout: jsdom's `Intl` follows the
 * process locale, the provider never does.
 */
import React from 'react';
import Alert from '@cloudscape-design/components/alert';
import { act, render, screen, waitFor } from '@testing-library/react';
import { FormattedMessage, useIntl } from 'react-intl';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DocumentsCommonHeader, type MappedDocument } from '../../components/document-list/documents-table-config';
import { KNOWN_LOCALES, LOCALE_STORAGE_KEY, type Locale } from '../../i18n/locales';
import { useLocale } from '../../i18n/LocaleProvider';
import { loaders as fixtureLoaders } from '../i18nCatalogsMock';
import { renderHookWithIntl, renderWithIntl } from '../renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../i18nCatalogsMock'));
// CircuitBreakerBadge talks to the API on mount (as in DocumentsCommonHeader.test.tsx).
vi.mock('../../components/document-list/CircuitBreakerBadge', () => ({ default: () => <span /> }));

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

const FR = {
  'test.hello': 'Bonjour',
  'test.files': '{count, plural, one {# fichier} other {# fichiers}}',
};

const Greeting = (): React.JSX.Element => (
  <div>
    <span data-testid="hello">
      <FormattedMessage id="test.hello" defaultMessage="Hello" />
    </span>
    <span data-testid="files">
      <FormattedMessage id="test.files" defaultMessage="{count, plural, one {# file} other {# files}}" values={{ count: 2 }} />
    </span>
  </div>
);

/** Lets every pending microtask run (the catalog loader's promise chain included). */
const settle = (): Promise<void> =>
  act(async () => {
    await new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });
  });

describe('renderWithIntl', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('renders the inline English by default, under en, with the allow-list the app ships', () => {
    const result = renderWithIntl(<Greeting />);
    expect(result.locale).toBe('en');
    expect(result.getByTestId('hello')).toHaveTextContent('Hello');
    expect(result.getByTestId('files')).toHaveTextContent('2 files');
    expect(document.documentElement.lang).toBe('en');
  });

  it('renders French from the fixture catalog under fr-CA and sets <html lang>', async () => {
    const result = renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR, messages: FR });
    expect(result.locale).toBe('fr-CA');
    expect(document.documentElement.lang).toBe('fr-CA');
    // The inline English shows while the catalog loads, as in the browser.
    expect(result.getByTestId('hello')).toHaveTextContent('Hello');
    await waitFor(() => expect(result.getByTestId('hello')).toHaveTextContent('Bonjour'));
    expect(result.getByTestId('files')).toHaveTextContent('2 fichiers');
  });

  it('falls back to the English defaultMessage for an id the fixture lacks, with no console output', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const result = renderWithIntl(
      <>
        <Greeting />
        <FormattedMessage id="test.untranslated" defaultMessage="Only in English" />
      </>,
      { locale: 'fr-CA', enabledLocales: EN_FR, messages: FR },
    );
    await waitFor(() => expect(result.getByTestId('hello')).toHaveTextContent('Bonjour'));
    expect(result.getByText('Only in English')).toBeInTheDocument();
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it('composes with a wrapper, rendered inside the provider', async () => {
    const WhereAmI = (): React.JSX.Element => <span data-testid="path">{useLocation().pathname}</span>;
    // Reads the locale itself, which throws unless the wrapper sits inside LocaleProvider.
    const Routed = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
      <MemoryRouter initialEntries={['/documents/upload']}>
        <span data-testid="wrapper-locale">{useLocale().locale}</span>
        {children}
      </MemoryRouter>
    );
    const result = renderWithIntl(
      <>
        <WhereAmI />
        <Greeting />
      </>,
      { locale: 'fr-CA', enabledLocales: EN_FR, messages: FR, wrapper: Routed },
    );
    expect(result.getByTestId('path')).toHaveTextContent('/documents/upload');
    expect(result.getByTestId('wrapper-locale')).toHaveTextContent('fr-CA');
    await waitFor(() => expect(result.getByTestId('hello')).toHaveTextContent('Bonjour'));
  });

  it('switches the rendered text through setLocale, both ways', async () => {
    const result = renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR, messages: FR });
    await waitFor(() => expect(result.getByTestId('hello')).toHaveTextContent('Bonjour'));

    result.setLocale('en');
    expect(result.locale).toBe('en');
    expect(result.getByTestId('hello')).toHaveTextContent('Hello');
    expect(document.documentElement.lang).toBe('en');

    result.setLocale('fr-CA');
    expect(result.locale).toBe('fr-CA');
    await waitFor(() => expect(result.getByTestId('hello')).toHaveTextContent('Bonjour'));
    // It is the app's own setLocale: the choice is remembered, as from the switcher.
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('fr-CA');
  });

  it('ignores a setLocale outside the allow-list, as the app does', () => {
    const result = renderWithIntl(<Greeting />, { enabledLocales: EN_FR });
    result.setLocale('fr-FR');
    expect(result.locale).toBe('en');
    expect(result.getByTestId('hello')).toHaveTextContent('Hello');
  });

  it('pins the locale whatever the browser prefers', () => {
    vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['fr-CA', 'fr']);
    const result = renderWithIntl(<Greeting />, { enabledLocales: EN_FR });
    expect(result.locale).toBe('en');
  });

  it('seeds storage for the first render only and puts the previous value back', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
    const result = renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR });
    expect(result.locale).toBe('fr-CA');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('en');
    result.unmount();

    window.localStorage.clear();
    renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR });
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBeNull();
  });

  it('refuses a locale outside enabledLocales, and a fixture for en', () => {
    expect(() => renderWithIntl(<Greeting />, { locale: 'fr-CA' })).toThrow(/"fr-CA" is not in enabledLocales/);
    expect(() => renderWithIntl(<Greeting />, { messages: FR })).toThrow(/English is inline/);
  });

  it('starts each call from an empty fixture registry', async () => {
    const first = renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR, messages: FR });
    await waitFor(() => expect(first.getByTestId('hello')).toHaveTextContent('Bonjour'));
    expect(Object.keys((await fixtureLoaders['fr-CA']()).default)).toEqual(['test.hello', 'test.files']);
    first.unmount();

    const second = renderWithIntl(<Greeting />, { locale: 'fr-CA', enabledLocales: EN_FR });
    expect((await fixtureLoaders['fr-CA']()).default).toEqual({});
    await settle();
    expect(second.locale).toBe('fr-CA');
    expect(second.getByTestId('hello')).toHaveTextContent('Hello');
  });

  it('renders a hook under the same provider', async () => {
    const result = renderHookWithIntl(() => useIntl().formatMessage({ id: 'test.hello', defaultMessage: 'Hello' }), {
      locale: 'fr-CA',
      enabledLocales: EN_FR,
      messages: FR,
    });
    expect(result.result.current).toBe('Hello');
    await waitFor(() => expect(result.result.current).toBe('Bonjour'));
    result.setLocale('en');
    expect(result.locale).toBe('en');
    expect(result.result.current).toBe('Hello');
  });

  it('loads the Cloudscape fr bundle under fr-CA, as the app does', async () => {
    renderWithIntl(
      <Alert dismissible type="info">
        hello
      </Alert>,
      { locale: 'fr-CA', enabledLocales: EN_FR },
    );
    // The value in node_modules/@cloudscape-design/components/i18n/messages/all.fr.json.
    expect(await screen.findByRole('button', { name: "Ignorer l'alerte" })).toBeInTheDocument();
  });

  it('offers a fixture loader for every known locale other than en', () => {
    expect(Object.keys(fixtureLoaders).sort()).toEqual(KNOWN_LOCALES.filter((locale) => locale !== 'en').sort());
  });
});

/**
 * React's `useId` values and Cloudscape's performance marks (a counter plus a timestamp) differ
 * between any two renders of the same tree; nothing else may.
 */
const withoutPerRenderIds = (html: string): string =>
  html.replace(/:r[0-9a-z]+:/g, ':r:').replace(/data-analytics-performance-mark="[^"]*"/g, 'data-analytics-performance-mark=""');

describe('renderWithIntl under en, against plain render', () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  const row = (objectKey: string, overrides: Partial<MappedDocument> = {}): MappedDocument =>
    ({ objectKey, objectStatus: 'COMPLETED', evaluationStatus: 'NOT_EVALUATED', ...overrides }) as MappedDocument;

  it('renders the document-list header identically, apart from per-render generated ids', () => {
    // The render of DocumentsCommonHeader.test.tsx: a Cloudscape Header with its action
    // buttons, a disabled-with-reason set, and the Download menu.
    const header = (
      <DocumentsCommonHeader
        selectedItems={[row('a.pdf'), row('b.pdf', { evaluationStatus: 'BASELINE_AVAILABLE' })]}
        totalItems={[row('a.pdf'), row('b.pdf')]}
        downloadToExcel={vi.fn()}
        onDownloadSelected={vi.fn()}
        onDelete={vi.fn()}
        onReprocess={vi.fn()}
        onAbort={vi.fn()}
        destructiveDisabledReason="Managed by their test run"
      />
    );
    const bare = render(header);
    const bareHtml = bare.container.innerHTML;
    bare.unmount();
    expect(bareHtml).toContain('Reprocess');
    expect(bareHtml).toContain('Managed by their test run');

    const wrapped = renderWithIntl(header);
    expect(withoutPerRenderIds(wrapped.container.innerHTML)).toBe(withoutPerRenderIds(bareHtml));
  });
});
