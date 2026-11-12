// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `renderWithIntl` in a file that does NOT mock `virtual:i18n-catalogs`: the build's own
 * module is in place, so a locale renders with whatever catalog the build has (none for
 * `fr-CA` today, so English by fallback), and a fixture is refused with the line to add,
 * rather than being dropped on the floor and rendering English as if it had applied.
 */
import React from 'react';
import { FormattedMessage } from 'react-intl';
import { afterEach, describe, expect, it } from 'vitest';
import type { Locale } from '../../i18n/locales';
import { renderWithIntl } from '../renderWithIntl';

const EN_FR: readonly Locale[] = ['en', 'fr-CA'];

describe('renderWithIntl without the catalog mock', () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('renders fr-CA through the real catalog module: an id no catalog has stays English', () => {
    const result = renderWithIntl(<FormattedMessage id="test.unmocked.hello" defaultMessage="Hello" />, {
      locale: 'fr-CA',
      enabledLocales: EN_FR,
    });
    expect(result.locale).toBe('fr-CA');
    expect(document.documentElement.lang).toBe('fr-CA');
    expect(result.getByText('Hello')).toBeInTheDocument();
  });

  it('refuses a fixture catalog and names the vi.mock line to add', () => {
    expect(() =>
      renderWithIntl(<p>x</p>, { locale: 'fr-CA', enabledLocales: EN_FR, messages: { 'test.unmocked.hello': 'Bonjour' } }),
    ).toThrow(/vi\.mock\('virtual:i18n-catalogs'/);
    // Nothing was mounted and nothing was left in storage.
    expect(window.localStorage.getItem('idp-locale')).toBeNull();
  });
});
