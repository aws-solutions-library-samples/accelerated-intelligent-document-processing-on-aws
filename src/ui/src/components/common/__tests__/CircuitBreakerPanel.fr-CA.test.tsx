// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The circuit breaker dialog under `fr-CA`: with a fixture catalog its title, a field label, the
 * state, whose French label keeps the stored state `OPEN`, an Admin button and the confirmation
 * button it opens read French. Under `en` the same renders read today's English. A notice shown
 * under `en` reads French after a switch to `fr-CA`: the dialog stays mounted, so its notice
 * outlives a language change.
 */
import React from 'react';
import { act, fireEvent, screen } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { setFixtureCatalog } from '../../../test-utils/i18nCatalogsMock';

// One value for every render: an Admin, and an open circuit as the badge's hook hands it to the dialog.
const state = vi.hoisted(() => ({
  role: { isAdmin: true, loading: false },
  status: {
    enabled: true,
    state: 'OPEN',
    openedAt: null,
    lastCheckedAt: null,
    failureCount: 3,
    recoveryAttempts: 1,
    lastError: 'Bedrock throttled',
  },
  onAction: vi.fn(),
}));

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));

import CircuitBreakerPanel from '../CircuitBreakerPanel';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'common.circuitBreakerPanel.title': 'Disjoncteur',
  'common.circuitBreakerPanel.field.state': 'État',
  'common.circuitBreakerPanel.state.open': 'OPEN (en pause)',
  'common.circuitBreakerPanel.resume.buttonLabel': 'Reprendre le traitement',
  'common.circuitBreakerPanel.resume.confirmLabel': 'Reprendre',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

const dialog = (
  <CircuitBreakerPanel
    visible
    status={state.status}
    onDismiss={vi.fn()}
    onPause={state.onAction}
    onResume={state.onAction}
    onProbe={state.onAction}
  />
);

/** The dialog's title. */
const title = (): string | null => createWrapper().findModal()!.findHeader()!.getElement().textContent;

/** The text of the State field's indicator. */
const stateLabel = (): string | null => createWrapper().findStatusIndicator()!.getElement().textContent;

afterEach(() => {
  state.onAction.mockReset();
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('en');
});

describe('the circuit breaker dialog under fr-CA', () => {
  it('reads its title, a field label, the state and the Resume button and its confirmation in French', async () => {
    renderWithIntl(dialog, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByText('État')).toBeInTheDocument();
    expect(title()).toBe('Disjoncteur');
    expect(stateLabel()).toBe('OPEN (en pause)');
    fireEvent.click(screen.getByRole('button', { name: 'Reprendre le traitement' }));
    expect(screen.getByRole('button', { name: 'Reprendre' })).toBeInTheDocument();
    expect(screen.queryByText('Circuit breaker')).toBeNull();
    expect(screen.queryByText('OPEN (paused)')).toBeNull();
  });
});

describe('a notice across a switch from en to fr-CA', () => {
  it.each([
    {
      notice: 'the success notice',
      outcome: () => undefined,
      en: 'Resume request submitted.',
      id: 'common.circuitBreakerPanel.resume.submitted',
      fr: 'Demande de reprise envoyée.',
    },
    {
      notice: 'the notice of a failure without a reason',
      outcome: () => Promise.reject({}),
      en: 'Request failed',
      id: 'common.circuitBreakerPanel.requestFailed',
      fr: 'Échec de la demande',
    },
  ])('rewords $notice', async ({ outcome, en, id, fr }) => {
    state.onAction.mockImplementation(outcome);
    const view = renderWithIntl(dialog, { enabledLocales: ['en', 'fr-CA'] });
    fireEvent.click(screen.getByRole('button', { name: 'Resume processing' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Bedrock quota raised' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    });
    expect(await screen.findByText(en)).toBeInTheDocument();

    setFixtureCatalog('fr-CA', { [id]: fr });
    view.setLocale('fr-CA');
    expect(await screen.findByText(fr)).toBeInTheDocument();
    expect(screen.queryByText(en)).toBeNull();
  });
});

describe('the circuit breaker dialog under en', () => {
  it('reads today’s English', () => {
    renderWithIntl(dialog);
    expect(title()).toBe('Circuit breaker');
    expect(screen.getByText('State')).toBeInTheDocument();
    expect(stateLabel()).toBe('OPEN (paused)');
    fireEvent.click(screen.getByRole('button', { name: 'Resume processing' }));
    expect(screen.getByRole('button', { name: 'Resume' })).toBeInTheDocument();
  });
});
