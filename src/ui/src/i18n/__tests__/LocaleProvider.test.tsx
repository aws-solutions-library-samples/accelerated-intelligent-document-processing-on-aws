// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { FormattedMessage, MessageFormatError, MissingTranslationError } from 'react-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { LocaleProvider, onIntlError, useLocale } from '../LocaleProvider';

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

const Probe = (): React.JSX.Element => {
  const { locale, intlLocale, setLocale, enabledLocales } = useLocale();
  return (
    <div>
      <span data-testid="locale">{locale}</span>
      <span data-testid="intl-locale">{String(intlLocale)}</span>
      <span data-testid="enabled">{enabledLocales.join(',')}</span>
      <button type="button" onClick={() => setLocale('fr-CA')}>
        choose fr-CA
      </button>
      <button type="button" onClick={() => setLocale('fr-FR')}>
        choose fr-FR
      </button>
    </div>
  );
};

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
};

const silenceConsole = (): { error: ReturnType<typeof vi.spyOn>; warn: ReturnType<typeof vi.spyOn> } => ({
  error: vi.spyOn(console, 'error').mockImplementation(() => {}),
  warn: vi.spyOn(console, 'warn').mockImplementation(() => {}),
});

describe('LocaleProvider', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('renders its children', () => {
    render(
      <LocaleProvider>
        <p>hello</p>
      </LocaleProvider>,
    );
    expect(screen.getByText('hello')).toBeInTheDocument();
  });

  it('resolves en by default and sets <html lang>', () => {
    setBrowserLanguages(['fr-FR']);
    render(
      <LocaleProvider>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(screen.getByTestId('enabled')).toHaveTextContent('en');
    expect(document.documentElement.lang).toBe('en');
  });

  it('sets <html lang> from the resolved locale and updates it after setLocale', () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(document.documentElement.lang).toBe('en');
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(screen.getByTestId('locale')).toHaveTextContent('fr-CA');
    expect(document.documentElement.lang).toBe('fr-CA');
  });

  it('resolves the browser language against the enabled list', () => {
    setBrowserLanguages(['fr-FR']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('fr-CA');
    expect(document.documentElement.lang).toBe('fr-CA');
  });

  it('exposes the Intl formatting locale: none under en, the tag under fr-CA', () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('intl-locale')).toHaveTextContent('undefined');
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(screen.getByTestId('intl-locale')).toHaveTextContent('fr-CA');
  });

  it('persists an enabled choice under idp-locale', () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('fr-CA');
  });

  it('ignores an invalid setLocale value and persists nothing', () => {
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-FR' }));
    expect(screen.getByTestId('locale')).toHaveTextContent('en');
    expect(document.documentElement.lang).toBe('en');
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBeNull();
  });

  it('still switches when localStorage.setItem throws', () => {
    setBrowserLanguages(['en-US']);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage disabled');
    });
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
      </LocaleProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(screen.getByTestId('locale')).toHaveTextContent('fr-CA');
  });

  it('renders the English defaultMessage for a missing id under en, silently', () => {
    const { error, warn } = silenceConsole();
    setBrowserLanguages(['en-US']);
    render(
      <LocaleProvider>
        <FormattedMessage id="test.missing" defaultMessage="Hello" />
      </LocaleProvider>,
    );
    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it('swallows MISSING_TRANSLATION under a non-default locale and shows the English default', () => {
    const { error, warn } = silenceConsole();
    setBrowserLanguages(['fr-FR']);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <Probe />
        <FormattedMessage id="test.missing.fr" defaultMessage="Hello" />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('locale')).toHaveTextContent('fr-CA');
    expect(screen.getByText('Hello')).toBeInTheDocument();
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it('logs a malformed message once and renders its text verbatim instead of throwing', () => {
    const { error, warn } = silenceConsole();
    setBrowserLanguages(['en-US']);
    const broken = '{count, plural, one {one} other {many}';
    render(
      <LocaleProvider>
        <FormattedMessage id="test.broken.render" defaultMessage={broken} values={{ count: 1 }} />
      </LocaleProvider>,
    );
    expect(screen.getByText(broken)).toBeInTheDocument();
    expect(error).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain('FORMAT_ERROR');
    expect(String(warn.mock.calls[0][0])).toContain('test.broken.render');
  });
});

describe('onIntlError', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('swallows MISSING_TRANSLATION entirely', () => {
    const { error, warn } = silenceConsole();
    onIntlError(new MissingTranslationError({ id: 'test.direct.missing', defaultMessage: 'x' }, 'fr-CA'));
    expect(error).not.toHaveBeenCalled();
    expect(warn).not.toHaveBeenCalled();
  });

  it('logs any other code once per distinct error, never throwing', () => {
    const { warn } = silenceConsole();
    const first = new MessageFormatError('first problem', 'en', { id: 'test.direct.first' });
    const second = new MessageFormatError('second problem', 'en', { id: 'test.direct.second' });
    expect(() => {
      onIntlError(first);
      onIntlError(first);
      onIntlError(second);
    }).not.toThrow();
    expect(warn).toHaveBeenCalledTimes(2);
    expect(String(warn.mock.calls[0][0])).toContain('FORMAT_ERROR');
    expect(String(warn.mock.calls[0][0])).toContain('first problem');
    expect(String(warn.mock.calls[1][0])).toContain('second problem');
  });
});
