// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The edit page's two review panels under `fr-CA`. Review Reason reads its title, the
 * Escalated header, a reason chip, the "+N more alerts" button for 1 and 3 and Show fewer in
 * French. The review history dialog reads its title with the count, a column header, the
 * claimed and skipped events and the empty text in French, and shows an event the UI does
 * not know readably, as stored. Escalation notes, reasons and field names stay as written.
 * Under `en` the same renders read today's English.
 */
import React from 'react';
import { screen, within } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import userEvent from '@testing-library/user-event';
import { I18n } from 'aws-amplify/utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItemDetail } from '../../../graphql/generated/operation-types';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));

import ReviewHistoryModal from '../ReviewHistoryModal';
import ReviewTaskContext from '../ReviewTaskContext';

const reason = (i: number) => ({
  kind: 'low_confidence',
  sectionId: '1',
  field: `field_${i}`,
  label: `Field ${i}`,
  score: 0.1 + i / 100,
  threshold: 0.8,
});

/** An item with `stored` reasons, all listed; `over` adds, for example, an escalation. */
const detail = (stored: number, over: Record<string, unknown> = {}) =>
  ({
    item: { reasons: Array.from({ length: stored }, (_, i) => reason(i)), reasonCount: stored, escalated: false, ...over },
    checklist: [],
  }) as unknown as ReviewItemDetail;

const MINUTE = 60_000;
const ago = (minutes: number) => new Date(Date.now() - minutes * MINUTE).toISOString();

/** Oldest first, as the API returns it; the table lists it newest first. */
const history = (): ReviewItemDetail['history'] => [
  { at: ago(60), event: 'claimed', actor: 'reviewer', details: null },
  { at: ago(30), event: 'released', actor: 'reviewer', details: JSON.stringify({ reason: 'Needs expert review' }) },
  { at: ago(10), event: 'something_new', actor: null, details: null },
];

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'reviewQueues.taskContext.title': 'Motif de la révision',
  'reviewQueues.taskContext.escalated': 'Escaladé',
  'reviewQueues.taskContext.moreAlerts': '+{count, plural, one {# autre alerte} other {# autres alertes}}',
  'reviewQueues.taskContext.showFewer': 'Afficher moins',
  'reviewQueues.reasons.lowConfidenceField': 'Confiance faible : {field} {score, number, ::.00} < {threshold, number, ::.00}',
  'reviewQueues.reviewHistory.title': 'Historique de la révision ({count})',
  'reviewQueues.reviewHistory.column.event': 'Événement',
  'reviewQueues.reviewHistory.event.claimed': 'Pris en charge',
  'reviewQueues.reviewHistory.event.released': 'Passé',
  'reviewQueues.reviewHistory.empty': 'Rien ne s’est encore produit pour cet élément.',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

const modal = () => {
  const found = createWrapper(document.body).findModal();
  if (!found) throw new Error('dialog not rendered');
  return found;
};
const historyTable = () => {
  const found = modal().findContent().findTable();
  if (!found) throw new Error('history table not rendered');
  return found;
};
const columnHeaders = () =>
  historyTable()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);
// Columns: When, Event, By, Reason.
const eventCells = () => {
  const table = historyTable();
  return table.findRows().map((_, i) => table.findBodyCell(i + 1, 2)?.getElement().textContent);
};

describe('Review Reason under fr-CA', () => {
  it('reads its title, the Escalated header and a reason chip in French; the note stays as written', async () => {
    renderWithIntl(<ReviewTaskContext detail={detail(3, { escalated: true, escalationNote: 'Customer called twice' })} />, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect(await screen.findByRole('heading', { name: /Motif de la révision/ })).toBeInTheDocument();
    expect(screen.getByText('Escaladé')).toBeInTheDocument();
    expect(screen.getByText('Customer called twice')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confiance faible : Field 0 0,10 < 0,80' })).toBeInTheDocument();
  });

  it.each([
    [6, '+1 autre alerte'],
    [8, '+3 autres alertes'],
  ])('counts the reasons beyond the first five in French (%s stored)', async (stored, text) => {
    renderWithIntl(<ReviewTaskContext detail={detail(stored)} />, FR_CA);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('collapses with Show fewer in French', async () => {
    renderWithIntl(<ReviewTaskContext detail={detail(8)} />, FR_CA);
    await userEvent.click(await screen.findByText('+3 autres alertes'));
    expect(screen.getByRole('button', { name: 'Afficher moins' })).toBeInTheDocument();
  });
});

describe('the review history under fr-CA', () => {
  it('reads its title, a column header and the claimed and skipped events in French; an unknown event reads as stored', async () => {
    renderWithIntl(<ReviewHistoryModal history={history()} onDismiss={() => undefined} />, FR_CA);
    expect(await screen.findByText('Historique de la révision (3)')).toBeInTheDocument();
    expect(columnHeaders()).toContain('Événement');
    expect(eventCells()).toEqual(['Something new', 'Passé', 'Pris en charge']);
    expect(within(historyTable().getElement()).getByText('Needs expert review')).toBeInTheDocument();
  });

  it('says in French that nothing has happened yet', async () => {
    renderWithIntl(<ReviewHistoryModal history={[]} onDismiss={() => undefined} />, FR_CA);
    expect(await screen.findByText('Rien ne s’est encore produit pour cet élément.')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toBe('Historique de la révision (0)');
  });
});

describe('Review Reason under en', () => {
  it('reads the inline English for its title, the Escalated header and a reason chip', () => {
    renderWithIntl(<ReviewTaskContext detail={detail(3, { escalated: true, escalationNote: 'Customer called twice' })} />);
    expect(screen.getByRole('heading', { name: /Review Reason/ })).toBeInTheDocument();
    expect(screen.getByText('Escalated')).toBeInTheDocument();
    expect(screen.getByText('Customer called twice')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Low confidence: Field 0 0.10 < 0.80' })).toBeInTheDocument();
  });

  it.each([
    [6, '+1 more alert'],
    [8, '+3 more alerts'],
  ])('counts the reasons beyond the first five (%s stored)', (stored, text) => {
    renderWithIntl(<ReviewTaskContext detail={detail(stored)} />);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it('collapses with Show fewer', async () => {
    renderWithIntl(<ReviewTaskContext detail={detail(8)} />);
    await userEvent.click(screen.getByText('+3 more alerts'));
    expect(screen.getByRole('button', { name: 'Show fewer' })).toBeInTheDocument();
  });
});

describe('the review history under en', () => {
  it('reads the inline English for its title, a column header and the events', () => {
    renderWithIntl(<ReviewHistoryModal history={history()} onDismiss={() => undefined} />);
    expect(modal().findHeader().getElement().textContent).toBe('Review history (3)');
    expect(columnHeaders()).toContain('Event');
    expect(eventCells()).toEqual(['Something new', 'Skipped', 'Claimed']);
  });

  it('says that nothing has happened yet', () => {
    renderWithIntl(<ReviewHistoryModal history={[]} onDismiss={() => undefined} />);
    expect(screen.getByText('Nothing has happened to this item yet.')).toBeInTheDocument();
    expect(modal().findHeader().getElement().textContent).toBe('Review history (0)');
  });
});
