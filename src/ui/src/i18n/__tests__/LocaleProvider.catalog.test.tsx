// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The provider with a real compiled catalog behind a mocked `virtual:i18n-catalogs`: the first
 * test in this series that renders French. The fixture goes through `compileCatalog`, so the
 * path exercised is the catalog format → the build's compiler → `loadCatalog` → `IntlProvider`.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { FormattedMessage } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { compileCatalog, sourceHash } from '../../../scripts/i18n/catalog.mjs';
import type { CatalogMessages } from '../loadCatalog';
import { LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { LocaleProvider, useLocale } from '../LocaleProvider';

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

const EN_JSON = {
  'test.hello': { defaultMessage: 'Hello', description: 'fixture' },
  'test.count': { defaultMessage: '{count, plural, one {# document} other {# documents}}', description: 'fixture' },
};
const FR_JSON = {
  'test.hello': { message: 'Bonjour', sourceHash: sourceHash('Hello') },
  'test.count': {
    message: '{count, plural, one {# document} other {# documents}}',
    sourceHash: sourceHash(EN_JSON['test.count'].defaultMessage),
  },
};

const { frLoader } = vi.hoisted(() => ({ frLoader: vi.fn<() => Promise<{ default: Record<string, unknown> }>>() }));

vi.mock('virtual:i18n-catalogs', () => ({
  availableLocales: ['en', 'fr-CA'],
  loaders: { 'fr-CA': frLoader },
}));

const compiled = (): CatalogMessages => {
  const { messages, problems } = compileCatalog(EN_JSON, FR_JSON, 'fr-CA');
  expect(problems).toEqual([]);
  return messages;
};

const Probe = (): React.JSX.Element => {
  const { locale, setLocale } = useLocale();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="hello">
        <FormattedMessage id="test.hello" defaultMessage="Hello" />
      </span>
      <span data-testid="count">
        <FormattedMessage id="test.count" defaultMessage="{count, plural, one {# document} other {# documents}}" values={{ count: 2 }} />
      </span>
      <button type="button" onClick={() => setLocale('en')}>
        choose en
      </button>
      <button type="button" onClick={() => setLocale('fr-CA')}>
        choose fr-CA
      </button>
    </div>
  );
};

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
};

describe('LocaleProvider with a message catalog', () => {
  beforeEach(() => {
    frLoader.mockReset();
    frLoader.mockImplementation(async () => ({ default: compiled() }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('renders French from the fixture catalog under fr-CA, English inline while it loads', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('fr-CA');
    expect(frLoader).toHaveBeenCalledTimes(1);
    // Children render at once with the inline English while the chunk loads.
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');

    await waitFor(() => expect(screen.getByTestId('hello')).toHaveTextContent('Bonjour'));
    expect(screen.getByTestId('count')).toHaveTextContent('2 documents');
  });

  it('renders English under en and calls no loader', () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');
    expect(frLoader).not.toHaveBeenCalled();
  });

  it('switching to fr-CA loads the catalog, and back to en drops it at once', async () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(frLoader).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(frLoader).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId('hello')).toHaveTextContent('Bonjour'));

    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');
  });

  it('ignores a catalog that arrives after the locale switched back to en', async () => {
    let resolveFr: (value: { default: Record<string, unknown> }) => void = () => {};
    frLoader.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveFr = resolve;
        }),
    );
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    await act(async () => {
      resolveFr({ default: compiled() });
      await Promise.resolve();
    });
    // react-intl looks messages up by id, not by locale: a stale French table handed to the
    // provider under `en` would render "Bonjour" here.
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');
  });

  it('logs a catalog that fails to load once per locale and keeps rendering English', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    frLoader.mockRejectedValueOnce(new Error('chunk unreachable')).mockRejectedValueOnce(new Error('chunk unreachable again'));
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    await waitFor(() => expect(warn).toHaveBeenCalledTimes(1));
    expect(String(warn.mock.calls[0][0])).toContain('fr-CA');
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');

    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    await waitFor(() => expect(frLoader).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
    });
    expect(warn).toHaveBeenCalledTimes(1);
    expect(error).not.toHaveBeenCalled();
    expect(screen.getByTestId('hello')).toHaveTextContent('Hello');
  });
});
