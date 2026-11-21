// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The flash bar's own texts under `fr-CA`: the welcome banner and the dismiss label of every
 * message read French, while an error set through the app context keeps the text it was set
 * with. The banner is added once, not again when the catalog arrives. Under `en` the hook
 * returns today's English.
 */
import { waitFor } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderHookWithIntl } from '../../test-utils/renderWithIntl';

// One value for every render. The setters write it, as the app context's state would.
const state = vi.hoisted(() => {
  const app = {
    errorMessage: undefined as string | undefined,
    successMessage: undefined as string | undefined,
    setErrorMessage: vi.fn((value: string | undefined) => {
      app.errorMessage = value;
    }),
    setSuccessMessage: vi.fn((value: string | undefined) => {
      app.successMessage = value;
    }),
  };
  return { app };
});

vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));
vi.mock('../../contexts/app', () => ({ default: () => state.app }));

import useNotifications from '../use-notifications';

/** The strings the assertions read. */
const FR = {
  'common.notifications.welcome': 'Bienvenue dans GenAI IDP (GenAIIDP)',
  'common.notifications.dismiss': 'Fermer le message',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const ERROR = 'Failed to load the queue: network error';

afterEach(() => {
  state.app.errorMessage = undefined;
  state.app.successMessage = undefined;
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the flash bar texts under fr-CA', () => {
  it('words the welcome banner and its dismiss label in French, once', async () => {
    const { result } = renderHookWithIntl(() => useNotifications(), FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() =>
      expect(result.current).toEqual([
        expect.objectContaining({
          id: 'welcome-1',
          type: 'info',
          content: 'Bienvenue dans GenAI IDP (GenAIIDP)',
          dismissLabel: 'Fermer le message',
        }),
      ]),
    );
  });

  it('keeps an error as it was set, with the French dismiss label', async () => {
    state.app.errorMessage = ERROR;
    const { result } = renderHookWithIntl(() => useNotifications(), FR_CA);
    await waitFor(() =>
      expect(result.current.filter((n) => n.type === 'error')).toEqual([
        expect.objectContaining({ content: ERROR, dismissLabel: 'Fermer le message' }),
      ]),
    );
  });
});

describe('the flash bar texts under en', () => {
  it('returns the welcome banner and its dismiss label in English', async () => {
    const { result } = renderHookWithIntl(() => useNotifications());
    await waitFor(() =>
      expect(result.current).toEqual([
        expect.objectContaining({
          id: 'welcome-1',
          type: 'info',
          content: 'Welcome to GenAI IDP (GenAIIDP)',
          dismissLabel: 'Dismiss message',
        }),
      ]),
    );
  });

  it('keeps an error as it was set, with the English dismiss label', async () => {
    state.app.errorMessage = ERROR;
    const { result } = renderHookWithIntl(() => useNotifications());
    await waitFor(() =>
      expect(result.current.filter((n) => n.type === 'error')).toEqual([
        expect.objectContaining({ content: ERROR, dismissLabel: 'Dismiss message' }),
      ]),
    );
  });
});
