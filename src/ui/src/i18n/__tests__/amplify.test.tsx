// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { translations } from '@aws-amplify/ui-react';
import { fireEvent, render, screen } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { registerAmplifyVocabularies } from '../amplify';
import { LOCALE_STORAGE_KEY, type Locale } from '../locales';
import { LocaleProvider, useLocale } from '../LocaleProvider';

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

// Exact entry from node_modules/@aws-amplify/ui/dist/esm/i18n/dictionaries/authenticator/fr.mjs.
const SIGN_IN = 'Sign in';
const FR_SIGN_IN = 'Se connecter';

/**
 * Reads Amplify's dictionary while rendering, as the Authenticator's `translate()` does, and
 * records what every render saw so the FIRST render can be asserted on its own.
 */
const makeSignInProbe = (seen: string[]) => {
  const SignInProbe = (): React.JSX.Element => {
    const { setLocale } = useLocale();
    const text: string = I18n.get(SIGN_IN);
    seen.push(text);
    return (
      <div>
        <span data-testid="sign-in">{text}</span>
        <button type="button" onClick={() => setLocale('en')}>
          choose en
        </button>
        <button type="button" onClick={() => setLocale('fr-CA')}>
          choose fr-CA
        </button>
      </div>
    );
  };
  return SignInProbe;
};

const setBrowserLanguages = (languages: readonly string[]): void => {
  vi.spyOn(navigator, 'languages', 'get').mockReturnValue(languages);
  vi.spyOn(navigator, 'language', 'get').mockReturnValue(languages[0]);
};

describe('Amplify UI dictionary', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('under fr-CA, the sign-in form reads French on its very first render', () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'fr-CA');
    setBrowserLanguages(['en-US']);
    const seen: string[] = [];
    const SignInProbe = makeSignInProbe(seen);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <SignInProbe />
      </LocaleProvider>,
    );
    expect(seen[0]).toBe(FR_SIGN_IN);
    expect(screen.getByTestId('sign-in')).toHaveTextContent(FR_SIGN_IN);
  });

  it('under en, the sign-in form keeps Amplify UI English defaults', () => {
    setBrowserLanguages(['en-US']);
    const seen: string[] = [];
    const SignInProbe = makeSignInProbe(seen);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <SignInProbe />
      </LocaleProvider>,
    );
    expect(seen[0]).toBe(SIGN_IN);
    expect(screen.getByTestId('sign-in')).toHaveTextContent(SIGN_IN);
  });

  it('a French browser on an English-only deployment gets an English sign-in form (D19)', () => {
    setBrowserLanguages(['fr-FR']);
    // The hazard: once a vocabulary exists, Amplify defaults its language to
    // `navigator.language` the first time nothing has been set. An empty language puts the
    // singleton back in that "nothing set" state, as on a fresh page load.
    registerAmplifyVocabularies();
    I18n.setLanguage('');
    expect(I18n.get(SIGN_IN)).toBe(FR_SIGN_IN);

    const setLanguage = vi.spyOn(I18n, 'setLanguage');
    const seen: string[] = [];
    const SignInProbe = makeSignInProbe(seen);
    render(
      <LocaleProvider>
        <SignInProbe />
      </LocaleProvider>,
    );
    expect(setLanguage).toHaveBeenCalledWith('en');
    expect(seen[0]).toBe(SIGN_IN);
    expect(screen.getByTestId('sign-in')).toHaveTextContent(SIGN_IN);
    expect(document.documentElement.lang).toBe('en');
  });

  it('setLocale switches the sign-in text on re-render, both ways', () => {
    setBrowserLanguages(['en-US']);
    const seen: string[] = [];
    const SignInProbe = makeSignInProbe(seen);
    render(
      <LocaleProvider enabledLocales={EN_FR}>
        <SignInProbe />
      </LocaleProvider>,
    );
    expect(screen.getByTestId('sign-in')).toHaveTextContent(SIGN_IN);
    fireEvent.click(screen.getByRole('button', { name: 'choose fr-CA' }));
    expect(screen.getByTestId('sign-in')).toHaveTextContent(FR_SIGN_IN);
    fireEvent.click(screen.getByRole('button', { name: 'choose en' }));
    expect(screen.getByTestId('sign-in')).toHaveTextContent(SIGN_IN);
  });

  it('registerAmplifyVocabularies registers the fr dictionary once however often it runs', async () => {
    // A fresh copy of the module, so its once-per-page-load guard starts unset here.
    vi.resetModules();
    const put = vi.spyOn(I18n, 'putVocabulariesForLanguage');
    const fresh = await import('../amplify');
    fresh.registerAmplifyVocabularies();
    fresh.registerAmplifyVocabularies();
    expect(put).toHaveBeenCalledTimes(1);
    expect(put).toHaveBeenCalledWith('fr', translations.fr);
  });
});
